# Cutoffs data CDN

Public static JSON for the [Cutoffs](https://github.com/NetnuOnline/cutoffsV2) app.

**Live URL:** https://netnuonline.github.io/cutoffsV2-data/

Example endpoints:

- `https://netnuonline.github.io/cutoffsV2-data/api/summary.json`
- `https://netnuonline.github.io/cutoffsV2-data/api/marts/layoffs-summary.json`

## Refresh

Data is rebuilt daily (noon UTC) or on manual dispatch:

**Actions → Publish data to GitHub Pages → Run workflow**

Source: WARNTracker public Airtable embed (see `data/scripts/fetch-warn-airtable.py`).

## App config

On Vercel:

```bash
NEXT_PUBLIC_DATA_BASE_URL=https://netnuonline.github.io/cutoffsV2-data
```
