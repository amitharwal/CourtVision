import io
import os
import re
import time
import unicodedata
from datetime import datetime
from zoneinfo import ZoneInfo
from functools import lru_cache

import click
import pandas as pd
from flask import Flask, render_template, request, jsonify, send_file
from werkzeug.routing import BaseConverter
from nba_api.stats.endpoints import LeagueGameLog, teamdashboardbygeneralsplits
from nba_api.stats.static import teams, players

from metrics import calculate_efficiency, calculate_true_shooting
from nba_client import (
    ANY,
    OFFLINE,
    SEASON,
    NBAUnavailable,
    get_league_player_stats,
    get_league_team_stats,
    get_player_gamelog,
    get_player_info,
    get_player_positions,
    get_player_profile,
    get_scoreboard,
    get_seasons,
    get_shot_chart,
    get_standings,
    get_team_estimated_metrics,
    get_team_gamelog_cached,
    nbacall_retry,
    parse_season_type,
    published_player_ids,
    published_seasons,
    warm_cache,
)

# ------------------------------------------------------------------------------
# Flask app
# ------------------------------------------------------------------------------
app = Flask(__name__)

# Data routes take everything from the path (no query strings), so `flask build` can
# save each response as a static file at the same URL.
class SeasonConverter(BaseConverter):
    """A season like 2025-26."""
    regex = r"\d{4}-\d{2}"

class SeasonTypeConverter(BaseConverter):
    """"regular" or "playoffs" in a URL; routes receive the NBA API name."""
    regex = r"regular|playoffs"

    def to_python(self, value):
        return parse_season_type(value)

    def to_url(self, value):
        return "playoffs" if value == "Playoffs" else "regular"

app.url_map.converters["season"] = SeasonConverter
app.url_map.converters["season_type"] = SeasonTypeConverter

NAV_LINKS = [
    ("home", "Home"),
    ("players_page", "Players"),
    ("team_trends", "Teams"),
    ("standings", "Standings"),
    ("shot_charts", "Shot Charts"),
    ("compare_players", "Compare"),
    ("advanced_metrics", "Adv. Metrics"),
]

# Pages that live under another nav section.
NAV_PARENTS = {"player_detail": "players_page"}

@app.context_processor
def inject_layout():
    return {
        "nav_links": NAV_LINKS,
        "active_nav": NAV_PARENTS.get(request.endpoint, request.endpoint),
        "current_year": datetime.now().year,
    }

NOT_ON_SITE = "This isn't on the site yet. Try the current season, or check back after the next update."

# Cache-key patterns (see nba_client.published_seasons) for the data behind each page.
LEAGUE_STATS = ("league_player_stats", SEASON, "Base", "Regular Season")
PLAYOFF_STATS = ("league_player_stats", SEASON, "Base", "Playoffs")

def season_choices(pattern=LEAGUE_STATS):
    """
    Seasons a page offers, newest first: every season, or in offline mode (and builds)
    just those with cached data matching pattern (at least the current one, so pages that
    have nothing yet still render and explain themselves).
    """
    published = published_seasons(pattern)
    if published is None:
        return get_seasons()
    return published or get_seasons()[:1]

def current_season():
    """The newest season with league data: the default wherever no season is given."""
    return season_choices()[0]

def playoff_seasons():
    """Seasons with playoff games to show, or None when any season can be requested."""
    published = published_seasons(PLAYOFF_STATS)
    if published is None:
        return None
    seasons = []
    for season in published:
        try:
            if not get_league_player_stats(season, "Base", "Playoffs").empty:
                seasons.append(season)
        except Exception as e:
            print(f"[WARN] playoff stats for {season} unreadable: {e}")
    return seasons

def error_response(e, message, status=503):
    """JSON error for a data route; data that isn't cached (offline mode, builds) gets an explanation instead."""
    if isinstance(e, NBAUnavailable):
        return jsonify({"success": False, "error": NOT_ON_SITE, "offline": True}), 503
    print(f"[ERROR] {request.path}: {e}")
    return jsonify({"success": False, "error": message}), status

@app.route("/")
def home():
    # Games, leaders and standings load client-side so the page renders without
    # waiting on the NBA API.
    return render_template("home.html", season=current_season())

@app.route("/privacy_policy")
def privacy_policy():
    return render_template("privacy_policy.html")

@app.route("/data/games-today.json")
def api_games_today():
    # NBA schedules run on Eastern time, so "today" is the current date in New York.
    today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")

    try:
        games_df, teams_df = get_scoreboard(today)
        games = [_scoreboard_game(g, teams_df[teams_df["gameId"] == g["gameId"]]) for _, g in games_df.iterrows()]
        return jsonify({"success": True, "date": today, "games": games, "count": len(games)})
    except Exception as e:
        return error_response(e, "Unable to fetch today's games.")

def _scoreboard_game(game, team_rows):
    """One ScoreboardV3 game with its two team rows -> card data for the home page."""
    # gameCode is "YYYYMMDD/AWYHOM": away tricode first, then home.
    code = str(game["gameCode"]).split("/")[-1]
    away_code, home_code = code[:3], code[3:6]
    by_code = {r["teamTricode"]: r for _, r in team_rows.iterrows()}

    def team(tricode):
        r = by_code.get(tricode)
        if r is None:
            return {"id": None, "tricode": tricode, "name": tricode, "wins": 0, "losses": 0, "score": None}
        return {
            "id": int(r["teamId"]),
            "tricode": tricode,
            "name": f"{r['teamCity']} {r['teamName']}",
            "wins": int(r["wins"] or 0),
            "losses": int(r["losses"] or 0),
            "score": int(r["score"]) if pd.notna(r["score"]) else None,
        }

    return {
        "game_id": game["gameId"],
        "status": int(game["gameStatus"]),  # 1 scheduled, 2 live, 3 final
        "status_text": str(game["gameStatusText"] or "").strip(),
        "label": str(game.get("gameLabel") or "").strip(),
        "series": str(game.get("seriesText") or "").strip(),
        "away": team(away_code),
        "home": team(home_code),
    }

@app.route("/data/leaders/<season:season>.json")
def api_league_leaders(season):
    """Top 5 players per game in PTS/REB/AST for the home page."""
    limit = 5

    try:
        df = get_league_player_stats(season)
    except Exception as e:
        return error_response(e, "Unable to fetch league leaders.")

    if df is None or df.empty:
        return jsonify({"success": True, "season": season, "leaders": {}})

    # Qualify players who appeared in at least half of the most games anyone has played,
    # so a 3-game hot streak doesn't top a per-game leaderboard.
    min_gp = max(1, int(df["GP"].max() * 0.5))
    qualified = df[df["GP"] >= min_gp]

    leaders = {}
    for stat in ("PTS", "REB", "AST"):
        per_game = qualified[stat] / qualified["GP"]
        top = qualified.assign(VALUE=per_game).nlargest(limit, "VALUE")
        leaders[stat] = [
            {
                "player_id": int(r["PLAYER_ID"]),
                "name": r["PLAYER_NAME"],
                "team_id": int(r["TEAM_ID"]) if pd.notna(r["TEAM_ID"]) else None,
                "team": r["TEAM_ABBREVIATION"],
                "value": round(float(r["VALUE"]), 1),
            }
            for _, r in top.iterrows()
        ]

    return jsonify({"success": True, "season": season, "min_games": min_gp, "leaders": leaders})

@app.route("/players")
def players_page():
    nba_teams = teams.get_teams()
    return render_template("players.html", teams=nba_teams, seasons=get_seasons())

@app.route("/shot-charts")
def shot_charts():
    nba_teams = teams.get_teams()
    return render_template("shot_charts.html", teams=nba_teams,
                           seasons=season_choices(("shot_chart", ANY, SEASON, "Regular Season")),
                           playoff_seasons=playoff_seasons())

@app.route("/advanced-metrics")
def advanced_metrics():
    return render_template("advanced_metrics.html", seasons=season_choices(), playoff_seasons=playoff_seasons())

@app.route("/team-trends")
def team_trends():
    team_list = teams.get_teams()
    seasons = season_choices(("team_gamelog", ANY, SEASON))
    return render_template("team_trends.html", teams=team_list, seasons=seasons)

@app.route("/standings")
def standings():
    return render_template("standings.html", seasons=season_choices(("standings", SEASON)))

@app.route("/data/standings/<season:season>.json")
def api_standings(season):

    try:
        df = get_standings(season)
    except Exception as e:
        return error_response(e, "Unable to fetch standings from NBA API.")

    if df is None or df.empty:
        return jsonify({"success": False, "error": f"No standings for {season}."}), 404

    def text(value):
        """A text column, or None where older seasons have no value (NaN)."""
        return str(value).strip() if pd.notna(value) else None

    def row(r):
        return {
            "team_id": int(r["TeamID"]),
            "team": f"{r['TeamCity']} {r['TeamName']}",
            "rank": int(r["PlayoffRank"]),
            "wins": int(r["WINS"]),
            "losses": int(r["LOSSES"]),
            "win_pct": round(float(r["WinPCT"]), 3),
            "games_back": float(r["ConferenceGamesBack"]),
            "conf_record": text(r["ConferenceRecord"]),
            "home": text(r["HOME"]),
            "road": text(r["ROAD"]),
            "last10": text(r["L10"]),
            "streak": text(r["strCurrentStreak"]),
            "ppg": round(float(r["PointsPG"]), 1),
            "opp_ppg": round(float(r["OppPointsPG"]), 1),
            "diff": round(float(r["DiffPointsPG"]), 1),
        }

    conferences = {}
    for conf in ("East", "West"):
        sub = df[df["Conference"] == conf].sort_values("PlayoffRank")
        conferences[conf.lower()] = [row(r) for _, r in sub.iterrows()]

    return jsonify({"success": True, "season": season, **conferences})

@app.route("/compare")
def compare_players():
    return render_template("compare.html", seasons=get_seasons())

def team_monthly_series(team_id: int, season: str) -> dict:
    """Win % and record by regular-season month."""
    df = get_team_gamelog_cached(team_id, season, timeout_sec=30)  # longer timeout
    if df is None or df.empty:
        return {"months": [], "win_pct": [], "wins": [], "losses": []}

    df = df.copy()
    df["GAME_DATE"] = pd.to_datetime(df["GAME_DATE"], format="%b %d, %Y", errors="coerce")
    df["M"] = df["GAME_DATE"].dt.month

    # Regular-season months in order; months without games are left out rather than
    # reported as 0% (e.g. early in a season).
    order = [(10, "Oct"), (11, "Nov"), (12, "Dec"), (1, "Jan"), (2, "Feb"), (3, "Mar"), (4, "Apr")]
    labels, values, wins, losses = [], [], [], []
    for mnum, mlabel in order:
        sub = df[df["M"] == mnum]
        w = int((sub["WL"] == "W").sum())
        l = int((sub["WL"] == "L").sum())
        if w + l == 0:
            continue
        labels.append(mlabel)
        values.append(round(w / (w + l) * 100, 1))
        wins.append(w)
        losses.append(l)

    return {"months": labels, "win_pct": values, "wins": wins, "losses": losses}

def team_stats(team_id_int: int, season: str) -> dict:
    """Record, per-game averages, shooting, ratings and home/road splits for a team-season."""
    if OFFLINE:
        # The fallbacks below swallow errors and return zeros; in offline mode a
        # season that wasn't fetched should say so instead.
        get_league_team_stats(season)
        get_team_gamelog_cached(team_id_int, season)  # raises if not cached

    gl_df = get_team_gamelog_cached(team_id_int, season, timeout_sec=30)

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

    ppg = rpg = apg = spg = bpg = 0.0
    fg_pct = fg3_pct = ft_pct = 0.0
    opp_ppg = 0.0
    sanity_ok = False

    try:
        df = get_league_team_stats(season)
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
        em_df = get_team_estimated_metrics(season)
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

    return stats_dict

def roster_leaders(team_id: int, season: str) -> dict:
    """
    The team's top scorer, rebounder, playmaker and most efficient player per game,
    from the league player table (TeamPlayerDashboard returns nothing as of Oct 2026).
    A player traded mid-season counts for his latest team, with his full-season totals.
    """
    league = get_league_player_stats(season)
    stats = league[league["TEAM_ID"] == int(team_id)]
    if stats.empty:
        return {
            "top_scorer": {"name": "N/A", "stat": 0},
            "top_rebounder": {"name": "N/A", "stat": 0},
            "top_playmaker": {"name": "N/A", "stat": 0},
            "most_efficient": {"name": "N/A", "stat": 0},
        }

    stats = stats.copy()
    stats["EFF_RAW"] = stats.apply(calculate_efficiency, axis=1)
    stats["EFF"] = stats.apply(lambda r: (r["EFF_RAW"] / (r.get("GP", 1) or 1)), axis=1)

    def build_player_dict(row, stat_col):
        if row is None:
            return {"name": "N/A", "stat": 0}
        gp = row.get("GP") or 1
        total = row.get(stat_col, 0)
        per_game = total / gp
        return {
            "name": row.get("PLAYER_NAME", "N/A"),
            "player_id": int(row["PLAYER_ID"]) if row.get("PLAYER_ID") is not None else None,
            "stat": round(per_game, 1),
        }

    top_scorer = stats.sort_values("PTS", ascending=False).iloc[0] if not stats.empty else None
    top_rebounder = stats.sort_values("REB", ascending=False).iloc[0] if not stats.empty else None
    top_playmaker = stats.sort_values("AST", ascending=False).iloc[0] if not stats.empty else None
    most_efficient = stats.sort_values("EFF", ascending=False).iloc[0] if not stats.empty else None

    return {
        "top_scorer": build_player_dict(top_scorer, "PTS"),
        "top_rebounder": build_player_dict(top_rebounder, "REB"),
        "top_playmaker": build_player_dict(top_playmaker, "AST"),
        # build_player_dict divides by GP, so pass the season total (EFF is already per game)
        "most_efficient": build_player_dict(most_efficient, "EFF_RAW"),
    }

@app.route("/data/teams/<season:season>/<int:team_id>.json")
def api_team(season, team_id):
    """Everything the Teams page shows for one team-season."""
    try:
        return jsonify({
            "success": True,
            "stats": team_stats(team_id, season),
            "monthly": team_monthly_series(team_id, season),
            "roster": roster_leaders(team_id, season),
        })
    except Exception as e:
        return error_response(e, "Unable to load team data.", 500)

@app.route("/data/players/<season:season>/<season_type:season_type>.json")
def get_players(season, season_type):
    """Every player's season stats and metrics, highest scorers first; pages filter and sort."""
    try:
        df = get_league_player_stats(season, season_type=season_type).copy()

        # Official NBA advanced metrics (fractions, e.g. 0.312) replace local estimates.
        adv_cols = ["USG_PCT", "AST_PCT", "REB_PCT", "PIE"]
        adv = get_league_player_stats(season, "Advanced", season_type)
        df = df.merge(adv[["PLAYER_ID"] + adv_cols], on="PLAYER_ID", how="left")
        df[adv_cols] = df[adv_cols].fillna(0) * 100

        try:
            positions = get_player_positions()
        except Exception as e:
            print(f"[WARN] PlayerIndex positions unavailable: {e}")
            positions = {}
        df["POSITION"] = df["PLAYER_ID"].map(lambda pid: positions.get(int(pid), ""))

        df["TS_PCT"] = df.apply(lambda r: calculate_true_shooting(r), axis=1)
        df["EFF"] = df.apply(lambda r: calculate_efficiency(r) / (r["GP"] or 1), axis=1)

        display_columns = [
            "PLAYER_ID",
            "PLAYER_NAME",
            "TEAM_ID",
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
            "FGA",
            "FTA",
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

        df_display = df_display.sort_values(by="PTS", ascending=False)

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
            for stat in ["MIN", "PTS", "REB", "AST", "STL", "BLK", "PF", "FGA", "FTA"]:
                if stat in p:
                    p[stat] = round(p[stat], 1)

        return jsonify(
            {
                "success": True,
                "data": players_data,
                "count": len(players_data),
            }
        )

    except Exception as e:
        return error_response(e, "Unable to fetch player data from NBA API. Please try again later.")

@app.route("/data/player/<int:player_id>.json")
def get_player_detail(player_id: int):
    """
    Returns:
      - player_info: dict from CommonPlayerInfo (current team info)
      - seasons_regular / seasons_postseason: list[dict] season rows with derived
        stats (newest->oldest; a traded player has a row per team plus "TOT")
      - career_regular: list[dict] CareerTotalsRegularSeason (1 row if available)
      - available_seasons: list[str] newest->oldest
      - gamelog_seasons: see gamelog_seasons()
    """
    try:
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
        info_df = get_player_info(player_id)
        # Missing bio fields (undrafted, no school) are NaN, which isn't valid JSON.
        info = info_df.astype(object).where(info_df.notna(), None).to_dict("records")[0] if len(info_df) > 0 else {}

        # 2) Player profile (regular season tables)
        norm = get_player_profile(player_id)

        seasons_regular = norm.get("SeasonTotalsRegularSeason", []) or []
        career_regular  = norm.get("CareerTotalsRegularSeason", []) or []
        seasons_postseason = norm.get("SeasonTotalsPostSeason", []) or []

        # Sort newest -> oldest
        def season_key(row):
            try:
                return int(str(row.get("SEASON_ID", "0-00")).split("-")[0])
            except Exception:
                return -1
        seasons_regular = sorted(seasons_regular, key=season_key, reverse=True)
        seasons_postseason = sorted(seasons_postseason, key=season_key, reverse=True)

        # Available seasons list (traded players have several rows per season)
        available_seasons = list(dict.fromkeys(r.get("SEASON_ID") for r in seasons_regular if r.get("SEASON_ID")))

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

            # Totals (rename for clarity)
            r["MIN_TOTAL"] = min_tot
            r["PTS_TOTAL"] = float(r.get("PTS") or 0.0)
            r["REB_TOTAL"] = float(r.get("REB") or 0.0)
            r["AST_TOTAL"] = float(r.get("AST") or 0.0)
            r["STL_TOTAL"] = float(r.get("STL") or 0.0)
            r["BLK_TOTAL"] = float(r.get("BLK") or 0.0)
            r["TOV_TOTAL"] = float(r.get("TOV") or 0.0)

            # Advanced shooting
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
            # Per-game TOV, set last so the totals and per-36 above use the season total
            r["TOV"] = r["TOV_TOTAL"] / gp

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
        seasons_postseason = [enrich(r) for r in seasons_postseason]

        return jsonify({
            "success": True,
            "gamelog_seasons": gamelog_seasons(player_id),
            "player_info": info,                 # current team/bio
            "seasons_regular": seasons_regular,  # enriched rows (newest->oldest)
            "seasons_postseason": seasons_postseason,
            "career_regular": career_regular,
            "available_seasons": available_seasons,
        })

    except Exception as e:
        return error_response(e, "Unable to load this player.", 500)

def gamelog_seasons(player_id: int):
    """
    {"regular": [...], "playoffs": [...]}: seasons whose game logs this player's page
    can show, or None when any season can be requested.
    """
    regular = published_seasons(("player_gamelog", player_id, SEASON, "Regular Season"))
    if regular is None:
        return None
    return {"regular": regular,
            "playoffs": published_seasons(("player_gamelog", player_id, SEASON, "Playoffs"))}

@app.route("/data/player/<int:player_id>/gamelog/<season:season>/<season_type:season_type>.json")
def api_player_gamelog(player_id: int, season, season_type):
    try:
        df = get_player_gamelog(player_id, season, season_type)
    except Exception as e:
        return error_response(e, "Unable to fetch game log from NBA API.")

    if df is None or df.empty:
        return jsonify({"success": True, "season": season, "season_type": season_type, "games": []})

    columns = ["MATCHUP", "WL", "MIN", "PTS", "REB", "AST", "STL", "BLK", "TOV",
               "FGM", "FGA", "FG3M", "FG3A", "FTM", "FTA", "PLUS_MINUS"]
    games = df.iloc[::-1]  # oldest -> newest, for charting
    dates = pd.to_datetime(games["GAME_DATE"], format="%b %d, %Y", errors="coerce")
    records = []
    for (_, g), date in zip(games.iterrows(), dates):
        rec = {c: g[c] for c in columns if c in g}
        rec["GAME_ID"] = g["Game_ID"]
        rec["GAME_DATE"] = date.strftime("%Y-%m-%d") if pd.notna(date) else g["GAME_DATE"]
        records.append(rec)

    return jsonify({"success": True, "season": season, "season_type": season_type, "games": records})

@app.route("/data/player/<int:player_id>/shots/<season:season>/<season_type:season_type>.json")
def api_shot_chart(player_id: int, season, season_type):
    """Every field-goal attempt; an empty list when the player took none."""
    try:
        df = get_shot_chart(player_id, season, season_type)
    except Exception as e:
        return error_response(e, "Unable to fetch shot data from NBA API.")

    if df is None or df.empty:
        return jsonify({"success": True, "shots": [], "count": 0})

    columns = [
        "LOC_X",
        "LOC_Y",
        "SHOT_MADE_FLAG",
        "PERIOD",
        "SHOT_TYPE",
        "SHOT_DISTANCE",
        "SHOT_ZONE_BASIC",
        "SHOT_ZONE_AREA",
        "ACTION_TYPE",
        "GAME_DATE",
    ]
    shots = df[[c for c in columns if c in df.columns]].to_dict("records")
    return jsonify({"success": True, "shots": shots, "count": len(shots)})

@app.route("/player/<int:player_id>")
def player_detail(player_id: int):
    return render_template("player_detail.html", player_id=player_id, seasons=get_seasons())

@app.route("/data/players/<season:season>/<season_type:season_type>.csv")
def export_players(season, season_type):
    """League player stats as a CSV download, highest scorers first."""
    try:
        df = get_league_player_stats(season, season_type=season_type).sort_values(by="PTS", ascending=False)

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
            download_name=f"nba_players_{season}.csv",
        )
    except Exception as e:
        return error_response(e, "Unable to export player stats.", 500)

def normalize_name(text: str) -> str:
    """Lowercase, strip accents and punctuation: "P.J. Dončić-Smith" -> "pj doncic smith"."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = re.sub(r"[.'’]", "", text)          # "P.J." -> "pj", "D'Angelo" -> "dangelo"
    text = re.sub(r"[^a-z0-9]+", " ", text)     # hyphens and other separators -> space
    return text.strip()

@lru_cache(maxsize=1)
def searchable_players():
    """(player, normalized full name) pairs from nba_api's static player list."""
    return [(p, normalize_name(p["full_name"])) for p in players.get_players()]

@app.route("/data/player-index.json")
def api_player_index():
    """
    Players to search, as {id, name, is_active, key} with key the normalize_name() form
    pages match against. In offline mode (and builds), only players whose pages are cached.
    """
    published = published_player_ids()
    return jsonify({
        "success": True,
        "players": [
            {"id": p["id"], "name": p["full_name"], "is_active": p["is_active"], "key": key}
            for p, key in searchable_players()
            if published is None or p["id"] in published
        ],
    })

@app.errorhandler(404)
def not_found(e):
    return render_template("not_found.html"), 404

# ------------------------------------------------------------------------------
# Static site: build.py saves every page and data file the cache can fill, and
# deploys the result to Cloudflare (Workers static assets; see wrangler.jsonc).
# ------------------------------------------------------------------------------
@app.cli.command("build")
@click.option("--out", default=None, help="Output folder (default: dist/).")
def build_command(out):
    """Build the static site from the data already in the cache (never calls the NBA API)."""
    import build

    summary = build.build_site(out or build.DIST_DIR)
    click.echo(f"Built {summary['files']} files in {summary['seconds']:.0f}s -> {summary['out']}")
    click.echo(f"  {len(summary['skipped'])} URLs had no data (e.g. {', '.join(summary['skipped'][:3]) or '-'})")
    for url, problem in summary["invalid"].items():
        click.echo(f"  left out, invalid JSON: {url}: {problem}", err=True)

@app.cli.command("preview")
@click.option("--port", default=8080, help="Port to serve on.")
def preview_command(port):
    """Serve the built site (dist/) locally, the way Cloudflare will."""
    import build

    build.preview(port=port)

@app.cli.command("update")
@click.option("--season", default=None, help="Season like 2025-26 (default: the current season).")
@click.option("--players", is_flag=True, help="Also fetch every active player's pages (nightly; ~4 requests per player).")
@click.option("--live", is_flag=True, help="Only refresh today's scoreboard (cheap; every ~10 minutes).")
@click.option("--no-deploy", is_flag=True, help="Build dist/ but don't deploy it.")
def update_command(season, players, live, no_deploy):
    """Fetch NBA data, rebuild the static site, and deploy it if anything changed."""
    import build

    started = time.time()
    last_report = [0.0]

    def progress(done, total, name):
        if time.time() - last_report[0] > 15 or done == total:
            last_report[0] = time.time()
            click.echo(f"  fetched {done}/{total}")

    result = warm_cache(season, include_players=players, live_only=live, progress=progress)
    click.echo(f"Fetched {len(result['ok'])} datasets for {result['season']} ({len(result['failed'])} failed, "
               f"{result['unchanged']} players unchanged) in {time.time() - started:.0f}s")
    for name, error in list(result["failed"].items())[:10]:
        click.echo(f"  failed: {name}: {error}", err=True)

    click.echo(build.build_and_deploy(deploy_site=not no_deploy))

@app.cli.command("warm-cache")
@click.option("--season", default=None, help="Season like 2025-26 (default: the current season).")
@click.option("--skip-teams", is_flag=True, help="Skip the per-team game logs and rosters.")
def warm_cache_command(season, skip_teams):
    """Pre-fetch NBA data into the cache so first page loads are fast."""
    started = time.time()
    result = warm_cache(season, include_teams=not skip_teams)
    click.echo(f"Warmed {len(result['ok'])} datasets for {result['season']} in {time.time() - started:.0f}s")
    for name, error in result["failed"].items():
        click.echo(f"  failed: {name}: {error}", err=True)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1", port=port)
