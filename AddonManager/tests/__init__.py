"""Tests run with: python -m unittest discover -s tests -t .   (from AddonManager\\)

Settings and log always go to a temp folder, never the real %APPDATA%."""

import atexit
import logging
import os
import shutil
import tempfile

logging.getLogger().addHandler(logging.NullHandler())  # keep test output quiet

if os.environ.get("ADDONMANAGER_TEST_TMP"):
    os.makedirs(os.environ["ADDONMANAGER_TEST_TMP"], exist_ok=True)
os.environ["ADDONMANAGER_HOME"] = tempfile.mkdtemp(
    prefix="v1core_home_", dir=os.environ.get("ADDONMANAGER_TEST_TMP"))
atexit.register(shutil.rmtree, os.environ["ADDONMANAGER_HOME"], True)
# Backups never go into the real Documents folder.
os.environ["ADDONMANAGER_DOCUMENTS"] = tempfile.mkdtemp(
    prefix="backup_restore_docs_", dir=os.environ.get("ADDONMANAGER_TEST_TMP"))
atexit.register(shutil.rmtree, os.environ["ADDONMANAGER_DOCUMENTS"], True)
# The player's clipboard is never read (Receive pre-fills a code; tests pass their own).
os.environ["ADDONMANAGER_NO_CLIPBOARD"] = "1"

# Tests never go online: no real addon list, and "GitHub" is a closed local port.
from am import config  # noqa: E402

REAL_MANIFEST_URL = config.DEFAULT_MANIFEST_URL
REAL_GITHUB_API = config.GITHUB_API
config.DEFAULT_MANIFEST_URL = ""
config.GITHUB_API = "https://127.0.0.1:9"
# ...and no usage ping ever goes out (stats tests pass their own local server).
REAL_STATS_URL = config.STATS_URL
config.STATS_URL = ""
# ...and no backup is ever sent to the real relay (share tests use tests\fake_relay.py).
REAL_SHARE_URL = config.SHARE_URL
config.SHARE_URL = ""
# ...and nothing ever goes to a real Send server (tests\fake_send.py stands in).
from am import share_send  # noqa: E402

REAL_SEND_URLS = {k: s.url for k, s in share_send.SITES.items()}
for _site in share_send.SITES.values():
    _site.url = ""
