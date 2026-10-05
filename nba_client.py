"""
Thin client for stats.nba.com via nba_api: retries, timeouts and a two-level TTL
cache (in-process memory backed by a SQLite file shared across processes/restarts).

Configuration (environment variables):
  NBA_PROXY          proxy URL for stats.nba.com, or several separated by commas
                     (one is picked per request). Needed on hosts the NBA blocks.
  NBA_TIMEOUT        request timeout in seconds (default 30)
  COURTVISION_CACHE  path of the SQLite cache file (default instance/cache.sqlite3);
                     "off" disables the disk layer
"""
import os
import pickle
import sqlite3
import threading
import time
from datetime import datetime
from time import time as _now

import pandas as pd
from nba_api.stats.endpoints import (
    LeagueGameLog,
    PlayerGameLog,
    TeamGameLog,
    TeamPlayerDashboard,
    leaguedashplayerstats,
    leaguedashteamstats,
    leaguestandingsv3,
    playerindex,
    teamestimatedmetrics,
)
from nba_api.stats.static import teams as static_teams

# ------------------------------------------------------------------------------
# Constants: proxy/timeout
# ------------------------------------------------------------------------------
# No custom headers: stats.nba.com stalls requests carrying the old hand-rolled
# browser headers, while nba_api's built-in defaults are accepted.

def _proxy_setting():
    """NBA_PROXY as nba_api expects it: a URL string, or a list to rotate through."""
    proxies = [p.strip() for p in os.environ.get("NBA_PROXY", "").split(",") if p.strip()]
    if not proxies:
        return None
    return proxies[0] if len(proxies) == 1 else proxies

PROXY = _proxy_setting()
DEFAULT_TIMEOUT = int(os.environ.get("NBA_TIMEOUT", "30"))

# First season covered by the league dashboard, advanced stats and shot chart endpoints.
FIRST_STATS_SEASON_YEAR = 1996

# Season types the UI can request, keyed by the query-string value.
SEASON_TYPES = {"regular": "Regular Season", "playoffs": "Playoffs"}

def parse_season_type(value) -> str:
    """Map a ?season_type= value ("regular"/"playoffs") to the NBA API name."""
    return SEASON_TYPES.get((value or "").lower(), SEASON_TYPES["regular"])

# ------------------------------------------------------------------------------
# Two-level TTL cache for NBA API responses
# ------------------------------------------------------------------------------
TTL_SHORT = 120          # 2 minutes (live scoreboard)
TTL_DEFAULT = 1800       # 30 minutes (season stats)
TTL_LONG = 24 * 3600     # 1 day (player index / positions)
DISK_MAX_AGE = 7 * 24 * 3600  # entries older than this are pruned at startup

class DiskCache:
    """
    SQLite-backed key/value store shared by every server process, so a restart or
    a second gunicorn worker doesn't start cold. Values are pickled: the file must
    only ever be writable by this app.
    """

    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, ts REAL, value BLOB)")

    def _connect(self):
        return sqlite3.connect(self.path, timeout=5)

    def get(self, key):
        """Return (ts, value) or None."""
        try:
            with self._connect() as db:
                row = db.execute("SELECT ts, value FROM cache WHERE key = ?", (repr(key),)).fetchone()
            return (row[0], pickle.loads(row[1])) if row else None
        except Exception as e:
            print(f"[WARN] disk cache read failed for {key}: {e}")
            return None

    def set(self, key, ts, value):
        try:
            blob = pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
            with self._connect() as db:
                db.execute("INSERT OR REPLACE INTO cache (key, ts, value) VALUES (?, ?, ?)", (repr(key), ts, blob))
        except Exception as e:
            print(f"[WARN] disk cache write failed for {key}: {e}")

    def prune(self, max_age: float):
        with self._connect() as db:
            db.execute("DELETE FROM cache WHERE ts < ?", (_now() - max_age,))

def _open_disk_cache():
    path = os.environ.get("COURTVISION_CACHE", os.path.join(os.path.dirname(__file__), "instance", "cache.sqlite3"))
    if path.lower() in ("", "off", "none", "0"):
        return None
    try:
        disk = DiskCache(path)
        disk.prune(DISK_MAX_AGE)
        return disk
    except Exception as e:
        print(f"[WARN] disk cache disabled ({path}): {e}")
        return None

_DISK = _open_disk_cache()
_CACHE = {}
_CACHE_LOCK = threading.Lock()

def cached(key, ttl: int, loader):
    """
    Return loader() cached under key for ttl seconds, checking memory first and then
    the shared disk cache. If loader fails, serve a stale copy from either layer.
    Cached DataFrames are shared: callers must .copy() before mutating them.
    """
    now = _now()
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
    if entry and now - entry["ts"] < ttl:
        return entry["value"]

    if _DISK is not None:
        stored = _DISK.get(key)
        if stored and (entry is None or stored[0] > entry["ts"]):
            entry = {"ts": stored[0], "value": stored[1]}
            with _CACHE_LOCK:
                _CACHE[key] = entry
            if now - entry["ts"] < ttl:
                return entry["value"]

    try:
        value = loader()
    except Exception as e:
        if entry:
            print(f"[WARN] serving stale cache for {key} due to: {e}")
            return entry["value"]
        raise

    with _CACHE_LOCK:
        _CACHE[key] = {"ts": now, "value": value}
    if _DISK is not None:
        _DISK.set(key, now, value)
    return value

def get_team_gamelog_cached(team_id: int, season: str, timeout_sec: int = 10):
    """
    Fetch TeamGameLog, cached for 30min. Returns an empty DataFrame on failure.
    """
    def load():
        df = nbacall_retry(
            TeamGameLog,
            team_id=int(team_id),
            season=season,
            season_type_all_star="Regular Season",
            timeout=timeout_sec,
        ).get_data_frames()[0]
        return df if df is not None else pd.DataFrame()

    try:
        return cached(("team_gamelog", int(team_id), season), TTL_DEFAULT, load)
    except Exception as e:
        print(f"[WARN] get_team_gamelog_cached failed: {e}")
        return pd.DataFrame()

def get_league_player_stats(season: str, measure_type: str = "Base", season_type: str = "Regular Season"):
    """League-wide LeagueDashPlayerStats (season totals), cached for 30min."""
    def load():
        return nbacall_retry(
            leaguedashplayerstats.LeagueDashPlayerStats,
            season=season,
            season_type_all_star=season_type,
            measure_type_detailed_defense=measure_type,
        ).get_data_frames()[0]

    return cached(("league_player_stats", season, measure_type, season_type), TTL_DEFAULT, load)

def get_league_team_stats(season: str):
    """League-wide per-game LeagueDashTeamStats, cached for 30min."""
    def load():
        return nbacall_retry(
            leaguedashteamstats.LeagueDashTeamStats,
            season=season,
            season_type_all_star="Regular Season",
            per_mode_detailed="PerGame",
            league_id_nullable="00",
        ).get_data_frames()[0]

    return cached(("league_team_stats", season), TTL_DEFAULT, load)

def get_team_estimated_metrics(season: str):
    """League-wide TeamEstimatedMetrics, cached for 30min."""
    def load():
        return nbacall_retry(
            teamestimatedmetrics.TeamEstimatedMetrics,
            season=season,
            season_type="Regular Season",
        ).get_data_frames()[0]

    return cached(("team_estimated_metrics", season), TTL_DEFAULT, load)

def get_standings(season: str):
    """LeagueStandingsV3 for the regular season, cached for 30min."""
    def load():
        return nbacall_retry(
            leaguestandingsv3.LeagueStandingsV3,
            season=season,
            season_type="Regular Season",
        ).get_data_frames()[0]

    return cached(("standings", season), TTL_DEFAULT, load)

def get_player_gamelog(player_id: int, season: str, season_type: str = "Regular Season"):
    """PlayerGameLog for one season (newest game first), cached for 30min."""
    def load():
        return nbacall_retry(
            PlayerGameLog,
            player_id=player_id,
            season=season,
            season_type_all_star=season_type,
        ).get_data_frames()[0]

    return cached(("player_gamelog", player_id, season, season_type), TTL_DEFAULT, load)

def get_team_player_dashboard(team_id, season: str):
    """TeamPlayerDashboard data frames (index 1 = per-player season totals), cached for 30min."""
    def load():
        return nbacall_retry(TeamPlayerDashboard, team_id=int(team_id), season=season).get_data_frames()

    return cached(("team_player_dashboard", int(team_id), season), TTL_DEFAULT, load)

def get_player_positions():
    """
    Map PLAYER_ID -> position string ("G", "F-C", ...) from PlayerIndex.
    LeagueDashPlayerStats has no position column, so positions are joined from here.
    """
    def load():
        df = nbacall_retry(playerindex.PlayerIndex, historical_nullable="1").get_data_frames()[0]
        return {int(r.PERSON_ID): (r.POSITION or "") for r in df.itertuples()}

    return cached(("player_positions",), TTL_LONG, load)

def season_has_started(season: str) -> bool:
    """
    True once the season's regular season has at least one game in LeagueGameLog.
    Cached for an hour. If the NBA API is unreachable, assume a season has started
    unless it's October, when the new season usually hasn't tipped off yet.
    """
    def load():
        df = nbacall_retry(
            LeagueGameLog,
            season=season,
            season_type_all_star="Regular Season",
            retries=1,
            timeout=10,
        ).get_data_frames()[0]
        return df is not None and not df.empty

    try:
        return cached(("season_started", season), 3600, load)
    except Exception as e:
        print(f"[WARN] season_has_started({season}) check failed: {e}")
        return datetime.now().month != 10

def get_seasons(start_year: int = FIRST_STATS_SEASON_YEAR):
    """
    Ordered newest -> oldest, starting from the newest season with regular-season games.
    NBA season spans two years. From October on, the current calendar year's season
    (e.g., 2026-27) is a candidate; it's listed once its regular season has started.
    """
    today = datetime.now()
    latest_start_year = today.year if today.month >= 10 else today.year - 1

    seasons = [f"{year}-{str(year + 1)[-2:]}" for year in range(latest_start_year, start_year - 1, -1)]
    if seasons and not season_has_started(seasons[0]):
        seasons = seasons[1:]
    return seasons

def nbacall_retry(endpoint_cls, retries: int = 3, backoff: float = 0.5, **kwargs):
    """
    Wrapper for nba_api endpoint classes with consistent timeout/proxy and
    a simple retry with linear backoff.
    """
    kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
    if PROXY:
        kwargs.setdefault("proxy", PROXY)

    last_err = None
    for attempt in range(1, retries + 1):
        try:
            return endpoint_cls(**kwargs)
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(backoff * attempt)
            else:
                raise
    if last_err:
        raise last_err

def warm_cache(season: str = None, include_teams: bool = True) -> dict:
    """
    Pre-fetch the data the busiest pages need for a season, so the first visitor
    doesn't wait on the NBA API: league player/team tables, standings, positions
    and (optionally) every team's game log and roster. Returns {"ok": [...], "failed": {...}}.
    """
    season = season or get_seasons()[0]
    jobs = [
        ("league player stats", lambda: get_league_player_stats(season)),
        ("league advanced stats", lambda: get_league_player_stats(season, "Advanced")),
        ("league team stats", lambda: get_league_team_stats(season)),
        ("team estimated metrics", lambda: get_team_estimated_metrics(season)),
        ("standings", lambda: get_standings(season)),
        ("player positions", get_player_positions),
    ]
    if include_teams:
        for team in static_teams.get_teams():
            tid, abbr = team["id"], team["abbreviation"]
            jobs.append((f"{abbr} game log", lambda tid=tid: get_team_gamelog_cached(tid, season, timeout_sec=DEFAULT_TIMEOUT)))
            jobs.append((f"{abbr} roster", lambda tid=tid: get_team_player_dashboard(tid, season)))

    ok, failed = [], {}
    for name, job in jobs:
        try:
            job()
            ok.append(name)
        except Exception as e:
            failed[name] = str(e)
    return {"season": season, "ok": ok, "failed": failed}

