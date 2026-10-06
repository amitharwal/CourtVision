#!/usr/bin/env bash
# Fetch NBA data on this machine and publish it to the hosted Court Vision.
#
#   scripts/run_fetcher.sh live     # today's scoreboard           (every ~5 min on game days)
#   scripts/run_fetcher.sh hourly   # league tables, standings, teams (hourly)
#   scripts/run_fetcher.sh nightly  # + every active player's pages (nightly)
#
# Settings come from the environment or, if present, from an env file (default
# ~/.config/courtvision/publish.env; keep it chmod 600):
#   COURTVISION_URL=https://your-site.example.com
#   COURTVISION_PUBLISH_TOKEN=...          (same value as on the host)
#   COURTVISION_PYTHON=/path/to/python3    (optional; scheduled jobs have a minimal PATH)
set -euo pipefail

ENV_FILE="${COURTVISION_ENV_FILE:-$HOME/.config/courtvision/publish.env}"
if [[ -f "$ENV_FILE" ]]; then
  set -a; source "$ENV_FILE"; set +a
fi
: "${COURTVISION_URL:?Set COURTVISION_URL (in $ENV_FILE or the environment)}"
: "${COURTVISION_PUBLISH_TOKEN:?Set COURTVISION_PUBLISH_TOKEN (in $ENV_FILE or the environment)}"
PYTHON="${COURTVISION_PYTHON:-python3}"

cd "$(dirname "$0")/.."
case "${1:-}" in
  live)    mode=(--live) ;;
  hourly)  mode=() ;;
  nightly) mode=(--players) ;;
  *) echo "usage: $0 live|hourly|nightly" >&2; exit 2 ;;
esac

echo "[$(date '+%Y-%m-%d %H:%M:%S')] publish $1"
exec "$PYTHON" -m flask --app app publish --url "$COURTVISION_URL" "${mode[@]}"
