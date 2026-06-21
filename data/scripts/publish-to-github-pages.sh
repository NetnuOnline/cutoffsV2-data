#!/usr/bin/env bash
# Fetch all WARN + WARNTracker company data locally, then deploy to GitHub Pages.
#
# Requires: python3, git, gh (authenticated: gh auth login)
#
# Usage:
#   ./data/scripts/publish-to-github-pages.sh
#   ./data/scripts/publish-to-github-pages.sh --skip-fetch    # redeploy existing publish/
#   ./data/scripts/publish-to-github-pages.sh --skip-deploy # fetch only
#
set -euo pipefail

DATA_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PUBLISH_DIR="$DATA_ROOT/publish"
GITHUB_REPO="${CUTOFFS_DATA_GITHUB_REPO:-NetnuOnline/cutoffsV2-data}"
GITHUB_PAGES_BRANCH="${CUTOFFS_DATA_GITHUB_BRANCH:-gh-pages}"

SKIP_FETCH=0
SKIP_DEPLOY=0

for arg in "$@"; do
  case "$arg" in
    --skip-fetch) SKIP_FETCH=1 ;;
    --skip-deploy) SKIP_DEPLOY=1 ;;
    -h | --help)
      sed -n '2,12p' "$0"
      exit 0
      ;;
    *)
      echo "Unknown option: $arg" >&2
      exit 1
      ;;
  esac
done

if ! command -v gh >/dev/null 2>&1; then
  echo "Missing gh CLI. Install: https://cli.github.com/" >&2
  exit 1
fi

if ! gh auth status >/dev/null 2>&1; then
  echo "Run: gh auth login" >&2
  exit 1
fi

if [[ "$SKIP_FETCH" -eq 0 ]]; then
  echo "==> [1/3] WARN layoffs (Airtable — all rows, no limit)"
  "$DATA_ROOT/scripts/fetch-warn.sh"

  echo ""
  echo "==> [2/3] WARNTracker company profiles (all WARN companies, no limit)"
  echo "    This can take 1–2 hours for ~37k companies (rate-limited requests)."
  WARNTRACKER_COMPANY_LIMIT=0 "$DATA_ROOT/scripts/fetch-warntracker.sh" \
    --warn-slugs-only \
    --limit 0 \
    --skip-existing

  echo ""
  echo "==> [2b/3] Merge H-1B LCA counts into company browse pages"
  python3 "$DATA_ROOT/scripts/fetch-warn-airtable.py" --merge-lca-only
else
  echo "==> Skipping fetch (--skip-fetch)"
fi

if [[ ! -f "$PUBLISH_DIR/api/summary.json" ]]; then
  echo "Missing $PUBLISH_DIR/api/summary.json — run fetch first." >&2
  exit 1
fi

if [[ "$SKIP_DEPLOY" -eq 0 ]]; then
  echo ""
  echo "==> [3/3] Deploy to GitHub Pages ($GITHUB_REPO → $GITHUB_PAGES_BRANCH)"
  TOKEN="$(gh auth token)"
  DEPLOY_DIR="$(mktemp -d)"
  trap 'rm -rf "$DEPLOY_DIR"' EXIT

  rsync -a --delete \
    --exclude='.gitignore' \
    --exclude='.cache/' \
    "$PUBLISH_DIR/" "$DEPLOY_DIR/"

  cd "$DEPLOY_DIR"
  git init -q
  git config user.name "Cutoffs Data Publish"
  git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
  git add -A
  if ! git diff --cached --quiet; then
    git commit -q -m "Deploy data $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  else
    echo "Nothing to deploy (no file changes)." >&2
    exit 1
  fi

  api_count="$(find api -type f 2>/dev/null | wc -l | tr -d ' ')"
  echo "    Staged ${api_count} files under api/"

  REMOTE="https://x-access-token:${TOKEN}@github.com/${GITHUB_REPO}.git"
  git branch -M "$GITHUB_PAGES_BRANCH"
  git remote add origin "$REMOTE"
  git push -f origin "$GITHUB_PAGES_BRANCH"

  echo ""
  echo "Pushed to https://github.com/${GITHUB_REPO}/tree/${GITHUB_PAGES_BRANCH}"
  echo "GitHub Pages will rebuild in ~1–2 minutes."
  echo "CDN: https://netnuonline.github.io/cutoffsV2-data/"
  echo ""
  echo "Verify:"
  echo "  curl -s https://netnuonline.github.io/cutoffsV2-data/api/summary.json"
else
  echo "==> Skipping deploy (--skip-deploy)"
fi
