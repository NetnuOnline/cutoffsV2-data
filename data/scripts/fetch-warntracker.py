#!/usr/bin/env python3
"""Scrape WARNTracker company detail pages and publish company profiles."""

from __future__ import annotations

import argparse
import json
import os
import re
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

WARNTRACKER_ORIGIN = "https://www.warntracker.com"

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_ROOT = SCRIPT_DIR.parent
PUBLISH_ROOT = DATA_ROOT / "publish"
CACHE_DIR = PUBLISH_ROOT / ".cache" / "warntracker"
COMPANIES_DIR = PUBLISH_ROOT / "api" / "companies"
MARTS_DIR = PUBLISH_ROOT / "api" / "marts"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
DEFAULT_BUILD_ID = os.environ.get("WARNTRACKER_BUILD_ID", "LuQAx7LBhgTa-CZHS6J-3")
REQUEST_DELAY = float(os.environ.get("WARNTRACKER_REQUEST_DELAY", "0.15"))


def log(message: str) -> None:
    stamp = datetime.now().strftime("%H:%M:%S")
    print(f"[warntracker {stamp}] {message}", flush=True)


def fetch_json(url: str) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"HTTP {exc.code} for {url}: {body[:500]}") from exc


def discover_build_id() -> str:
    request = urllib.request.Request(WARNTRACKER_ORIGIN, headers={"User-Agent": USER_AGENT})
    html = urllib.request.urlopen(request, timeout=60).read().decode("utf-8")
    match = re.search(r"/_next/data/([^/]+)/", html)
    if not match:
        log(f"Could not detect build id — using default {DEFAULT_BUILD_ID}")
        return DEFAULT_BUILD_ID
    build_id = match.group(1)
    log(f"Detected build id: {build_id}")
    return build_id


def data_url(build_id: str, path: str) -> str:
    clean = path.strip("/")
    return f"{WARNTRACKER_ORIGIN}/_next/data/{build_id}/{clean}.json"


def format_warn_date(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
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


def parse_warn_date(value: str | None) -> date | None:
    if not value:
        return None
    match = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", value)
    if not match:
        return None
    month, day, year = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    try:
        return date(year, month, day)
    except ValueError:
        return None


def city_from_row(row: dict[str, Any]) -> str | None:
    for key in row:
        if "City/Jurisdiction" in key or key.lower() == "city":
            value = row.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def aggregate_top_states(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts = Counter(event["state"] for event in events if event.get("state"))
    return [{"state": state, "count": count} for state, count in counts.most_common(10)]


def transform_company_profile(slug: str, page_props: dict[str, Any]) -> dict[str, Any] | None:
    rows = page_props.get("data") or []
    if not rows:
        return None

    h1b = page_props.get("h1bData") or {}
    company_name = rows[0].get("Company Name") or slug.replace("-", " ").title()

    warn_events: list[dict[str, Any]] = []
    for row in rows:
        notice_date = format_warn_date(row.get("Notice Date"))
        effective_date = format_warn_date(row.get("Layoff date")) or notice_date
        workers_raw = row.get("# Laid off")
        try:
            workers = int(workers_raw) if workers_raw is not None else 0
        except (TypeError, ValueError):
            workers = 0

        if not notice_date and not effective_date:
            continue

        warn_events.append(
            {
                "date": notice_date or effective_date,
                "effective_date": effective_date or notice_date,
                "state": str(row.get("State") or "US").upper(),
                "workers": workers,
                "city": city_from_row(row),
            }
        )

    warn_events.sort(
        key=lambda event: parse_warn_date(event["date"]) or date.min,
        reverse=True,
    )

    top_titles = [
        {"title": title, "count": 0, "median_wage_usd": None}
        for title in (h1b.get("topJobTitles") or [])
    ]
    fiscal_years = h1b.get("fiscalYears") or []
    by_fy = [{"fy": int(fy), "filings": 0, "median_wage_usd": None} for fy in fiscal_years]

    return {
        "slug": slug,
        "canonical_name": h1b.get("employerName") or company_name,
        "fein": None,
        "lca_filing_count": int(h1b.get("totalLCAs") or 0),
        "warn_notice_count": len(warn_events),
        "median_wage_usd": h1b.get("medianWage"),
        "by_fy": by_fy,
        "top_titles": top_titles,
        "top_states": aggregate_top_states(warn_events),
        "top_counties": [],
        "warn_events": warn_events,
        "retraining_boards": page_props.get("boards") or [],
        "sec8k_data": page_props.get("sec8kData"),
        "bankruptcy_data": page_props.get("bankruptcyData"),
        "source": "warntracker",
    }


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load_warn_company_slugs(limit: int | None = None) -> list[str]:
    path = MARTS_DIR / "warn-by-company-top.json"
    if not path.is_file():
        return []
    rows = json.loads(path.read_text(encoding="utf-8"))
    slugs = [row["slug"] for row in rows if row.get("slug")]
    return slugs[:limit] if limit else slugs


def load_top_company_slugs(build_id: str, limit: int = 500) -> list[str]:
    payload = fetch_json(data_url(build_id, "companies"))
    top = payload.get("pageProps", {}).get("topCompanies") or []
    return [row["id"] for row in top[:limit] if row.get("id")]


def collect_slugs(
    build_id: str,
    explicit: list[str],
    limit: int,
    include_warn: bool,
    include_top: bool,
) -> list[str]:
    if explicit:
        seen: set[str] = set()
        slugs: list[str] = []
        for slug in explicit:
            normalized = slug.strip().lower()
            if normalized and normalized not in seen:
                seen.add(normalized)
                slugs.append(normalized)
        return slugs

    slugs: list[str] = []
    seen: set[str] = set()

    def add(items: list[str]) -> None:
        for slug in items:
            normalized = slug.strip().lower()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            slugs.append(normalized)

    if include_top:
        add(load_top_company_slugs(build_id, limit=limit))
    if include_warn:
        add(load_warn_company_slugs(limit=limit if not include_top else None))

    if not slugs:
        add(load_top_company_slugs(build_id, limit=limit))

    return slugs[:limit] if limit else slugs


def fetch_company_profile(build_id: str, slug: str) -> dict[str, Any] | None:
    payload = fetch_json(data_url(build_id, f"company/{slug}"))
    page_props = payload.get("pageProps") or {}
    return transform_company_profile(slug, page_props)


def publish_companies_index(build_id: str) -> None:
    log("Fetching companies index")
    payload = fetch_json(data_url(build_id, "companies"))
    page_props = payload.get("pageProps") or {}
    write_json(CACHE_DIR / "companies-index.json", page_props)

    top_rows = []
    for row in page_props.get("topCompanies") or []:
        top_rows.append(
            {
                "slug": row.get("id"),
                "canonical_name": row.get("name"),
                "warn_notice_count": int(row.get("listing_count") or 0),
                "workers_affected": int(row.get("laid_off") or 0),
                "latest_year": row.get("latest_year"),
                "states": row.get("states") or [],
            }
        )

    write_json(MARTS_DIR / "warntracker-top-companies.json", top_rows)
    log(f"Cached companies summary ({len(top_rows):,} top companies)")


def publish_retraining_boards(build_id: str) -> None:
    log("Fetching retraining boards")
    payload = fetch_json(data_url(build_id, "retraining"))
    boards = payload.get("pageProps", {}).get("boards") or []
    write_json(CACHE_DIR / "retraining-boards.json", boards)
    write_json(MARTS_DIR / "retraining-boards.json", boards)
    log(f"Published {len(boards):,} retraining boards")


def publish_company_profiles(build_id: str, slugs: list[str]) -> None:
    ok = 0
    skipped = 0
    for index, slug in enumerate(slugs, start=1):
        try:
            profile = fetch_company_profile(build_id, slug)
        except SystemExit as exc:
            log(f"  [{index}/{len(slugs)}] {slug}: failed ({exc})")
            skipped += 1
            time.sleep(REQUEST_DELAY)
            continue

        if not profile:
            log(f"  [{index}/{len(slugs)}] {slug}: no data")
            skipped += 1
        else:
            write_json(COMPANIES_DIR / f"{slug}.json", profile)
            ok += 1
            if index <= 5 or index % 50 == 0 or index == len(slugs):
                log(
                    f"  [{index}/{len(slugs)}] {slug}: "
                    f"{profile['warn_notice_count']} notices, "
                    f"{sum(e['workers'] for e in profile['warn_events']):,} workers"
                )

        time.sleep(REQUEST_DELAY)

    log(f"Published {ok:,} company profiles ({skipped:,} skipped)")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scrape WARNTracker company detail pages into publish/api/companies/."
    )
    parser.add_argument("--build-id", default=None, help="Next.js build id (auto-detected if omitted)")
    parser.add_argument("--slug", action="append", default=[], help="Fetch a specific company slug (repeatable)")
    parser.add_argument(
        "--limit",
        type=int,
        default=int(os.environ.get("WARNTRACKER_COMPANY_LIMIT", "500")),
        help="Max company profiles to fetch (0 = no limit, default: 500 or WARNTRACKER_COMPANY_LIMIT)",
    )
    parser.add_argument(
        "--skip-index",
        action="store_true",
        help="Skip fetching companies.json summary",
    )
    parser.add_argument(
        "--skip-retraining",
        action="store_true",
        help="Skip fetching retraining boards",
    )
    parser.add_argument(
        "--warn-slugs-only",
        action="store_true",
        help="Only fetch slugs present in warn-by-company-top.json",
    )
    parser.add_argument(
        "--top-only",
        action="store_true",
        help="Only fetch slugs from WARNTracker topCompanies",
    )
    args = parser.parse_args()

    build_id = args.build_id or discover_build_id()

    if not args.skip_index:
        publish_companies_index(build_id)
    if not args.skip_retraining:
        publish_retraining_boards(build_id)

    slugs = collect_slugs(
        build_id,
        explicit=[slug.lower() for slug in args.slug],
        limit=args.limit,
        include_warn=not args.top_only,
        include_top=not args.warn_slugs_only,
    )

    if not slugs:
        raise SystemExit("No company slugs to fetch")

    log(f"Fetching {len(slugs):,} company profile(s)")
    publish_company_profiles(build_id, slugs)


if __name__ == "__main__":
    main()
