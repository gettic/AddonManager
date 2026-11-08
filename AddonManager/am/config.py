"""Settings file, log file and the default addon list address."""

import json
import logging
import logging.handlers
import os

# Where the guild's addon list (manifest.json) lives. Hosting is decided later:
# while this is empty the app shows "No addon list is set up yet".
DEFAULT_MANIFEST_URL = ""

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


def load_settings():
    """Return the saved settings as a dict ({} if missing or unreadable)."""
    try:
        with open(settings_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


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
