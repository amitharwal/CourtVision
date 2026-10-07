#!/usr/bin/env bash
# Fetch NBA data on this machine, rebuild the static site, and deploy it to
# Cloudflare if anything changed.
#
#   scripts/run_fetcher.sh live     # today's scoreboard            (every ~10 min)
#   scripts/run_fetcher.sh hourly   # league tables, standings, teams (hourly)
#   scripts/run_fetcher.sh nightly  # + every active player's pages  (nightly)
#
# Deploys use your `npx wrangler login`, or an API token if one is set. Settings come
# from the environment or, if present, from an env file (default
# ~/.config/courtvision/deploy.env; keep it chmod 600):
#   CLOUDFLARE_API_TOKEN=...               (optional; "Edit Cloudflare Workers" template)
#   CLOUDFLARE_ACCOUNT_ID=...              (needed with a token)
#   COURTVISION_PYTHON=/path/to/python3    (optional; scheduled jobs have a minimal PATH)
#   PATH=...                               (scheduled jobs: must include node/npx)
set -euo pipefail

ENV_FILE="${COURTVISION_ENV_FILE:-$HOME/.config/courtvision/deploy.env}"
if [[ -f "$ENV_FILE" ]]; then
  set -a; source "$ENV_FILE"; set +a
fi
PYTHON="${COURTVISION_PYTHON:-python3}"

cd "$(dirname "$0")/.."
case "${1:-}" in
  live)    mode=(--live) ;;
  hourly)  mode=() ;;
  nightly) mode=(--players) ;;
  *) echo "usage: $0 live|hourly|nightly" >&2; exit 2 ;;
esac

echo "[$(date '+%Y-%m-%d %H:%M:%S')] update $1"
# ${mode[@]+...}: macOS's bash 3.2 treats an empty array as unset under `set -u`.
exec "$PYTHON" -m flask --app app update ${mode[@]+"${mode[@]}"}
