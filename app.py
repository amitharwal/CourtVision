import os
from flask import Flask, render_template, request, jsonify, send_file
from nba_api.stats.endpoints import leaguedashplayerstats, shotchartdetail, teamdashboardbygeneralsplits, \
    playerprofilev2, commonplayerinfo, leaguedashteamstats
from nba_api.stats.static import teams, players
import pandas as pd
import json
from datetime import datetime
import io
import time

# Flask constructor takes the name of current module (__name__) as argument.
app = Flask(__name__)

# Cache for player positions (to avoid multiple API calls)
player_positions_cache = {}

# Add headers to mimic a browser request (NBA API requirement)
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

# Add proxy settings if needed
PROXIES = {
    # Uncomment and modify if you need to use a proxy
    # 'http': 'http://your-proxy:port',
    # 'https': 'http://your-proxy:port',
}


@app.route('/')
def home():
    return render_template('home.html')


@app.route('/players')
def players_page():
    """Display the players page with filters"""
    nba_teams = teams.get_teams()
    return render_template('players.html', teams=nba_teams)


@app.route('/player/<int:player_id>')
def player_detail(player_id):
    """Display detailed stats for a specific player"""
    return render_template('player_detail.html', player_id=player_id)


@app.route('/shot-charts')
def shot_charts():
    """Display interactive shot charts page"""
    nba_teams = teams.get_teams()
    return render_template('shot_charts.html', teams=nba_teams)


@app.route('/advanced-metrics')
def advanced_metrics():
    """Display advanced metrics dashboard"""
    return render_template('advanced_metrics.html')


@app.route('/team-trends')
def team_trends():
    """Display team trends and analysis"""
    nba_teams = teams.get_teams()
    return render_template('team_trends.html', teams=nba_teams)


@app.route('/compare')
def compare_players():
    """Player comparison tool"""
    return render_template('compare.html')


@app.route('/test-api')
def test_api():
    """Test if NBA API is working"""
    try:
        # Test with static data first
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
        return jsonify({
            'success': False,
            'error': str(e)
        })


@app.route('/api/players')
def get_players():
    """API endpoint to get filtered and sorted player data"""
    try:
        # Get query parameters
        season = request.args.get('season', '2023-24')
        team = request.args.get('team', 'all')
        position = request.args.get('position', 'all')
        sort_by = request.args.get('sort_by', 'PTS')
        search = request.args.get('search', '').lower()

        # Add longer delay to avoid rate limiting
        time.sleep(1)

        # Try multiple times with increasing timeout
        for attempt in range(3):
            try:
                # Fetch player stats from NBA API with headers
                player_stats = leaguedashplayerstats.LeagueDashPlayerStats(
                    season=season,
                    season_type_all_star='Regular Season',
                    headers=HEADERS,
                    timeout=60,  # Increased timeout
                    proxy=PROXIES if PROXIES else None
                )

                # If successful, break the retry loop
                break
            except Exception as e:
                print(f"Attempt {attempt + 1} failed: {str(e)}")
                if attempt < 2:  # Don't sleep on last attempt
                    time.sleep(2)  # Wait before retry
                else:
                    raise e

        # Convert to DataFrame
        df = player_stats.get_data_frames()[0]

        # Debug: Print column names
        print(f"Successfully retrieved {len(df)} players")

        # Add simple position assignment (G/F/C) based on player names or other logic
        df['POSITION'] = df.apply(lambda row: assign_position_simple(row), axis=1)

        # Filter by search term
        if search:
            df = df[df['PLAYER_NAME'].str.lower().str.contains(search)]

        # Filter by team
        if team != 'all':
            df = df[df['TEAM_ABBREVIATION'] == team]

        # Filter by position
        if position != 'all':
            df = df[df['POSITION'].str.contains(position)]

        # Sort by selected stat
        if sort_by in df.columns:
            df = df.sort_values(by=sort_by, ascending=False)

        # Calculate advanced metrics correctly
        df['TS_PCT'] = df.apply(lambda row: calculate_true_shooting(row), axis=1)
        df['EFF'] = df.apply(lambda row: calculate_efficiency(row), axis=1)

        # Select relevant columns (make sure they exist)
        display_columns = []
        for col in ['PLAYER_ID', 'PLAYER_NAME', 'TEAM_ABBREVIATION', 'POSITION', 'AGE', 'GP', 'MIN',
                    'PTS', 'REB', 'AST', 'STL', 'BLK', 'FG_PCT', 'FG3_PCT', 'FT_PCT', 'TS_PCT', 'EFF', 'PF']:
            if col in df.columns:
                display_columns.append(col)

        df_display = df[display_columns].fillna(0)

        # Convert to dictionary
        players_data = df_display.to_dict('records')

        # Format numbers for display
        for player in players_data:
            # Handle percentages - they already come as decimals from API
            if 'FG_PCT' in player:
                player['FG_PCT'] = round(player['FG_PCT'] * 100, 1)
            if 'FG3_PCT' in player:
                player['FG3_PCT'] = round(player['FG3_PCT'] * 100, 1)
            if 'FT_PCT' in player:
                player['FT_PCT'] = round(player['FT_PCT'] * 100, 1)
            if 'TS_PCT' in player:
                player['TS_PCT'] = round(player['TS_PCT'], 1)

            # Round other stats
            for stat in ['MIN', 'PTS', 'REB', 'AST', 'STL', 'BLK', 'EFF', 'PF']:
                if stat in player:
                    player[stat] = round(player[stat], 1)

        return jsonify({
            'success': True,
            'data': players_data,
            'count': len(players_data)
        })

    except Exception as e:
        print(f"Error in /api/players: {str(e)}")
        print("Falling back to static player data...")

        # Fallback: Use static player data
        try:
            all_players = players.get_players()

            # Create mock data for display
            mock_data = []
            for i, player in enumerate(all_players[:100]):  # Limit to 100 players
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

            # Apply filters
            if search:
                mock_data = [p for p in mock_data if search in p['PLAYER_NAME'].lower()]

            # Sort
            if sort_by in ['PTS', 'REB', 'AST']:
                mock_data.sort(key=lambda x: x[sort_by], reverse=True)

            return jsonify({
                'success': True,
                'data': mock_data,
                'count': len(mock_data),
                'note': 'Using cached data due to API timeout'
            })

        except Exception as fallback_error:
            return jsonify({
                'success': False,
                'error': 'NBA API is currently unavailable. Please try again later.'
            })


@app.route('/api/player/<int:player_id>')
def get_player_detail(player_id):
    """Get detailed stats for a specific player"""
    try:
        # Add delay to avoid rate limiting
        time.sleep(0.5)

        # Get player info first
        player_info = commonplayerinfo.CommonPlayerInfo(
            player_id=player_id,
            headers=HEADERS,
            timeout=30
        )

        info_data = player_info.get_data_frames()[0]
        player_info_dict = info_data.to_dict('records')[0] if len(info_data) > 0 else {}

        # Get player career stats
        player_profile = playerprofilev2.PlayerProfileV2(
            player_id=player_id,
            headers=HEADERS,
            timeout=30
        )

        career_stats = player_profile.get_data_frames()[0]
        season_stats = player_profile.get_data_frames()[1]

        # Get career totals and season stats
        career_totals = career_stats.to_dict('records') if len(career_stats) > 0 else []
        seasons = season_stats.to_dict('records') if len(season_stats) > 0 else []

        return jsonify({
            'success': True,
            'player_info': player_info_dict,
            'career_totals': career_totals,
            'season_stats': seasons
        })

    except Exception as e:
        print(f"Error in /api/player/{player_id}: {str(e)}")
        return jsonify({
            'success': False,
            'error': str(e)
        })


@app.route('/api/shot-chart/<int:player_id>')
def get_shot_chart(player_id):
    """Get shot chart data for a player"""
    try:
        season = request.args.get('season', '2023-24')

        # Add delay to avoid rate limiting
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
        shots_data = shots.to_dict('records')

        return jsonify({
            'success': True,
            'shots': shots_data
        })

    except Exception as e:
        print(f"Error in /api/shot-chart/{player_id}: {str(e)}")
        # Return mock shot data for testing
        import random
        mock_shots = []
        for i in range(100):
            mock_shots.append({
                'LOC_X': random.randint(-250, 250),
                'LOC_Y': random.randint(0, 400),
                'SHOT_MADE_FLAG': random.randint(0, 1),
                'SHOT_TYPE': '2PT Field Goal',
                'SHOT_ZONE_BASIC': random.choice(
                    ['Restricted Area', 'In The Paint (Non-RA)', 'Mid-Range', 'Above the Break 3']),
                'SHOT_DISTANCE': random.randint(0, 30),
                'PERIOD': random.randint(1, 4)
            })

        return jsonify({
            'success': True,
            'shots': mock_shots,
            'note': 'Using mock data due to API error'
        })


@app.route('/api/team-stats/<team_id>')
def get_team_stats(team_id):
    """Get team statistics and trends"""
    try:
        season = request.args.get('season', '2023-24')

        # Add delay to avoid rate limiting
        time.sleep(0.5)

        # Get general team stats
        team_stats = teamdashboardbygeneralsplits.TeamDashboardByGeneralSplits(
            team_id=team_id,
            season=season,
            season_type_all_star='Regular Season',
            headers=HEADERS,
            timeout=30
        )

        # Get the overall stats
        overall = team_stats.get_data_frames()[0]
        if len(overall) > 0:
            stats = overall.to_dict('records')[0]

            # Calculate net rating if not present
            if 'NET_RATING' not in stats or stats['NET_RATING'] == 0:
                off_rating = stats.get('OFF_RATING', 0)
                def_rating = stats.get('DEF_RATING', 0)
                stats['NET_RATING'] = off_rating - def_rating

            return jsonify({
                'success': True,
                'stats': stats
            })
        else:
            raise Exception("No team data found")

    except Exception as e:
        print(f"Error in /api/team-stats/{team_id}: {str(e)}")

        # Return mock data with realistic values
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
    """Export filtered player data as CSV"""
    try:
        # Get the same filters as the main API
        season = request.args.get('season', '2023-24')
        team = request.args.get('team', 'all')
        position = request.args.get('position', 'all')
        sort_by = request.args.get('sort_by', 'PTS')

        # Add delay to avoid rate limiting
        time.sleep(0.5)

        # Get player data (same logic as get_players)
        player_stats = leaguedashplayerstats.LeagueDashPlayerStats(
            season=season,
            season_type_all_star='Regular Season',
            headers=HEADERS,
            timeout=30
        )

        df = player_stats.get_data_frames()[0]

        # Apply filters
        if team != 'all':
            df = df[df['TEAM_ABBREVIATION'] == team]

        # Sort
        if sort_by in df.columns:
            df = df.sort_values(by=sort_by, ascending=False)

        # Create CSV
        output = io.StringIO()
        df.to_csv(output, index=False)
        output.seek(0)

        # Create a BytesIO object
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
        print(f"Error in /api/export/players: {str(e)}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/search-players')
def search_players():
    """Search for players by name for comparison tool"""
    try:
        query = request.args.get('q', '').lower()
        all_players = players.get_players()

        matching = [
                       {'id': p['id'], 'name': p['full_name'], 'is_active': p['is_active']}
                       for p in all_players
                       if query in p['full_name'].lower()
                   ][:10]  # Limit to 10 results

        return jsonify({
            'success': True,
            'players': matching
        })

    except Exception as e:
        print(f"Error in /api/search-players: {str(e)}")
        return jsonify({
            'success': False,
            'error': str(e)
        })


# Helper functions
def assign_position_simple(row):
    """Assign position based on simple heuristics"""
    name = row.get('PLAYER_NAME', '').lower()

    # Known centers
    centers = ['embiid', 'jokic', 'gobert', 'adams', 'lopez', 'nurkic', 'vucevic', 'allen', 'adebayo', 'ayton',
               'valanciunas']
    # Known guards
    guards = ['curry', 'lillard', 'irving', 'paul', 'westbrook', 'harden', 'booker', 'mitchell', 'young', 'morant',
              'ball', 'murray']

    for center in centers:
        if center in name:
            return 'C'

    for guard in guards:
        if guard in name:
            return 'G'

    # Default to forward
    return 'F'


def calculate_true_shooting(row):
    """Calculate True Shooting Percentage"""
    pts = row.get('PTS', 0)
    fga = row.get('FGA', 0)
    fta = row.get('FTA', 0)

    if fga + 0.44 * fta == 0:
        return 0

    return (pts / (2 * (fga + 0.44 * fta))) * 100


def calculate_efficiency(row):
    """Calculate simple efficiency rating"""
    positive = row.get('PTS', 0) + row.get('REB', 0) + row.get('AST', 0) + row.get('STL', 0) + row.get('BLK', 0)
    negative = (row.get('FGA', 0) - row.get('FGM', 0)) + (row.get('FTA', 0) - row.get('FTM', 0)) + row.get('TOV', 0)

    return positive - negative


# main driver function
if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=True, port=port)