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

## Deploying (free, as a static site)
stats.nba.com blocks or stalls requests from cloud servers (the "NBA API connectivity"
GitHub Action confirms it: every endpoint times out from a GitHub/Azure runner). So the
site is built where the NBA API works, such as your own computer, and deployed as plain
files to [Cloudflare Pages](https://pages.cloudflare.com/) (free plan).

```
your computer                                                    Cloudflare Pages
stats.nba.com -> instance/cache.sqlite3 -> flask build -> dist/ -- wrangler -->  courtvision.pages.dev
```

`flask --app app build` saves every page and data file the cache can fill into `dist/`.
Each file is the Flask app's own response for that URL (`/standings` ->
`standings.html`, `/data/standings/2025-26.json` -> the same path), so the static site
and `python app.py` serve the same data. Building never calls the NBA API: the site
offers only the seasons, players and season types that were fetched, and anything else
shows a "not on the site yet" message.

The cache keeps season data indefinitely (only old daily scoreboards are pruned), so
once a season has been fetched it stays on the site. To add an older season:
`flask --app app update --season 2023-24 --players`.

### One-time setup
1. Install [Node.js](https://nodejs.org/) (deploys run `npx wrangler`).
2. Create a free Cloudflare account, then the Pages project:
   `npx wrangler login && npx wrangler pages project create courtvision --production-branch main`
3. Create an API token with the **Cloudflare Pages: Edit** permission (My Profile → API
   Tokens) and put it in `~/.config/courtvision/deploy.env` (`chmod 600`):
   ```bash
   CLOUDFLARE_API_TOKEN=...
   CLOUDFLARE_ACCOUNT_ID=...                 # shown on the Cloudflare dashboard
   COURTVISION_PAGES_PROJECT=courtvision     # optional, this is the default
   ```
4. First deploy: `scripts/run_fetcher.sh nightly` (fetches everything, ~1 hour).

### Keeping it up to date
```bash
scripts/run_fetcher.sh live      # today's scoreboard (seconds; every ~10 min)
scripts/run_fetcher.sh hourly    # league tables, standings, all teams (~1 min)
scripts/run_fetcher.sh nightly   # + every active player's pages and shot charts (and playoff ones in the playoffs)
```
Each run fetches, rebuilds `dist/` and deploys only if something changed, so the live
job is quiet outside game times. `deploy/macos/` has launchd schedules for all three
jobs (every 10 min / hourly / 4:30 AM). The site is only as fresh as the last run, so
the computer needs to be on and online.

### Previewing a build
```bash
flask --app app build      # write dist/ from the current cache
flask --app app preview    # http://127.0.0.1:8080, served the way Pages serves it
```
`COURTVISION_OFFLINE=1 python app.py` also shows only cached data, with the dev server.

| Variable | Purpose |
|---|---|
| `NBA_PROXY` | Proxy URL for stats.nba.com (comma-separate several to rotate) |
| `NBA_TIMEOUT` | NBA API timeout in seconds (default 30) |
| `COURTVISION_CACHE` | Cache file path (default `instance/cache.sqlite3`) |
| `COURTVISION_OFFLINE` | `1`: the dev server never calls stats.nba.com, like a build |

## Project layout
- `app.py`: Flask routes (pages, and JSON data files under `/data/`)
- `nba_client.py`: stats.nba.com access with retries and a memory + SQLite TTL cache
- `build.py`: builds the static site from the cache and deploys it to Cloudflare Pages
- `metrics.py`: box-score derived metrics
- `templates/`: Jinja templates; every page extends `base.html`
- `static/css/`: per-page stylesheets; `static/js/`: shared scripts (`site-data.js` loads data files and searches players)

## Tests
```bash
python -m unittest discover -s tests -t .
```
The tests mock the NBA API and run offline. GitHub Actions runs them on every pull request.
