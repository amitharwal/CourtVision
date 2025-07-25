import os
from flask import Flask, render_template, request, jsonify, send_file
from nba_api.stats.endpoints import (leaguedashplayerstats, shotchartdetail, teamdashboardbygeneralsplits, \
                                     playerprofilev2, commonplayerinfo, ScoreboardV2, HomePageLeaders, TeamGameLog,
                                     CommonTeamRoster, PlayerDashboardByYearOverYear, TeamDashboardByGeneralSplits,
                                     TeamDashboardByShootingSplits, TeamPlayerDashboard, LeagueGameLog,
                                     leaguedashteamstats,
                                     teamdashboardbygeneralsplits, teamestimatedmetrics, CumeStatsTeam)
from nba_api.stats.static import teams, players
import pandas as pd
from datetime import datetime
import json
import io
import time

# Initialize Flask app
app = Flask(__name__)

# Headers to mimic a browser request.
HEADERS = {
    'Host': 'stats.nba.com',
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:72.0) Gecko/20100101 Firefox/72.0',
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'en-US,en;q=0.5',
    'Accept-Encoding': 'gzip, deflate, br',
    'x-nba-stats-origin': 'stats',
    'x-nba-stats-token': 'true',
    'Connection': 'keep-alive',
    'Referer': 'https://stats.nba.com/',
    'Pragma': 'no-cache',
    'Cache-Control': 'no-cache'
}

# Proxy settings (not needed)
PROXIES = {
    # 'http': 'http://your-proxy:port',
    # 'https': 'http://your-proxy:port',
}


def get_seasons(start_year: int = 1951):
    """
    The list is ordered from newest
    to oldest.

    The NBA season spans two calendar years.  If it's July or later, the
    current calendar year marks the start of the new season (e.g. July 2025 is
    the beginning of the 2025‑26 season).  Before July, we consider the
    previous year the most recently completed season.
    """
    today = datetime.now()
    current_year = today.year
    # Determine the start year of the latest season.  NBA seasons start in October, except lockout/covid seasons.
    if today.month >= 10:
        latest_start_year = current_year
    else:
        latest_start_year = current_year - 1

    seasons = []
    for year in range(latest_start_year, start_year - 1, -1):
        next_year_suffix = str(year + 1)[-2:]
        seasons.append(f"{year}-{next_year_suffix}")
    return seasons


def get_team_players(team_id, season):
    from nba_api.stats.endpoints import CommonTeamRoster, PlayerDashboardByYearOverYear

    roster = CommonTeamRoster(team_id=team_id, season=season).get_data_frames()[0]
    player_ids = roster['PLAYER_ID'].tolist()

    player_stats = []

    for pid in player_ids:
        try:
            dash = PlayerDashboardByYearOverYear(player_id=pid, season=season)
            stats = dash.get_normalized_dict()['SeasonTotalsRegularSeason'][0]

            player_stats.append({
                'player': stats.get('PLAYER_NAME'),
                'ppg': round(stats.get('PTS', 0), 1),
                'rpg': round(stats.get('REB', 0), 1),
                'apg': round(stats.get('AST', 0), 1),
                'gp': stats.get('GP', 0)
            })
        except Exception:
            continue

    player_stats.sort(key=lambda x: x['gp'], reverse=True)
    return player_stats[:5]


@app.route("/")
def home():
    today = datetime.today().strftime('%m/%d/%Y')
    games = []

    # try to fetch today's scoreboard, but recover if WinProbability is missing
    try:
        scoreboard = ScoreboardV2(game_date=today)
        raw = scoreboard.get_normalized_dict()
        games = raw.get('GameHeader', [])
    except KeyError as e:
        # e will be 'WinProbability'
        print(f"[WARN] ScoreboardV2 missing data-set: {e}. Continuing with no games_today.")
    except Exception as e:
        # anything else network‑y
        print(f"[ERROR] Could not fetch scoreboard: {e}")

    games_today = []
    for game in games:
        games_today.append({
            'game_id': game['GAME_ID'],
            'matchup': f"{game['VISITOR_TEAM_ABBREVIATION']} @ {game['HOME_TEAM_ABBREVIATION']}",
            'game_time': game['GAME_TIME'],
            'arena': game['ARENA_NAME']
        })

    return render_template(
        "home.html",
        games_today=games_today,
        games_count=len(games_today),
        seasons=get_seasons()
    )



@app.route('/players')
def players_page():
    """
    Display the players page with filtering options.
    Provide NBA teams and list of seasons for the filters.
    """
    nba_teams = teams.get_teams()
    return render_template('players.html', teams=nba_teams, seasons=get_seasons())


@app.route("/api/leaders")
def get_homepage_leaders():
    from flask import request, jsonify

    stat = request.args.get("stat", "Points")
    season = request.args.get("season", "2024-25")

    # Map short stat keys (PTS, REB, etc.) to full stat_category names
    stat_category_map = {
        "PTS": "Points",
        "REB": "Rebounds",
        "AST": "Assists",
        "STL": "Defense",
        "BLK": "Defense"
    }

    try:
        homepage = HomePageLeaders(
            game_scope_detailed="Season",
            league_id="00",
            player_or_team="Player",
            player_scope="All Players",
            season=season,
            season_type_playoffs="Regular Season",
            stat_category=stat_category_map.get(stat, "Points")
        )
        df = homepage.get_data_frames()[0]
        return jsonify({
            "success": True,
            "data": df.to_dict(orient="records"),
            "count": len(df)
        })
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route('/player/<int:player_id>')
def player_detail(player_id: int):
    """
    Display detailed stats for a specific player.  The template receives the player_id
    and the seasons list for proper default values.
    """
    return render_template('player_detail.html', player_id=player_id, seasons=get_seasons())


@app.route('/shot-charts')
def shot_charts():
    """
    Interactive shot charts page.  Provide the list of teams and seasons
    so the user can pick any team or year without hard‑coded options.
    """
    nba_teams = teams.get_teams()
    return render_template('shot_charts.html', teams=nba_teams, seasons=get_seasons())


@app.route('/advanced-metrics')
def advanced_metrics():
    """Display advanced metrics dashboard."""
    return render_template('advanced_metrics.html', seasons=get_seasons())


@app.route("/team-trends")
def team_trends():
    team_list = teams.get_teams()
    seasons = get_seasons()
    return render_template("team_trends.html", teams=team_list, seasons=seasons)


@app.route('/api/team-monthly')
def api_team_monthly():
    team_id = request.args.get("team_id")
    season = request.args.get("season")
    month = int(request.args.get("month"))

    from nba_api.stats.endpoints import teamgamelog

    gamelog = teamgamelog.TeamGameLog(team_id=team_id, season=season).get_data_frames()[0]
    wins = 0
    losses = 0

    for _, row in gamelog.iterrows():
        date = pd.to_datetime(row['GAME_DATE'])
        if date.month == month:
            if row['WL'] == 'W':
                wins += 1
            elif row['WL'] == 'L':
                losses += 1

    return jsonify({
        "success": True,
        "wins": wins,
        "losses": losses
    })


@app.route('/api/team-stats/<team_id>')
def api_team_stats(team_id):
    season = request.args.get("season", "2023-24")

    stats_df = leaguedashteamstats.LeagueDashTeamStats(season=season).get_data_frames()[0]
    stats_row = stats_df[stats_df['TEAM_ID'] == int(team_id)].iloc[0]

    gp = stats_row['GP'] or 1
    ppg = stats_row['PTS'] / gp
    rpg = stats_row['REB'] / gp
    apg = stats_row['AST'] / gp
    spg = stats_row['STL'] / gp
    bpg = stats_row['BLK'] / gp

    opp_total = stats_row['PTS'] - stats_row['PLUS_MINUS']
    opp_ppg = opp_total / gp

    try:
        em_df = teamestimatedmetrics.TeamEstimatedMetrics(
            season=season,
            season_type='Regular Season'
        ).get_data_frames()[0]
        em = em_df[em_df['TEAM_ID'] == int(team_id)].iloc[0]
        off_rating = em['E_OFF_RATING']
        def_rating = em['E_DEF_RATING']
        net_rating = em['E_NET_RATING']
        pace = em['E_PACE']
    except Exception:
        off_rating = def_rating = net_rating = pace = 0

    splits = teamdashboardbygeneralsplits.TeamDashboardByGeneralSplits(
        team_id=team_id, season=season
    ).get_data_frames()
    by_loc = splits[1]
    home_row = by_loc[by_loc['GROUP_VALUE'] == 'Home'].iloc[0]
    road_row = by_loc[by_loc['GROUP_VALUE'] == 'Road'].iloc[0]
    home_record = f"{int(home_row['W'])}-{int(home_row['L'])}"
    road_record = f"{int(road_row['W'])}-{int(road_row['L'])}"

    stats_dict = {
        'W': int(stats_row['W']),
        'L': int(stats_row['L']),
        'W_PCT': round(stats_row['W_PCT'], 3),

        'PPG': round(ppg, 1),
        'RPG': round(rpg, 1),
        'APG': round(apg, 1),
        'SPG': round(spg, 1),
        'BPG': round(bpg, 1),

        'FG_PCT': round(stats_row['FG_PCT'] * 100, 1),
        'FG3_PCT': round(stats_row['FG3_PCT'] * 100, 1),
        'FT_PCT': round(stats_row['FT_PCT'] * 100, 1),

        'OFF_RATING': round(off_rating, 1),
        'DEF_RATING': round(def_rating, 1),
        'NET_RATING': round(net_rating, 1),
        'PACE': round(pace, 1),

        'OPP_PPG': round(opp_ppg, 1),

        'HOME_RECORD': home_record,
        'ROAD_RECORD': road_record
    }

    return jsonify({"success": True, "stats": stats_dict})


def calculate_efficiency(row):
    positive = row.get('PTS', 0) + row.get('REB', 0) + row.get('AST', 0) + row.get('STL', 0) + row.get('BLK', 0)
    negative = (row.get('FGA', 0) - row.get('FGM', 0)) + (row.get('FTA', 0) - row.get('FTM', 0)) + row.get('TOV', 0)
    return positive - negative


@app.route('/api/roster-analysis/<team_id>')
def api_roster_analysis(team_id):
    season = request.args.get("season", "2023-24")
    from nba_api.stats.endpoints import TeamPlayerDashboard

    dash = TeamPlayerDashboard(team_id=team_id, season=season)
    stats = dash.get_data_frames()[1]  # 'OverallTeamPlayerDashboard'

    stats['EFF'] = stats.apply(calculate_efficiency, axis=1)

    def build_player_dict(row, stat_col):
        if row is None:
            return {"name": "N/A", "stat": 0}
        gp = row.get('GP') or 1
        total = row.get(stat_col, 0)
        per_game = total / gp
        return {
            "name": row['PLAYER_NAME'],
            "stat": round(per_game, 1)
        }

    top_scorer = stats.sort_values('PTS', ascending=False).iloc[0] if not stats.empty else None
    top_rebounder = stats.sort_values('REB', ascending=False).iloc[0] if not stats.empty else None
    top_playmaker = stats.sort_values('AST', ascending=False).iloc[0] if not stats.empty else None
    most_efficient = stats.sort_values('EFF', ascending=False).iloc[0] if not stats.empty else None

    return jsonify({
        "success": True,
        "top_scorer": build_player_dict(top_scorer, 'PTS'),
        "top_rebounder": build_player_dict(top_rebounder, 'REB'),
        "top_playmaker": build_player_dict(top_playmaker, 'AST'),
        "most_efficient": build_player_dict(most_efficient, 'EFF'),
    })


@app.route('/api/team_trends_data')
def team_trends_data():
    team_id = request.args.get('team_id')

    # Basic Stats
    general_data = TeamDashboardByGeneralSplits(team_id=team_id).get_normalized_dict()
    overall_stats = general_data['OverallTeamDashboard'][0]

    # Shooting/Advanced Stats
    shooting_data = TeamDashboardByShootingSplits(team_id=team_id).get_normalized_dict()
    shooting_stats = shooting_data['OverallTeamDashboard'][0]

    # Game Log for Monthly Chart
    game_logs = TeamGameLog(team_id=team_id, season='2023-24').get_data_frames()[0]
    game_logs['GAME_DATE'] = pd.to_datetime(game_logs['GAME_DATE'])
    game_logs['MONTH'] = game_logs['GAME_DATE'].dt.month_name()
    monthly_avg = game_logs.groupby('MONTH')['PTS'].mean().reindex([
        'October', 'November', 'December', 'January', 'February', 'March', 'April'
    ]).fillna(0).to_dict()

    # Home vs Away split
    home_games = game_logs[game_logs['MATCHUP'].str.contains('vs')]
    away_games = game_logs[game_logs['MATCHUP'].str.contains('@')]
    home_avg_pts = round(home_games['PTS'].mean(), 1)
    away_avg_pts = round(away_games['PTS'].mean(), 1)

    # Conference split
    east = ['ATL', 'BOS', 'BKN', 'CHA', 'CHI', 'CLE', 'DET', 'IND', 'MIA', 'MIL', 'NYK', 'ORL', 'PHI', 'TOR', 'WAS']
    game_logs['OPP_CONF'] = game_logs['MATCHUP'].str.extract(r'([A-Z]{3})$')[0].apply(
        lambda x: 'East' if x in east else 'West')
    conf_avg = game_logs.groupby('OPP_CONF')['PTS'].mean().to_dict()

    # Roster impact
    player_data = TeamPlayerDashboard(team_id=team_id).get_normalized_dict()
    roster_stats = sorted(player_data['TeamPlayerDashboard'], key=lambda x: x['GP'], reverse=True)[:5]

    return jsonify({
        'basic': {
            'wins': overall_stats['W'],
            'losses': overall_stats['L'],
            'ppg': round(overall_stats['PTS'], 1),
            'apg': round(overall_stats['AST'], 1),
            'rpg': round(overall_stats['REB'], 1),
            'spg': round(overall_stats['STL'], 1),
            'bpg': round(overall_stats['BLK'], 1),
            'opp_ppg': round(overall_stats['OPP_PTS'], 1),
        },
        'efficiency': {
            'off_rating': round(overall_stats['OFF_RATING'], 1),
            'def_rating': round(overall_stats['DEF_RATING'], 1),
            'net_rating': round(overall_stats['NET_RATING'], 1),
            'pace': round(overall_stats['PACE'], 1),
        },
        'rankings': {
            'pts_rank': overall_stats['PTS_RANK'],
            'reb_rank': overall_stats['REB_RANK'],
            'ast_rank': overall_stats['AST_RANK'],
            'opp_pts_rank': overall_stats['OPP_PTS_RANK'],
        },
        'trends': {
            'monthly': monthly_avg,
            'home_avg': home_avg_pts,
            'away_avg': away_avg_pts,
            'east_avg': round(conf_avg.get('East', 0), 1),
            'west_avg': round(conf_avg.get('West', 0), 1),
        },
        'roster': [
            {
                'player': p['PLAYER_NAME'],
                'ppg': round(p['PTS'], 1),
                'rpg': round(p['REB'], 1),
                'apg': round(p['AST'], 1)
            } for p in roster_stats
        ]
    })


@app.route('/compare')
def compare_players():
    return render_template('compare.html', seasons=get_seasons())


@app.route('/test-api')
def test_api():
    try:
        all_teams = teams.get_teams()
        all_players = players.get_players()
        return jsonify({
            'success': True,
            'teams_count': len(all_teams),
            'players_count': len(all_players),
            'sample_team': all_teams[0] if all_teams else None,
            'sample_player': all_players[0] if all_players else None
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/players')
def get_players():
    try:
        season = request.args.get('season', get_seasons()[0])
        team_code = request.args.get('team', 'all')
        position_filter = request.args.get('position', 'all')
        sort_by = request.args.get('sort_by', 'PTS')
        search = request.args.get('search', '').lower()

        time.sleep(1)
        for attempt in range(3):
            try:
                stats = leaguedashplayerstats.LeagueDashPlayerStats(
                    season=season,
                    season_type_all_star='Regular Season',
                    headers=HEADERS,
                    timeout=60,
                    proxy=PROXIES or None
                )
                break
            except Exception as e:
                print(f"LeagueDashPlayerStats attempt {attempt + 1} failed:", e)
                if attempt == 2:
                    raise

        df = stats.get_data_frames()[0]

        if search:
            df = df[df['PLAYER_NAME'].str.lower().str.contains(search)]
        if team_code != 'all':
            df = df[df['TEAM_ABBREVIATION'] == team_code]
        if position_filter != 'all':
            df = df[df['POSITION'].str.contains(position_filter, na=False)]

        df['TS_PCT'] = df.apply(lambda r: calculate_true_shooting(r), axis=1)
        df['EFF'] = df.apply(
            lambda r: calculate_efficiency(r) / (r['GP'] or 1),
            axis=1
        )

        tot_mp = df['MIN'].sum()
        tot_fga = df['FGA'].sum()
        tot_fta = df['FTA'].sum()
        tot_tov = df['TOV'].sum()
        tot_fgm = df['FGM'].sum()
        tot_reb = df['REB'].sum()
        tot_oreb = df['OREB'].sum()
        tot_dreb = df['DREB'].sum()

        df['USG_PCT'] = (
                100
                * ((df['FGA'] + 0.44 * df['FTA'] + df['TOV']) * (tot_mp / 5))
                / (df['MIN'] * (tot_fga + 0.44 * tot_fta + tot_tov))
        ).fillna(0)

        df['AST_PCT'] = (
                100
                * df['AST']
                / (
                        ((df['MIN'] / (tot_mp / 5)) * tot_fgm)
                        - df['FGM']
                )
        ).fillna(0)

        df['REB_PCT'] = (
                100
                * (df['REB'] * (tot_mp / 5))
                / (df['MIN'] * (tot_reb + tot_reb))
        ).fillna(0)

        num = (
                df['PTS']
                + df['FGM']
                + df['FTM']
                - df['FGA']
                - df['FTA']
                + df['DREB']
                + 0.5 * df['OREB']
                + df['AST']
                + df['STL']
                + 0.5 * df['BLK']
                - df['PF']
                - df['TOV']
        )
        total_num = num.sum()
        df['PIE'] = (num / total_num * 100).fillna(0)

        display_columns = [
            'PLAYER_ID', 'PLAYER_NAME', 'TEAM_ABBREVIATION', 'POSITION', 'AGE', 'GP', 'MIN',
            'PTS', 'REB', 'AST', 'STL', 'BLK',
            'FG_PCT', 'FG3_PCT', 'FT_PCT',
            'TS_PCT', 'EFF',
            'USG_PCT', 'AST_PCT', 'REB_PCT', 'PIE',
            'PF'
        ]
        display_columns = [c for c in display_columns if c in df.columns]
        df_display = df[display_columns].fillna(0)

        if sort_by in df_display.columns:
            df_display = df_display.sort_values(by=sort_by, ascending=False)

        players_data = df_display.to_dict('records')
        for p in players_data:
            if 'FG_PCT' in p: p['FG_PCT'] = round(p['FG_PCT'] * 100, 1)
            if 'FG3_PCT' in p: p['FG3_PCT'] = round(p['FG3_PCT'] * 100, 1)
            if 'FT_PCT' in p: p['FT_PCT'] = round(p['FT_PCT'] * 100, 1)
            if 'TS_PCT' in p: p['TS_PCT'] = round(p['TS_PCT'], 1)
            if 'EFF' in p: p['EFF'] = round(p['EFF'], 1)
            if 'USG_PCT' in p: p['USG_PCT'] = round(p['USG_PCT'], 1)
            if 'AST_PCT' in p: p['AST_PCT'] = round(p['AST_PCT'], 1)
            if 'REB_PCT' in p: p['REB_PCT'] = round(p['REB_PCT'], 1)
            if 'PIE' in p: p['PIE'] = round(p['PIE'], 1)
            for stat in ['MIN', 'PTS', 'REB', 'AST', 'STL', 'BLK', 'PF']:
                if stat in p: p[stat] = round(p[stat], 1)

        return jsonify({'success': True, 'data': players_data, 'count': len(players_data)})

    except Exception as e:
        print("Error in /api/players:", e)
        return jsonify({
            'success': False,
            'error': 'Unable to fetch player data from NBA API. Please try again later.'
        }), 503


@app.route('/api/player/<int:player_id>')
def get_player_detail(player_id: int):
    try:
        time.sleep(0.5)
        player_info = commonplayerinfo.CommonPlayerInfo(
            player_id=player_id,
            headers=HEADERS,
            timeout=30
        )
        info_data = player_info.get_data_frames()[0]
        player_info_dict = info_data.to_dict('records')[0] if len(info_data) > 0 else {}

        player_profile = playerprofilev2.PlayerProfileV2(
            player_id=player_id,
            headers=HEADERS,
            timeout=30
        )
        career_stats = player_profile.get_data_frames()[0]
        season_stats = player_profile.get_data_frames()[1]

        return jsonify({
            'success': True,
            'player_info': player_info_dict,
            'career_totals': career_stats.to_dict('records') if len(career_stats) > 0 else [],
            'season_stats': season_stats.to_dict('records') if len(season_stats) > 0 else []
        })
    except Exception as e:
        print(f"Error in /api/player/{player_id}: {e}")
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/shot-chart/<int:player_id>')
def get_shot_chart(player_id: int):
    season = request.args.get('season', get_seasons()[0])
    time.sleep(0.5)

    try:
        team_id = 0

        try:
            profile = playerprofilev2.PlayerProfileV2(
                player_id=player_id,
                headers=HEADERS,
                timeout=30
            )
            seasonal_df = profile.get_data_frames()[1]  # Season stats

            if not seasonal_df.empty and 'SEASON_ID' in seasonal_df.columns:
                match = seasonal_df[seasonal_df['SEASON_ID'] == season]
                if not match.empty:
                    team_id = int(match.iloc[0]['TEAM_ID'])
                else:
                    print(f"[WARN] No season match for player {player_id} in {season}")
            else:
                print(f"[WARN] SEASON_ID missing or empty for player {player_id}")
        except Exception as e:
            print(f"[ERROR] Failed to retrieve team_id: {e}")

        shot_chart = shotchartdetail.ShotChartDetail(
            team_id=team_id,
            player_id=player_id,
            season_nullable=season,
            season_type_all_star='Regular Season',
            context_measure_simple='FGA',
            last_n_games=0,
            league_id='00',
            month=0,
            opponent_team_id=0,
            period=0,
            rookie_year_nullable='',
            season_segment_nullable='',
            player_position_nullable='',
            location_nullable='',
            game_segment_nullable='',
            date_from_nullable='',
            date_to_nullable='',
            outcome_nullable='',
            vs_conference_nullable='',
            vs_division_nullable=''
        )

        shots = shot_chart.get_data_frames()[0]
        print("Total shots fetched:", len(shots))
        print("Missed shots:", len(shots[shots['SHOT_MADE_FLAG'] == 0]))
        print("Made shots:", len(shots[shots['SHOT_MADE_FLAG'] == 1]))

        return jsonify({'success': True, 'shots': shots.to_dict('records')})
    except Exception as e:
        print(f"ShotChartDetail failed: {e}")
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/export/players')
def export_players():
    try:
        season = request.args.get('season', get_seasons()[0])
        team_code = request.args.get('team', 'all')
        position_filter = request.args.get('position', 'all')
        sort_by = request.args.get('sort_by', 'PTS')
        time.sleep(0.5)

        player_stats = leaguedashplayerstats.LeagueDashPlayerStats(
            season=season,
            season_type_all_star='Regular Season',
            headers=HEADERS,
            timeout=30
        )
        df = player_stats.get_data_frames()[0]
        if team_code != 'all':
            df = df[df['TEAM_ABBREVIATION'] == team_code]
        if sort_by in df.columns:
            df = df.sort_values(by=sort_by, ascending=False)

        output = io.StringIO()
        df.to_csv(output, index=False)
        output.seek(0)
        mem = io.BytesIO()
        mem.write(output.getvalue().encode('utf-8'))
        mem.seek(0)
        return send_file(
            mem,
            mimetype='text/csv',
            as_attachment=True,
            download_name=f'nba_players_{season}_{datetime.now().strftime("%Y%m%d")}.csv'
        )
    except Exception as e:
        print(f"Error in /api/export/players: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/search-players')
def search_players():
    try:
        query = request.args.get('q', '').lower()
        all_players = players.get_players()
        matching = [
                       {'id': p['id'], 'name': p['full_name'], 'is_active': p['is_active']}
                       for p in all_players
                       if query in p['full_name'].lower()
                   ][:10]
        return jsonify({'success': True, 'players': matching})
    except Exception as e:
        print(f"Error in /api/search-players: {e}")
        return jsonify({'success': False, 'error': str(e)})


def calculate_true_shooting(row):
    pts = row.get('PTS', 0)
    fga = row.get('FGA', 0)
    fta = row.get('FTA', 0)
    denominator = fga + 0.44 * fta
    if denominator == 0:
        return 0
    return (pts / (2 * denominator)) * 100


def calculate_efficiency(row):
    positive = row.get('PTS', 0) + row.get('REB', 0) + row.get('AST', 0) + row.get('STL', 0) + row.get('BLK', 0)
    negative = (row.get('FGA', 0) - row.get('FGM', 0)) + (row.get('FTA', 0) - row.get('FTM', 0)) + row.get('TOV', 0)
    return positive - negative


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    app.run(debug=True, port=port)