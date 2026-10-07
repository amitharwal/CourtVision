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
import build
import nba_client
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

    def test_prune_drops_old_scoreboards_but_keeps_season_data(self):
        long_ago = time.time() - 400 * 24 * 3600
        self.disk.set(("scoreboard", "2025-01-15"), long_ago, 1)
        self.disk.set(("standings", "2024-25"), long_ago, 2)
        self.disk.set(("scoreboard", "2026-10-05"), time.time(), 3)
        self.disk.prune_scoreboards(7 * 24 * 3600)
        self.assertIsNone(self.disk.get(("scoreboard", "2025-01-15")))
        self.assertEqual(self.disk.get(("standings", "2024-25"))[1], 2)
        self.assertEqual(self.disk.get(("scoreboard", "2026-10-05"))[1], 3)


class PlayerRefreshTest(unittest.TestCase):
    """warm_cache skips players with no new games since their data was fetched."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.disk = nba_client.DiskCache(os.path.join(self.tmp.name, "cache.sqlite3"))
        patcher = mock.patch.object(nba_client, "_DISK", self.disk)
        patcher.start()
        self.addCleanup(patcher.stop)
        nba_client._CACHE.clear()

    def store_player(self, pid, games, age=0):
        ts = time.time() - age
        log = pd.DataFrame({"Game_ID": [str(i) for i in range(games)]})
        for key, value in [(("player_info", pid), pd.DataFrame()), (("player_profile", pid), {}),
                           (("player_gamelog", pid, "2025-26", "Regular Season"), log),
                           (("shot_chart", pid, "2025-26", "Regular Season"), pd.DataFrame())]:
            self.disk.set(key, ts, value)

    def to_fetch(self, pid, games):
        return nba_client._player_kinds_to_fetch(pid, "2025-26", "Regular Season",
                                                 ("info", "profile", "game log", "shot chart"), games)

    def test_same_games_played_needs_nothing(self):
        self.store_player(1, games=3)
        self.assertEqual(self.to_fetch(1, 3), [])

    def test_new_game_refetches_stats_but_not_bio(self):
        self.store_player(1, games=3)
        self.assertEqual(self.to_fetch(1, 4), ["profile", "game log", "shot chart"])

    def test_missing_data_is_fetched(self):
        self.assertEqual(self.to_fetch(1, 0), ["info", "profile", "game log", "shot chart"])

    def test_week_old_bio_is_refreshed_alone(self):
        self.store_player(2, games=3, age=8 * 24 * 3600)
        self.assertEqual(self.to_fetch(2, 3), ["info"])

    def test_empty_nba_answer_is_not_asked_again_for_a_week(self):
        self.store_player(1, games=3)
        key = ("shot_chart", 1, "2025-26", "Playoffs")
        to_fetch = lambda: nba_client._player_kinds_to_fetch(1, "2025-26", "Playoffs", ("shot chart",), 3)
        self.assertEqual(to_fetch(), ["shot chart"])
        empty = json.JSONDecodeError("Expecting value", "", 0)
        with mock.patch.object(nba_client, "get_shot_chart", side_effect=empty), self.assertRaises(json.JSONDecodeError):
            nba_client._player_job(1, "2025-26", "Playoffs", "shot chart")()
        self.assertEqual(to_fetch(), [])
        with mock.patch.object(nba_client, "_now", return_value=time.time() + 8 * 24 * 3600):
            self.assertEqual(to_fetch(), ["shot chart"])

    def test_warm_cache_fetches_only_changed_players(self):
        self.store_player(1, games=3)
        league = pd.DataFrame({"PLAYER_ID": [1, 2], "GP": [3, 5]})
        tables = {"Regular Season": league, "Playoffs": league.iloc[0:0]}
        fetched = []
        with mock.patch.object(nba_client, "get_league_player_stats",
                               side_effect=lambda season, measure="Base", season_type="Regular Season": tables[season_type]), \
                mock.patch.object(nba_client, "_player_job",
                                  side_effect=lambda pid, *a: (lambda: fetched.append(pid))):
            result = nba_client.warm_cache("2025-26", include_teams=False, include_players=True)
        self.assertEqual(set(fetched), {2})
        self.assertEqual(result["unchanged"], 1)


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


def _standings_frame():
    return pd.DataFrame([{"TeamID": 1, "TeamCity": "Test", "TeamName": "Team", "Conference": "East",
                          "PlayoffRank": 1, "WINS": 50, "LOSSES": 32, "WinPCT": 0.61, "ConferenceGamesBack": 0.0,
                          "ConferenceRecord": "30-22", "HOME": "25-16", "ROAD": "25-16", "L10": "6-4",
                          "strCurrentStreak": "W 1", "PointsPG": 115.0, "OppPointsPG": 110.0, "DiffPointsPG": 5.0}])


class PublishedDataTestCase(unittest.TestCase):
    """Serving only what's in the cache (COURTVISION_OFFLINE=1, and every build)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.disk = nba_client.DiskCache(os.path.join(self.tmp.name, "cache.sqlite3"))
        for patcher in (mock.patch.object(nba_client, "_DISK", self.disk),
                        mock.patch.object(nba_client, "OFFLINE", True),
                        mock.patch.object(app_module, "OFFLINE", True)):
            patcher.start()
            self.addCleanup(patcher.stop)
        nba_client._CACHE.clear()
        nba_client._PUBLISHED["ts"] = 0.0
        self.client = app_module.app.test_client()

    def put(self, key, value):
        """Store one entry in the cache, as a fetch would."""
        self.disk.set(key, time.time(), value)
        nba_client._PUBLISHED["ts"] = 0.0

    def put_player(self, pid, season="2025-26"):
        self.put(("player_info", pid), pd.DataFrame([{"PERSON_ID": pid, "DISPLAY_FIRST_LAST": "Test Player",
                                                       "DRAFT_NUMBER": float("nan")}]))
        self.put(("player_profile", pid), {"SeasonTotalsRegularSeason": [
            {"SEASON_ID": season, "TEAM_ID": 1, "GP": 10, "MIN": 300.0, "PTS": 200}]})


class PublishedDataTest(PublishedDataTestCase):
    def test_missing_data_explains_itself(self):
        resp = self.client.get("/data/standings/2024-25.json")
        self.assertEqual(resp.status_code, 503)
        self.assertTrue(resp.get_json()["offline"])

    def test_missing_team_log_is_not_reported_as_an_empty_season(self):
        self.assertEqual(self.client.get("/data/teams/2024-25/1.json").status_code, 503)

    def test_cached_data_is_served(self):
        self.put(("standings", "2024-25"), _standings_frame())
        data = self.client.get("/data/standings/2024-25.json").get_json()
        self.assertEqual(data["east"][0]["wins"], 50)

    def test_pages_offer_only_published_seasons(self):
        self.put(("standings", "2023-24"), pd.DataFrame())
        self.put(("standings", "2025-26"), pd.DataFrame())
        html = self.client.get("/standings").get_data(as_text=True)
        self.assertEqual(re.findall(r'<option value="([^"]+)"', html), ["2025-26", "2023-24"])

    def test_default_season_is_newest_published_league_season(self):
        self.put(("league_player_stats", "2024-25", "Base", "Regular Season"), pd.DataFrame())
        with app_module.app.test_request_context():
            self.assertEqual(app_module.current_season(), "2024-25")

    def test_player_index_lists_only_published_players(self):
        self.put(("player_profile", 2544), {})
        players = self.client.get("/data/player-index.json").get_json()["players"]
        self.assertEqual([p["name"] for p in players], ["LeBron James"])

    def test_playoffs_offered_only_once_they_have_games(self):
        frames = _league_frames()
        self.put(("league_player_stats", "2024-25", "Base", "Playoffs"), frames["Base"])
        self.put(("league_player_stats", "2025-26", "Base", "Playoffs"), frames["Base"].iloc[0:0])
        self.assertEqual(app_module.playoff_seasons(), ["2024-25"])

    def test_player_lists_published_game_log_seasons(self):
        self.put(("player_gamelog", 1, "2025-26", "Regular Season"), pd.DataFrame())
        self.put(("player_gamelog", 1, "2024-25", "Playoffs"), pd.DataFrame())
        self.assertEqual(app_module.gamelog_seasons(1), {"regular": ["2025-26"], "playoffs": ["2024-25"]})


class BuildTest(PublishedDataTestCase):
    def setUp(self):
        super().setUp()
        self.out = os.path.join(self.tmp.name, "dist")
        for patcher in (mock.patch.object(build, "STATE_PATH", os.path.join(self.tmp.name, "state.json")),
                        mock.patch.object(build, "LOCK_PATH", os.path.join(self.tmp.name, "build.lock"))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def read(self, path):
        with open(os.path.join(self.out, path), "rb") as f:
            return f.read()

    def test_saves_pages_and_data_at_their_urls(self):
        self.put(("standings", "2024-25"), _standings_frame())
        self.put_player(1)
        self.put(("shot_chart", 1, "2025-26", "Playoffs"), pd.DataFrame())
        summary = build.build_site(self.out)

        self.assertIn(b"<nav>", self.read("standings.html"))
        self.assertIn(b"<nav>", self.read("index.html"))
        self.assertIn(b"<nav>", self.read("player/1.html"))
        self.assertEqual(json.loads(self.read("data/standings/2024-25.json"))["east"][0]["wins"], 50)
        player = json.loads(self.read("data/player/1.json"))
        self.assertEqual(player["available_seasons"], ["2025-26"])
        self.assertIsNone(player["player_info"]["DRAFT_NUMBER"])  # undrafted: NaN -> null
        self.assertEqual(json.loads(self.read("data/player/1/shots/2025-26/playoffs.json"))["shots"], [])
        self.assertIn(b"Page not found", self.read("404.html"))
        self.assertTrue(os.path.exists(os.path.join(self.out, "static", "js", "site-data.js")))
        # Today's scoreboard was never fetched: no file, so the page reports it missing.
        self.assertIn("/data/games-today.json", summary["skipped"])
        self.assertEqual(summary["invalid"], {})

    def test_build_never_calls_the_nba_api(self):
        with mock.patch.object(nba_client, "OFFLINE", False), \
                mock.patch("nba_api.stats.library.http.NBAStatsHTTP.send_api_request",
                           side_effect=AssertionError("called the NBA API")) as send:
            build.build_site(self.out)
        send.assert_not_called()

    def test_deploys_only_when_something_changed(self):
        self.put(("standings", "2024-25"), _standings_frame())
        deployer = mock.Mock()
        build.build_and_deploy(self.out, deployer=deployer)
        build.build_and_deploy(self.out, deployer=deployer)
        self.assertEqual(deployer.call_count, 1)
        self.put(("standings", "2023-24"), _standings_frame())
        build.build_and_deploy(self.out, deployer=deployer)
        self.assertEqual(deployer.call_count, 2)


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

    def _players(self, season_type="regular"):
        data = self.client.get(f"/data/players/2024-25/{season_type}.json").get_json()
        self.assertTrue(data["success"])
        return {p["PLAYER_NAME"]: p for p in data["data"]}

    def test_official_advanced_metrics_scaled_to_percent(self):
        alpha = self._players()["Alpha Guard"]
        self.assertEqual(alpha["USG_PCT"], 30.0)
        self.assertEqual(alpha["PIE"], 15.0)

    def test_highest_scorers_first(self):
        self.assertEqual(list(self._players()), ["Alpha Guard", "Beta Center"])

    def test_playoffs_season_type_passed_through(self):
        self._players("playoffs")
        season_types = {c.kwargs.get("season_type") or c.args[2]
                        for c in app_module.get_league_player_stats.call_args_list}
        self.assertEqual(season_types, {"Playoffs"})

    def test_positions_joined(self):
        self.assertEqual(self._players()["Alpha Guard"]["POSITION"], "G-F")

    def test_csv_export(self):
        resp = self.client.get("/data/players/2024-25/regular.csv")
        self.assertEqual(resp.mimetype, "text/csv")
        self.assertEqual(resp.get_data(as_text=True).splitlines()[1].split(",")[1], "Alpha Guard")

    def test_bad_season_or_type_is_404(self):
        self.assertEqual(self.client.get("/data/players/2024/regular.json").status_code, 404)
        self.assertEqual(self.client.get("/data/players/2024-25/preseason.json").status_code, 404)


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
            data = self.client.get("/data/player/1.json").get_json()
        sel = data["seasons_regular"][0]
        self.assertEqual(sel["TOV_TOTAL"], 200)
        self.assertEqual(sel["TOV"], 4.0)
        self.assertAlmostEqual(sel["TOV_P36"], 4.0 * 36 / 35)


class PlayerIndexTest(unittest.TestCase):
    """Search keys pages match against (the matching itself runs in the browser)."""

    @classmethod
    def setUpClass(cls):
        players = app_module.app.test_client().get("/data/player-index.json").get_json()["players"]
        cls.keys = {p["name"]: p["key"] for p in players}

    def test_keys_ignore_case_accents_and_punctuation(self):
        self.assertEqual(self.keys["Luka Dončić"], "luka doncic")
        self.assertEqual(self.keys["P.J. Washington"], "pj washington")
        self.assertEqual(self.keys["D'Angelo Russell"], "dangelo russell")
        self.assertEqual(self.keys["Shai Gilgeous-Alexander"], "shai gilgeous alexander")

    def test_lists_every_player_outside_offline_mode(self):
        self.assertGreater(len(self.keys), 4000)


class GameLogApiTest(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()

    def test_games_oldest_first_with_iso_dates(self):
        df = pd.DataFrame([
            {"Game_ID": "2", "GAME_DATE": "Apr 12, 2026", "MATCHUP": "LAL vs. UTA", "WL": "W", "PTS": 18},
            {"Game_ID": "1", "GAME_DATE": "Oct 21, 2025", "MATCHUP": "LAL @ GSW", "WL": "L", "PTS": 25},
        ])
        with mock.patch.object(app_module, "get_player_gamelog", return_value=df) as get:
            data = self.client.get("/data/player/2544/gamelog/2025-26/regular.json").get_json()
        self.assertEqual([g["GAME_DATE"] for g in data["games"]], ["2025-10-21", "2026-04-12"])
        self.assertEqual(get.call_args.args, (2544, "2025-26", "Regular Season"))

    def test_playoffs_season_type(self):
        with mock.patch.object(app_module, "get_player_gamelog", return_value=pd.DataFrame()) as get:
            data = self.client.get("/data/player/2544/gamelog/2018-19/playoffs.json").get_json()
        self.assertEqual(get.call_args.args[2], "Playoffs")
        self.assertEqual(data["games"], [])


class RosterLeadersTest(unittest.TestCase):
    def test_team_leaders_per_game_from_league_table(self):
        league = pd.DataFrame([
            {"PLAYER_ID": 77, "PLAYER_NAME": "Star", "TEAM_ID": 1, "GP": 10, "PTS": 300, "REB": 50, "AST": 60,
             "STL": 10, "BLK": 5, "FGA": 200, "FGM": 100, "FTA": 50, "FTM": 40, "TOV": 25},
            {"PLAYER_ID": 88, "PLAYER_NAME": "Elsewhere", "TEAM_ID": 2, "GP": 10, "PTS": 900, "REB": 0, "AST": 0,
             "STL": 0, "BLK": 0, "FGA": 0, "FGM": 0, "FTA": 0, "FTM": 0, "TOV": 0},
        ])
        with mock.patch.object(app_module, "get_league_player_stats", return_value=league):
            data = app_module.roster_leaders(1, "2024-25")
        # (300+50+60+10+5) - ((200-100) + (50-40) + 25) = 290 over 10 games
        self.assertEqual(data["most_efficient"]["stat"], 29.0)
        self.assertEqual(data["top_scorer"]["stat"], 30.0)
        self.assertEqual(data["top_scorer"]["player_id"], 77)  # only this team's players


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
            data = self.client.get("/data/games-today.json").get_json()
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
            data = self.client.get("/data/leaders/2024-25.json").get_json()
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
            data = app_module.team_monthly_series(1, "2024-25")
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
            data = self.client.get("/data/standings/2024-25.json").get_json()
        self.assertEqual([r["team"] for r in data["east"]], ["East One Team", "East Two Team"])
        self.assertEqual(len(data["west"]), 1)
        self.assertEqual(data["east"][0]["streak"], "W 2")

    def test_empty_season_is_404(self):
        with mock.patch.object(app_module, "get_standings", return_value=pd.DataFrame()):
            resp = self.client.get("/data/standings/2024-25.json")
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
            data = self.client.get("/data/player/2544/shots/2024-25/regular.json").get_json()
        self.assertEqual(data["count"], 2)
        self.assertEqual(call.call_args.kwargs["context_measure_simple"], "FGA")

    def test_no_shots_is_an_empty_list(self):
        endpoint = mock.Mock(get_data_frames=mock.Mock(return_value=[pd.DataFrame()]))
        with mock.patch.object(nba_client, "nbacall_retry", return_value=endpoint):
            data = self.client.get("/data/player/2544/shots/1999-00/regular.json").get_json()
        self.assertEqual((data["success"], data["shots"]), (True, []))


if __name__ == "__main__":
    unittest.main()
