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


# A test that hangs ends the run with its name and every thread's stack (instead of the
# CI running silently until its time limit). ADDONMANAGER_TEST_LIMIT: seconds per test
# (default 300; the slowest test takes well under a minute), 0 = off.
import faulthandler  # noqa: E402
import sys  # noqa: E402
import threading  # noqa: E402
import unittest  # noqa: E402

try:
    TEST_LIMIT = float(os.environ.get("ADDONMANAGER_TEST_LIMIT", "300"))
except ValueError:
    TEST_LIMIT = 300.0
_plain_run = unittest.TestCase.run


def _hung(test_id):
    try:
        sys.stderr.write(f"\n\nTEST HUNG: {test_id} ran over {TEST_LIMIT:g} s. Stacks:\n")
        sys.stderr.flush()
        faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
        sys.stderr.flush()
    finally:
        os._exit(3)


def _run_with_watchdog(self, result=None):
    if TEST_LIMIT <= 0:
        return _plain_run(self, result)
    timer = threading.Timer(TEST_LIMIT, _hung, args=(self.id(),))
    timer.daemon = True
    timer.start()
    # The C-level backstop, in case no Python thread gets to run.
    faulthandler.dump_traceback_later(TEST_LIMIT + 60, exit=True)
    try:
        return _plain_run(self, result)
    finally:
        timer.cancel()
        faulthandler.cancel_dump_traceback_later()


unittest.TestCase.run = _run_with_watchdog
