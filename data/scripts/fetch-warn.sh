#!/usr/bin/env bash
# Fetch WARN layoffs from WARNTracker Airtable → data/publish/api/marts/
set -euo pipefail

DATA_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="$DATA_ROOT/scripts/fetch-warn-airtable.py"

if [[ -f "$DATA_ROOT/scripts/.env" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$DATA_ROOT/scripts/.env"
  set +a
fi

exec python3 "$SCRIPT" "$@"
