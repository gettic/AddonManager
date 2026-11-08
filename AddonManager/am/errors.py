"""Plain messages for players. The technical detail goes to the log file."""

from am.download import DownloadError
from am.installer import InstallError
from am.manifest import ManifestError

DETAILS = " (Details were saved to the log.)"

NOT_FOUND = ("I couldn't find WoW Forever on this PC. Click Choose folder and pick "
             "the folder where WoW Forever is installed.")
NOT_WOW = ("That folder doesn't look like WoW Forever. Pick the folder that contains "
           "the game (the one with the Interface and WTF folders).")
CHOOSE_CLIENT = ("That folder holds more than one game version. Open it and pick the "
                 "WoW Forever folder inside it.")
FOLDER_GONE = "WoW Forever isn't where it was last time. Please choose its folder again."
GAME_RUNNING = ("WoW Forever is running. You can still install: afterwards type /reload "
                "in the game or restart it to use the new versions.")


def manifest_message(exc):
    kind = getattr(exc, "kind", "")
    if kind == "not_set":
        return "No addon list is set up yet."
    if kind == "unreachable":
        return "Can't reach the internet. Check your connection and click Check again."
    if kind == "timeout":
        return "The download server isn't answering. Try again in a few minutes."
    if kind == "too_new":
        return ("This Addon Manager is too old for the guild's addon list. "
                "Please download the newest one.")
    return ("The guild's addon list can't be read right now. Try again later, "
            "or tell the guild officer." + DETAILS)


def install_message(exc, name):
    """Message for a failed install of the addon called `name`."""
    kind = getattr(exc, "kind", "")
    if isinstance(exc, DownloadError):
        if kind == "offline":
            return "Can't reach the internet. Check your connection and click Check again."
        if kind == "timeout":
            return "The download server isn't answering. Try again in a few minutes."
        if kind in ("checksum", "too_big"):
            return _damaged(name)
        return (f"The download for {name} isn't available right now. Try again later, "
                "or tell the guild officer." + DETAILS)
    if isinstance(exc, InstallError):
        if kind in ("unsafe", "bad_zip", "mismatch"):
            return _damaged(name)
        if kind == "permission":
            return ("Windows won't let me write into the WoW Forever folder. Close WoW, then "
                    "try again. If it still fails, right-click Addon Manager and choose "
                    "Run as administrator.")
        if kind == "locked":
            return f"Some files of {name} are in use. Close WoW and try again."
        if kind == "disk_full":
            return "Your disk is full. Free some space and try again."
        if kind == "bad_folder":
            return FOLDER_GONE
        if kind == "link":
            return (f"The {name} folder is a link to somewhere else (a developer setup), "
                    "so I left it alone.")
    if isinstance(exc, ManifestError):
        return manifest_message(exc)
    return (f"Something went wrong while installing {name}. Your old version is still "
            "there." + DETAILS)


def _damaged(name):
    return (f"The download for {name} was damaged or doesn't match. Nothing was changed. "
            "Try again; if it keeps happening, tell the guild officer.")


def success_message(name, version_text):
    return f"{name} {version_text} installed. Restart WoW or type /reload to use it."
