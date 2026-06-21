#!/usr/bin/env python3
"""Scrape WARN layoff records from WARNTracker's public Airtable embed."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any

# WARNTracker live WARN table:
# https://airtable.com/embed/appgEFzJfcBqdpM7F/shr28XJ6olggYjPe5/tblP732bg4BNVJOVh
DEFAULT_EMBED_URL = (
    "https://airtable.com/embed/appgEFzJfcBqdpM7F/shr28XJ6olggYjPe5/tblP732bg4BNVJOVh"
)

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_ROOT = SCRIPT_DIR.parent
PUBLISH_ROOT = DATA_ROOT / "publish"
CACHE_DIR = PUBLISH_ROOT / ".cache" / "warn"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
TIMEZONE = os.environ.get("AIRTABLE_TIMEZONE", "America/Los_Angeles")

BROWSE_PAGE_SIZE = 50
RECENT_FEED_LIMIT = 100
RECENT_WINDOW_DAYS = 30
MAP_LIMIT = 500
COMPANIES_PAGE_SIZE = 500
COMPANIES_DIR = PUBLISH_ROOT / "api" / "companies"

PROMO_MARKERS = (
    "warntracker.com/get-data",
    "✨ want historical data",
    "✨ want exact layoff numbers",
    "✨ office address & city details",
)


def log(message: str) -> None:
    stamp = datetime.now().strftime("%H:%M:%S")
    print(f"[warn-fetch {stamp}] {message}", flush=True)


def build_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))


def fetch_text(opener: urllib.request.OpenerDirector, url: str, headers: dict[str, str]) -> str:
    request = urllib.request.Request(url, headers=headers)
    try:
        with opener.open(request, timeout=120) as response:
            return response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"HTTP {exc.code} for {url}: {body}") from exc


def decode_embed_html(html: str) -> str:
    return html.replace("\\u002F", "/").replace("\\/", "/")


def extract_shared_view_query(html: str) -> tuple[str, str, dict[str, Any]]:
    decoded = decode_embed_html(html)
    marker = "readSharedViewData?"
    start = decoded.find(marker)
    if start < 0:
        raise SystemExit("Could not find readSharedViewData query in embed HTML")

    chunk = decoded[start : start + 5000]
    end = chunk.find('",')
    if end < 0:
        raise SystemExit("Could not parse readSharedViewData query from embed HTML")

    query = chunk[:end]
    view_match = re.search(r"modelIdSelector%22%3A%22(viw[a-zA-Z0-9]+)", query)
    if not view_match:
        raise SystemExit("Could not find view id in embed HTML")

    access_policy_encoded = query[query.find("accessPolicy=") + len("accessPolicy=") :]
    policy = json.loads(urllib.parse.unquote(access_policy_encoded))
    view_id = view_match.group(1)
    api_query = query[len("readSharedViewData?") :]
    return view_id, api_query, policy


def fetch_shared_view_table(embed_url: str) -> dict[str, Any]:
    opener = build_opener()
    base_headers = {"User-Agent": USER_AGENT}

    log(f"Loading embed page: {embed_url}")
    html = fetch_text(opener, embed_url, base_headers)
    view_id, api_query, policy = extract_shared_view_query(html)

    api_url = f"https://airtable.com/v0.3/view/{view_id}/readSharedViewData?{api_query}"
    headers = {
        **base_headers,
        "Referer": embed_url,
        "Accept": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "x-airtable-application-id": policy["applicationId"],
        "x-time-zone": TIMEZONE,
        "x-user-locale": "en",
    }

    log("Downloading shared view data")
    payload = json.loads(fetch_text(opener, api_url, headers))
    if payload.get("msg") != "SUCCESS":
        raise SystemExit(f"Unexpected Airtable response: {payload}")

    table = payload["data"]["table"]
    row_count = len(table.get("rows", []))
    log(f"Received {row_count:,} rows from WARNTracker")
    return table


def build_column_maps(table: dict[str, Any]) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    columns = table.get("columns", [])
    by_name = {column["name"]: column["id"] for column in columns}
    by_id = {column["id"]: column["name"] for column in columns}

    state_choices: dict[str, str] = {}
    state_column = next((column for column in columns if column["name"] == "State"), None)
    if state_column:
        choices = state_column.get("typeOptions", {}).get("choices", {})
        state_choices = {
            choice_id: choice.get("name", choice_id)
            for choice_id, choice in choices.items()
        }

    return by_name, by_id, state_choices


def normalize_scalar(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    if isinstance(value, dict):
        for key in ("name", "text", "url", "label", "value"):
            if key in value:
                return normalize_scalar(value[key])
    return str(value).strip() or None


def parse_workers_range(value: Any) -> int | None:
    text = normalize_scalar(value)
    if not text:
        return None

    lowered = text.lower()
    if lowered.endswith("+"):
        digits = re.sub(r"[^\d]", "", text)
        return int(digits) if digits else None

    match = re.match(r"([\d,]+)\s*-\s*([\d,]+)", text)
    if match:
        low = int(match.group(1).replace(",", ""))
        high = int(match.group(2).replace(",", ""))
        return (low + high) // 2

    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None


def format_warn_date(value: Any) -> str | None:
    text = normalize_scalar(value)
    if not text:
        return None

    iso_match = re.match(r"^(\d{4})-(\d{2})-(\d{2})", text)
    if iso_match:
        year, month, day = iso_match.groups()
        return f"{int(month):02d}/{int(day):02d}/{year}"

    mdy_match = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", text)
    if mdy_match:
        month, day, year = mdy_match.groups()
        return f"{int(month):02d}/{int(day):02d}/{year}"

    return text


def parse_warn_date(value: Any) -> date | None:
    text = format_warn_date(value)
    if not text:
        return None
    match = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", text)
    if not match:
        return None
    month, day, year = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    try:
        return date(year, month, day)
    except ValueError:
        return None


def slugify_company(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower())
    return re.sub(r"-+$", "", re.sub(r"^-+", "", slug))


def build_layoff_id(row: dict[str, Any]) -> str:
    payload = "|".join(
        [
            str(row.get("source_id") or "").strip(),
            str(row.get("company") or ""),
            str(row.get("date") or ""),
            str(row.get("effective_date") or ""),
            str(row.get("workers") or 0),
            str(row.get("region") or ""),
            str(row.get("city") or ""),
            "warn",
        ]
    )
    return "warn-" + hashlib.md5(payload.encode("utf-8")).hexdigest()[:16]


def attach_layoff_ids(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[str, int] = {}
    with_ids: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        layoff_id = build_layoff_id(row)
        count = seen.get(layoff_id, 0)
        seen[layoff_id] = count + 1
        if count:
            layoff_id = f"{layoff_id}-{count + 1}-{index}"
        with_ids.append({**row, "id": layoff_id})
    return with_ids


def is_promo_row(company: str | None) -> bool:
    if not company:
        return True
    lowered = company.lower()
    return company.startswith("✨") or any(marker in lowered for marker in PROMO_MARKERS)


def resolve_state(value: Any, state_choices: dict[str, str]) -> str | None:
    if value is None:
        return None
    if isinstance(value, str) and value in state_choices:
        return state_choices[value]
    text = normalize_scalar(value)
    if not text:
        return None
    if text in state_choices:
        return state_choices[text]
    if len(text) == 2 and text.isalpha():
        return text.upper()
    return text


def normalize_shared_row(
    row: dict[str, Any],
    column_ids: dict[str, str],
    state_choices: dict[str, str],
) -> dict[str, Any] | None:
    cells = row.get("cellValuesByColumnId", {})

    company = normalize_scalar(cells.get(column_ids["Company Name"]))
    if is_promo_row(company):
        return None

    notice_date = format_warn_date(cells.get(column_ids["Notice Date"]))
    effective_date = format_warn_date(cells.get(column_ids["Layoff date"])) or notice_date
    if not notice_date and not effective_date:
        return None

    company_id = normalize_scalar(cells.get(column_ids.get("Company Id", "")))
    company_slug = company_id or slugify_company(company or "")

    city_value = normalize_scalar(cells.get(column_ids.get("Layoff office address & city", "")))
    city = None if not city_value or is_promo_row(city_value) else city_value

    return {
        "airtable_id": row.get("id"),
        "company": company,
        "company_slug": company_slug,
        "date": notice_date or effective_date,
        "effective_date": effective_date or notice_date,
        "region": resolve_state(cells.get(column_ids["State"]), state_choices) or "US",
        "city": city,
        "workers": parse_workers_range(cells.get(column_ids["# Laid off range"])),
    }


def normalize_shared_table(table: dict[str, Any]) -> list[dict[str, Any]]:
    column_ids, _, state_choices = build_column_maps(table)
    required = ["Company Name", "State", "Notice Date", "# Laid off range", "Layoff date"]
    missing = [name for name in required if name not in column_ids]
    if missing:
        raise SystemExit(f"Missing expected WARNTracker columns: {', '.join(missing)}")

    records: list[dict[str, Any]] = []
    for row in table.get("rows", []):
        normalized = normalize_shared_row(row, column_ids, state_choices)
        if normalized is not None:
            records.append(normalized)
    return records


def is_upcoming(effective_date: str | None) -> bool:
    parsed = parse_warn_date(effective_date)
    if not parsed:
        return False
    return parsed > date.today()


def is_recent_notice(notice_date: str | None, window_days: int = RECENT_WINDOW_DAYS) -> bool:
    parsed = parse_warn_date(notice_date)
    if not parsed:
        return False
    cutoff = date.today() - timedelta(days=window_days)
    return parsed >= cutoff


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    log(f"Wrote {path.relative_to(DATA_ROOT)} ({path.stat().st_size:,} bytes)")


def write_pages_index(summary: dict[str, Any]) -> None:
    """GitHub Pages serves data/publish/ at the site root — add a landing page."""
    totals = summary.get("totals", {})
    generated = summary.get("generated_at", "unknown")
    warn = totals.get("warn_notices", 0)
    companies = totals.get("companies", 0)
    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Cutoffs data CDN</title>
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 42rem; margin: 2rem auto; padding: 0 1rem; line-height: 1.5; }}
    code {{ background: #f4f4f5; padding: 0.1rem 0.35rem; border-radius: 0.25rem; }}
    ul {{ padding-left: 1.2rem; }}
  </style>
</head>
<body>
  <h1>Cutoffs data CDN</h1>
  <p>Static JSON for the Cutoffs app. Generated <time>{generated}</time>.</p>
  <p><strong>{warn:,}</strong> WARN notices · <strong>{companies:,}</strong> companies</p>
  <h2>Endpoints</h2>
  <ul>
    <li><a href="api/summary.json"><code>api/summary.json</code></a></li>
    <li><a href="api/marts/layoffs-summary.json"><code>api/marts/layoffs-summary.json</code></a></li>
    <li><a href="api/marts/layoffs-recent-feed.json"><code>api/marts/layoffs-recent-feed.json</code></a></li>
    <li><a href="api/marts/warn-layoffs.json"><code>api/marts/warn-layoffs.json</code></a> (full index)</li>
  </ul>
  <p>App config: <code>NEXT_PUBLIC_DATA_BASE_URL=https://netnuonline.github.io/cutoffsV2-data</code></p>
</body>
</html>
"""
    index_path = PUBLISH_ROOT / "index.html"
    index_path.write_text(html, encoding="utf-8")
    log(f"Wrote {index_path.relative_to(DATA_ROOT)} ({index_path.stat().st_size:,} bytes)")


def load_company_lca_overlay(slug: str) -> dict[str, Any]:
    path = COMPANIES_DIR / f"{slug}.json"
    if not path.is_file():
        return {}

    try:
        profile = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

    top_states = profile.get("top_states") or []
    top_titles = profile.get("top_titles") or []
    return {
        "lca_filing_count": int(profile.get("lca_filing_count") or 0),
        "median_wage_usd": profile.get("median_wage_usd"),
        "top_state": top_states[0]["state"] if top_states else None,
        "top_job_title": top_titles[0]["title"] if top_titles else None,
    }


def apply_lca_overlay(row: dict[str, Any]) -> dict[str, Any]:
    overlay = load_company_lca_overlay(row["slug"])
    if not overlay:
        return row

    merged = dict(row)
    lca_count = overlay.get("lca_filing_count") or 0
    if lca_count > 0:
        merged["lca_filing_count"] = lca_count
    if overlay.get("median_wage_usd") is not None:
        merged["median_wage_usd"] = overlay["median_wage_usd"]
    if overlay.get("top_job_title"):
        merged["top_job_title"] = overlay["top_job_title"]
    if overlay.get("top_state") and not merged.get("top_state"):
        merged["top_state"] = overlay["top_state"]
    return merged


def patch_summary(
    warn_count: int,
    company_count: int,
    *,
    total_lca_filings: int | None = None,
) -> None:
    summary_path = PUBLISH_ROOT / "api" / "summary.json"
    if summary_path.is_file():
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    else:
        summary = {"schema_version": 1, "totals": {}}

    totals = summary.setdefault("totals", {})
    totals["warn_notices"] = warn_count
    totals.setdefault("companies", company_count)
    if total_lca_filings is not None and not totals.get("lca_filings"):
        totals["lca_filings"] = total_lca_filings
    summary["generated_at"] = datetime.now(timezone.utc).isoformat()
    write_json(summary_path, summary)


def merge_lca_into_company_pages() -> int:
    """Patch existing companies-page-*.json from WARNTracker profile JSON."""
    api_root = PUBLISH_ROOT / "api" / "marts"
    index_path = api_root / "companies-index.json"
    if not index_path.is_file():
        raise SystemExit(f"Missing companies index: {index_path}")

    index = json.loads(index_path.read_text(encoding="utf-8"))
    page_count = int(index.get("page_count") or 0)
    if page_count <= 0:
        raise SystemExit("companies-index.json has no pages")

    company_list: list[dict[str, Any]] = []
    for page_num in range(1, page_count + 1):
        page_path = api_root / f"companies-page-{page_num:03d}.json"
        if not page_path.is_file():
            raise SystemExit(f"Missing companies page: {page_path}")
        page_rows = json.loads(page_path.read_text(encoding="utf-8"))
        merged_rows = [apply_lca_overlay(row) for row in page_rows]
        write_json(page_path, merged_rows)
        company_list.extend(merged_rows)

    total_lca_filings = sum(int(row.get("lca_filing_count") or 0) for row in company_list)
    search_index = [
        {
            "slug": row["slug"],
            "name": row["canonical_name"],
            "lca_filing_count": row.get("lca_filing_count") or 0,
        }
        for row in company_list
    ]
    write_json(PUBLISH_ROOT / "api" / "search" / "index.min.json", search_index)

    index["total_lca_filings"] = total_lca_filings
    write_json(index_path, index)

    warn_count = 0
    summary_path = PUBLISH_ROOT / "api" / "summary.json"
    if summary_path.is_file():
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        warn_count = int(summary.get("totals", {}).get("warn_notices") or 0)

    patch_summary(
        warn_count,
        len(company_list),
        total_lca_filings=total_lca_filings,
    )

    with_lca = sum(1 for row in company_list if int(row.get("lca_filing_count") or 0) > 0)
    log(
        f"Merged LCA overlays into {len(company_list):,} company rows "
        f"({with_lca:,} with LCA data, {total_lca_filings:,} total filings)"
    )
    return total_lca_filings


def build_marts(records: list[dict[str, Any]]) -> None:
    layoff_rows = attach_layoff_ids(
        [
            {
                "company_slug": row["company_slug"],
                "company": row["company"],
                "date": row["date"],
                "effective_date": row["effective_date"],
                "region": row["region"],
                "city": row["city"],
                "workers": row["workers"],
                "source_id": row.get("airtable_id"),
            }
            for row in records
        ]
    )

    upcoming_rows = [row for row in layoff_rows if is_upcoming(row["effective_date"])]
    upcoming_rows.sort(key=lambda row: parse_warn_date(row["effective_date"]) or date.max)

    past_rows = [row for row in layoff_rows if not is_upcoming(row["effective_date"])]
    recent_past_rows = [row for row in past_rows if is_recent_notice(row["date"])]
    sorted_recent_past = sorted(
        recent_past_rows,
        key=lambda row: parse_warn_date(row["date"]) or date.min,
        reverse=True,
    )

    by_company: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"notice_count": 0, "workers_affected": 0, "states": defaultdict(int)}
    )
    for row in records:
        bucket = by_company[row["company_slug"]]
        bucket["canonical_name"] = row["company"]
        bucket["notice_count"] += 1
        bucket["workers_affected"] += row["workers"] or 0
        region = row.get("region")
        if region:
            bucket["states"][region] += 1

    warn_by_company = [
        {
            "slug": slug,
            "canonical_name": values["canonical_name"],
            "notice_count": values["notice_count"],
            "workers_affected": values["workers_affected"],
        }
        for slug, values in by_company.items()
    ]
    warn_by_company.sort(
        key=lambda row: (row["workers_affected"], row["notice_count"]),
        reverse=True,
    )

    def top_state(slug: str) -> str | None:
        states = by_company[slug]["states"]
        if not states:
            return None
        return max(states.items(), key=lambda item: item[1])[0]

    company_list = [
        apply_lca_overlay(
            {
                "slug": row["slug"],
                "canonical_name": row["canonical_name"],
                "lca_filing_count": 0,
                "warn_notice_count": row["notice_count"],
                "median_wage_usd": None,
                "top_state": top_state(row["slug"]),
                "top_job_title": None,
            }
        )
        for row in warn_by_company
    ]
    total_lca_filings = sum(int(row.get("lca_filing_count") or 0) for row in company_list)
    company_pages: list[list[dict[str, Any]]] = []
    for offset in range(0, len(company_list), COMPANIES_PAGE_SIZE):
        company_pages.append(company_list[offset : offset + COMPANIES_PAGE_SIZE])

    search_index = [
        {
            "slug": row["slug"],
            "name": row["canonical_name"],
            "lca_filing_count": row.get("lca_filing_count") or 0,
        }
        for row in company_list
    ]

    layoffs_summary = {
        "total_filings": len(layoff_rows),
        "total_workers": sum(row["workers"] or 0 for row in layoff_rows),
        "total_companies": len(by_company),
        "past_filings": len(past_rows),
        "upcoming_filings": len(upcoming_rows),
        "recent_30d_filings": len(recent_past_rows),
    }

    browse_pages: list[list[dict[str, Any]]] = []
    for offset in range(0, len(sorted_recent_past), BROWSE_PAGE_SIZE):
        browse_pages.append(sorted_recent_past[offset : offset + BROWSE_PAGE_SIZE])

    id_to_page: dict[str, int] = {}
    for page_num, page_rows in enumerate(browse_pages, start=1):
        for row in page_rows:
            id_to_page[row["id"]] = page_num
    for row in upcoming_rows:
        id_to_page[row["id"]] = 0

    api_root = PUBLISH_ROOT / "api" / "marts"
    write_json(CACHE_DIR / "warntracker-raw.json", records)
    write_json(api_root / "warn-layoffs.json", layoff_rows)
    write_json(api_root / "upcoming-layoffs.json", upcoming_rows)
    write_json(api_root / "warn-by-company-top.json", warn_by_company)
    write_json(
        api_root / "companies-index.json",
        {
            "total": len(company_list),
            "page_size": COMPANIES_PAGE_SIZE,
            "page_count": len(company_pages),
            "total_lca_filings": total_lca_filings,
        },
    )
    for page_num, page_rows in enumerate(company_pages, start=1):
        write_json(api_root / f"companies-page-{page_num:03d}.json", page_rows)
    write_json(PUBLISH_ROOT / "api" / "search" / "index.min.json", search_index)
    write_json(api_root / "layoffs-summary.json", layoffs_summary)
    write_json(api_root / "layoffs-recent-feed.json", sorted_recent_past[:RECENT_FEED_LIMIT])
    write_json(api_root / "layoffs-map.json", sorted_recent_past[:MAP_LIMIT])
    write_json(
        api_root / "layoffs-browse-index.json",
        {
            "total": len(sorted_recent_past),
            "page_size": BROWSE_PAGE_SIZE,
            "page_count": len(browse_pages),
        },
    )
    write_json(api_root / "layoffs-id-index.json", id_to_page)

    for page_num, page_rows in enumerate(browse_pages, start=1):
        write_json(
            api_root / f"layoffs-browse-page-{page_num:03d}.json",
            page_rows,
        )

    patch_summary(
        len(records),
        len(warn_by_company),
        total_lca_filings=total_lca_filings,
    )
    summary = json.loads((PUBLISH_ROOT / "api" / "summary.json").read_text(encoding="utf-8"))
    write_pages_index(summary)

    log(
        f"Published {len(layoff_rows):,} layoffs "
        f"({len(upcoming_rows):,} upcoming, {len(recent_past_rows):,} recent 30d, "
        f"{len(warn_by_company):,} companies, {len(browse_pages):,} browse pages)"
    )


def print_fields(table: dict[str, Any]) -> None:
    print("WARNTracker columns:")
    for column in table.get("columns", []):
        print(f"  - {column['name']} ({column['type']})")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scrape WARN layoffs from WARNTracker's public Airtable embed."
    )
    parser.add_argument(
        "--embed-url",
        default=os.environ.get("WARNTRACKER_EMBED_URL", DEFAULT_EMBED_URL),
        help="Public Airtable embed URL for the live WARN table.",
    )
    parser.add_argument(
        "--list-fields",
        "--discover-fields",
        action="store_true",
        dest="list_fields",
        help="Print scraped column names and exit.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scrape and normalize only; do not write publish artifacts.",
    )
    parser.add_argument(
        "--merge-lca-only",
        action="store_true",
        help="Merge H-1B LCA counts from api/companies/*.json into company pages (no scrape).",
    )
    args = parser.parse_args()

    if args.merge_lca_only:
        merge_lca_into_company_pages()
        return

    table = fetch_shared_view_table(args.embed_url)

    if args.list_fields:
        print_fields(table)
        return

    records = normalize_shared_table(table)
    if not records:
        raise SystemExit("No WARN records normalized from shared view")

    if args.dry_run:
        log(f"Dry run: normalized {len(records):,} records")
        print(json.dumps(records[:3], indent=2))
        return

    build_marts(records)


if __name__ == "__main__":
    main()
