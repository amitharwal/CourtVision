"""
Production server settings:  gunicorn app:app -c gunicorn.conf.py

Every worker shares the on-disk cache (instance/cache.sqlite3). At startup the
master launches `flask warm-cache` as a separate process to fill it, so first page
loads don't wait on the NBA API. (A separate process, not a thread: threads doing
network I/O while gunicorn forks workers can crash or deadlock them.)
Set COURTVISION_WARM=0 to skip warming.
"""
import os
import subprocess
import sys

bind = f"0.0.0.0:{os.environ.get('PORT', '8000')}"
workers = int(os.environ.get("WEB_CONCURRENCY", "2"))
threads = int(os.environ.get("GUNICORN_THREADS", "4"))
# NBA API calls can take a while on a cold cache; don't kill requests too early.
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "90"))
accesslog = "-"


def when_ready(server):
    if os.environ.get("COURTVISION_WARM", "1") == "0":
        return
    project_dir = os.path.dirname(os.path.abspath(__file__))
    subprocess.Popen([sys.executable, "-m", "flask", "--app", "app", "warm-cache"], cwd=project_dir)
    server.log.info("Started cache warming in the background")
