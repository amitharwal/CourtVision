import os
import io
import time
import json
from datetime import datetime
from functools import lru_cache
import threading
from time import time as _now

import pandas as pd
from flask import Flask, render_template, request, jsonify, send_file

# nba_api imports
from nba_api.stats.endpoints import (
    leaguedashplayerstats,
    shotchartdetail,
    teamdashboardbygeneralsplits,
    playerprofilev2,
    commonplayerinfo,
    ScoreboardV2,
    HomePageLeaders,
    TeamGameLog,
    TeamDashboardByGeneralSplits,
    TeamDashboardByShootingSplits,
    TeamPlayerDashboard,
    leaguedashteamstats,
    teamestimatedmetrics,
    LeagueGameLog,
    PlayerGameLog
)
from nba_api.stats.static import teams, players

# ------------------------------------------------------------------------------
# Flask app
# ------------------------------------------------------------------------------
app = Flask(__name__)

# ------------------------------------------------------------------------------
# Constants: headers/proxy/timeout
# ------------------------------------------------------------------------------
HEADERS = {
    "Host": "stats.nba.com",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:72.0) Gecko/20100101 Firefox/72.0",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate, br",
    "x-nba-stats-origin": "stats",
    "x-nba-stats-token": "true",
    "Connection": "keep-alive",
    "Referer": "https://stats.nba.com/",
    "Pragma": "no-cache",
    "Cache-Control": "no-cache",
}

PROXIES = {
    # "http": "http://your-proxy:port",
    # "https": "http://your-proxy:port",
}

DEFAULT_TIMEOUT = 60

_TGL_CACHE = {}
_TGL_LOCK = threading.Lock()
_TGL_TTL_SECONDS = 1800  # 30 minutes

def get_team_gamelog_cached(team_id: int, season: str, timeout_sec: int = 10):
    """
    Fetch TeamGameLog with a small timeout, cache for 30min.
    Falls back to cached copy immediately if NBA API call fails or times out.
    """
    key = (int(team_id), season)
    now = _now()

    with _TGL_LOCK:
        entry = _TGL_CACHE.get(key)
        if entry and now - entry["ts"] < _TGL_TTL_SECONDS:
            return entry["df"]

    try:
        df = nbacall_retry(
            TeamGameLog,
            team_id=int(team_id),
            season=season,
            season_type_all_star="Regular Season",
            timeout=timeout_sec,
        ).get_data_frames()[0]
        if df is None:
            df = pd.DataFrame()
        with _TGL_LOCK:
            _TGL_CACHE[key] = {"ts": now, "df": df}
        return df
    except Exception as e:
        print(f"[WARN] get_team_gamelog_cached fallback due to: {e}")
        with _TGL_LOCK:
            entry = _TGL_CACHE.get(key)
            if entry:
                return entry["df"]
        return pd.DataFrame()

def get_seasons(start_year: int = 1951):
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
    Wrapper for nba_api endpoint classes with consistent headers/timeout/proxy and
    a simple retry with linear backoff.
    """
    kwargs.setdefault("headers", HEADERS)
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

def calculate_true_shooting(row):
    pts = row.get("PTS", 0)
    fga = row.get("FGA", 0)
    fta = row.get("FTA", 0)
    denom = fga + 0.44 * fta
    if denom == 0:
        return 0
    return (pts / (2 * denom)) * 100

def calculate_efficiency(row):
    positive = row.get("PTS", 0) + row.get("REB", 0) + row.get("AST", 0) + row.get("STL", 0) + row.get("BLK", 0)
    negative = (row.get("FGA", 0) - row.get("FGM", 0)) + (row.get("FTA", 0) - row.get("FTM", 0)) + row.get("TOV", 0)
    return positive - negative

@app.route("/")
def home():
    today = datetime.now().strftime("%m/%d/%Y")
    games = []
    try:
        scoreboard = nbacall_retry(ScoreboardV2, game_date=today, timeout=30)
        raw = scoreboard.get_normalized_dict()
        games = raw.get("GameHeader", []) or []
    except KeyError as e:
        print(f"[WARN] ScoreboardV2 missing dataset: {e}. Showing empty games list.")
    except Exception as e:
        print(f"[ERROR] Could not fetch scoreboard: {e}. Showing empty games list.")

    games_today = []
    for g in games:
        try:
            games_today.append(
                {
                    "game_id": g.get("GAME_ID"),
                    "matchup": f"{g.get('VISITOR_TEAM_ABBREVIATION')} @ {g.get('HOME_TEAM_ABBREVIATION')}",
                    "game_time": g.get("GAME_TIME"),
                    "arena": g.get("ARENA_NAME"),
                }
            )
        except Exception:
            continue

    return render_template(
        "home.html",
        games_today=games_today,
        games_count=len(games_today),
        seasons=get_seasons(),
    )

@app.route("/players")
def players_page():
    nba_teams = teams.get_teams()
    return render_template("players.html", teams=nba_teams, seasons=get_seasons())

@app.route("/shot-charts")
def shot_charts():
    nba_teams = teams.get_teams()
    return render_template("shot_charts.html", teams=nba_teams, seasons=get_seasons())

@app.route("/advanced-metrics")
def advanced_metrics():
    return render_template("advanced_metrics.html", seasons=get_seasons())

@app.route("/team-trends")
def team_trends():
    team_list = teams.get_teams()
    seasons = get_seasons()
    return render_template("team_trends.html", teams=team_list, seasons=seasons)

@app.route("/compare")
def compare_players():
    return render_template("compare.html", seasons=get_seasons())

@app.route("/api/leaders")
def get_homepage_leaders():
    stat = request.args.get("stat", "Points")
    season = request.args.get("season", get_seasons()[0])

    stat_category_map = {
        "PTS": "Points",
        "REB": "Rebounds",
        "AST": "Assists",
        "STL": "Defense",
        "BLK": "Defense",
    }

    try:
        homepage = nbacall_retry(
            HomePageLeaders,
            game_scope_detailed="Season",
            league_id="00",
            player_or_team="Player",
            player_scope="All Players",
            season=season,
            season_type_playoffs="Regular Season",
            stat_category=stat_category_map.get(stat, "Points"),
            timeout=30,
        )
        df = homepage.get_data_frames()[0]
        return jsonify({"success": True, "data": df.to_dict(orient="records"), "count": len(df)})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/team-monthly")
def api_team_monthly():
    team_id = request.args.get("team_id")
    season  = request.args.get("season", get_seasons()[0])
    month   = int(request.args.get("month", "1"))

    try:
        tid = int(team_id)
    except (TypeError, ValueError):
        return jsonify({"success": True, "wins": 0, "losses": 0})

    gl_df = get_team_gamelog_cached(tid, season, timeout_sec=30)

    if gl_df is None or gl_df.empty:
        return jsonify({"success": True, "wins": 0, "losses": 0})

    gl_df = gl_df.copy()
    gl_df["GAME_DATE"] = pd.to_datetime(gl_df["GAME_DATE"], errors="coerce")

    wins = int(((gl_df["GAME_DATE"].dt.month == month) & (gl_df["WL"] == "W")).sum())
    losses = int(((gl_df["GAME_DATE"].dt.month == month) & (gl_df["WL"] == "L")).sum())
    return jsonify({"success": True, "wins": wins, "losses": losses})

@app.route("/api/team-monthly-series")
def api_team_monthly_series():
    team_id = request.args.get("team_id", type=int)
    season  = request.args.get("season", default=get_seasons()[0])

    if not team_id:
        return jsonify({"success": False, "error": "team_id required"}), 400

    df = get_team_gamelog_cached(team_id, season, timeout_sec=30)  # longer timeout
    if df is None or df.empty:
        return jsonify({"success": True, "months": ["Oct","Nov","Dec","Jan","Feb","Mar","Apr"], "win_pct": [0]*7})

    df = df.copy()
    df["GAME_DATE"] = pd.to_datetime(df["GAME_DATE"], errors="coerce")
    df["M"] = df["GAME_DATE"].dt.month

    order = [(10,"Oct"),(11,"Nov"),(12,"Dec"),(1,"Jan"),(2,"Feb"),(3,"Mar"),(4,"Apr")]
    labels, values = [], []
    for mnum, mlabel in order:
        sub = df[df["M"] == mnum]
        w = int((sub["WL"] == "W").sum()) if not sub.empty else 0
        l = int((sub["WL"] == "L").sum()) if not sub.empty else 0
        total = w + l
        labels.append(mlabel)
        values.append(round((w/total)*100, 1) if total else 0.0)

    return jsonify({"success": True, "months": labels, "win_pct": values})

@app.route("/api/team-stats/<team_id>")
def api_team_stats(team_id):
    season = request.args.get("season", get_seasons()[0])

    try:
        team_id_int = int(team_id)

        def get_teamgamelog_df():
            try:
                tgl = nbacall_retry(
                    TeamGameLog,
                    team_id=team_id_int,
                    season=season,
                    season_type_all_star="Regular Season",
                    timeout=30,
                ).get_data_frames()[0]
                return tgl if tgl is not None else pd.DataFrame()
            except Exception as e:
                print(f"[WARN] TeamGameLog failed: {e}")
                return pd.DataFrame()

        gl_df = get_teamgamelog_df()

        if gl_df.empty:
            try:
                lgl = nbacall_retry(
                    LeagueGameLog,
                    season=season,
                    season_type_all_star="Regular Season",
                    league_id="00",
                    timeout=30,
                ).get_data_frames()[0]
                if lgl is not None and not lgl.empty:
                    gl_df = lgl[lgl["TEAM_ID"] == team_id_int].copy()
                else:
                    gl_df = pd.DataFrame()
            except Exception as e:
                print(f"[WARN] LeagueGameLog fallback failed: {e}")
                gl_df = pd.DataFrame()

        if gl_df is not None and not gl_df.empty and "WL" in gl_df.columns:
            wl_w = int((gl_df["WL"] == "W").sum())
            wl_l = int((gl_df["WL"] == "L").sum())
        else:
            wl_w = wl_l = 0

        home_w = home_l = road_w = road_l = 0
        if gl_df is not None and not gl_df.empty and "MATCHUP" in gl_df.columns:
            home_mask = gl_df["MATCHUP"].str.contains("vs", na=False)
            road_mask = gl_df["MATCHUP"].str.contains("@", na=False)
            home_w = int((gl_df[home_mask]["WL"] == "W").sum())
            home_l = int((gl_df[home_mask]["WL"] == "L").sum())
            road_w = int((gl_df[road_mask]["WL"] == "W").sum())
            road_l = int((gl_df[road_mask]["WL"] == "L").sum())
        home_record_fallback = f"{home_w}-{home_l}"
        road_record_fallback = f"{road_w}-{road_l}"

        ppg = rpg = apg = spg = bpg = 0.0
        fg_pct = fg3_pct = ft_pct = 0.0
        opp_ppg = 0.0
        sanity_ok = False

        try:
            stats = nbacall_retry(
                leaguedashteamstats.LeagueDashTeamStats,
                season=season,
                season_type_all_star="Regular Season",
                per_mode_detailed="PerGame",
                league_id_nullable="00",
            )
            df = stats.get_data_frames()[0]
            row_df = df[df["TEAM_ID"] == team_id_int]
            if not row_df.empty:
                row = row_df.iloc[0]
                ppg = float(row.get("PTS", 0.0))
                rpg = float(row.get("REB", 0.0))
                apg = float(row.get("AST", 0.0))
                spg = float(row.get("STL", 0.0))
                bpg = float(row.get("BLK", 0.0))

                plus_minus = float(row.get("PLUS_MINUS", 0.0)) if "PLUS_MINUS" in row else 0.0
                opp_ppg = ppg - plus_minus

                fg_pct  = float(row.get("FG_PCT", 0.0)) * 100.0
                fg3_pct = float(row.get("FG3_PCT", 0.0)) * 100.0
                ft_pct  = float(row.get("FT_PCT", 0.0)) * 100.0

                sanity_ok = ppg >= 90 or season < "1980-81"
        except Exception as e:
            print(f"[WARN] LeagueDashTeamStats failed: {e}")

        if not sanity_ok and gl_df is not None and not gl_df.empty:
            ppg = float(gl_df["PTS"].mean()) if "PTS" in gl_df.columns else 0.0
            rpg = float(gl_df["REB"].mean()) if "REB" in gl_df.columns else 0.0
            apg = float(gl_df["AST"].mean()) if "AST" in gl_df.columns else 0.0
            spg = float(gl_df["STL"].mean()) if "STL" in gl_df.columns else 0.0
            bpg = float(gl_df["BLK"].mean()) if "BLK" in gl_df.columns else 0.0
            if "PTS" in gl_df.columns and "PLUS_MINUS" in gl_df.columns:
                opp_ppg = float((gl_df["PTS"] - gl_df["PLUS_MINUS"]).mean())

        off_rating = def_rating = net_rating = pace = 0.0
        try:
            em_df = nbacall_retry(
                teamestimatedmetrics.TeamEstimatedMetrics,
                season=season,
                season_type="Regular Season",
                timeout=30,
            ).get_data_frames()[0]
            em_row = em_df[em_df["TEAM_ID"] == team_id_int]
            if not em_row.empty:
                em = em_row.iloc[0]
                off_rating = float(em.get("E_OFF_RATING", 0.0))
                def_rating = float(em.get("E_DEF_RATING", 0.0))
                net_rating = float(em.get("E_NET_RATING", 0.0))
                pace       = float(em.get("E_PACE", 0.0))
        except Exception as e:
            print(f"[WARN] TeamEstimatedMetrics failed: {e}")

        home_record = f"{home_w}-{home_l}"
        road_record = f"{road_w}-{road_l}"

        if gl_df is None or gl_df.empty:
            try:
                splits = nbacall_retry(
                    teamdashboardbygeneralsplits.TeamDashboardByGeneralSplits,
                    team_id=team_id_int,
                    season=season,
                    season_type_all_star="Regular Season",
                    timeout=30,
                ).get_data_frames()
                by_loc = splits[1] if len(splits) > 1 else None
                if by_loc is not None and not by_loc.empty:
                    home_df = by_loc[by_loc["GROUP_VALUE"] == "Home"]
                    road_df = by_loc[by_loc["GROUP_VALUE"] == "Road"]
                    if not home_df.empty:
                        home_record = f"{int(home_df['W'].iloc[0])}-{int(home_df['L'].iloc[0])}"
                    if not road_df.empty:
                        road_record = f"{int(road_df['W'].iloc[0])}-{int(road_df['L'].iloc[0])}"
            except Exception as e:
                print(f"[WARN] GeneralSplits failed: {e}")

        stats_dict = {
            "W": wl_w,
            "L": wl_l,
            "W_PCT": round((wl_w / max(1, (wl_w + wl_l))), 3),
            "PPG": round(ppg, 1),
            "RPG": round(rpg, 1),
            "APG": round(apg, 1),
            "SPG": round(spg, 1),
            "BPG": round(bpg, 1),
            "FG_PCT": round(fg_pct, 1),
            "FG3_PCT": round(fg3_pct, 1),
            "FT_PCT": round(ft_pct, 1),
            "OFF_RATING": round(off_rating, 1),
            "DEF_RATING": round(def_rating, 1),
            "NET_RATING": round(net_rating, 1),
            "PACE": round(pace, 1),
            "OPP_PPG": round(opp_ppg, 1),
            "HOME_RECORD": home_record,
            "ROAD_RECORD": road_record,
        }

        return jsonify({"success": True, "stats": stats_dict})

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": f"Server error fetching team stats: {str(e)}"}), 500

@app.route("/api/roster-analysis/<team_id>")
def api_roster_analysis(team_id):
    season = request.args.get("season", get_seasons()[0])

    try:
        dash = nbacall_retry(TeamPlayerDashboard, team_id=team_id, season=season, timeout=30)
        dfs = dash.get_data_frames()
        stats = dfs[1] if len(dfs) > 1 else pd.DataFrame()
        if stats is None or stats.empty:
            return jsonify(
                {
                    "success": True,
                    "top_scorer": {"name": "N/A", "stat": 0},
                    "top_rebounder": {"name": "N/A", "stat": 0},
                    "top_playmaker": {"name": "N/A", "stat": 0},
                    "most_efficient": {"name": "N/A", "stat": 0},
                }
            )

        stats = stats.copy()
        stats["EFF_RAW"] = stats.apply(calculate_efficiency, axis=1)
        stats["EFF"] = stats.apply(lambda r: (r["EFF_RAW"] / (r.get("GP", 1) or 1)), axis=1)

        def build_player_dict(row, stat_col):
            if row is None:
                return {"name": "N/A", "stat": 0}
            gp = row.get("GP") or 1
            total = row.get(stat_col, 0)
            per_game = total / gp
            return {"name": row.get("PLAYER_NAME", "N/A"), "stat": round(per_game, 1)}

        top_scorer = stats.sort_values("PTS", ascending=False).iloc[0] if not stats.empty else None
        top_rebounder = stats.sort_values("REB", ascending=False).iloc[0] if not stats.empty else None
        top_playmaker = stats.sort_values("AST", ascending=False).iloc[0] if not stats.empty else None
        most_efficient = stats.sort_values("EFF", ascending=False).iloc[0] if not stats.empty else None

        return jsonify(
            {
                "success": True,
                "top_scorer": build_player_dict(top_scorer, "PTS"),
                "top_rebounder": build_player_dict(top_rebounder, "REB"),
                "top_playmaker": build_player_dict(top_playmaker, "AST"),
                "most_efficient": build_player_dict(most_efficient, "EFF"),
            }
        )

    except Exception as e:
        print(f"[ERROR] /api/roster-analysis/{team_id}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/team_trends_data")
def team_trends_data():
    team_id = request.args.get("team_id")
    season = request.args.get("season", get_seasons()[0])

    try:
        general = nbacall_retry(
            TeamDashboardByGeneralSplits,
            team_id=team_id,
            season=season,
            season_type_all_star="Regular Season",
            league_id="00",
            timeout=30,
        ).get_normalized_dict()
        overall_stats = (general.get("OverallTeamDashboard") or [{}])[0] if general else {}

        _shooting = nbacall_retry(
            TeamDashboardByShootingSplits,
            team_id=team_id,
            season=season,
            season_type_all_star="Regular Season",
            league_id="00",
            timeout=30,
        ).get_normalized_dict()

        gl_df = nbacall_retry(
            TeamGameLog,
            team_id=team_id,
            season=season,
            season_type_all_star="Regular Season",
            timeout=30,
        ).get_data_frames()[0]
        if gl_df is None or gl_df.empty:
            monthly_avg = {m: 0 for m in ["October", "November", "December", "January", "February", "March", "April"]}
            home_avg_pts = away_avg_pts = 0.0
            east_avg = west_avg = 0.0
        else:
            gl_df = gl_df.copy()
            gl_df["GAME_DATE"] = pd.to_datetime(gl_df["GAME_DATE"])
            gl_df["MONTH"] = gl_df["GAME_DATE"].dt.month_name()

            month_order = ["October", "November", "December", "January", "February", "March", "April"]
            monthly_avg = (
                gl_df.groupby("MONTH")["PTS"].mean().reindex(month_order).fillna(0).round(1).to_dict()
            )

            home_games = gl_df[gl_df["MATCHUP"].str.contains("vs", na=False)]
            away_games = gl_df[gl_df["MATCHUP"].str.contains("@", na=False)]
            home_avg_pts = round(home_games["PTS"].mean(), 1) if not home_games.empty else 0.0
            away_avg_pts = round(away_games["PTS"].mean(), 1) if not away_games.empty else 0.0

            east = {"ATL", "BOS", "BKN", "CHA", "CHI", "CLE", "DET", "IND", "MIA", "MIL", "NYK", "ORL", "PHI", "TOR", "WAS"}
            gl_df["OPP_ABBR"] = gl_df["MATCHUP"].str.extract(r"([A-Z]{3})$")[0]
            gl_df["OPP_CONF"] = gl_df["OPP_ABBR"].apply(lambda x: "East" if x in east else "West")
            conf_avg_map = gl_df.groupby("OPP_CONF")["PTS"].mean().round(1).to_dict()
            east_avg = float(conf_avg_map.get("East", 0.0))
            west_avg = float(conf_avg_map.get("West", 0.0))

        try:
            pdash = nbacall_retry(TeamPlayerDashboard, team_id=team_id, season=season, timeout=30).get_normalized_dict()
            roster_list = sorted(pdash.get("TeamPlayerDashboard", []), key=lambda x: x.get("GP", 0), reverse=True)[:5]
        except Exception:
            roster_list = []

        payload = {
            "basic": {
                "wins": overall_stats.get("W", 0),
                "losses": overall_stats.get("L", 0),
                "ppg": round(float(overall_stats.get("PTS", 0.0)), 1),
                "apg": round(float(overall_stats.get("AST", 0.0)), 1),
                "rpg": round(float(overall_stats.get("REB", 0.0)), 1),
                "spg": round(float(overall_stats.get("STL", 0.0)), 1),
                "bpg": round(float(overall_stats.get("BLK", 0.0)), 1),
                "opp_ppg": round(float(overall_stats.get("OPP_PTS", 0.0)), 1),
            },
            "efficiency": {
                "off_rating": round(float(overall_stats.get("OFF_RATING", 0.0)), 1),
                "def_rating": round(float(overall_stats.get("DEF_RATING", 0.0)), 1),
                "net_rating": round(float(overall_stats.get("NET_RATING", 0.0)), 1),
                "pace": round(float(overall_stats.get("PACE", 0.0)), 1),
            },
            "rankings": {
                "pts_rank": overall_stats.get("PTS_RANK", None),
                "reb_rank": overall_stats.get("REB_RANK", None),
                "ast_rank": overall_stats.get("AST_RANK", None),
                "opp_pts_rank": overall_stats.get("OPP_PTS_RANK", None),
            },
            "trends": {
                "monthly": monthly_avg,
                "home_avg": home_avg_pts,
                "away_avg": away_avg_pts,
                "east_avg": east_avg,
                "west_avg": west_avg,
            },
            "roster": [
                {
                    "player": p.get("PLAYER_NAME", "N/A"),
                    "ppg": round(float(p.get("PTS", 0.0)), 1),
                    "rpg": round(float(p.get("REB", 0.0)), 1),
                    "apg": round(float(p.get("AST", 0.0)), 1),
                }
                for p in roster_list
            ],
        }
        return jsonify(payload)

    except Exception as e:
        print(f"[ERROR] /api/team_trends_data: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/players")
def get_players():
    try:
        season = request.args.get("season", get_seasons()[0])
        team_code = request.args.get("team", "all")
        position_filter = request.args.get("position", "all")
        sort_by = request.args.get("sort_by", "PTS")
        search = request.args.get("search", "").lower()

        time.sleep(0.25)
        stats = nbacall_retry(
            leaguedashplayerstats.LeagueDashPlayerStats,
            season=season,
            season_type_all_star="Regular Season",
        )
        df = stats.get_data_frames()[0]

        if search:
            df = df[df["PLAYER_NAME"].str.lower().str.contains(search)]
        if team_code != "all":
            df = df[df["TEAM_ABBREVIATION"] == team_code]
        if position_filter != "all" and "POSITION" in df.columns:
            df = df[df["POSITION"].str.contains(position_filter, na=False)]

        df = df.copy()
        df["TS_PCT"] = df.apply(lambda r: calculate_true_shooting(r), axis=1)
        df["EFF"] = df.apply(lambda r: calculate_efficiency(r) / (r["GP"] or 1), axis=1)

        tot_mp = df["MIN"].sum()
        tot_fga = df["FGA"].sum()
        tot_fta = df["FTA"].sum()
        tot_tov = df["TOV"].sum()
        tot_fgm = df["FGM"].sum()
        tot_reb = df["REB"].sum()

        df["USG_PCT"] = (
            100
            * ((df["FGA"] + 0.44 * df["FTA"] + df["TOV"]) * (tot_mp / 5))
            / (df["MIN"] * (tot_fga + 0.44 * tot_fta + tot_tov))
        ).fillna(0)

        df["AST_PCT"] = (
            100
            * df["AST"]
            / ((((df["MIN"] / (tot_mp / 5)) * tot_fgm) - df["FGM"]).replace(0, pd.NA))
        ).fillna(0)

        df["REB_PCT"] = (100 * (df["REB"] * (tot_mp / 5)) / (df["MIN"] * (tot_reb + tot_reb))).fillna(0)

        num = (
            df["PTS"]
            + df["FGM"]
            + df["FTM"]
            - df["FGA"]
            - df["FTA"]
            + df["DREB"]
            + 0.5 * df["OREB"]
            + df["AST"]
            + df["STL"]
            + 0.5 * df["BLK"]
            - df["PF"]
            - df["TOV"]
        )
        total_num = num.sum() if num.sum() != 0 else 1
        df["PIE"] = (num / total_num * 100).fillna(0)

        display_columns = [
            "PLAYER_ID",
            "PLAYER_NAME",
            "TEAM_ABBREVIATION",
            "POSITION",
            "AGE",
            "GP",
            "MIN",
            "PTS",
            "REB",
            "AST",
            "STL",
            "BLK",
            "FG_PCT",
            "FG3_PCT",
            "FT_PCT",
            "TS_PCT",
            "EFF",
            "USG_PCT",
            "AST_PCT",
            "REB_PCT",
            "PIE",
            "PF",
        ]
        display_columns = [c for c in display_columns if c in df.columns]
        df_display = df[display_columns].fillna(0)

        if sort_by in df_display.columns:
            df_display = df_display.sort_values(by=sort_by, ascending=False)

        players_data = df_display.to_dict("records")
        for p in players_data:
            if "FG_PCT" in p:
                p["FG_PCT"] = round(p["FG_PCT"] * 100, 1)
            if "FG3_PCT" in p:
                p["FG3_PCT"] = round(p["FG3_PCT"] * 100, 1)
            if "FT_PCT" in p:
                p["FT_PCT"] = round(p["FT_PCT"] * 100, 1)
            if "TS_PCT" in p:
                p["TS_PCT"] = round(p["TS_PCT"], 1)
            if "EFF" in p:
                p["EFF"] = round(p["EFF"], 1)
            if "USG_PCT" in p:
                p["USG_PCT"] = round(p["USG_PCT"], 1)
            if "AST_PCT" in p:
                p["AST_PCT"] = round(p["AST_PCT"], 1)
            if "REB_PCT" in p:
                p["REB_PCT"] = round(p["REB_PCT"], 1)
            if "PIE" in p:
                p["PIE"] = round(p["PIE"], 1)
            for stat in ["MIN", "PTS", "REB", "AST", "STL", "BLK", "PF"]:
                if stat in p:
                    p[stat] = round(p[stat], 1)

        return jsonify(
            {
                "success": True,
                "data": players_data,
                "count": len(players_data),
                "meta": {"estimated_fields": ["USG_PCT", "AST_PCT", "REB_PCT", "PIE"]},
            }
        )

    except Exception as e:
        print("Error in /api/players:", e)
        return jsonify(
            {"success": False, "error": "Unable to fetch player data from NBA API. Please try again later."}
        ), 503

@app.route("/api/player/<int:player_id>")
def get_player_detail(player_id: int):
    """
    Returns:
      - player_info: dict from CommonPlayerInfo (current team info)
      - seasons_regular: list[dict] SeasonTotalsRegularSeason rows (newest->oldest)
      - career_regular: list[dict] CareerTotalsRegularSeason (1 row if available)
      - available_seasons: list[str] newest->oldest
      - selected_season: dict for the requested ?season= (per-season team + derived stats)
    """
    try:
        # Optional season query (e.g., "2018-19")
        req_season = request.args.get("season")

        # 0) Build a fast TEAM_ID -> names map for accurate per-season team labeling
        team_map = {}
        try:
            for t in teams.get_teams():
                team_map[int(t["id"])] = {
                    "TEAM_NAME": t["full_name"],
                    "TEAM_ABBREVIATION": t["abbreviation"],
                    "TEAM_CITY": t.get("city", ""),
                }
        except Exception:
            pass

        # 1) Player bio
        cpi = nbacall_retry(commonplayerinfo.CommonPlayerInfo, player_id=player_id, timeout=30)
        info_df = cpi.get_data_frames()[0]
        info = info_df.to_dict("records")[0] if len(info_df) > 0 else {}

        # 2) Player profile (regular season tables)
        prof = nbacall_retry(playerprofilev2.PlayerProfileV2, player_id=player_id, timeout=30)
        norm = prof.get_normalized_dict()

        seasons_regular = norm.get("SeasonTotalsRegularSeason", []) or []
        career_regular  = norm.get("CareerTotalsRegularSeason", []) or []

        # Sort newest -> oldest
        def season_key(row):
            try:
                return int(str(row.get("SEASON_ID", "0-00")).split("-")[0])
            except Exception:
                return -1
        seasons_regular = sorted(seasons_regular, key=season_key, reverse=True)

        # Available seasons list
        available_seasons = [r.get("SEASON_ID") for r in seasons_regular if r.get("SEASON_ID")]

        # Helper to compute derived metrics for a season row
        def enrich(row):
            r = dict(row)  # copy
            gp  = max(int(r.get("GP") or 0), 1)
            min_tot = float(r.get("MIN") or 0.0)
            r["MPG"] = (min_tot / gp) if gp else 0.0
            r["PPG"] = float(r.get("PTS") or 0.0) / gp
            r["RPG"] = float(r.get("REB") or 0.0) / gp
            r["APG"] = float(r.get("AST") or 0.0) / gp
            r["SPG"] = float(r.get("STL") or 0.0) / gp
            r["BPG"] = float(r.get("BLK") or 0.0) / gp
            r["TOV"] = float(r.get("TOV") or 0.0) / gp

            # Totals (rename for clarity)
            r["MIN_TOTAL"] = min_tot
            r["PTS_TOTAL"] = float(r.get("PTS") or 0.0)
            r["REB_TOTAL"] = float(r.get("REB") or 0.0)
            r["AST_TOTAL"] = float(r.get("AST") or 0.0)
            r["STL_TOTAL"] = float(r.get("STL") or 0.0)
            r["BLK_TOTAL"] = float(r.get("BLK") or 0.0)
            r["TOV_TOTAL"] = float(r.get("TOV") or 0.0)

            # Advanced shooting
            fg_pct  = float(r.get("FG_PCT") or 0.0) * 100.0
            fg3_pct = float(r.get("FG3_PCT") or 0.0) * 100.0
            ft_pct  = float(r.get("FT_PCT") or 0.0) * 100.0
            fga     = float(r.get("FGA") or 0.0)
            fta     = float(r.get("FTA") or 0.0)
            fgm     = float(r.get("FGM") or 0.0)
            fg3a    = float(r.get("FG3A") or 0.0)
            fg3m    = float(r.get("FG3M") or 0.0)
            pts     = float(r.get("PTS") or 0.0)

            # eFG%: (FGM + 0.5*3PM) / FGA
            r["EFG_PCT"] = ((fgm + 0.5 * fg3m) / fga * 100.0) if fga else None
            # TS%: PTS / (2*(FGA + 0.44*FTA))
            denom = fga + 0.44 * fta
            r["TS_PCT"] = ((pts / (2.0 * denom)) * 100.0) if denom else None
            # Volume rates
            r["THREEPAR"] = ((fg3a / fga) * 100.0) if fga else None  # 3PA rate
            r["FTR"]      = ((fta / fga) * 100.0) if fga else None   # FT rate

            # Per‑36
            mpg = r["MPG"]
            scale = (36.0 / mpg) if mpg else 0.0
            for k in ("PTS","REB","AST","STL","BLK","TOV","OREB","DREB","FG3M","FTA","FGA"):
                val = float(r.get(k) or 0.0) / gp
                r[f"{k}_P36"] = val * scale if scale else 0.0

            # Ensure per‑season team name/abbr come from the season row (not current team)
            team_id = r.get("TEAM_ID")
            if isinstance(team_id, str) and team_id.isdigit():
                team_id = int(team_id)
            team_label = team_map.get(team_id, {})
            if team_label:
                r["TEAM_NAME"] = team_label.get("TEAM_NAME")
                r["TEAM_ABBREVIATION"] = team_label.get("TEAM_ABBREVIATION", r.get("TEAM_ABBREVIATION"))
            # leave TEAM_ABBREVIATION from the row as a fallback

            return r

        # Enrich all rows for front‑end (keeps TEAM_ABBREVIATION from the season)
        seasons_regular = [enrich(r) for r in seasons_regular]

        # Determine selected_season
        selected = None
        if req_season:
            selected = next((r for r in seasons_regular if r.get("SEASON_ID") == req_season), None)
        if not selected and seasons_regular:
            selected = seasons_regular[0]

        return jsonify({
            "success": True,
            "player_info": info,                 # current team/bio
            "seasons_regular": seasons_regular,  # enriched rows (newest->oldest)
            "career_regular": career_regular,
            "available_seasons": available_seasons,
            "selected_season": selected,
        })

    except Exception as e:
        print(f"[ERROR] /api/player/{player_id}: {e}")
        return jsonify({"success": False, "error": str(e)})

@app.route("/player/<int:player_id>")
def player_detail(player_id: int):
    return render_template("player_detail.html", player_id=player_id, seasons=get_seasons())

@app.route("/api/export/players")
def export_players():
    try:
        season = request.args.get("season", get_seasons()[0])
        team_code = request.args.get("team", "all")
        sort_by = request.args.get("sort_by", "PTS")

        player_stats = nbacall_retry(
            leaguedashplayerstats.LeagueDashPlayerStats,
            season=season,
            season_type_all_star="Regular Season",
            timeout=30,
        )
        df = player_stats.get_data_frames()[0]
        if team_code != "all":
            df = df[df["TEAM_ABBREVIATION"] == team_code]
        if sort_by in df.columns:
            df = df.sort_values(by=sort_by, ascending=False)

        output = io.StringIO()
        df.to_csv(output, index=False)
        output.seek(0)
        mem = io.BytesIO()
        mem.write(output.getvalue().encode("utf-8"))
        mem.seek(0)
        return send_file(
            mem,
            mimetype="text/csv",
            as_attachment=True,
            download_name=f"nba_players_{season}_{datetime.now().strftime('%Y%m%d')}.csv",
        )
    except Exception as e:
        print(f"Error in /api/export/players: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/search-players")
def search_players():
    try:
        query = request.args.get("q", "").lower()
        all_players = players.get_players()
        matching = [
            {"id": p["id"], "name": p["full_name"], "is_active": p["is_active"]}
            for p in all_players
            if query in p["full_name"].lower()
        ][:10]
        return jsonify({"success": True, "players": matching})
    except Exception as e:
        print(f"Error in /api/search-players: {e}")
        return jsonify({"success": False, "error": str(e)})

@app.route("/test-api")
def test_api():
    try:
        all_teams = teams.get_teams()
        all_players = players.get_players()
        return jsonify(
            {
                "success": True,
                "teams_count": len(all_teams),
                "players_count": len(all_players),
                "sample_team": all_teams[0] if all_teams else None,
                "sample_player": all_players[0] if all_players else None,
            }
        )
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(debug=True, port=port)