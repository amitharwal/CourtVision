import unittest
from datetime import datetime
from unittest import mock

import pandas as pd

import app as app_module
import nba_client
from metrics import calculate_efficiency, calculate_true_shooting


def setUpModule():
    # Keep the suite offline: routes call get_seasons(), which checks the NBA API.
    patcher = mock.patch.object(nba_client, "season_has_started", return_value=True)
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


class MetricsTest(unittest.TestCase):
    def test_true_shooting(self):
        # 30 pts on 20 FGA and 10 FTA -> 30 / (2 * 24.4)
        row = {"PTS": 30, "FGA": 20, "FTA": 10}
        self.assertAlmostEqual(calculate_true_shooting(row), 61.475, places=2)

    def test_true_shooting_no_attempts(self):
        self.assertEqual(calculate_true_shooting({"PTS": 0, "FGA": 0, "FTA": 0}), 0)

    def test_efficiency(self):
        row = {"PTS": 20, "REB": 10, "AST": 5, "STL": 2, "BLK": 1,
               "FGA": 15, "FGM": 8, "FTA": 4, "FTM": 3, "TOV": 3}
        # positives 38, negatives (7 missed FG + 1 missed FT + 3 TOV) = 11
        self.assertEqual(calculate_efficiency(row), 27)


class SeasonsTest(unittest.TestCase):
    def _seasons_on(self, when, started=True):
        with mock.patch.object(nba_client, "datetime") as dt, \
                mock.patch.object(nba_client, "season_has_started", return_value=started):
            dt.now.return_value = when
            return nba_client.get_seasons()

    def test_new_season_listed_once_started(self):
        self.assertEqual(self._seasons_on(datetime(2026, 10, 25))[0], "2026-27")

    def test_new_season_hidden_until_started(self):
        self.assertEqual(self._seasons_on(datetime(2026, 10, 5), started=False)[0], "2025-26")

    def test_before_october_uses_previous_season(self):
        self.assertEqual(self._seasons_on(datetime(2026, 9, 30))[0], "2025-26")

    def test_oldest_season_is_first_stats_season(self):
        self.assertEqual(self._seasons_on(datetime(2026, 10, 5))[-1], "1996-97")


class CacheTest(unittest.TestCase):
    def setUp(self):
        nba_client._CACHE.clear()

    def test_serves_cached_value_within_ttl(self):
        loader = mock.Mock(return_value=1)
        nba_client.cached("k", 60, loader)
        self.assertEqual(nba_client.cached("k", 60, loader), 1)
        loader.assert_called_once()

    def test_serves_stale_value_when_refresh_fails(self):
        nba_client.cached("k", 60, lambda: "old")
        failing = mock.Mock(side_effect=TimeoutError)
        self.assertEqual(nba_client.cached("k", 0, failing), "old")

    def test_raises_when_nothing_cached(self):
        with self.assertRaises(TimeoutError):
            nba_client.cached("k", 60, mock.Mock(side_effect=TimeoutError))


class PagesTest(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()

    def test_pages_render_with_shared_layout(self):
        for url in ["/", "/players", "/team-trends", "/shot-charts", "/compare",
                    "/advanced-metrics", "/privacy_policy", "/player/2544", "/standings"]:
            with self.subTest(url=url):
                resp = self.client.get(url)
                self.assertEqual(resp.status_code, 200)
                self.assertIn(b'<nav>', resp.data)
                self.assertIn(b'href="/privacy_policy"', resp.data)

    def test_active_nav_link(self):
        html = self.client.get("/shot-charts").get_data(as_text=True)
        self.assertIn('<a href="/shot-charts" class="active">', html)
        # Player detail pages highlight the Players section.
        html = self.client.get("/player/2544").get_data(as_text=True)
        self.assertIn('<a href="/players" class="active">', html)


def _league_frames():
    base = pd.DataFrame({
        "PLAYER_ID": [1, 2],
        "PLAYER_NAME": ["Alpha Guard", "Beta Center"],
        "TEAM_ABBREVIATION": ["AAA", "BBB"],
        "AGE": [25, 30], "GP": [10, 10], "MIN": [300.0, 300.0],
        "PTS": [200, 100], "REB": [40, 120], "AST": [60, 10], "STL": [10, 5],
        "BLK": [2, 20], "FGM": [70, 40], "FGA": [150, 70], "FTM": [40, 20],
        "FTA": [50, 30], "TOV": [20, 10], "PF": [20, 30], "OREB": [5, 40],
        "DREB": [35, 80], "FG_PCT": [0.467, 0.571], "FG3_PCT": [0.35, 0.0],
        "FT_PCT": [0.8, 0.667],
    })
    adv = pd.DataFrame({
        "PLAYER_ID": [1, 2], "USG_PCT": [0.30, 0.18], "AST_PCT": [0.35, 0.05],
        "REB_PCT": [0.07, 0.20], "PIE": [0.15, 0.11],
    })
    return {"Base": base, "Advanced": adv}


class PlayersApiTest(unittest.TestCase):
    def setUp(self):
        frames = _league_frames()
        patches = [
            mock.patch.object(app_module, "get_league_player_stats",
                              side_effect=lambda season, measure="Base", season_type=None: frames[measure]),
            mock.patch.object(app_module, "get_player_positions",
                              return_value={1: "G-F", 2: "C"}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.client = app_module.app.test_client()

    def _players(self, query=""):
        data = self.client.get(f"/api/players?season=2024-25{query}").get_json()
        self.assertTrue(data["success"])
        return {p["PLAYER_NAME"]: p for p in data["data"]}

    def test_official_advanced_metrics_scaled_to_percent(self):
        alpha = self._players()["Alpha Guard"]
        self.assertEqual(alpha["USG_PCT"], 30.0)
        self.assertEqual(alpha["PIE"], 15.0)

    def test_search_does_not_change_metrics(self):
        unfiltered = self._players()["Alpha Guard"]
        filtered = self._players("&search=alpha")
        self.assertEqual(list(filtered), ["Alpha Guard"])
        self.assertEqual(filtered["Alpha Guard"]["USG_PCT"], unfiltered["USG_PCT"])

    def test_playoffs_season_type_passed_through(self):
        self._players("&season_type=playoffs")
        season_types = {c.kwargs.get("season_type") or c.args[2]
                        for c in app_module.get_league_player_stats.call_args_list}
        self.assertEqual(season_types, {"Playoffs"})

    def test_positions_joined_and_filterable(self):
        self.assertEqual(self._players()["Alpha Guard"]["POSITION"], "G-F")
        self.assertEqual(list(self._players("&position=C")), ["Beta Center"])


class PlayerApiTest(unittest.TestCase):
    def setUp(self):
        nba_client._CACHE.clear()
        self.client = app_module.app.test_client()

    def test_turnover_total_and_per_game(self):
        season = {"SEASON_ID": "2024-25", "TEAM_ID": 0, "TEAM_ABBREVIATION": "TOT", "GP": 50,
                  "MIN": 1750.0, "PTS": 1400, "REB": 400, "AST": 400, "STL": 50, "BLK": 20,
                  "TOV": 200, "FGA": 1000, "FGM": 450, "FTA": 300, "FTM": 240}
        info = mock.Mock(get_data_frames=mock.Mock(return_value=[pd.DataFrame([{"PERSON_ID": 1}])]))
        profile = mock.Mock(get_normalized_dict=mock.Mock(return_value={
            "SeasonTotalsRegularSeason": [season], "CareerTotalsRegularSeason": []}))
        with mock.patch.object(app_module, "nbacall_retry", side_effect=[info, profile]):
            data = self.client.get("/api/player/1?season=2024-25").get_json()
        sel = data["selected_season"]
        self.assertEqual(sel["TOV_TOTAL"], 200)
        self.assertEqual(sel["TOV"], 4.0)
        self.assertAlmostEqual(sel["TOV_P36"], 4.0 * 36 / 35)


class SearchPlayersApiTest(unittest.TestCase):
    def test_lookup_by_ids_keeps_order_and_skips_unknown(self):
        client = app_module.app.test_client()
        data = client.get("/api/search-players?ids=201939,2544,999999999,abc").get_json()
        self.assertEqual([p["name"] for p in data["players"]], ["Stephen Curry", "LeBron James"])


class GameLogApiTest(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()

    def test_games_oldest_first_with_iso_dates(self):
        df = pd.DataFrame([
            {"Game_ID": "2", "GAME_DATE": "Apr 12, 2026", "MATCHUP": "LAL vs. UTA", "WL": "W", "PTS": 18},
            {"Game_ID": "1", "GAME_DATE": "Oct 21, 2025", "MATCHUP": "LAL @ GSW", "WL": "L", "PTS": 25},
        ])
        with mock.patch.object(app_module, "get_player_gamelog", return_value=df) as get:
            data = self.client.get("/api/player/2544/gamelog?season=2025-26").get_json()
        self.assertEqual([g["GAME_DATE"] for g in data["games"]], ["2025-10-21", "2026-04-12"])
        self.assertEqual(get.call_args.args, (2544, "2025-26", "Regular Season"))

    def test_playoffs_season_type(self):
        with mock.patch.object(app_module, "get_player_gamelog", return_value=pd.DataFrame()) as get:
            data = self.client.get("/api/player/2544/gamelog?season=2018-19&season_type=playoffs").get_json()
        self.assertEqual(get.call_args.args[2], "Playoffs")
        self.assertEqual(data["games"], [])


class StandingsApiTest(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()

    def _row(self, team_id, city, conf, rank, wins):
        return {"TeamID": team_id, "TeamCity": city, "TeamName": "Team", "Conference": conf,
                "PlayoffRank": rank, "WINS": wins, "LOSSES": 82 - wins, "WinPCT": wins / 82,
                "ConferenceGamesBack": 0.0, "ConferenceRecord": "30-22", "HOME": "25-16",
                "ROAD": "20-21", "L10": "6-4", "strCurrentStreak": "W 2 ", "PointsPG": 115.04,
                "OppPointsPG": 110.0, "DiffPointsPG": 5.04}

    def test_splits_and_orders_conferences(self):
        df = pd.DataFrame([self._row(1, "East Two", "East", 2, 50), self._row(2, "West One", "West", 1, 60),
                           self._row(3, "East One", "East", 1, 55)])
        with mock.patch.object(app_module, "get_standings", return_value=df):
            data = self.client.get("/api/standings?season=2024-25").get_json()
        self.assertEqual([r["team"] for r in data["east"]], ["East One Team", "East Two Team"])
        self.assertEqual(len(data["west"]), 1)
        self.assertEqual(data["east"][0]["streak"], "W 2")

    def test_empty_season_is_404(self):
        with mock.patch.object(app_module, "get_standings", return_value=pd.DataFrame()):
            resp = self.client.get("/api/standings?season=2024-25")
        self.assertEqual(resp.status_code, 404)


class ShotChartApiTest(unittest.TestCase):
    def setUp(self):
        nba_client._CACHE.clear()
        self.client = app_module.app.test_client()

    def test_returns_shots(self):
        df = pd.DataFrame({"LOC_X": [0, -220], "LOC_Y": [5, 10], "SHOT_MADE_FLAG": [1, 0],
                           "PERIOD": [1, 5], "SHOT_ZONE_BASIC": ["Restricted Area", "Left Corner 3"]})
        endpoint = mock.Mock(get_data_frames=mock.Mock(return_value=[df]))
        with mock.patch.object(app_module, "nbacall_retry", return_value=endpoint) as call:
            data = self.client.get("/api/shot-chart/2544?season=2024-25").get_json()
        self.assertEqual(data["count"], 2)
        self.assertEqual(call.call_args.kwargs["context_measure_simple"], "FGA")

    def test_no_shots_is_404(self):
        endpoint = mock.Mock(get_data_frames=mock.Mock(return_value=[pd.DataFrame()]))
        with mock.patch.object(app_module, "nbacall_retry", return_value=endpoint):
            resp = self.client.get("/api/shot-chart/2544?season=1999-00")
        self.assertEqual(resp.status_code, 404)


if __name__ == "__main__":
    unittest.main()
