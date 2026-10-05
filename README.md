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
```bash
gunicorn app:app -c gunicorn.conf.py    # also what the Procfile runs
```
`gunicorn.conf.py` binds to `$PORT`, runs `WEB_CONCURRENCY` workers (default 2) and starts
`flask --app app warm-cache` in the background so the cache is full before visitors arrive.

**Check the NBA API first.** stats.nba.com blocks or stalls requests from many cloud
providers. Run this on the host (or look at the "NBA API connectivity" GitHub Action, which
runs it from a cloud runner):
```bash
python scripts/check_nba_api.py
```
If it fails, set `NBA_PROXY` to a proxy the NBA accepts, or host somewhere that isn't blocked.

| Variable | Purpose |
|---|---|
| `NBA_PROXY` | Proxy URL for stats.nba.com (comma-separate several to rotate) |
| `NBA_TIMEOUT` | NBA API timeout in seconds (default 30) |
| `COURTVISION_CACHE` | Cache file path (default `instance/cache.sqlite3`), or `off` |
| `COURTVISION_WARM` | `0` skips cache warming at startup |
| `WEB_CONCURRENCY`, `GUNICORN_THREADS`, `GUNICORN_TIMEOUT` | gunicorn workers, threads per worker, request timeout |

The cache lives on local disk; on hosts with ephemeral disks it simply re-warms after a
restart. Warm it by hand any time with `flask --app app warm-cache`.

## Project layout
- `app.py`: Flask routes (pages and JSON API)
- `nba_client.py`: stats.nba.com access with retries and a memory + SQLite TTL cache
- `metrics.py`: box-score derived metrics
- `templates/`: Jinja templates; every page extends `base.html`
- `static/css/`: per-page stylesheets

## Tests
```bash
python -m unittest discover -s tests -t .
```
The tests mock the NBA API and run offline. GitHub Actions runs them on every pull request.
