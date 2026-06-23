# Data coverage

Published static JSON for the Cutoffs app. Last rebuild: see `generated_at` in `api/summary.json` and `meta.json`.

## H-1B LCA disclosures (DOL)

| Coverage | Detail |
|----------|--------|
| **Fiscal years in app** | **FY 2020 – FY 2025** (6 years) |
| **Source** | U.S. Department of Labor certified LCA disclosure files |
| **Certified filings (all years)** | ~713,600 |
| **Employers with LCA history** | ~69,800 company profiles |
| **Bulk downloads** | `downloads/lca-fy2020.parquet` … `lca-fy2025.parquet` |

Federal fiscal year runs **October 1 – September 30**. FY 2025 covers Oct 2024 – Sep 2025.

### Where it appears

- **Homepage** — `summary.lca_by_fy` table (all six fiscal years)
- **Company H-1B tab** — `by_fy` breakdown per employer
- **Company browse** — `lca_filing_count` is the **sum across all loaded fiscal years**
- **Top LCA sponsors** — ranked by total filings across loaded years

### Rebuild LCA marts

After parquet downloads are present under `publish/downloads/`:

```bash
./data/scripts/build-lca-marts.sh
```

This merges multi-year LCA aggregates into `summary.json`, `lca-by-fiscal-year.json`, company profiles, and browse pages **without** overwriting WARN layoff marts.

---

## WARN layoffs

| Coverage | Detail |
|----------|--------|
| **Filed dates** | **1987 – 2026** (historical archive + current filings) |
| **Effective dates** | Through **2027** for upcoming notices |
| **Total notices** | ~78,400 |
| **Sources** | WARNTracker live table (Airtable embed) + WARNTracker company detail pages |

Most volume is from the **2010s and 2020s**. The 2020 spike reflects COVID-era mass filings. **~625** notices are classified as upcoming (future effective date).

### Where it appears

- **Layoffs browse / detail** — `marts/warn-layoffs.json`, browse pages, map
- **Company layoffs tab** — filtered from WARN mart by `company_slug`
- **Home feed & map** — recent / upcoming marts

### Refresh WARN data

```bash
./data/scripts/fetch-warn.sh
./data/scripts/fetch-warntracker.sh --warn-slugs-only --limit 0 --skip-existing
```

---

## Combined company records

A company may appear from **LCA only**, **WARN only**, or **both**:

- **LCA-only** — profile built from DOL data; layoffs tab populated from WARN mart when notices exist
- **WARN-only** — no `companies/{slug}.json` profile scrape; page synthesized from WARN marts + browse list
- **Both** — full profile with H-1B history and WARN events

---

## Production CDN

- **Repo:** [NetnuOnline/cutoffsV2-data](https://github.com/NetnuOnline/cutoffsV2-data)
- **URL:** `https://netnuonline.github.io/cutoffsV2-data/`
- **App env:** `NEXT_PUBLIC_DATA_BASE_URL` (see `app/.env.local`)

Run `./data/scripts/build-lca-marts.sh` locally before `./data/scripts/publish-to-github-pages.sh` when LCA parquets change.
