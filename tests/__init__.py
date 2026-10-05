# Keep tests off the real on-disk cache (set before app/nba_client are imported).
import os

os.environ.setdefault("COURTVISION_CACHE", "off")
