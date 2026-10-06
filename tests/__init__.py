# Test isolation (set before app/nba_client are imported):
# - keep tests off the real on-disk cache
# - hosted mode, so any NBA API call a test forgets to mock fails fast instead of
#   reaching stats.nba.com
import os

os.environ.setdefault("COURTVISION_CACHE", "off")
os.environ.setdefault("COURTVISION_OFFLINE", "1")
