"""Plain messages for players. The technical detail goes to the log file."""

import os

from am.download import DownloadError
from am.installer import InstallError
from am.manifest import ManifestError

DETAILS = " (Details were saved to the log.)"
SYSTEM = "Windows" if os.name == "nt" else "Your system"

NOT_FOUND = ("I couldn't find WoW Forever on this PC. Click Choose folder and pick "
             "the folder where WoW Forever is installed.")
if os.name != "nt":
    NOT_FOUND = ("I couldn't find WoW Forever. Click Choose folder and pick the Battle.net "
                 "Proton folder (in Steam: .local/share/Steam/steamapps/compatdata/<number>) "
                 "or the World of Warcraft folder inside it.")
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
            if os.name != "nt":
                return ("Your system won't let me write into the WoW Forever folder. Close "
                        "WoW, then try again.")
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
        return (f"{SYSTEM} won't let the Addon Manager change files in its folder. Click Open "
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


# Going back from an installed beta to the normal version.
BETA_INSTALLED = "Beta installed"
NO_NORMAL_YET = "There's no normal version of this addon yet."
GO_BACK_TITLE = "Go back to the normal version?"
GO_BACK_OK = "Go back"
GO_BACK_CANCEL = "Cancel"


def go_back_button(normal_text):
    return f"Use {normal_text}"


def go_back_text(name, beta_text, normal_text):
    return (f"{name} {beta_text} (beta) will be replaced by the normal version "
            f"{normal_text}. Your {name} settings are kept.")


# Removing an addon (the row's Uninstall button / right-click Remove).
BIN_NAME = "Recycle Bin" if os.name == "nt" else "Trash"
REMOVE_TIP = "Remove this addon"
REMOVE_RIGHT_CLICK = "Right-click the row to remove."
REMOVED = "Removed"
REMOVING = "Removing..."
REMOVE_OK = "Remove"
DELETE_OK = "Delete"
REMOVE_CANCEL = "Cancel"


def remove_menu_label(name):
    return f"Remove {name}..."


def remove_title(name):
    return f"Remove {name}?"


def remove_text(name, where, needed_by=()):
    """The question before removing. where: "bin"/"trash" or None (deleted for good)."""
    if where:
        first = f"The {name} folder is moved to the {BIN_NAME}."
    else:
        first = (f"This drive has no {BIN_NAME}, so the {name} folder will be deleted "
                 "for good.")
    text = (f"{first} Your settings for it in the game are kept, so if you install it again "
            "later, it is set up like before. Install/update all won't put it back; use its "
            "Install button if you want it again.")
    if needed_by:
        names = ", ".join(needed_by)
        verb = "needs" if len(needed_by) == 1 else "need"
        text += f"\n\n{names} {verb} {name} and will stop working."
    return text


def removed_message(name, where):
    place = f" (it's in the {BIN_NAME})" if where else ""
    return (f"{name} was removed{place}. If WoW is running, type /reload or restart it.")


def remove_message(exc, name):
    """Message when removing the addon called `name` didn't work (nothing was changed)."""
    kind = getattr(exc, "kind", "")
    if kind == "locked":
        return (f"Some files of {name} are in use, so nothing was removed. Close WoW (or "
                "the program using them) and try again.")
    if kind == "permission":
        return (f"{SYSTEM} didn't give me permission to change the {name} folder, so nothing "
                "was removed. Check that folder's permissions (or whether it's protected), "
                "then try again.")
    if kind == "partly":
        return (f"{name} was only partly deleted: some of its files couldn't be deleted. "
                "WoW won't load what's left, and I'll try to delete the rest the next time "
                "the Addon Manager starts.")
    if kind == "link":
        return (f"The {name} folder is a link to somewhere else (a developer setup), "
                "so I left it alone.")
    if kind == "not_guild":
        return f"I only remove the guild's addons, so I left {name} alone."
    if kind == "missing":
        return f"{name} isn't installed any more."
    if kind == "bad_folder":
        return FOLDER_GONE
    if kind == "no_trash":
        return (f"{name} couldn't be moved to the {BIN_NAME}, so nothing was removed. "
                "Try again.")
    return (f"{name} couldn't be removed, so nothing was changed. Try again; if it keeps "
            "happening, close WoW first." + DETAILS)


def success_message(name, version_text):
    return f"{name} {version_text} installed. Restart WoW or type /reload to use it."


# ---------- Backup and restore (Settings window) ----------
BACKUP_HEADING = "Backup"
BACKUP_BUTTON = "Make a backup..."
RESTORE_BUTTON = "Restore a backup..."
BACKUP_NOTE = "Your addons and their settings in one file, to use on another PC."
BACKUP_TITLE = "Make a backup"
BACKUP_SETTINGS = "Addon settings (key bindings and macros too)"
BACKUP_ADDONS = "Addons themselves"
BACKUP_PRIVATE = ("This file contains your account and character names and addon data. "
                  "Don't share it.")
BACKUP_CLOUD = "To keep a copy online, save it in a folder your cloud app syncs:"
BACKUP_PICK_ONE = "Tick at least one of the two."
BACKUP_MAKE = "Make backup"
BACKUP_MAKING = "Making the backup..."
BACKUP_OPEN_FOLDER = "Open folder"
BACKUP_CHANGE = "Change..."
CLOSE = "Close"
CANCEL = "Cancel"
GAME_OPEN = "Close WoW first, then try again."
RESTORE_TITLE = "Restore a backup"
RESTORE_ADDONS = "Addons"
RESTORE_SETTINGS = "Addon settings"
RESTORE_BINDINGS = "Key bindings and macros"
RESTORE_MERGE = "Addons and settings you have here that are not in the backup are kept."
RESTORE_SAFETY = "A safety copy of what gets replaced is saved first."
RESTORE_OK = "Restore"
RESTORING = "Restoring..."
RESTORE_SKIP = "(skip)"
RESTORE_PICK_FILE = "Choose a backup to restore"
RESTORE_UNDONE = ("A restore was cut off last time, so it was undone: your addons and "
                  "settings are as they were before it. You can restore again.")
FOUND_HEADING = "Backups found (newest first):"
FOUND_LOOKING = "Looking for backups..."
FOUND_NONE = "No backups found in your backup folders. Use Browse... to pick one."
FOUND_SAFETY = "Show safety copies (made before a restore)"
FOUND_NOTE = "Click a backup to restore it. Files marked online download first."
FOUND_BROWSE = "Browse..."
FOUND_OPENING = "Opening the backup..."
FOUND_DOWNLOADING = "Downloading the backup from your cloud first..."
FOUND_MORE = "Older backups: use Browse..."
FOUND_SAFETY_HEADING = "Safety copies (made before a restore):"
PLACE_DOCUMENTS = "Documents"
ONLINE_MARK = "online"
NO_ACCOUNT = ("No account folder on this PC yet: log in to the game once, close it, then "
              "restore to get your settings back.")


def size_text(size):
    """Bytes as a short 'NN MB' text."""
    if size < 1024 * 1024:
        return f"{max(1, round(size / 1024))} KB"
    return f"{size / (1024 * 1024):.0f} MB"


def addons_label(size):
    return f"{BACKUP_ADDONS} (about {size_text(size)})" if size else BACKUP_ADDONS


def saved_to(folder):
    return f"Saved to: {folder}"


def backup_saved(name, size):
    return f"Backup saved: {name} ({size_text(size)})."


def backup_from(info):
    """'From: 2026-11-10, 23 addons, settings for 1 account, 4 characters'."""
    parts = [info.created.split(" ")[0] if info.created else "an unknown date"]
    n = len(info.addons)
    if n:
        parts.append(f"{n} addon{'s' if n != 1 else ''}")
    a = len(info.accounts)
    if a:
        parts.append(f"settings for {a} account{'s' if a != 1 else ''}")
        c = info.characters
        if c:
            parts.append(f"{c} character{'s' if c != 1 else ''}")
    head = "Safety copy from" if info.kind == "safety" else "From"
    return f"{head}: " + ", ".join(parts)


def found_row(when, size, place, online=False):
    """One Backups found row: '2026-11-10 21:30 · 12 MB · OneDrive (online)'."""
    return " · ".join((when, size_text(size), f"{place} ({ONLINE_MARK})" if online else place))


def account_line(name):
    return f'Settings of account "{name}" go to:'


def restore_confirm(addons, files, folder):
    """The yes/no question before a restore."""
    what = []
    if addons:
        what.append(f"{addons} addon{'s' if addons != 1 else ''}")
    if files:
        what.append(f"{files} settings file{'s' if files != 1 else ''}")
    return (f"This replaces {' and '.join(what)} here with the ones from the backup. "
            f"Everything else stays. A safety copy of what gets replaced is saved first in "
            f"{folder}.")


def restored_message(result):
    text = "Restored."
    if result.safety:
        text += f" Safety copy: {result.safety}"
    return text + " Restart WoW to use them."


def backup_message(exc):
    """Backup or restore didn't work (nothing was changed)."""
    kind = getattr(exc, "kind", "")
    if kind == "game":
        return GAME_OPEN
    if kind in ("not_backup", "bad_zip"):
        return "That file isn't an Addon Manager backup (or it is damaged). Nothing was changed."
    if kind == "newer_format":
        return "That backup was made by a newer Addon Manager. Update this one first."
    if kind == "unsafe":
        return ("That backup holds files in places it shouldn't, so I didn't use it. "
                "Nothing was changed." + DETAILS)
    if kind == "nothing":
        return "There was nothing to back up or restore."
    if kind == "inside_game":
        return "Pick a folder outside the WoW Forever folder for backups."
    if kind == "no_account":
        return NO_ACCOUNT
    if kind == "same_account":
        return ("Two accounts from the backup would go to the same account here. Pick a "
                "different account for each, or (skip) one of them.")
    if kind == "bad_folder":
        return FOLDER_GONE
    if kind == "locked":
        return ("Some files are in use, so nothing was changed. Close WoW (or the program "
                "using them) and try again.")
    if kind == "permission":
        return (f"{SYSTEM} won't let me write there, so nothing was changed. Pick another "
                "folder or close WoW and try again.")
    if kind == "disk_full":
        return "The disk is full, so nothing was changed. Free some space and try again."
    return "That didn't work, so nothing was changed. Try again." + DETAILS
