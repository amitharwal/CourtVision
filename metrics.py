"""
Box-score derived player metrics.
"""

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
