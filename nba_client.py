"""
Thin client for stats.nba.com via nba_api: retries, timeouts and a two-level TTL
cache (in-process memory backed by a SQLite file shared across processes/restarts).

Configuration (environment variables):
  NBA_PROXY          proxy URL for stats.nba.com, or several separated by commas
                     (one is picked per request). Needed on hosts the NBA blocks.
  NBA_TIMEOUT        request timeout in seconds (default 30)
  COURTVISION_CACHE  path of the SQLite cache file (default instance/cache.sqlite3);
                     "off" disables the disk layer
  COURTVISION_OFFLINE  "1" = offline mode: never call stats.nba.com; serve only data
                     already in the cache (what the static site has; see build.py)
"""
import ast
import json
import os
import sqlite3
import threading
import time
import zlib
from contextlib import contextmanager
from datetime import datetime
from io import StringIO
from time import time as _now

import numpy as np

import pandas as pd
from nba_api.stats.endpoints import (
    LeagueGameLog,
    PlayerGameLog,
    ScoreboardV3,
    TeamGameLog,
    commonplayerinfo,
    playercareerstats,
    shotchartdetail,
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
OFFLINE = os.environ.get("COURTVISION_OFFLINE", "0") == "1"

class NBAUnavailable(Exception):
    """Raised instead of calling stats.nba.com in offline mode (COURTVISION_OFFLINE=1, and builds)."""

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
SCOREBOARD_MAX_AGE = 7 * 24 * 3600  # past days' scoreboards older than this are pruned at startup

# ------------------------------------------------------------------------------
# Value codec: cached values as tagged JSON (never pickle), so cache entries can
# be shipped between machines without the receiver executing anything.
# ------------------------------------------------------------------------------
def encode_value(value):
    """Turn a cached value (DataFrames, lists, dicts, scalars) into JSON-safe data."""
    if isinstance(value, pd.DataFrame):
        return {"__df__": value.to_json(orient="split", date_format="iso", double_precision=15)}
    if isinstance(value, dict):
        if all(isinstance(k, str) for k in value):
            return {"__dict__": {k: encode_value(v) for k, v in value.items()}}
        return {"__pairs__": [[encode_value(k), encode_value(v)] for k, v in value.items()]}
    if isinstance(value, (list, tuple)):
        return [encode_value(v) for v in value]
    if isinstance(value, np.generic):
        return value.item()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"cannot cache value of type {type(value).__name__}")

def decode_value(data):
    """Inverse of encode_value."""
    if isinstance(data, dict):
        if "__df__" in data:
            # dtype/convert_dates off: keep IDs like "0022400561" and date strings as sent
            return pd.read_json(StringIO(data["__df__"]), orient="split", dtype=False, convert_dates=False, precise_float=True)
        if "__dict__" in data:
            return {k: decode_value(v) for k, v in data["__dict__"].items()}
        if "__pairs__" in data:
            return {decode_value(k): decode_value(v) for k, v in data["__pairs__"]}
        raise ValueError("unknown tagged value")
    if isinstance(data, list):
        return [decode_value(v) for v in data]
    return data

def dumps_value(value) -> bytes:
    return zlib.compress(json.dumps(encode_value(value), separators=(",", ":")).encode())

def loads_value(blob: bytes):
    return decode_value(json.loads(zlib.decompress(blob)))

class DiskCache:
    """
    SQLite-backed key/value store shared by every server process, so a restart or
    a second gunicorn worker doesn't start cold. Values are stored with the JSON
    codec above (zlib-compressed); keys are repr() of the cache-key tuple.
    """

    TABLE = "cache_v2"  # v1 held pickles; it is never read again

    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with self._db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(f"CREATE TABLE IF NOT EXISTS {self.TABLE} (key TEXT PRIMARY KEY, ts REAL, value BLOB)")

    @contextmanager
    def _db(self):
        """A short-lived connection: committed (or rolled back) and always closed, so no
        process keeps an old read snapshot or a leaked handle on the shared file."""
        db = sqlite3.connect(self.path, timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, key):
        """Return (ts, value) or None."""
        try:
            with self._db() as db:
                rows = db.execute(f"SELECT ts, value FROM {self.TABLE} WHERE key = ?", (repr(key),)).fetchall()
            return (rows[0][0], loads_value(rows[0][1])) if rows else None
        except Exception as e:
            print(f"[WARN] disk cache read failed for {key}: {e}")
            return None

    def set(self, key, ts, value):
        try:
            self.put_raw(repr(key), ts, dumps_value(value))
        except Exception as e:
            print(f"[WARN] disk cache write failed for {key}: {e}")

    def put_raw(self, key_text: str, ts: float, blob: bytes):
        """Store an already-encoded entry, keeping whichever copy is newer."""
        with self._db() as db:
            db.execute(
                f"INSERT INTO {self.TABLE} (key, ts, value) VALUES (?, ?, ?) "
                f"ON CONFLICT(key) DO UPDATE SET ts = excluded.ts, value = excluded.value WHERE excluded.ts > ts",
                (key_text, ts, blob),
            )

    def entries_since(self, ts: float):
        """(key_text, ts, blob) for entries newer than ts, oldest first."""
        with self._db() as db:
            return db.execute(f"SELECT key, ts, value FROM {self.TABLE} WHERE ts > ? ORDER BY ts", (ts,)).fetchall()

    def keys(self):
        """Every stored key (the repr() text)."""
        with self._db() as db:
            return [row[0] for row in db.execute(f"SELECT key FROM {self.TABLE}")]

    def prune_scoreboards(self, max_age: float):
        """Drop old daily scoreboards. Everything else is kept: the static site is built
        from this cache, so past seasons stay on the site once fetched."""
        with self._db() as db:
            db.execute(f"DELETE FROM {self.TABLE} WHERE ts < ? AND key LIKE ?",
                       (_now() - max_age, "('scoreboard', %"))

def _open_disk_cache():
    path = os.environ.get("COURTVISION_CACHE", os.path.join(os.path.dirname(__file__), "instance", "cache.sqlite3"))
    if path.lower() in ("", "off", "none", "0"):
        return None
    try:
        disk = DiskCache(path)
        disk.prune_scoreboards(SCOREBOARD_MAX_AGE)
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
            if not isinstance(e, NBAUnavailable):  # expected on every cache miss in offline mode
                print(f"[WARN] serving stale cache for {key} due to: {e}")
            return entry["value"]
        raise

    with _CACHE_LOCK:
        _CACHE[key] = {"ts": now, "value": value}
    if _DISK is not None:
        _DISK.set(key, now, value)
    return value

# ------------------------------------------------------------------------------
# What goes on the site. In offline mode (and every build) only cached data can be
# served, so pages offer just the seasons, players and season types in the cache.
# ------------------------------------------------------------------------------
ANY = object()     # published_seasons() pattern part: matches anything
SEASON = object()  # published_seasons() pattern part: the season to report
PUBLISHED_TTL = 60  # seconds between re-reads of the published keys

_PUBLISHED = {"ts": 0.0, "keys": frozenset()}

def published_keys():
    """
    Cache-key tuples on disk, re-read at most once a minute. None when anything can
    be fetched on demand (not offline mode, or no disk cache to read).
    """
    if not OFFLINE or _DISK is None:
        return None
    now = _now()
    if now - _PUBLISHED["ts"] > PUBLISHED_TTL:
        keys = set()
        for text in _DISK.keys():
            try:
                key = ast.literal_eval(text)  # keys are repr() of tuples of literals
            except (ValueError, SyntaxError):
                continue
            if isinstance(key, tuple):
                keys.add(key)
        _PUBLISHED.update(ts=now, keys=frozenset(keys))
    return _PUBLISHED["keys"]

def published_seasons(pattern):
    """
    Seasons (newest first) with a published entry matching pattern, a cache-key tuple
    with SEASON where the season goes and ANY for parts that don't matter, e.g.
    ("shot_chart", ANY, SEASON, "Regular Season"). None when not limited to published data.
    """
    keys = published_keys()
    if keys is None:
        return None
    found = set()
    for key in keys:
        if len(key) != len(pattern):
            continue
        season = None
        for part, want in zip(key, pattern):
            if want is SEASON:
                season = part
            elif want is not ANY and part != want:
                break
        else:
            if isinstance(season, str):
                found.add(season)
    return sorted(found, reverse=True)

def published_player_ids():
    """IDs of players whose pages were published, or None when not limited to published data."""
    keys = published_keys()
    if keys is None:
        return None
    return {key[1] for key in keys if len(key) == 2 and key[0] == "player_profile"}

def get_team_gamelog_cached(team_id: int, season: str, timeout_sec: int = 10):
    """
    Fetch TeamGameLog, cached for 30min. Returns an empty DataFrame on failure, except
    in offline mode, where uncached data raises NBAUnavailable.
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
    except NBAUnavailable:
        raise  # offline mode: "not fetched" must not look like "no games played"
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

def get_scoreboard(game_date: str):
    """ScoreboardV3 for a YYYY-MM-DD date as [games_df, teams_df], cached for 2min."""
    def load():
        frames = nbacall_retry(ScoreboardV3, game_date=game_date, league_id="00").get_data_frames()
        return [frames[1], frames[2]]

    return cached(("scoreboard", game_date), TTL_SHORT, load)

def get_player_info(player_id: int):
    """CommonPlayerInfo bio row(s), cached for 30min."""
    return cached(
        ("player_info", int(player_id)),
        TTL_DEFAULT,
        lambda: nbacall_retry(commonplayerinfo.CommonPlayerInfo, player_id=player_id).get_data_frames()[0],
    )

def get_player_profile(player_id: int):
    """
    A player's season and career tables (SeasonTotalsRegularSeason, ...) as a
    normalized dict, cached for 30min. From PlayerCareerStats: PlayerProfileV2 has
    the same tables but stalls for many players (Oct 2026).
    """
    return cached(
        ("player_profile", int(player_id)),
        TTL_DEFAULT,
        lambda: nbacall_retry(playercareerstats.PlayerCareerStats, player_id=player_id).get_normalized_dict(),
    )

def get_shot_chart(player_id: int, season: str, season_type: str = "Regular Season"):
    """Every field-goal attempt for a player and season, cached for 30min."""
    def load():
        return nbacall_retry(
            shotchartdetail.ShotChartDetail,
            team_id=0,
            player_id=player_id,
            season_nullable=season,
            season_type_all_star=season_type,
            context_measure_simple="FGA",  # the default ("PTS") returns made shots only
        ).get_data_frames()[0]

    return cached(("shot_chart", int(player_id), season, season_type), TTL_DEFAULT, load)

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
    if OFFLINE:
        raise NBAUnavailable(f"{endpoint_cls.__name__}: offline mode serves cached data only")
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

PLAYER_REFRESH_AGE = 7 * 24 * 3600  # refetch every player at least weekly (trades, bios)

def _games_played(season: str, season_type: str) -> dict:
    """{player_id: games played} from the league player table."""
    df = get_league_player_stats(season, "Base", season_type)
    return {int(r.PLAYER_ID): int(r.GP) for r in df.itertuples()}

def _player_job(pid: int, season: str, season_type: str, kind: str):
    """A warm_cache job fetching one kind of player data."""
    return {
        "info": lambda: get_player_info(pid),
        "profile": lambda: get_player_profile(pid),
        "game log": lambda: get_player_gamelog(pid, season, season_type),
        "shot chart": lambda: get_shot_chart(pid, season, season_type),
    }[kind]

def _player_unchanged(pid: int, season: str, season_type: str, kinds, games_played: int) -> bool:
    """
    True when every kind of this player's data is on disk, under PLAYER_REFRESH_AGE
    old, and the cached game log has games_played games: there's nothing new to fetch.
    """
    if _DISK is None:
        return False
    keys = {
        "info": ("player_info", pid),
        "profile": ("player_profile", pid),
        "game log": ("player_gamelog", pid, season, season_type),
        "shot chart": ("shot_chart", pid, season, season_type),
    }
    stored = {kind: _DISK.get(keys[kind]) for kind in kinds}
    if any(entry is None or _now() - entry[0] > PLAYER_REFRESH_AGE for entry in stored.values()):
        return False
    return len(stored["game log"][1]) == games_played

def warm_cache(season: str = None, include_teams: bool = True, include_players: bool = False,
               live_only: bool = False, progress=None) -> dict:
    """
    Pre-fetch data into the cache so pages don't wait on the NBA API (and so a fetcher
    can build the static site from it).

    - live_only: just today's scoreboard (cheap; run every few minutes during games)
    - default: league tables, standings, positions, today's scoreboard and, with
      include_teams, every team's game log and roster (~66 requests)
    - include_players: also every active player's bio, career profile, game log and
      shot chart for the season (~4 requests per player; run nightly), plus the
      playoff game log and shot chart of everyone who played in the playoffs
    progress(done, total, name) is called after each job if given.
    Players whose games-played count matches their cached game log are skipped
    (nothing new since the last fetch), unless their data is over a week old.
    Returns {"season", "ok": [...], "failed": {name: error}, "unchanged": players skipped}.
    """
    from zoneinfo import ZoneInfo

    season = season or get_seasons()[0]
    today_et = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    jobs = [("today's scoreboard", lambda: get_scoreboard(today_et))]

    if not live_only:
        jobs += [
            ("season status", lambda: season_has_started(season)),
            ("league player stats", lambda: get_league_player_stats(season)),
            ("league advanced stats", lambda: get_league_player_stats(season, "Advanced")),
            # Empty until the playoffs start; pages offer playoffs once it has rows.
            ("league playoff stats", lambda: get_league_player_stats(season, "Base", "Playoffs")),
            ("league playoff advanced stats", lambda: get_league_player_stats(season, "Advanced", "Playoffs")),
            ("league team stats", lambda: get_league_team_stats(season)),
            ("team estimated metrics", lambda: get_team_estimated_metrics(season)),
            ("standings", lambda: get_standings(season)),
            ("player positions", get_player_positions),
        ]
        if include_teams:
            for team in static_teams.get_teams():
                tid, abbr = team["id"], team["abbreviation"]
                jobs.append((f"{abbr} game log", lambda tid=tid: get_team_gamelog_cached(tid, season, timeout_sec=DEFAULT_TIMEOUT)))

    ok, failed = [], {}

    def run(name, job):
        try:
            job()
            ok.append(name)
        except Exception as e:
            failed[name] = str(e)

    total = len(jobs)
    for i, (name, job) in enumerate(jobs, 1):
        run(name, job)
        if progress:
            progress(i, total, name)

    unchanged = 0
    if include_players and not live_only:
        player_jobs = []
        for season_type, kinds in (("Regular Season", ("info", "profile", "game log", "shot chart")),
                                   ("Playoffs", ("game log", "shot chart"))):
            try:
                games = _games_played(season, season_type)
            except Exception as e:
                failed[f"{season_type} player list"] = str(e)
                continue
            for pid, gp in games.items():
                if _player_unchanged(pid, season, season_type, kinds, gp):
                    unchanged += 1
                    continue
                player_jobs += [(f"player {pid} {season_type} {kind}", _player_job(pid, season, season_type, kind))
                                for kind in kinds]
        total += len(player_jobs)
        for i, (name, job) in enumerate(player_jobs, len(jobs) + 1):
            run(name, job)
            if progress:
                progress(i, total, name)

    return {"season": season, "ok": ok, "failed": failed, "unchanged": unchanged}
