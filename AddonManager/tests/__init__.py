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
