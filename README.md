# CourtVision
An NBA analytics web app built with Flask and [nba_api](https://github.com/swar/nba_api).

## Features
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
Set `PORT` to use a different port. In production, run with gunicorn: `gunicorn app:app`.

## Project layout
- `app.py`: Flask routes (pages and JSON API)
- `nba_client.py`: stats.nba.com access with retries and an in-memory TTL cache
- `metrics.py`: box-score derived metrics
- `templates/`: Jinja templates; every page extends `base.html`
- `static/css/`: per-page stylesheets

## Tests
```bash
python -m unittest discover -s tests -t .
```
The tests mock the NBA API and run offline.
