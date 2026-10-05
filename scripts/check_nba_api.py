"""
Check whether this machine can reach stats.nba.com through nba_api.

stats.nba.com is known to block or stall requests from many cloud providers, so
run this on (or in CI next to) any host before deploying there:

    python scripts/check_nba_api.py

Exits 0 when every endpoint answers, 1 otherwise. Honors NBA_PROXY / NBA_TIMEOUT.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("COURTVISION_CACHE", "off")  # always hit the network

from nba_api.stats.endpoints import (  # noqa: E402
    ScoreboardV3,
    leaguedashplayerstats,
    leaguestandingsv3,
    shotchartdetail,
)

import nba_client  # noqa: E402

TIMEOUT = int(os.environ.get("NBA_TIMEOUT", "20"))
SEASON = "2024-25"

CHECKS = [
    ("LeagueDashPlayerStats", leaguedashplayerstats.LeagueDashPlayerStats, {"season": SEASON}),
    ("LeagueStandingsV3", leaguestandingsv3.LeagueStandingsV3, {"season": SEASON}),
    ("ScoreboardV3", ScoreboardV3, {"game_date": "2025-01-15"}),
    ("ShotChartDetail", shotchartdetail.ShotChartDetail,
     {"team_id": 0, "player_id": 2544, "season_nullable": SEASON, "context_measure_simple": "FGA"}),
]


def main() -> int:
    print(f"Proxy: {'set' if nba_client.PROXY else 'none'} · timeout {TIMEOUT}s")
    failures = 0
    for name, endpoint, params in CHECKS:
        started = time.time()
        try:
            frames = nba_client.nbacall_retry(endpoint, retries=1, timeout=TIMEOUT, **params).get_data_frames()
            rows = sum(len(f) for f in frames)
            print(f"  OK    {name:<22} {time.time() - started:5.1f}s  {rows} rows")
        except Exception as e:
            failures += 1
            print(f"  FAIL  {name:<22} {time.time() - started:5.1f}s  {type(e).__name__}: {str(e)[:120]}")

    if failures:
        print(f"\n{failures}/{len(CHECKS)} endpoints failed: stats.nba.com is unreachable or blocking this host.")
        print("Options: set NBA_PROXY to a proxy the NBA accepts, or host somewhere that isn't blocked.")
        return 1
    print("\nAll endpoints reachable.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
