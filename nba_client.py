"""
Thin client for stats.nba.com via nba_api: retries, timeouts and an in-memory TTL cache.
"""
import threading
import time
from datetime import datetime
from time import time as _now

import pandas as pd
from nba_api.stats.endpoints import (
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

def get_seasons(start_year: int = FIRST_STATS_SEASON_YEAR):
    """
    Ordered newest -> oldest.
    NBA season spans two years. If it's October or later, treat the current calendar
    year as the new season's start (e.g., December 2025 -> 2025-26). Before October,
    the latest completed is last year's start (e.g., August 2025 -> 2024-25).
    """
    today = datetime.now()
    current_year = today.year
    latest_start_year = current_year if today.month >= 10 else current_year - 1

    seasons = []
    for year in range(latest_start_year, start_year - 1, -1):
        seasons.append(f"{year}-{str(year + 1)[-2:]}")
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
