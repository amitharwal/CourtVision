"""
Thin client for stats.nba.com via nba_api: retries, timeouts and an in-memory TTL cache.
"""
import threading
import time
from datetime import datetime
from time import time as _now

import pandas as pd
from nba_api.stats.endpoints import (
    LeagueGameLog,
    TeamGameLog,
    leaguedashplayerstats,
    leaguedashteamstats,
    playerindex,
    teamestimatedmetrics,
)

# ------------------------------------------------------------------------------
# Constants: proxy/timeout
# ------------------------------------------------------------------------------
# No custom headers: stats.nba.com stalls requests carrying the old hand-rolled
# browser headers, while nba_api's built-in defaults are accepted.
PROXIES = {
    # "http": "http://your-proxy:port",
    # "https": "http://your-proxy:port",
}

DEFAULT_TIMEOUT = 30

# First season covered by the league dashboard, advanced stats and shot chart endpoints.
FIRST_STATS_SEASON_YEAR = 1996

# ------------------------------------------------------------------------------
# In-memory TTL cache for NBA API responses
# ------------------------------------------------------------------------------
_CACHE = {}
_CACHE_LOCK = threading.Lock()

TTL_SHORT = 120          # 2 minutes (live scoreboard)
TTL_DEFAULT = 1800       # 30 minutes (season stats)
TTL_LONG = 24 * 3600     # 1 day (player index / positions)

def cached(key, ttl: int, loader):
    """
    Return loader() cached under key for ttl seconds.
    If loader fails and a stale copy exists, serve the stale copy instead.
    Cached DataFrames are shared: callers must .copy() before mutating them.
    """
    now = _now()
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
    if entry and now - entry["ts"] < ttl:
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

def get_league_player_stats(season: str, measure_type: str = "Base"):
    """League-wide LeagueDashPlayerStats (season totals), cached for 30min."""
    def load():
        return nbacall_retry(
            leaguedashplayerstats.LeagueDashPlayerStats,
            season=season,
            season_type_all_star="Regular Season",
            measure_type_detailed_defense=measure_type,
        ).get_data_frames()[0]

    return cached(("league_player_stats", season, measure_type), TTL_DEFAULT, load)

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
    if PROXIES:
        kwargs.setdefault("proxy", PROXIES)

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
