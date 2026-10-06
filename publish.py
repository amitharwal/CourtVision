"""
Publish cached NBA data to a hosted Court Vision (one running with COURTVISION_OFFLINE=1).

The fetcher runs wherever stats.nba.com is reachable (e.g. your own computer), fills
its local cache with nba_client.warm_cache, then sends every entry that changed since
the last publish to the host's /api/admin/cache-entries endpoint. Values travel as the
JSON codec from nba_client (never pickle), gzip-compressed, in batches.

Use it through the CLI:  flask --app app publish --url https://your-site --players
"""
import gzip
import json
import os
import time
import zlib

import requests

import nba_client

STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "instance", "publish_state.json")
BATCH_BYTES = 4 * 1024 * 1024  # uncompressed JSON per request
ENDPOINT = "/api/admin/cache-entries"


def _load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


def _batches(entries):
    """Group (key, ts, blob) rows into lists of JSON-ready dicts of ~BATCH_BYTES each."""
    batch, size = [], 0
    for key_text, ts, blob in entries:
        item = {"key": key_text, "ts": ts, "value": json.loads(zlib.decompress(blob))}
        item_size = len(json.dumps(item["value"], separators=(",", ":")))
        if batch and size + item_size > BATCH_BYTES:
            yield batch
            batch, size = [], 0
        batch.append(item)
        size += item_size
    if batch:
        yield batch


def publish(url: str, token: str, full: bool = False, echo=print) -> dict:
    """
    Send cache entries newer than the last successful publish to `url`.
    full=True resends everything (e.g. for a freshly deployed host).
    """
    if nba_client._DISK is None:
        raise RuntimeError("publishing needs the disk cache (COURTVISION_CACHE is off)")

    url = url.rstrip("/")
    state = _load_state()
    since = 0.0 if full else state.get(url, 0.0)
    sent = stored = 0
    newest = since

    for batch in _batches(nba_client._DISK.entries_since(since)):
        body = gzip.compress(json.dumps(batch, separators=(",", ":")).encode())
        resp = requests.post(
            url + ENDPOINT,
            data=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Content-Encoding": "gzip",
            },
            timeout=120,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"{url} rejected the upload: HTTP {resp.status_code} {resp.text[:200]}")
        sent += len(batch)
        stored += resp.json().get("stored", 0)
        newest = max(newest, max(item["ts"] for item in batch))
        # Record progress after every batch so an interrupted run resumes where it stopped.
        state[url] = newest
        _save_state(state)
        echo(f"  sent {sent} entries ({len(body) / 1e6:.1f} MB batch)")

    return {"sent": sent, "stored": stored, "published_through": time.ctime(newest) if newest else None}
