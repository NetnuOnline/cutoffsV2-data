#!/usr/bin/env bash
# Scrape WARNTracker company pages → data/publish/api/companies/
set -euo pipefail

DATA_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="$DATA_ROOT/scripts/fetch-warntracker.py"

if [[ -f "$DATA_ROOT/scripts/.env" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$DATA_ROOT/scripts/.env"
  set +a
fi

exec python3 "$SCRIPT" "$@"
