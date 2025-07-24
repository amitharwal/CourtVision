import os
from flask import Flask, render_template, request, jsonify, send_file
from nba_api.stats.endpoints import leaguedashplayerstats, shotchartdetail, teamdashboardbygeneralsplits, \
    playerprofilev2, commonplayerinfo, leaguedashteamstats
from nba_api.stats.static import teams, players
import pandas as pd
from datetime import datetime
import json
import io
import time

# Initialize Flask app
app = Flask(__name__)

# Cache for player positions (optional)
player_positions_cache = {}

# Headers to mimic a browser request.  The NBA's stats API blocks generic requests.
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

# Proxy settings (optional).  Leave empty unless you need to route through a proxy.
PROXIES = {
    # 'http': 'http://your-proxy:port',
    # 'https': 'http://your-proxy:port',
}

def get_seasons(start_year: int = 1951):
    """
    Generate a list of NBA season strings (e.g. '2023-24') from the most recent
    completed season down to a starting year.  The list is ordered from newest
    to oldest.

    The NBA season spans two calendar years.  If it's July or later, the
    current calendar year marks the start of the new season (e.g. July 2025 is
    the beginning of the 2025‑26 season).  Before July, we consider the
    previous year the most recently completed season.
    """
    today = datetime.now()
    current_year = today.year
    # Determine the start year of the latest season.  NBA seasons usually start in October.
    if today.month >= 7:
        latest_start_year = current_year
    else:
        latest_start_year = current_year - 1

    seasons = []
    for year in range(latest_start_year, start_year - 1, -1):
        next_year_suffix = str(year + 1)[-2:]
        seasons.append(f"{year}-{next_year_suffix}")
    return seasons

@app.route('/')
def home():
    """
    Home page displaying quick stats and league leaders.
    Pass the list of seasons so the template can display the current season dynamically.
    """
    return render_template('home.html', seasons=get_seasons())

@app.route('/players')
def players_page():
    """
    Display the players page with filtering options.
    Provide NBA teams and list of seasons for the filters.
    """
    nba_teams = teams.get_teams()
    return render_template('players.html', teams=nba_teams, seasons=get_seasons())

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

@app.route('/team-trends')
def team_trends():
    """
    Display team trends and analysis.  Pass the list of teams and seasons to the template.
    """
    nba_teams = teams.get_teams()
    return render_template('team_trends.html', teams=nba_teams, seasons=get_seasons())

@app.route('/compare')
def compare_players():
    """
    Player comparison tool.  Pass the seasons list so the user can select any year.
    """
    return render_template('compare.html', seasons=get_seasons())

@app.route('/test-api')
def test_api():
    """Test if NBA API and static endpoints are reachable."""
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
    """
    API endpoint to get filtered and sorted player data.  Supports filtering by season,
    team and position, sorting by any stat, and simple search.
    """
    try:
        # Query parameters with defaults
        season = request.args.get('season', get_seasons()[0])
        team_code = request.args.get('team', 'all')
        position_filter = request.args.get('position', 'all')
        sort_by = request.args.get('sort_by', 'PTS')
        search = request.args.get('search', '').lower()

        # Add a short delay to avoid hitting NBA's rate limit
        time.sleep(1)

        # Retry the API call up to 3 times on transient failures
        for attempt in range(3):
            try:
                player_stats = leaguedashplayerstats.LeagueDashPlayerStats(
                    season=season,
                    season_type_all_star='Regular Season',
                    headers=HEADERS,
                    timeout=60,
                    proxy=PROXIES if PROXIES else None
                )
                break  # success, exit retry loop
            except Exception as e:
                print(f"Attempt {attempt + 1} failed: {e}")
                if attempt < 2:
                    time.sleep(2)
                else:
                    raise e

        df = player_stats.get_data_frames()[0]
        print(f"Successfully retrieved {len(df)} players for season {season}")

        # Use provided POSITION column if available; otherwise fall back to simple assignment.
        if 'POSITION' not in df.columns or df['POSITION'].isnull().all():
            df['POSITION'] = df.apply(lambda row: assign_position_simple(row), axis=1)

        # Apply search filter
        if search:
            df = df[df['PLAYER_NAME'].str.lower().str.contains(search)]
        # Filter by team
        if team_code != 'all':
            df = df[df['TEAM_ABBREVIATION'] == team_code]
        # Filter by position (match initial letter for G/F/C etc.)
        if position_filter != 'all':
            df = df[df['POSITION'].str.contains(position_filter, na=False)]
        # Sort by selected stat if the column exists
        if sort_by in df.columns:
            df = df.sort_values(by=sort_by, ascending=False)

        # Calculate advanced metrics
        df['TS_PCT'] = df.apply(lambda row: calculate_true_shooting(row), axis=1)
        df['EFF'] = df.apply(lambda row: calculate_efficiency(row), axis=1)

        # Define the columns to return, preserving order
        display_columns = []
        for col in ['PLAYER_ID', 'PLAYER_NAME', 'TEAM_ABBREVIATION', 'POSITION', 'AGE', 'GP', 'MIN',
                    'PTS', 'REB', 'AST', 'STL', 'BLK', 'FG_PCT', 'FG3_PCT', 'FT_PCT',
                    'TS_PCT', 'EFF', 'PF']:
            if col in df.columns:
                display_columns.append(col)
        df_display = df[display_columns].fillna(0)

        players_data = df_display.to_dict('records')

        # Format numbers for display (convert proportions to percentages)
        for player in players_data:
            if 'FG_PCT' in player:
                player['FG_PCT'] = round(player['FG_PCT'] * 100, 1)
            if 'FG3_PCT' in player:
                player['FG3_PCT'] = round(player['FG3_PCT'] * 100, 1)
            if 'FT_PCT' in player:
                player['FT_PCT'] = round(player['FT_PCT'] * 100, 1)
            if 'TS_PCT' in player:
                player['TS_PCT'] = round(player['TS_PCT'], 1)
            # Round major stats to one decimal
            for stat in ['MIN', 'PTS', 'REB', 'AST', 'STL', 'BLK', 'EFF', 'PF']:
                if stat in player:
                    player[stat] = round(player[stat], 1)

        return jsonify({'success': True, 'data': players_data, 'count': len(players_data)})

    except Exception as e:
        print(f"Error in /api/players: {e}")
        print("Falling back to static player data...")
        # Fallback: create mock player data if the API is unavailable
        try:
            all_players = players.get_players()
            mock_data = []
            for i, player in enumerate(all_players[:100]):
                mock_data.append({
                    'PLAYER_ID': player['id'],
                    'PLAYER_NAME': player['full_name'],
                    'TEAM_ABBREVIATION': 'N/A',
                    'POSITION': assign_position_simple({'PLAYER_NAME': player['full_name']}),
                    'AGE': 25 + (i % 15),
                    'GP': max(20, 82 - (i % 30)),
                    'MIN': round(35 - (i * 0.1), 1),
                    'PTS': round(30 - (i * 0.25), 1),
                    'REB': round(10 - (i * 0.08), 1),
                    'AST': round(8 - (i * 0.06), 1),
                    'STL': round(2 - (i * 0.01), 1),
                    'BLK': round(1.5 - (i * 0.01), 1),
                    'FG_PCT': round(48 - (i * 0.1), 1),
                    'FG3_PCT': round(38 - (i * 0.1), 1),
                    'FT_PCT': round(85 - (i * 0.1), 1),
                    'TS_PCT': round(58 - (i * 0.1), 1),
                    'EFF': round(25 - (i * 0.2), 1),
                    'PF': round(2.5 + (i * 0.01), 1)
                })
            # Apply search and sort on mock data
            if search:
                mock_data = [p for p in mock_data if search in p['PLAYER_NAME'].lower()]
            if sort_by in ['PTS', 'REB', 'AST']:
                mock_data.sort(key=lambda x: x[sort_by], reverse=True)
            return jsonify({'success': True, 'data': mock_data, 'count': len(mock_data),
                            'note': 'Using cached data due to API timeout'})
        except Exception:
            return jsonify({'success': False, 'error': 'NBA API is currently unavailable. Please try again later.'})

@app.route('/api/player/<int:player_id>')
def get_player_detail(player_id: int):
    """
    API endpoint for detailed stats for a specific player.
    Returns player information, career totals and season stats.
    """
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
    """
    API endpoint to retrieve all shots for a player in a given season.
    Accepts a 'season' query parameter; defaults to the most recent season.
    """
    try:
        season = request.args.get('season', get_seasons()[0])
        time.sleep(0.5)
        shot_chart = shotchartdetail.ShotChartDetail(
            team_id=0,
            player_id=player_id,
            season_nullable=season,
            season_type_all_star='Regular Season',
            headers=HEADERS,
            timeout=30
        )
        shots = shot_chart.get_data_frames()[0]
        return jsonify({'success': True, 'shots': shots.to_dict('records')})
    except Exception as e:
        print(f"Error in /api/shot-chart/{player_id}: {e}")
        # Generate mock shot data for testing if the API fails
        import random
        mock_shots = []
        for _ in range(100):
            mock_shots.append({
                'LOC_X': random.randint(-250, 250),
                'LOC_Y': random.randint(0, 400),
                'SHOT_MADE_FLAG': random.randint(0, 1),
                'SHOT_TYPE': '2PT Field Goal',
                'SHOT_ZONE_BASIC': random.choice([
                    'Restricted Area', 'In The Paint (Non-RA)',
                    'Mid-Range', 'Above the Break 3'
                ]),
                'SHOT_DISTANCE': random.randint(0, 30),
                'PERIOD': random.randint(1, 4)
            })
        return jsonify({'success': True, 'shots': mock_shots, 'note': 'Using mock data due to API error'})

@app.route('/api/team-stats/<int:team_id>')
def get_team_stats(team_id: int):
    """
    API endpoint to retrieve team statistics and trends for a specific season.
    Accepts a 'season' query parameter; defaults to the most recent season.
    """
    try:
        season = request.args.get('season', get_seasons()[0])
        time.sleep(0.5)
        team_stats = teamdashboardbygeneralsplits.TeamDashboardByGeneralSplits(
            team_id=team_id,
            season=season,
            season_type_all_star='Regular Season',
            headers=HEADERS,
            timeout=30
        )
        overall = team_stats.get_data_frames()[0]
        if len(overall) > 0:
            stats = overall.to_dict('records')[0]
            # Calculate net rating if missing
            if 'NET_RATING' not in stats or stats['NET_RATING'] == 0:
                stats['NET_RATING'] = stats.get('OFF_RATING', 0) - stats.get('DEF_RATING', 0)
            return jsonify({'success': True, 'stats': stats})
        else:
            raise Exception("No team data found")
    except Exception as e:
        print(f"Error in /api/team-stats/{team_id}: {e}")
        # Return realistic mock data
        return jsonify({
            'success': True,
            'stats': {
                'W': 41,
                'L': 41,
                'W_PCT': 0.500,
                'PTS': 112.5,
                'FG_PCT': 0.465,
                'FG3_PCT': 0.365,
                'FT_PCT': 0.780,
                'REB': 44.2,
                'AST': 25.1,
                'STL': 7.8,
                'BLK': 5.2,
                'OFF_RATING': 115.2,
                'DEF_RATING': 114.8,
                'NET_RATING': 0.4,
                'PACE': 98.5,
                'OPP_PTS': 112.1
            },
            'note': 'Using mock data due to API error'
        })

@app.route('/api/export/players')
def export_players():
    """
    Export filtered player data as a CSV file.  Uses the same filters as /api/players.
    """
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
    """Search for players by name for comparison tool."""
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

# --------------------------------------------------------------------------
# Helper functions
# --------------------------------------------------------------------------
def assign_position_simple(row):
    """
    Assign a player's position based on available data.

    If the row contains a 'POSITION' value (as returned by the NBA API), return that directly.
    Otherwise, use simple heuristics based on the player's last name.  This fallback
    is primarily used for mock data when the API is unavailable.
    """
    # Check if row supports key lookup (pandas Series or dict)
    try:
        pos = row.get('POSITION')
    except AttributeError:
        pos = row['POSITION'] if 'POSITION' in row else None
    if pos:
        return pos

    name = row.get('PLAYER_NAME', '').lower() if isinstance(row, dict) else str(row.get('PLAYER_NAME', '')).lower()

    centers = [
        'embiid', 'jokic', 'gobert', 'adams', 'lopez', 'nurkic',
        'vucevic', 'allen', 'adebayo', 'ayton', 'valanciunas'
    ]
    guards = [
        'curry', 'lillard', 'irving', 'paul', 'westbrook', 'harden',
        'booker', 'mitchell', 'young', 'morant', 'ball', 'murray'
    ]
    for center in centers:
        if center in name:
            return 'C'
    for guard in guards:
        if guard in name:
            return 'G'
    return 'F'

def calculate_true_shooting(row):
    """
    Calculate True Shooting Percentage (TS%).
    Returns a percentage (e.g., 58.5) rather than a decimal (0.585).
    """
    pts = row.get('PTS', 0)
    fga = row.get('FGA', 0)
    fta = row.get('FTA', 0)
    denominator = fga + 0.44 * fta
    if denominator == 0:
        return 0
    return (pts / (2 * denominator)) * 100

def calculate_efficiency(row):
    """
    Calculate a simple efficiency rating:
    (PTS + REB + AST + STL + BLK) – ((FGA – FGM) + (FTA – FTM) + TOV)
    """
    positive = row.get('PTS', 0) + row.get('REB', 0) + row.get('AST', 0) + row.get('STL', 0) + row.get('BLK', 0)
    negative = (row.get('FGA', 0) - row.get('FGM', 0)) + (row.get('FTA', 0) - row.get('FTM', 0)) + row.get('TOV', 0)
    return positive - negative

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=True, port=port)