# CourtVision
An NBA analytics web app built with Flask and [nba_api](https://github.com/swar/nba_api).

## Features
- **Home dashboard**: today's games with live scores, league leaders, a standings snapshot
  and player search
- **Players**: search every NBA player and open a career detail page with a game-by-game
  log and rolling-average chart (regular season or playoffs)
- **Teams**: season record, efficiency ratings, monthly win % and roster leaders
- **Standings**: conference standings with playoff and play-in seeds
- **Shot Charts**: every field-goal attempt for a player and season, filterable by result and period
- **Compare**: side-by-side player comparison
- **Advanced Metrics**: TS%, usage, assist %, rebound % and PIE leaders, distributions and position averages
- **Export**: download league player stats as CSV

Every page keeps its selections in the URL (player, season, filters), so a view can be
bookmarked or shared and reloads exactly as it was.

Season data covers 1996-97 onward, the range the NBA's stats endpoints support.

## Running locally
```bash
pip install -r requirements.txt
python app.py                  # http://localhost:5001
FLASK_DEBUG=1 python app.py    # with the debugger and auto-reload
```
Set `PORT` to use a different port.

## Deploying
stats.nba.com blocks or stalls requests from cloud servers (the "NBA API connectivity"
GitHub Action confirms it: every endpoint times out from a GitHub/Azure runner). So the
hosted site never calls the NBA itself. Instead:

- **Host** runs in hosted mode and serves only data that has been published to it.
- **Fetcher** runs on a machine the NBA doesn't block (e.g. your own computer), fetches
  the data and publishes changed entries to the host over HTTPS.

```
your computer (fetcher)                         cloud host (COURTVISION_OFFLINE=1)
stats.nba.com -> cache.sqlite3 -- publish -->   /api/admin/cache-entries -> cache.sqlite3 -> pages
```

### 1. Host
```bash
gunicorn app:app -c gunicorn.conf.py    # also what the Procfile runs
```
with these environment variables:

| Variable | Value |
|---|---|
| `COURTVISION_OFFLINE` | `1` (never call stats.nba.com) |
| `COURTVISION_PUBLISH_TOKEN` | a long random secret, e.g. `python -c "import secrets; print(secrets.token_urlsafe(32))"`; publishing is disabled when unset |
| `COURTVISION_WARM` | `0` (nothing to warm on the host) |
| `COURTVISION_CACHE` | cache file path; put it on a persistent disk if the host has one |

Pages offer only what has been published: season menus list published seasons, the
Playoffs option turns on once a season's playoffs have games, and player search finds
players whose pages were published. Anything else (e.g. an old shared link) returns a
clear "not published yet" message immediately instead of waiting on the NBA API.

### 2. Fetcher
Put the host's URL and token in `~/.config/courtvision/publish.env` (`chmod 600`):
```bash
COURTVISION_URL=https://your-site.example.com
COURTVISION_PUBLISH_TOKEN=the-same-secret-as-the-host
```
Then:
```bash
scripts/run_fetcher.sh hourly    # league tables, standings, all teams (~1 min)
scripts/run_fetcher.sh nightly   # + every active player's pages and shot charts (and playoff ones in the playoffs)
scripts/run_fetcher.sh live      # today's scoreboard (seconds; for game nights)
```
Only entries that changed since the last publish are sent. Add `--full` to the underlying
`flask --app app publish` command to resend everything to a freshly deployed host.
`deploy/macos/` has launchd schedules for all three jobs (every 5 min / hourly / 4:30 AM).
Data on the host is only as fresh as the last publish, so the fetcher machine needs to be
on and online.

### Self-hosting without a fetcher
On a machine that can reach stats.nba.com (check with `python scripts/check_nba_api.py`),
run the same gunicorn command without `COURTVISION_OFFLINE`. It calls the NBA API directly
and warms its cache at startup.

| Variable | Purpose |
|---|---|
| `NBA_PROXY` | Proxy URL for stats.nba.com (comma-separate several to rotate) |
| `NBA_TIMEOUT` | NBA API timeout in seconds (default 30) |
| `WEB_CONCURRENCY`, `GUNICORN_THREADS`, `GUNICORN_TIMEOUT` | gunicorn workers, threads per worker, request timeout |

## Project layout
- `app.py`: Flask routes (pages and JSON API)
- `nba_client.py`: stats.nba.com access with retries and a memory + SQLite TTL cache
- `publish.py`: sends cached data from a fetcher to a hosted copy of the site
- `metrics.py`: box-score derived metrics
- `templates/`: Jinja templates; every page extends `base.html`
- `static/css/`: per-page stylesheets

## Tests
```bash
python -m unittest discover -s tests -t .
```
The tests mock the NBA API and run offline. GitHub Actions runs them on every pull request.
