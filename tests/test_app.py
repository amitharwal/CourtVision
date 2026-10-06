import gzip
import json
import os
import re
import tempfile
import time
import unittest
from datetime import datetime
from unittest import mock

import pandas as pd

import app as app_module
import nba_client
import publish
from metrics import calculate_efficiency, calculate_true_shooting


def setUpModule():
    # Keep the suite offline: routes call get_seasons(), which checks the NBA API.
    patcher = mock.patch.object(nba_client, "season_has_started", return_value=True)
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)
    # Never read or write the real on-disk cache from tests.
    disk = mock.patch.object(nba_client, "_DISK", None)
    disk.start()
    unittest.addModuleCleanup(disk.stop)


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


class DiskCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.disk = nba_client.DiskCache(os.path.join(self.tmp.name, "cache.sqlite3"))
        patcher = mock.patch.object(nba_client, "_DISK", self.disk)
        patcher.start()
        self.addCleanup(patcher.stop)
        nba_client._CACHE.clear()

    def test_survives_a_restart(self):
        df = pd.DataFrame({"PTS": [30]})
        nba_client.cached("k", 60, lambda: df)
        nba_client._CACHE.clear()  # simulate a new process
        loader = mock.Mock()
        self.assertTrue(nba_client.cached("k", 60, loader).equals(df))
        loader.assert_not_called()

    def test_serves_stale_disk_copy_when_api_fails(self):
        self.disk.set("k", time.time() - 3600, "old")
        self.assertEqual(nba_client.cached("k", 60, mock.Mock(side_effect=TimeoutError)), "old")

    def test_prune_removes_old_entries(self):
        self.disk.set("old", time.time() - 10 * 24 * 3600, 1)
        self.disk.set("new", time.time(), 2)
        self.disk.prune(7 * 24 * 3600)
        self.assertIsNone(self.disk.get("old"))
        self.assertEqual(self.disk.get("new")[1], 2)


class CodecTest(unittest.TestCase):
    def roundtrip(self, value):
        return nba_client.loads_value(nba_client.dumps_value(value))

    def test_dataframe_keeps_ids_floats_and_missing_values(self):
        df = pd.DataFrame({"GAME_ID": ["0022400561"], "MIN": [2269.9333333333334],
                           "PCT": [float("nan")], "GP": [70], "NAME": ["Luka Dončić"]})
        back = self.roundtrip(df)
        self.assertEqual(back["GAME_ID"][0], "0022400561")
        self.assertAlmostEqual(back["MIN"][0], 2269.9333333333334, places=12)
        self.assertTrue(pd.isna(back["PCT"][0]))
        self.assertEqual((back["GP"][0], back["NAME"][0]), (70, "Luka Dončić"))

    def test_containers_and_non_string_keys(self):
        value = {"positions": {2544: "F", 201939: "G"}, "frames": [pd.DataFrame({"A": [1]}), pd.DataFrame()],
                 "flag": True, "rows": [{"x": 1.5}]}
        back = self.roundtrip(value)
        self.assertEqual(back["positions"], {2544: "F", 201939: "G"})
        self.assertTrue(back["frames"][0].equals(pd.DataFrame({"A": [1]})))
        self.assertEqual((back["flag"], back["rows"]), (True, [{"x": 1.5}]))

    def test_rejects_unknown_types(self):
        with self.assertRaises(TypeError):
            nba_client.dumps_value(object())


class HostedModeTest(unittest.TestCase):
    """Publishing into a host that runs with COURTVISION_OFFLINE=1."""

    TOKEN = "test-token-for-unit-tests"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.disk = nba_client.DiskCache(os.path.join(self.tmp.name, "host.sqlite3"))
        for patcher in (mock.patch.object(nba_client, "_DISK", self.disk),
                        mock.patch.object(nba_client, "OFFLINE", True),
                        mock.patch.object(app_module, "OFFLINE", True),
                        mock.patch.dict(os.environ, {"COURTVISION_PUBLISH_TOKEN": self.TOKEN})):
            patcher.start()
            self.addCleanup(patcher.stop)
        nba_client._CACHE.clear()
        nba_client._PUBLISHED["ts"] = 0.0
        self.client = app_module.app.test_client()

    def put(self, key, value):
        """Publish one entry straight into the host's cache."""
        self.disk.put_raw(repr(key), time.time(), nba_client.dumps_value(value))
        nba_client._PUBLISHED["ts"] = 0.0

    def upload(self, entries, token=TOKEN):
        body = gzip.compress(json.dumps(entries).encode())
        return self.client.post("/api/admin/cache-entries", data=body,
                                headers={"Authorization": f"Bearer {token}", "Content-Encoding": "gzip"})

    def standings_entry(self):
        df = pd.DataFrame([{"TeamID": 1, "TeamCity": "Test", "TeamName": "Team", "Conference": "East",
                            "PlayoffRank": 1, "WINS": 50, "LOSSES": 32, "WinPCT": 0.61, "ConferenceGamesBack": 0.0,
                            "ConferenceRecord": "30-22", "HOME": "25-16", "ROAD": "25-16", "L10": "6-4",
                            "strCurrentStreak": "W 1", "PointsPG": 115.0, "OppPointsPG": 110.0, "DiffPointsPG": 5.0}])
        return {"key": repr(("standings", "2024-25")), "ts": time.time(),
                "value": nba_client.encode_value(df)}

    def test_unpublished_data_explains_itself(self):
        resp = self.client.get("/api/standings?season=2024-25")
        self.assertEqual(resp.status_code, 503)
        self.assertTrue(resp.get_json()["hosted"])

    def test_unpublished_team_log_is_not_reported_as_an_empty_season(self):
        resp = self.client.get("/api/team-monthly-series?team_id=1&season=2024-25")
        self.assertEqual(resp.status_code, 503)

    def test_published_data_is_served(self):
        self.assertEqual(self.upload([self.standings_entry()]).get_json()["stored"], 1)
        data = self.client.get("/api/standings?season=2024-25").get_json()
        self.assertEqual(data["east"][0]["wins"], 50)

    def test_requires_the_token(self):
        self.assertEqual(self.upload([self.standings_entry()], token="wrong").status_code, 401)
        with mock.patch.dict(os.environ, {"COURTVISION_PUBLISH_TOKEN": ""}):
            self.assertEqual(self.upload([self.standings_entry()]).status_code, 404)

    def test_rejects_malformed_entries(self):
        bad = self.standings_entry() | {"key": "__import__('os')"}
        self.assertEqual(self.upload([bad]).status_code, 400)
        self.assertEqual(self.upload([self.standings_entry() | {"value": {"__df__": "not json"}}]).status_code, 400)


    def test_pages_offer_only_published_seasons(self):
        self.put(("standings", "2023-24"), pd.DataFrame())
        self.put(("standings", "2025-26"), pd.DataFrame())
        html = self.client.get("/standings").get_data(as_text=True)
        self.assertEqual(re.findall(r'<option value="([^"]+)"', html), ["2025-26", "2023-24"])

    def test_default_season_is_newest_published_league_season(self):
        self.put(("league_player_stats", "2024-25", "Base", "Regular Season"), pd.DataFrame())
        with app_module.app.test_request_context():
            self.assertEqual(app_module.current_season(), "2024-25")

    def test_search_finds_only_published_players(self):
        self.put(("player_profile", 2544), {})
        names = [p["name"] for p in self.client.get("/api/search-players?q=james").get_json()["players"]]
        self.assertEqual(names, ["LeBron James"])

    def test_playoffs_offered_only_once_they_have_games(self):
        frames = _league_frames()
        self.put(("league_player_stats", "2024-25", "Base", "Playoffs"), frames["Base"])
        self.put(("league_player_stats", "2025-26", "Base", "Playoffs"), frames["Base"].iloc[0:0])
        self.assertEqual(app_module.playoff_seasons(), ["2024-25"])

    def test_player_missing_from_published_playoffs_had_no_playoff_games(self):
        self.put(("league_player_stats", "2024-25", "Base", "Playoffs"), _league_frames()["Base"])
        url = "/api/player/{}/gamelog?season=2024-25&season_type=playoffs"
        self.assertEqual(self.client.get(url.format(999)).get_json()["games"], [])
        # A playoff player whose log wasn't published is reported as unpublished.
        self.assertEqual(self.client.get(url.format(1)).status_code, 503)
        self.assertEqual(self.client.get("/api/shot-chart/999?season=2024-25&season_type=playoffs").status_code, 404)

    def test_player_api_lists_published_game_log_seasons(self):
        self.put(("player_gamelog", 1, "2025-26", "Regular Season"), pd.DataFrame())
        self.put(("league_player_stats", "2024-25", "Base", "Playoffs"), _league_frames()["Base"])
        self.assertEqual(app_module.gamelog_seasons(1), {"regular": ["2025-26"], "playoffs": ["2024-25"]})


class PublishTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.disk = nba_client.DiskCache(os.path.join(self.tmp.name, "fetcher.sqlite3"))
        for patcher in (mock.patch.object(nba_client, "_DISK", self.disk),
                        mock.patch.object(publish, "STATE_PATH", os.path.join(self.tmp.name, "state.json"))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_sends_only_entries_newer_than_the_last_publish(self):
        self.disk.set(("a",), 100.0, 1)
        ok = mock.Mock(status_code=200, json=mock.Mock(return_value={"stored": 1}))
        with mock.patch.object(publish.requests, "post", return_value=ok) as post:
            publish.publish("http://host", "t", echo=lambda *_: None)
            self.disk.set(("b",), 200.0, 2)
            publish.publish("http://host", "t", echo=lambda *_: None)
        sent = [json.loads(gzip.decompress(c.kwargs["data"])) for c in post.call_args_list]
        self.assertEqual([[e["key"] for e in batch] for batch in sent], [["('a',)"], ["('b',)"]])
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer t")


class ProxySettingTest(unittest.TestCase):
    def test_single_proxy_is_a_string(self):
        with mock.patch.dict(os.environ, {"NBA_PROXY": "http://proxy:8080"}):
            self.assertEqual(nba_client._proxy_setting(), "http://proxy:8080")

    def test_several_proxies_rotate_as_a_list(self):
        with mock.patch.dict(os.environ, {"NBA_PROXY": "http://a:1, http://b:2"}):
            self.assertEqual(nba_client._proxy_setting(), ["http://a:1", "http://b:2"])

    def test_unset_means_no_proxy(self):
        with mock.patch.dict(os.environ, {"NBA_PROXY": ""}):
            self.assertIsNone(nba_client._proxy_setting())


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

    def test_search_ignores_case_and_accents(self):
        self.assertEqual(list(self._players("&search=ALPHA")), ["Alpha Guard"])

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
        with mock.patch.object(nba_client, "nbacall_retry", side_effect=[info, profile]):
            data = self.client.get("/api/player/1?season=2024-25").get_json()
        sel = data["selected_season"]
        self.assertEqual(sel["TOV_TOTAL"], 200)
        self.assertEqual(sel["TOV"], 4.0)
        self.assertAlmostEqual(sel["TOV_P36"], 4.0 * 36 / 35)


class SearchPlayersApiTest(unittest.TestCase):
    def _names(self, q):
        client = app_module.app.test_client()
        return [p["name"] for p in client.get("/api/search-players", query_string={"q": q}).get_json()["players"]]

    def test_ignores_accents_and_punctuation(self):
        self.assertIn("Luka Dončić", self._names("doncic"))
        self.assertIn("Nikola Jokić", self._names("JOKIC"))
        self.assertIn("P.J. Washington", self._names("pj washington"))
        self.assertIn("D'Angelo Russell", self._names("d'angelo"))
        self.assertIn("Shai Gilgeous-Alexander", self._names("gilgeous alexander"))

    def test_ranks_name_prefix_matches_before_substrings(self):
        names = self._names("james")
        self.assertIn("LeBron James", names)
        self.assertIn("James Harden", names)

    def test_blank_query_returns_nothing(self):
        self.assertEqual(self._names(" . "), [])

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


class RosterAnalysisApiTest(unittest.TestCase):
    def test_most_efficient_is_per_game(self):
        nba_client._CACHE.clear()
        roster = pd.DataFrame([{"PLAYER_ID": 77, "PLAYER_NAME": "Star", "GP": 10, "PTS": 300, "REB": 50, "AST": 60,
                                "STL": 10, "BLK": 5, "FGA": 200, "FGM": 100, "FTA": 50, "FTM": 40, "TOV": 25}])
        endpoint = mock.Mock(get_data_frames=mock.Mock(return_value=[pd.DataFrame(), roster]))
        with mock.patch.object(nba_client, "nbacall_retry", return_value=endpoint):
            data = app_module.app.test_client().get("/api/roster-analysis/1?season=2024-25").get_json()
        # (300+50+60+10+5) - ((200-100) + (50-40) + 25) = 290 over 10 games
        self.assertEqual(data["most_efficient"]["stat"], 29.0)
        self.assertEqual(data["top_scorer"]["stat"], 30.0)
        self.assertEqual(data["top_scorer"]["player_id"], 77)


class HomeApiTest(unittest.TestCase):
    def setUp(self):
        nba_client._CACHE.clear()
        self.client = app_module.app.test_client()

    def test_games_today_reads_home_and_away_from_game_code(self):
        games = pd.DataFrame([{"gameId": "1", "gameCode": "20250115/NYKPHI", "gameStatus": 3,
                               "gameStatusText": "Final/OT ", "gameLabel": "", "seriesText": ""}])
        teams_df = pd.DataFrame([
            {"gameId": "1", "teamId": 20, "teamCity": "Philadelphia", "teamName": "76ers",
             "teamTricode": "PHI", "wins": 15, "losses": 24, "score": 119},
            {"gameId": "1", "teamId": 10, "teamCity": "New York", "teamName": "Knicks",
             "teamTricode": "NYK", "wins": 27, "losses": 15, "score": 125},
        ])
        endpoint = mock.Mock(get_data_frames=mock.Mock(return_value=[pd.DataFrame(), games, teams_df]))
        with mock.patch.object(nba_client, "nbacall_retry", return_value=endpoint):
            data = self.client.get("/api/games-today").get_json()
        game = data["games"][0]
        self.assertEqual((game["away"]["tricode"], game["away"]["score"]), ("NYK", 125))
        self.assertEqual((game["home"]["tricode"], game["home"]["score"]), ("PHI", 119))
        self.assertEqual(game["status_text"], "Final/OT")

    def test_league_leaders_require_half_the_max_games(self):
        df = pd.DataFrame([
            {"PLAYER_ID": 1, "PLAYER_NAME": "Regular", "TEAM_ID": 5, "TEAM_ABBREVIATION": "AAA",
             "GP": 60, "PTS": 1500, "REB": 300, "AST": 300},
            {"PLAYER_ID": 2, "PLAYER_NAME": "Cameo", "TEAM_ID": 6, "TEAM_ABBREVIATION": "BBB",
             "GP": 5, "PTS": 200, "REB": 10, "AST": 10},
        ])
        with mock.patch.object(app_module, "get_league_player_stats", return_value=df):
            data = self.client.get("/api/league-leaders?season=2024-25").get_json()
        self.assertEqual(data["min_games"], 30)
        self.assertEqual([p["name"] for p in data["leaders"]["PTS"]], ["Regular"])
        self.assertEqual(data["leaders"]["PTS"][0]["value"], 25.0)


class TeamMonthlySeriesApiTest(unittest.TestCase):
    def test_skips_months_without_games_and_returns_records(self):
        log = pd.DataFrame([
            {"GAME_DATE": "Oct 24, 2024", "WL": "W"},
            {"GAME_DATE": "Oct 26, 2024", "WL": "L"},
            {"GAME_DATE": "Dec 02, 2024", "WL": "W"},
        ])
        with mock.patch.object(app_module, "get_team_gamelog_cached", return_value=log):
            data = app_module.app.test_client().get(
                "/api/team-monthly-series?team_id=1&season=2024-25").get_json()
        self.assertEqual(data["months"], ["Oct", "Dec"])
        self.assertEqual(data["win_pct"], [50.0, 100.0])
        self.assertEqual((data["wins"], data["losses"]), ([1, 1], [1, 0]))


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
        with mock.patch.object(nba_client, "nbacall_retry", return_value=endpoint) as call:
            data = self.client.get("/api/shot-chart/2544?season=2024-25").get_json()
        self.assertEqual(data["count"], 2)
        self.assertEqual(call.call_args.kwargs["context_measure_simple"], "FGA")

    def test_no_shots_is_404(self):
        endpoint = mock.Mock(get_data_frames=mock.Mock(return_value=[pd.DataFrame()]))
        with mock.patch.object(nba_client, "nbacall_retry", return_value=endpoint):
            resp = self.client.get("/api/shot-chart/2544?season=1999-00")
        self.assertEqual(resp.status_code, 404)


if __name__ == "__main__":
    unittest.main()
