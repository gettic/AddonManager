"""Settings file, log file and the default addon list address."""

import json
import logging
import logging.handlers
import os

# Where the guild's addon list lives: addons.json in the public AddonManager repo.
# Adding an addon = add an entry there and push; no new .exe needed.
DEFAULT_MANIFEST_URL = "https://raw.githubusercontent.com/gettic/AddonManager/main/addons.json"

# Used when addons.json can't be read. Keep it the same as addons.json at the repo root.
BUILTIN_LIST = {
    "format": 1,
    "title": "Guild Addons",
    "addons": [
        {"folder": "GuildHub", "name": "Guild Hub", "repo": "gettic/GuildHub"},
        {"folder": "TaxiTime", "name": "Taxi Time", "repo": "gettic/TaxiTime"},
    ],
}

# The only place addon versions and downloads come from (tests may point elsewhere).
GITHUB_API = "https://api.github.com"

# The app's own releases (self-update). Fixed here: never taken from addons.json.
APP_REPO = "gettic/AddonManager"
APP_ASSET = "AddonManager.exe"

HOME_ENV = "ADDONMANAGER_HOME"


def home_dir():
    """Folder for settings and log: $ADDONMANAGER_HOME or %APPDATA%\\AddonManager."""
    path = os.environ.get(HOME_ENV)
    if not path:
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        path = os.path.join(base, "AddonManager")
    return path


def settings_path():
    return os.path.join(home_dir(), "settings.json")


def log_path():
    return os.path.join(home_dir(), "log.txt")


def cache_path():
    return os.path.join(home_dir(), "cache.json")


# Known settings and their type. A value of another type (a hand-edited or damaged
# settings.json) is dropped on load, as if it had never been saved.
SETTING_TYPES = {"wow_folder": str, "wow_folder_source": str, "manifest_url": str,
                 "update_failed": str, "known_addons": list}


def load_settings():
    """Return the saved settings as a dict ({} if missing or unreadable). Known keys
    with a wrong type (or empty text) are left out."""
    try:
        with open(settings_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    clean = {}
    for key, value in data.items():
        want = SETTING_TYPES.get(key)
        if want is None or (isinstance(value, want) and (want is not str or value.strip())):
            clean[key] = value
        else:
            logging.getLogger(__name__).warning("Ignoring bad setting %s=%r", key,
                                                str(value)[:80])
    return clean


def save_settings(settings):
    """Write settings atomically. Returns False (and logs) if it can't."""
    path = settings_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)
        os.replace(tmp, path)
        return True
    except OSError:
        logging.getLogger(__name__).exception("Could not save settings")
        return False


def manifest_url(settings, override=None):
    """The addon list address: command line, then settings, then the built-in default."""
    if override:
        return override
    return settings.get("manifest_url") or DEFAULT_MANIFEST_URL


def setup_logging():
    """Log to a small rotating file in home_dir(). Never fails."""
    root = logging.getLogger()
    if any(getattr(h, "_am_handler", False) for h in root.handlers):
        return
    try:
        os.makedirs(home_dir(), exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            log_path(), maxBytes=256 * 1024, backupCount=1, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        handler._am_handler = True
        root.addHandler(handler)
        root.setLevel(logging.INFO)
    except OSError:
        pass
