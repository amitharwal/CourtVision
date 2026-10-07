"""
Build Court Vision as a static site and deploy it to Cloudflare Pages.

stats.nba.com blocks cloud servers, so the site is built where the NBA API works
(e.g. your own computer) from the data already in the cache, and deployed as plain
files. Every file is the Flask app's own response for that URL, saved at the same
path, so the static site and `python app.py` serve the same data:

    /standings                          -> standings.html  (Pages serves it at /standings)
    /player/2544                        -> player/2544.html
    /data/standings/2025-26.json        -> data/standings/2025-26.json

Use it through the CLI:
    flask --app app build               # write dist/
    flask --app app preview             # serve dist/ the way Cloudflare Pages will
    flask --app app update [--live | --players]   # fetch, build, deploy if anything changed

Deploying needs Node.js (for npx wrangler) and, in the environment:
    CLOUDFLARE_API_TOKEN, CLOUDFLARE_ACCOUNT_ID
    COURTVISION_PAGES_PROJECT   Pages project name (default "courtvision")
"""
import fcntl
import hashlib
import http.server
import json
import os
import shutil
import subprocess
import time
from contextlib import contextmanager
from functools import partial

from nba_api.stats.static import teams

import nba_client

ROOT = os.path.dirname(os.path.abspath(__file__))
DIST_DIR = os.path.join(ROOT, "dist")
STATE_PATH = os.path.join(ROOT, "instance", "deploy_state.json")
LOCK_PATH = os.path.join(ROOT, "instance", "build.lock")
PAGES_FILE_LIMIT = 20_000  # Cloudflare Pages free plan: files per deployment

PAGES = ["home", "players_page", "team_trends", "standings", "shot_charts",
         "compare_players", "advanced_metrics", "privacy_policy"]


@contextmanager
def _offline():
    """Serve only what's cached while building: a build never calls the NBA API."""
    import app as site

    saved = nba_client.OFFLINE, site.OFFLINE
    nba_client.OFFLINE = site.OFFLINE = True
    nba_client._PUBLISHED["ts"] = 0.0  # read the cache's keys afresh
    try:
        yield site
    finally:
        nba_client.OFFLINE, site.OFFLINE = saved
        nba_client._PUBLISHED["ts"] = 0.0


def site_urls(site):
    """Every URL the cache has data for: pages first, then data files."""
    from flask import url_for

    keys = nba_client.published_keys() or frozenset()
    player_ids = sorted(nba_client.published_player_ids() or ())
    league_seasons = nba_client.published_seasons(site.LEAGUE_STATS) or []

    urls = [url_for(endpoint) for endpoint in PAGES]
    urls += [url_for("player_detail", player_id=pid) for pid in player_ids]

    urls += [url_for("api_games_today"), url_for("api_player_index")]
    for season in league_seasons:
        urls.append(url_for("api_league_leaders", season=season))
    for season_type, seasons in (("Regular Season", league_seasons), ("Playoffs", site.playoff_seasons() or [])):
        for season in seasons:
            urls.append(url_for("get_players", season=season, season_type=season_type))
            urls.append(url_for("export_players", season=season, season_type=season_type))
    for season in nba_client.published_seasons(("standings", nba_client.SEASON)) or []:
        urls.append(url_for("api_standings", season=season))
    team_ids = {t["id"] for t in teams.get_teams()}
    urls += [url_for("api_team", season=key[2], team_id=key[1]) for key in sorted(keys, key=repr)
             if len(key) == 3 and key[0] == "team_gamelog" and key[1] in team_ids]

    urls += [url_for("get_player_detail", player_id=pid) for pid in player_ids]
    for key in sorted(keys, key=repr):
        if len(key) == 4 and key[0] in ("player_gamelog", "shot_chart") and key[1] in player_ids:
            endpoint = "api_player_gamelog" if key[0] == "player_gamelog" else "api_shot_chart"
            urls.append(url_for(endpoint, player_id=key[1], season=key[2], season_type=key[3]))
    return urls


def _invalid_json(url, data):
    """Why a .json response browsers couldn't parse is invalid (e.g. pandas NaN), or None."""
    if not url.endswith(".json"):
        return None

    def reject(constant):
        raise ValueError(f"{constant} is not valid JSON")

    try:
        json.loads(data, parse_constant=reject)
    except ValueError as e:
        return str(e)
    return None


def _file_path(out_dir, url):
    """Where a URL's response is saved: pages as .html, data files as named."""
    path = url.strip("/") or "index"
    if not os.path.splitext(path)[1]:
        path += ".html"
    return os.path.join(out_dir, path)


def build_site(out_dir=DIST_DIR):
    """
    Write the whole site to out_dir. Returns {"out", "files", "skipped", "invalid",
    "seconds"}: skipped URLs have no data; invalid ones returned JSON browsers can't parse.
    """
    started = time.time()
    tmp_dir = out_dir.rstrip("/") + ".tmp"
    shutil.rmtree(tmp_dir, ignore_errors=True)
    shutil.copytree(os.path.join(ROOT, "static"), os.path.join(tmp_dir, "static"))

    skipped, invalid = [], {}
    with _offline() as site:
        client = site.app.test_client()
        with site.app.test_request_context():
            urls = site_urls(site)
        for url in urls:
            resp = client.get(url)
            if resp.status_code != 200:
                skipped.append(url)  # e.g. today's scoreboard was never fetched
                continue
            problem = _invalid_json(url, resp.data)
            if problem:
                invalid[url] = problem
                continue
            path = _file_path(tmp_dir, url)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(resp.data)

        # Pages serves 404.html for missing files; without one it would answer every
        # missing data file with the home page instead.
        with open(os.path.join(tmp_dir, "404.html"), "wb") as f:
            f.write(client.get("/404").data)

    total = sum(len(names) for _, _, names in os.walk(tmp_dir))
    if total > PAGES_FILE_LIMIT:
        raise RuntimeError(f"{total} files: over the Cloudflare Pages limit of {PAGES_FILE_LIMIT}")
    shutil.rmtree(out_dir, ignore_errors=True)
    os.replace(tmp_dir, out_dir)
    return {"out": out_dir, "files": total, "skipped": skipped, "invalid": invalid,
            "seconds": time.time() - started}


class _PagesHandler(http.server.SimpleHTTPRequestHandler):
    """Serves a build like Cloudflare Pages: /standings -> standings.html, else 404.html."""

    def send_head(self):
        path = self.translate_path(self.path)
        if not os.path.exists(path) and os.path.exists(path + ".html"):
            self.path = self.path.split("?", 1)[0] + ".html"
        elif not os.path.exists(path):
            self.send_response(404)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            return open(os.path.join(self.directory, "404.html"), "rb")
        return super().send_head()


def preview(out_dir=DIST_DIR, port=8080):
    """Serve a build locally until interrupted."""
    handler = partial(_PagesHandler, directory=out_dir)
    with http.server.ThreadingHTTPServer(("127.0.0.1", port), handler) as server:
        print(f"Previewing {out_dir} at http://127.0.0.1:{port} (Ctrl+C to stop)")
        server.serve_forever()


def site_digest(out_dir):
    """A hash of every file's path and contents, to tell whether a build changed anything."""
    digest = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(out_dir):
        dirnames.sort()
        for name in sorted(filenames):
            path = os.path.join(dirpath, name)
            digest.update(os.path.relpath(path, out_dir).encode() + b"\0")
            with open(path, "rb") as f:
                digest.update(hashlib.sha256(f.read()).digest())
    return digest.hexdigest()


def _load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def deploy(out_dir=DIST_DIR):
    """Upload out_dir to Cloudflare Pages (only files Pages doesn't have yet are sent)."""
    project = os.environ.get("COURTVISION_PAGES_PROJECT", "courtvision")
    wrangler = [shutil.which("wrangler")] if shutil.which("wrangler") else ["npx", "--yes", "wrangler"]
    subprocess.run(
        wrangler + ["pages", "deploy", out_dir, "--project-name", project, "--branch", "main",
                    "--commit-dirty=true"],
        check=True,
    )


@contextmanager
def _lock():
    """One build at a time: the live, hourly and nightly jobs can overlap."""
    os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
    with open(LOCK_PATH, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def build_and_deploy(out_dir=DIST_DIR, deploy_site=True, deployer=deploy) -> str:
    """Rebuild, then deploy unless the site is unchanged since the last deploy. Returns a summary line."""
    with _lock():
        summary = build_site(out_dir)
        line = f"Built {summary['files']} files in {summary['seconds']:.0f}s"
        if summary["invalid"]:
            line += f" ({len(summary['invalid'])} left out as invalid JSON; run `flask build` for details)"
        if not deploy_site:
            return line + " (not deployed)"
        digest = site_digest(out_dir)
        state = _load_state()
        if state.get("digest") == digest:
            return line + "; unchanged since the last deploy, not deploying"
        deployer(out_dir)
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        with open(STATE_PATH, "w") as f:
            json.dump({"digest": digest, "deployed_at": time.strftime("%Y-%m-%d %H:%M:%S")}, f, indent=2)
        return line + "; deployed"
