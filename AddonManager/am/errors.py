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


def only_other_clients(others):
    """WoW was found, but only other game versions (beta, PTR, Classic, Retail...)."""
    shown = ", ".join(others[:3]) + (", ..." if len(others) > 3 else "")
    return (f"I found WoW on this PC ({shown}) but no WoW Forever. Click Choose folder "
            "and pick your WoW Forever folder.")


def not_forever(client):
    """The player picked a folder that looks like another game version."""
    return (f"Note: {client} looks like a different WoW version (not WoW Forever). "
            "If that's wrong, click Change... and pick your WoW Forever folder.")


def switched_folder(old, new):
    return (f"Switched to your WoW Forever folder {new} (it was set to {old}, "
            "which is a different WoW version).")


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


def release_message(exc, name):
    """(short status text, full message) when an addon's latest release can't be read."""
    kind = getattr(exc, "kind", "")
    if kind == "rate_limited":
        minutes = max(1, (int(getattr(exc, "wait", 0) or 0) + 59) // 60)
        return ("Can't check now", "GitHub is limiting update checks from this PC. Try again "
                f"in {minutes} minute{'s' if minutes != 1 else ''}.")
    if kind == "offline":
        return ("Can't check now",
                "Can't reach the internet. Check your connection and click Check again.")
    if kind == "timeout":
        return ("Can't check now", "The download server isn't answering. Try again in a few minutes.")
    if kind == "no_release":
        return ("No release yet", f"{name} has no release to download yet.")
    if kind == "no_asset":
        return ("No download yet", f"{name}'s newest release has no download yet. "
                "Try again later, or tell the guild officer.")
    return ("Can't check now", f"{name}'s newest release can't be read right now. Try again "
            "later, or tell the guild officer." + DETAILS)


def install_message(exc, name):
    """Message for a failed install of the addon called `name`."""
    kind = getattr(exc, "kind", "")
    if isinstance(exc, DownloadError):
        if kind == "offline":
            return "Can't reach the internet. Check your connection and click Check again."
        if kind == "timeout":
            return "The download server isn't answering. Try again in a few minutes."
        if kind == "rate_limited":
            return release_message(exc, name)[1]
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


# ---------- the Addon Manager's own update (bar text: no game or company names) ----------
def app_update_bar(version_text):
    return f"Addon Manager {version_text} is out."


APP_DOWNLOADING = "Downloading the Addon Manager update..."
APP_RESTARTING = "Starting the new Addon Manager..."
APP_CANT_HERE_BAR = "This copy can't update itself where it is. Get the new version here:"
APP_DOWNLOAD_BUTTON = "Open download page"


def app_update_message(exc):
    """Message line text when the app's own update didn't work (the old version stays)."""
    kind = getattr(exc, "kind", "")
    if kind == "in_wow":
        return ("The Addon Manager is inside your game folder, so it won't update itself "
                "there. Click Open download page, and keep the new one outside the game folder.")
    if kind == "read_only":
        return ("Windows won't let the Addon Manager change files in its folder. Click Open "
                "download page to get the new version.")
    if kind == "offline":
        return "Can't reach the internet. Check your connection and try again."
    if kind == "timeout":
        return "The download server isn't answering. Try again in a few minutes."
    if kind == "rate_limited":
        return "GitHub is limiting downloads from this PC. Try again in a few minutes."
    if kind == "damaged":
        return ("The update download was damaged or doesn't match, so nothing was changed. "
                "Try again later.")
    return ("The update didn't finish. Your current version still works. Try again later."
            + DETAILS)


def app_rolled_back(version_text):
    return (f"The new Addon Manager {version_text} didn't start, so your old version is back. "
            "That version won't be offered again." + DETAILS)


def app_updated(version_text):
    return f"Addon Manager updated to {version_text}."


def success_message(name, version_text):
    return f"{name} {version_text} installed. Restart WoW or type /reload to use it."
