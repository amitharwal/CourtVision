import os
from flask import Flask, render_template, request, jsonify, send_file
from nba_api.stats.endpoints import leaguedashplayerstats, shotchartdetail, teamdashboardbygeneralsplits, \
    playerprofilev2
from nba_api.stats.static import teams, players
import pandas as pd
import json
from datetime import datetime
import io

# Flask constructor takes the name of current module (__name__) as argument.
app = Flask(__name__)

# Cache for player positions (to avoid multiple API calls)
player_positions_cache = {}


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

        # Fetch player stats from NBA API
        player_stats = leaguedashplayerstats.LeagueDashPlayerStats(
            season=season,
            season_type_all_star='Regular Season'
        )

        # Convert to DataFrame
        df = player_stats.get_data_frames()[0]

        # Add position data (simplified - in production, you'd get this from player info endpoint)
        # This is a basic position assignment based on player ID patterns
        df['POSITION'] = df.apply(lambda row: get_player_position(row['PLAYER_ID']), axis=1)

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

        # Calculate advanced metrics
        df['TS_PCT'] = df.apply(lambda row: calculate_true_shooting(row), axis=1)
        df['EFF'] = df.apply(lambda row: calculate_efficiency(row), axis=1)

        # Select relevant columns
        display_columns = [
            'PLAYER_ID', 'PLAYER_NAME', 'TEAM_ABBREVIATION', 'POSITION', 'AGE', 'GP', 'MIN',
            'PTS', 'REB', 'AST', 'STL', 'BLK', 'FG_PCT', 'FG3_PCT', 'FT_PCT', 'TS_PCT', 'EFF'
        ]

        df_display = df[display_columns].fillna(0)

        # Convert to dictionary
        players_data = df_display.to_dict('records')

        # Format numbers for display
        for player in players_data:
            player['FG_PCT'] = round(player['FG_PCT'] * 100, 1)
            player['FG3_PCT'] = round(player['FG3_PCT'] * 100, 1)
            player['FT_PCT'] = round(player['FT_PCT'] * 100, 1)
            player['TS_PCT'] = round(player['TS_PCT'], 1)
            player['MIN'] = round(player['MIN'], 1)
            player['PTS'] = round(player['PTS'], 1)
            player['REB'] = round(player['REB'], 1)
            player['AST'] = round(player['AST'], 1)
            player['EFF'] = round(player['EFF'], 1)

        return jsonify({
            'success': True,
            'data': players_data,
            'count': len(players_data)
        })

    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        })


@app.route('/api/player/<int:player_id>')
def get_player_detail(player_id):
    """Get detailed stats for a specific player"""
    try:
        season = request.args.get('season', '2023-24')

        # Get player career stats
        player_profile = playerprofilev2.PlayerProfileV2(
            player_id=player_id,
            per_mode36='PerGame'
        )

        career_stats = player_profile.get_data_frames()[0]
        season_stats = player_profile.get_data_frames()[1]

        # Get player info
        player_info = career_stats.to_dict('records')[0] if len(career_stats) > 0 else {}
        seasons = season_stats.to_dict('records') if len(season_stats) > 0 else []

        return jsonify({
            'success': True,
            'player_info': player_info,
            'season_stats': seasons
        })

    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        })


@app.route('/api/shot-chart/<int:player_id>')
def get_shot_chart(player_id):
    """Get shot chart data for a player"""
    try:
        season = request.args.get('season', '2023-24')

        shot_chart = shotchartdetail.ShotChartDetail(
            team_id=0,
            player_id=player_id,
            season_nullable=season,
            season_type_all_star='Regular Season'
        )

        shots = shot_chart.get_data_frames()[0]
        shots_data = shots.to_dict('records')

        return jsonify({
            'success': True,
            'shots': shots_data
        })

    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        })


@app.route('/api/team-stats/<team_id>')
def get_team_stats(team_id):
    """Get team statistics and trends"""
    try:
        season = request.args.get('season', '2023-24')

        team_stats = teamdashboardbygeneralsplits.TeamDashboardByGeneralSplits(
            team_id=team_id,
            season=season,
            season_type_all_star='Regular Season'
        )

        overall = team_stats.get_data_frames()[0].to_dict('records')[0]

        return jsonify({
            'success': True,
            'stats': overall
        })

    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
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

        # Get player data (same logic as get_players)
        player_stats = leaguedashplayerstats.LeagueDashPlayerStats(
            season=season,
            season_type_all_star='Regular Season'
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
        return jsonify({
            'success': False,
            'error': str(e)
        })


# Helper functions
def get_player_position(player_id):
    """Get player position (simplified version)"""
    # In a real app, you'd call the player info endpoint
    # This is a simplified approach
    if player_id % 5 == 0:
        return 'C'
    elif player_id % 5 in [1, 2]:
        return 'G'
    else:
        return 'F'


def calculate_true_shooting(row):
    """Calculate True Shooting Percentage"""
    pts = row['PTS']
    fga = row['FGA']
    fta = row['FTA']

    if fga + 0.44 * fta == 0:
        return 0

    return (pts / (2 * (fga + 0.44 * fta))) * 100


def calculate_efficiency(row):
    """Calculate simple efficiency rating"""
    return (row['PTS'] + row['REB'] + row['AST'] + row['STL'] + row['BLK'] -
            (row['FGA'] - row['FGM']) - (row['FTA'] - row['FTM']) - row['TOV'])


# main driver function
if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=True, port=port)