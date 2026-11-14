"""Plain messages for players. The technical detail goes to the log file."""

import os

from am.download import DownloadError
from am.installer import InstallError
from am.manifest import ManifestError

# Where the details are: Settings has an Open log folder link (no right-click needed).
DETAILS = " (Details: Settings > Open log folder.)"
SYSTEM = "Windows" if os.name == "nt" else "Your system"
THIS_PC = "this PC" if os.name == "nt" else "this computer"
YOUR_PC = "your PC" if os.name == "nt" else "your computer"

NOT_FOUND = ("I couldn't find WoW Forever on this PC. Click Choose folder and pick "
             "the folder where WoW Forever is installed.")
if os.name != "nt":
    NOT_FOUND = ("I couldn't find WoW Forever on this computer. Click Choose folder and pick "
                 "the Battle.net "
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
    return (f"I found WoW on {THIS_PC} ({shown}) but no WoW Forever. Click Choose folder "
            "and pick your WoW Forever folder.")


def not_forever(client):
    """The player picked a folder that looks like another game version."""
    return (f"Note: {client} looks like a different WoW version (not WoW Forever). "
            "If that's wrong, click Change... and pick your WoW Forever folder.")


def switched_folder(old, new):
    return (f"Switched to your WoW Forever folder {new} (it was set to {old}, "
            "which is a different WoW version).")


# One wording for every "WoW must load it" hint (installs, uninstalls, Add file...).
RELOAD = "Type /reload in WoW (or restart it)"
RELOAD_LOWER = "type /reload in WoW (or restart it)"
GAME_RUNNING = (f"WoW Forever is running. You can still install, then {RELOAD_LOWER} "
                "to use the new versions.")


# The secure connection to the download server couldn't be checked (no certificates found
# here, or a wrong clock); Send / Receive has its own SHARE_CERTIFICATE.
CERTIFICATE = ("Can't make a safe connection to the download server. Check that your "
               "computer's date and time are right, then try again. If it keeps happening, "
               "tell a guild officer.")


def manifest_message(exc):
    kind = getattr(exc, "kind", "")
    if kind == "not_set":
        return "No addon list is set up yet."
    if kind == "unreachable":
        return "Can't reach the internet. Check your connection and click Check again."
    if kind == "timeout":
        return "The download server isn't answering. Try again in a few minutes."
    if kind == "certificate":
        return CERTIFICATE
    if kind == "too_new":
        return ("This Addon Manager is too old for the guild's addon list. "
                "Please download the newest one.")
    return ("The guild's addon list can't be read right now. Try again later, "
            "or tell a guild officer." + DETAILS)


def release_message(exc, name):
    """(short status text, full message) when an addon's latest release can't be read."""
    kind = getattr(exc, "kind", "")
    if kind == "rate_limited":
        minutes = max(1, (int(getattr(exc, "wait", 0) or 0) + 59) // 60)
        return ("Can't check now", f"Too many update checks from {THIS_PC} for now. "
                f"Try again in {minutes} minute{'s' if minutes != 1 else ''}.")
    if kind == "offline":
        return ("Can't check now",
                "Can't reach the internet. Check your connection and click Check again.")
    if kind == "timeout":
        return ("Can't check now", "The download server isn't answering. Try again in a few minutes.")
    if kind == "certificate":
        return ("Can't check now", CERTIFICATE)
    if kind == "no_release":
        return ("Not out yet", f"{name} has no release to download yet.")
    if kind == "no_asset":
        return ("No download yet", f"{name}'s newest release has no download yet. "
                "Try again later, or tell a guild officer.")
    return ("Can't check now", f"{name}'s newest release can't be read right now. Try again "
            "later, or tell a guild officer." + DETAILS)


def install_message(exc, name):
    """Message for a failed install of the addon called `name`."""
    kind = getattr(exc, "kind", "")
    if isinstance(exc, DownloadError):
        if kind == "offline":
            return "Can't reach the internet. Check your connection and click Check again."
        if kind == "timeout":
            return "The download server isn't answering. Try again in a few minutes."
        if kind == "certificate":
            return CERTIFICATE
        if kind == "rate_limited":
            return release_message(exc, name)[1]
        if kind in ("checksum", "too_big"):
            return _damaged(name)
        return (f"The download for {name} isn't available right now. Try again later, "
                "or tell a guild officer." + DETAILS)
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
            "Try again; if it keeps happening, tell a guild officer.")


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
    if kind == "certificate":
        return CERTIFICATE
    if kind == "rate_limited":
        return f"Too many downloads from {THIS_PC} for now. Try again in a few minutes."
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
    # "v1.4.0-beta.2" already says beta: no second "(beta)" after it.
    mark = "" if "beta" in str(beta_text).lower() else " (beta)"
    return (f"{name} {beta_text}{mark} will be replaced by the normal version "
            f"{normal_text}. Your {name} settings are kept.")


# Uninstalling an addon (the row's Uninstall button / right-click Uninstall): one name.
BIN_NAME = "Recycle Bin" if os.name == "nt" else "Trash"
REMOVE_TIP = "Uninstall this addon."
REMOVE_RIGHT_CLICK = "Right-click the row to uninstall."
if os.name != "nt":   # a touch screen: the row's name opens the menu with press-and-hold
    REMOVE_RIGHT_CLICK = "Right-click the row (or press and hold its name) to uninstall."
REMOVED = "Uninstalled"
REMOVING = "Uninstalling..."
REMOVE_OK = "Uninstall"
DELETE_OK = "Delete"
REMOVE_CANCEL = "Cancel"


def remove_menu_label(name):
    return f"Uninstall {name}..."


def remove_title(name):
    return f"Uninstall {name}?"


# The main window's "all" button: Update all when at least one installed addon has an
# update, otherwise Install all (also when nothing is left to do: then it is greyed out).
# Both short, so the bottom row (with Backup) fits the smallest window on Linux too.
ALL_INSTALL = "Install all"
ALL_UPDATE = "Update all"
# Tooltips on the main window's buttons while they are greyed out: why.
NOTHING_TO_DO_TIP = "Nothing to install or update."
BUSY_TIP = "Wait for the current job to finish."
NO_FOLDER_TIP = "Choose your WoW Forever folder first (Choose folder above)."
BACKUP_NO_FOLDER_TIP = ("Choose your WoW Forever folder first (Choose folder in the main "
                        "window).")
CHECK_TIP = "Look for addon updates again (F5)."


def list_summary(updates, missing):
    """The message line after a check that found nothing else to say."""
    parts = []
    if updates:
        parts.append(f"{updates} update{'s' if updates != 1 else ''} available.")
    if missing:
        parts.append("1 addon isn't installed yet." if missing == 1
                     else f"{missing} addons aren't installed yet.")
    return " ".join(parts) or "All addons are up to date."


def remove_text(name, where, needed_by=(), from_file=False):
    """The question before removing. where: "bin"/"trash" or None (deleted for good).
    from_file: an addon installed with Add file... (it has no Install button)."""
    if where:
        first = f"{name} goes to the {BIN_NAME}."
    else:
        first = f"This drive has no {BIN_NAME}, so {name} will be deleted for good."
    text = f"{first} Its in-game settings are kept for if you install it again."
    if from_file:
        text += " To get it back, use Add file... again."
    else:
        text += (f" {ALL_INSTALL} and {ALL_UPDATE} will skip it; use its Install button to "
                 "get it back.")
    if needed_by:
        names = ", ".join(needed_by)
        verb = "needs" if len(needed_by) == 1 else "need"
        text += f"\n\n{names} {verb} {name} and will stop working."
    return text


def removed_message(name, where):
    place = f" (it's in the {BIN_NAME})" if where else ""
    return (f"{name} was uninstalled{place}. If WoW is running, {RELOAD_LOWER}.")


def remove_message(exc, name):
    """Message when uninstalling the addon called `name` didn't work (nothing was changed)."""
    kind = getattr(exc, "kind", "")
    if kind == "locked":
        return (f"Some files of {name} are in use, so nothing was uninstalled. Close WoW (or "
                "the program using them) and try again.")
    if kind == "permission":
        return (f"{SYSTEM} didn't give me permission to change the {name} folder, so nothing "
                f"was uninstalled. Close WoW and try again; if it still fails, restart "
                f"{YOUR_PC}.")
    if kind == "partly":
        return (f"{name} was only partly deleted: some of its files couldn't be deleted. "
                "WoW won't load what's left, and the rest is deleted next time the app "
                "starts.")
    if kind == "link":
        return (f"The {name} folder is a link to somewhere else (a developer setup), "
                "so I left it alone.")
    if kind == "not_guild":
        return f"I only uninstall the guild's addons, so I left {name} alone."
    if kind == "missing":
        return f"{name} isn't installed any more."
    if kind == "bad_folder":
        return FOLDER_GONE
    if kind == "no_trash":
        return (f"{name} couldn't be moved to the {BIN_NAME}, so nothing was uninstalled. "
                "Try again.")
    return (f"{name} couldn't be uninstalled, so nothing was changed. Try again; if it keeps "
            "happening, close WoW first." + DETAILS)


def success_message(name, version_text):
    return f"{name} {version_text} installed. {RELOAD} to use it."


def installed_list(names):
    """Start of the message line when some addons installed and others failed."""
    return "Installed: " + ", ".join(names) + "."


# ---------- Backup and restore (the Backup window, from the main window's Backup button) ----------
BACKUP_HEADING = "Backup"          # the main window's button and the Backup window's title
BACKUP_BUTTON = "Make a backup..."
RESTORE_BUTTON = "Restore a backup..."
BACKUP_NOTE = "Your addons and their settings in one file, to keep safe or use on another PC."
BACKUP_THIS_PC = "This PC"
BACKUP_OTHER_PC = "Another PC"
BACKUP_LAST_LOOKING = "Last backup: looking..."
BACKUP_LAST_NONE = "Last backup: none yet"


def days_ago(then, now=None):
    """'today', 'yesterday', '4 days ago' (calendar days, local time); '' for a date in
    the future or one that can't be read."""
    import datetime
    import time as _time
    try:
        a = datetime.date.fromtimestamp(then)
        b = datetime.date.fromtimestamp(_time.time() if now is None else now)
    except (OSError, ValueError, OverflowError, TypeError):
        return ""
    days = (b - a).days
    if days < 0:
        return ""
    if days == 0:
        return "today"
    return "yesterday" if days == 1 else f"{days} days ago"


def backup_last(when, place, ago=""):
    return f"Last backup: {when}, {ago} ({place})" if ago else f"Last backup: {when} ({place})"



BACKUP_TITLE = "Make a backup"
BACKUP_SETTINGS = "Addon settings (key bindings and macros too)"
BACKUP_ADDONS = "The addons themselves"
BACKUP_PRIVATE = ("This file contains your account and character names and addon data. "
                  "Don't share it.")
BACKUP_CLOUD = "To keep a copy online, save it in a folder your cloud app syncs:"
BACKUP_PICK_ONE = "Tick addon settings, the addons themselves, or both."
BACKUP_MAKE = "Make backup"
BACKUP_MAKING = "Making the backup..."


BACKUP_STOPPING = "Stopping..."
BACKUP_STOPPED = "Backup stopped. Nothing was saved."


def backup_making(percent, left_out=False):
    """left_out: the Spoken addons were left out (the note stays with the %)."""
    return f"Making the backup... {percent} %." + (" " + SPOKEN_LEFT_OUT if left_out else "")


def restoring(percent):
    return f"Restoring... {percent} %"


BACKUP_OPEN_FOLDER = "Open folder"           # Make a backup, after saving (its own folder)
BACKUP_OPEN_BACKUPS = "Open backup folder"    # the Backup window, beside Old backups...
BACKUP_CHANGE = "Change..."
CLOSE = "Close"
CANCEL = "Cancel"
BACK = "Back"   # Restore window: back to the Backups found list
GAME_OPEN = "Close WoW first, then try again."   # Restore only (a backup works with WoW open)
# Make a backup / Send a new backup while WoW is open (a strip in the window).
BACKUP_GAME_OPEN = ("WoW is open: settings are from your last /reload or logout. "
                    "Type /reload first to include your latest changes.")
# Send (a shorter one: the Send window must fit a Steam Deck screen with file.kiwi ticked).
SEND_GAME_OPEN = "WoW is open: type /reload first so the new backup has your latest settings."
BACKUP_GAME_SAVED = "Settings are from your last /reload or logout."
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
FOUND_SAFETY = "Show safety copies (made before a restore or install)"
FOUND_NOTE = ("Click a backup to restore it. Backups marked (online) are downloaded from "
              "your cloud first.")
FOUND_BROWSE = "Browse..."
FOUND_OPENING = "Opening the backup..."
FOUND_DOWNLOADING = "Downloading the backup from your cloud first..."
FOUND_MORE = "Older backups: use Browse..."
FOUND_SAFETY_HEADING = "Safety copies (made before a restore or install):"
PLACE_DOCUMENTS = "Documents"
ONLINE_MARK = "online"
NO_ACCOUNT = ("No account folder on this PC yet: log in to the game once, close it, then "
              "restore to get your settings back.")


# Send a backup with a code (am\share.py, am\share_ui.py). Never "sharing your account".
LICENCES_LINK = "Licences"
LICENCES_TITLE = "Licences"
LICENCES_MISSING = "The licence file is missing. It comes with every download of the app."
SEND_BUTTON = "Send to another PC..."
RECEIVE_BUTTON = "Receive..."
SEND_TITLE = "Send to another PC"
RECEIVE_TITLE = "Receive from another PC"
SEND_WHAT = "What to send:"
SEND_NEW = "A new backup now"


def send_new(addons=True, settings=True):
    """The "new backup" choice, with what the Make a backup ticks put in it."""
    what = ("addons + settings" if addons and settings else "addons only" if addons
            else "settings only" if settings else "")
    return f"{SEND_NEW} ({what})" if what else SEND_NEW



SEND_NEWEST = "Newest backup: "
SEND_OTHER = "Other file..."
SEND_CHANGE = "Change..."   # after an error: opens "What to send" again
SEND_CHOSEN = "File: "
# Folded "What to send" while a send runs / after an error: short, so it fits on one line
# beside Change... (the full choice shows again when the choices open).
SEND_FOLDED = "Sending:"
SEND_FOLDED_NEW = "a new backup"
SEND_PRIVATE = ("This file has your account and character names and data your addons saved "
                "about other players. Only give the code to yourself or someone you trust.")
SEND_SAFE = "Sent encrypted. The code works once, for 10 minutes."
SEND_GET_CODE = "Get code"
SEND_TRY_AGAIN = "Try again"   # the Get code button after an error a new try can fix
SEND_MAKING = "Making the backup..."
SEND_CONNECTING = "Getting a code..."
SEND_CODE_HEAD = "On the other PC: Backup, Receive..., then type:"
SEND_COPY = "Copy"
SEND_COPIED = "Copied."
SEND_WAITING = "Waiting for the other PC..."
SEND_SENT = "Sent. The other PC can restore it now."
RECEIVE_TYPE = "Type the code the other PC shows:"
RECEIVE_EXAMPLE = ("Like 37-amber-lamp-river. Spaces work too.\n"
                   "The first 3 letters of each word are enough.")
RECEIVE_NOTE = ("Only use a code from yourself or someone you trust. You'll see what's inside "
                "and pick what to restore. A safety copy is made first.")
RECEIVE_OK = "Receive"
RECEIVE_CONNECTING = "Connecting..."
RECEIVE_CHECKING = "Checking the file..."
# Closing Receive while it downloads from a Send server (the send can't come back).
RECEIVE_STOP_TITLE = "Stop receiving?"
RECEIVE_STOP_TEXT = ("The backup is deleted from the host and can't be received again; the "
                     "other PC must send it again.")
RECEIVE_STOP_OK = "Stop and delete"
RECEIVE_STOP_KEEP = "Keep receiving"
USE_CODE = "Use a code..."
RESTORE_CHOOSE = "Choose..."
RESTORE_CHOOSE_FIRST = "Choose which of your accounts gets these settings, or choose (skip)."
RECEIVED_ADDONS_NOTE = ("Addons from another PC are off. Get the guild's addons with Install / "
                        "Update in the main window; tick Addons only if you trust the sender.")
MAX_NAMES = 4   # names shown in a list line; the rest as "and N more"


def _names(names, most=MAX_NAMES):
    """'A, B, C, D and 2 more' (one function for every list line)."""
    names = list(names)
    if len(names) <= most:
        return ", ".join(names)
    return ", ".join(names[:most]) + f" and {len(names) - most} more"


def received_addons(not_listed, different):
    """The line under RECEIVED_ADDONS_NOTE: which addons aren't the guild's own."""
    parts = []
    if different:
        parts.append("Not the guild's version: " + _names(different) + ".")
    if not_listed:
        parts.append("Not on the guild list: " + _names(not_listed) + ".")
    return " ".join(parts)


# Send later (am\share_later.py, am\share_send.py): only when the player ticks it. Plain,
# honest texts (tos conditions): every host in the order tried, what each sees, how long it
# stays; the guild keeps no copy and doesn't run these hosts. Changing LATER_TICK/LATER_NOTE or
# the host rows needs a new share_later.CONSENT_VERSION.
KIWI_HOST = "file.kiwi"
SEND_HOSTS = {"adminforge": "adminForge's Send server", "visee": "send.vis.ee"}
LATER_TICK = ("Keep it online for later if the other PC isn't ready in 5 min (at once if "
              "over 500 MB), on the first host below that works:")
# Measured: the Send window at Linux 150 % stays at 953 of 960 px with this length; the rest of
# the tos points (operators, may vanish sooner, not the only copy) are in the README.
LATER_NOTE = ("Encrypted here: only this code opens it. The guild keeps no copy and doesn't "
              "run these hosts; they weren't asked and guarantee nothing. Hosts 1-2 delete it once "
              "downloaded or after 32 hours; file.kiwi (uses Cloudflare, Google) keeps it days. "
              "All see your IP, size and time.")
# The hosts, in the order tried: one row each, its pages as small links at the right.
LATER_TERMS = "terms"
LATER_PRIVACY = "privacy"
LATER_TERMS_PRIVACY = "terms & privacy"
LATER_HOST_ADMINFORGE = "1. adminForge Send (Germany)"
LATER_HOST_VISEE = "2. send.vis.ee (NL), private, no terms"   # fits 420 px with Linux fonts
# (100 % and 150 %; "1 person" was 8 px too wide); "private" = one private person (README)
LATER_ABOUT = "about"   # send.vis.ee has no terms page: its operator's README
LATER_LIST = "list"     # and the public Send instances list
LATER_HOST_KIWI = "3. file.kiwi (US)"
LATER_KEEP = "The code works for:"
LATER_NOW_TICK = "Upload right away (don't wait 5 min)"


def _host(host):
    return SEND_HOSTS.get(host, KIWI_HOST)


def later_uploading_text(reason, host=None):
    """The status when an upload for later starts (reason big / now / None = nobody came)."""
    where = _host(host)
    if reason == "big":
        return (f"This backup is over 500 MB, so it's being uploaded to {where}. The other "
                "PC can type the code any time. Keep this window open until it's done.")
    if reason == "now":
        return (f"Uploading to {where} now. The other PC can type the code in Receive... any "
                "time. Keep this window open until it's done.")
    return (f"The other PC isn't here, so it's being uploaded to {where} for later. Keep "
            "this window open until it's done.")


LATER_UPLOADING = later_uploading_text(None)
LATER_UPLOADING_BIG = later_uploading_text("big")
LATER_UPLOADING_NOW = later_uploading_text("now")
LATER_GOT = ("The other PC uploaded it to file.kiwi. Click Download to get it in your "
             "browser. I'll find it in your Downloads; if not, click Open file... and pick "
             "it.")
LATER_KEEP_OPEN = "Keep this window open until it's restored: the code works only once."
LATER_DOWNLOAD = "Download"
LATER_OPEN = "Open file..."   # short: it shares the row with Download (half each)
LATER_OPEN_TITLE = "Open the download"   # its file picker's title
LATER_LINK_NOTE = "Browser didn't open? Copy this link into it:"
LATER_COPY_LINK = "Copy link"
LATER_LINK_COPIED = "Link copied. Paste it into your browser."
LATER_NO_BROWSER = "Browser didn't open. Copy the link above."
LATER_LOOKING = "Waiting for the download..."
LATER_CHECKING = "Checking the file..."


def later_hours(hours):
    return f"{hours} hour" if hours == 1 else f"{hours} hours"


def later_in(seconds):
    seconds = max(0, int(seconds))
    return (f"Works once. If the other PC hasn't typed it in {seconds // 60}:{seconds % 60:02d}, "
            "it's uploaded for later.")


def later_uploading(done, total, left=None, host=None):
    return _with_left(f"Uploading to {_host(host)} for later: {size_text(done)} of "
                      f"{size_text(total)}...", left)


def later_ready(until_text, hours):
    return (f"Uploaded. On the other PC, type the code in Receive... any time until "
            f"{until_text} ({later_hours(hours)}). It works once. You can close the Addon "
            "Manager now.")


def later_waiting(permille):
    percent = max(0, min(100, permille // 10))
    if percent == 0:
        return ("The other PC is still uploading it (0 %), or waiting for the server. Keep "
                "this window open.")
    return f"The other PC is still uploading it ({percent} %). Keep this window open."


def later_kiwi_down(until_text):
    return (f"file.kiwi isn't answering right now. I'll try again by myself every 5 minutes "
            f"until {until_text}. Keep this window open; the code stays the same.")


def time_left(seconds):
    """'about 12 min left' (None = not known yet)."""
    if seconds is None:
        return ""
    minutes = round(seconds / 60)
    if minutes < 1:
        return "less than a minute left"
    if minutes < 90:
        return f"about {minutes} min left"
    return f"about {minutes // 60} h {minutes % 60} min left"


def _with_left(text, left):
    return f"{text} {time_left(left)}" if left is not None else text


def sending(done, total, left=None):
    return _with_left(f"Sending {size_text(done)} of {size_text(total)}...", left)


def receiving(done, total, left=None):
    return _with_left(f"Receiving {size_text(done)} of {size_text(total)}...", left)


# Spoken (AI voice-over addons): asked once when a backup would hold them (am\backup_ui.py).
SPOKEN_TITLE = "Leave out the Spoken addons?"
SPOKEN_WHY = ("They are big and hold almost no settings, so the backup is much smaller and "
              "faster without them.")
SPOKEN_KEPT = ("Their settings are still saved. On the other PC, install them from where you "
               "got them.")
SPOKEN_SKIP = "Leave them out"
SPOKEN_KEEP = "Keep them"
SPOKEN_CHECKING = "Checking the addons..."
SPOKEN_LEFT_OUT = "Spoken addons left out."


def spoken_found(names, size):
    return f"Found Spoken, the AI voice-over addon: {_names(names)} (about {limit_text(size)})."


def spoken_sizes(without, with_them):
    return (f"Backup without them: about {limit_text(without)} "
            f"(with: about {limit_text(with_them)})")


def spoken_tick(size):
    return f"Leave out Spoken, the AI voice-over addon (about {limit_text(size)})"


def restore_left_out(names):
    return (f"This backup left out: {_names(names)} (install them yourself if you want "
            "them).")


def restore_others(names):
    """Restore window: the Other addons (from Add file...) a backup's record brings back."""
    return ("With Addons ticked, these come back too and show under Other addons: "
            f"{_names(names)}.")


def limit_text(size):
    """A size limit as '500 MB' or '5 GB'."""
    gib = 1024 * 1024 * 1024
    if size >= gib:
        return f"{size / gib:.0f} GB" if size % gib == 0 else f"{size / gib:.1f} GB"
    return size_text(size)


def code_ends(seconds):
    seconds = max(0, int(seconds))
    return f"Works once. Ends in {seconds // 60}:{seconds % 60:02d}."


# A backup made just now for a send didn't pass the send check: trying again can't help,
# and the zip stays in the backup folder (so never "nothing was changed").
NEW_BACKUP_CHECK = ("Couldn't check the new backup, so it wasn't sent. It's kept in your "
                    "backup folder." + DETAILS)


# The secure connection couldn't be checked (no certificates found here, or a wrong clock).
SHARE_CERTIFICATE = ("Can't make a safe connection to the send service. Check that your "
                     "computer's date and time are right, then try again. If it keeps "
                     "happening, tell a guild officer.")


# A failed download from a Send server: the send is deleted there and the code is used up,
# so these never say "try again" or "nothing was changed".
STORE_NEW_CODE = (" The code works only once, so the backup was deleted from the host. Make a "
                  "new code on the other PC.")
STORE_USED_UP = {
    "cancelled": "Stopped.",
    "broken": "The file arrived damaged or cut off, so it was thrown away.",
    "file": "The file couldn't be saved on this PC.",
}


def share_message(exc, fresh=False):
    """Sending or receiving didn't work (nothing on this PC was changed).
    fresh: the backup was made just now for this send (a failed check isn't the player's
    file being wrong)."""
    if (fresh and getattr(exc, "kind", "") == "not_backup"
            and str(getattr(exc, "detail", "")).startswith("check ")):
        return NEW_BACKUP_CHECK
    kind = getattr(exc, "kind", "")
    if kind in STORE_USED_UP and str(getattr(exc, "detail", "")).startswith("store"):
        return STORE_USED_UP[kind] + STORE_NEW_CODE   # Send server: the code is used up
    if kind == "bad_format":
        return "That doesn't look like a code. Codes look like 37-amber-lamp-river."
    if kind == "wrong_code":
        return ("The code didn't match, so it can't be used any more. Make a new code on "
                "the other PC and try again.")
    if kind in ("not_found", "expired"):
        return "That code has ended or doesn't exist. Make a new code on the other PC."
    if kind == "used":
        return "That code was already used. Make a new code on the other PC."
    if kind == "ended":
        return "The other PC stopped. Nothing was changed. Try again with a new code."
    if kind == "cancelled":
        return "Stopped. Nothing was changed."
    if kind in ("offline", "timeout"):
        return "Can't reach the send service. Check your internet and try again."
    if kind == "certificate":
        return SHARE_CERTIFICATE
    if kind == "busy":
        return ("The send service is busy. Try again in a few minutes, or use a cloud "
                "folder or USB stick.")
    if kind == "too_big":
        limit = getattr(exc, "limit", None) or 500 * 1024 * 1024
        if getattr(exc, "detail", "") == "later_off":
            return (f"Over {limit_text(limit)} is too big to send directly. The \"Keep it "
                    "online for later\" option below (a public file host) can send it, or copy "
                    "the backup with a USB stick or cloud folder.")
        return (f"That backup is too big to send ({limit_text(limit)} at most). Use a cloud "
                "folder or USB stick.")
    if kind == "later_failed":
        if getattr(exc, "detail", "") == "down":
            return ("None of the 3 hosts took it (the last one didn't answer for 2 hours), so "
                    "the code was stopped. Try again later with a new code, or use a USB stick "
                    "or cloud folder.")
        return ("None of the 3 hosts took it, so the code was stopped. Try again later with a "
                "new code, or use a USB stick or cloud folder.")
    if kind == "later_newer":
        return ("This code was sent by a newer Addon Manager. Update this app, then ask for a "
                "new code.")
    if kind == "store_failed":
        return "The download stopped." + STORE_NEW_CODE
    if kind == "later_file":
        return ("That isn't the finished download for this code (or it is damaged). Download "
                "it again, then pick it.")
    if kind == "broken":
        return "The file arrived damaged or cut off, so it was thrown away. Try again."
    if kind == "not_backup":
        return "That file isn't a backup that can be sent (or it is damaged)."
    if kind == "disk_full":
        need = getattr(exc, "limit", None)
        if need and getattr(exc, "detail", "") == "store":   # Send server: the code is used up
            return (f"Not enough free disk space: about {limit_text(need)} is needed. The code "
                    "works only once, so free some space and ask the other PC to send it again.")
        if need:
            return (f"Not enough free disk space: about {limit_text(need)} is needed. Free "
                    "some space and try again.")
        return "The disk is full. Free some space and try again."
    if kind in ("game", "inside_game", "permission", "locked", "nothing"):
        return backup_message(exc)
    return "That didn't work. Nothing was changed. Try again." + DETAILS


def size_text(size):
    """Bytes as a short 'NN MB' text."""
    if size < 1024 * 1024:
        return f"{max(1, round(size / 1024))} KB"
    return f"{size / (1024 * 1024):.0f} MB"


def addons_label(size):
    return f"{BACKUP_ADDONS} (about {size_text(size)})" if size else BACKUP_ADDONS


SAVED_TO = "Saved to:"   # Make a backup: the folder is on its own line under it


def saved_to(folder):
    return f"Saved to: {folder}"


def backup_saved(name, size, game_open=False, skipped=()):
    """game_open: WoW was open while it was made. skipped: arcnames of files WoW was
    writing right then (left out)."""
    text = f"Backup saved: {name} ({size_text(size)})."
    if game_open:
        text += " " + BACKUP_GAME_SAVED
    if skipped:
        text += " " + backup_busy(skipped)
    return text


def backup_busy(skipped):
    """'1 settings file was busy and left out: Foo.lua.' (2 names at most). An entry
    "AddOns/<folder>" is an addon left out whole (its .toc was busy): 'Busy and left
    out: the Foo addon and Bar.lua.'"""
    parts = [a.replace("\\", "/").split("/") for a in skipped]
    whole = [p[1] for p in parts if len(p) == 2 and p[0] == "AddOns"]
    names = [f"the {f} addon" for f in whole] + [
        p[-1] for p in parts if not (len(p) == 2 and p[0] == "AddOns")]
    n = len(names)
    if whole:
        head = "Busy and left out: "
    else:
        what = "settings file" if all(a.startswith("WTF/") for a in skipped) else "file"
        head = f"{n} {what}{'s were' if n != 1 else ' was'} busy and left out: "
    if n <= 2:
        return head + " and ".join(names) + "."
    return head + f"{names[0]}, {names[1]} and {n - 2} more."


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


# Delete old backups (Backup window > Old backups...; am\backup_delete_ui.py).
DELETE_TITLE = "Delete old backups"
DELETE_OPEN = "Old backups..."
DELETE_OPEN_TIP = "Delete backups you don't need any more."
DELETE_NOTE = "Tick the backups you don't need any more."
DELETE_LOOKING = "Looking for backups..."
DELETE_NONE = "No backups found in your backup folders."
DELETE_SAFETY = "Show safety copies (made before a restore or install)"
DELETE_SAFETY_HEADING = "Safety copies:"
DELETE_NEWEST = "Tick all but the newest"
DELETE_NOTHING = "Nothing ticked"
DELETE_BUTTON = "Delete..."
DELETE_RECEIVED = "received"
DELETE_MORE = "Only the newest {n} are listed."
DELETING = "Deleting..."
DELETE_OK_BIN = f"Move to {BIN_NAME}"
DELETE_OK_GOOD = "Delete for good"
DELETE_TO_BIN = f"These go to the {BIN_NAME} (you can get them back):"
DELETE_FOR_GOOD = f"No {BIN_NAME} on that drive, so these will be deleted for good:"
DELETE_CLOUD = ("Backups in {clouds} are deleted on your other PCs too (the cloud keeps "
                "them in its own bin for a while).")
DELETE_SAFETY_LINE = "Safety copies let you undo a restore or an install from a file."
# Why a ticked backup was left (keys = am\backup_delete.py result kinds).
DELETE_REASONS = {
    "in_use": "in use: close anything that has it open, then try again",
    "permission": "no permission to delete it",
    "changed": "it changed since the list was made",
    "gone": "it's not there any more",
    "restore": "a restore didn't finish: start the app again first",
    "no_bin": f"its drive has no {BIN_NAME} now",
    "refused": "not one of this app's backups",
    "other": f"{SYSTEM} couldn't delete it",
}


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
    n = len(getattr(result, "others", None) or ())
    if n:
        text += f" {n} other addon{'s' if n != 1 else ''} added to Other addons."
    if result.safety:
        text += f" Safety copy saved as {result.safety}."
    # A restore needs WoW closed (it checks first), so WoW is started, not reloaded.
    return text + " Start WoW to use them."


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
    if kind == "cancelled":
        return BACKUP_STOPPED
    if kind == "changed":
        return ("Files changed while they were saved (WoW was writing them), so nothing was "
                "saved. Type /reload in WoW, then try again.")
    if kind == "nothing":
        return "There was nothing to back up or restore."
    if kind == "inside_game":
        return "Pick a folder outside the WoW Forever folder for backups."
    if kind == "no_account":
        return NO_ACCOUNT
    if kind == "same_account":
        return ("Two accounts from the backup would go to the same account here. Pick a "
                "different account for each, or choose (skip) for one of them.")
    if kind == "bad_folder":
        return FOLDER_GONE
    if kind == "locked":
        return ("Some files are in use, so nothing was changed. Close WoW (or the program "
                "using them) and try again.")
    if kind == "permission":
        return (f"{SYSTEM} won't let me write there, so nothing was changed. Pick another "
                "folder or close WoW and try again.")
    if kind == "read_denied":   # reading the game's own files (backup or safety copy)
        return (f"{SYSTEM} won't let me read some files in the WoW Forever folder, so "
                "nothing was changed. Close WoW and try again." + DETAILS)
    if kind == "disk_full":
        need = getattr(exc, "need", None)
        if need:
            return (f"Not enough free disk space, so nothing was changed: about "
                    f"{limit_text(need)} is needed. Free some space and try again.")
        return "The disk is full, so nothing was changed. Free some space and try again."
    return "That didn't work, so nothing was changed. Try again." + DETAILS


# Locked kept copies of received backups, Save zip, Unlock, Your codes
# (am\backup_lock.py, am\codes.py, am\codes_ui.py).
SAVE_ZIP = "Save zip..."
SAVE_ZIP_TITLE = "Save the backup as a zip"
KEPT_LOCKED = "Kept here, locked. This PC remembers the code."
if os.name != "nt":
    KEPT_LOCKED = "Kept here, locked. This computer remembers the code."
STOPPING = "Stopping..."
SAVE_ZIP_STOPPED = "Not saved."
FOUND_LOCKED = "locked"


def kept_no_code(code):
    """Locked here, but the codes file couldn't be saved: the player must keep the code."""
    return (f"Kept here, locked, but {THIS_PC} couldn't remember the code. "
            f"Write it down now: {code}")


# The main window's X while a Receive / Send runs (it stops first, then the app closes).
CLOSING_STOPPING = "Stopping... Addon Manager closes in a moment."
CLOSING_LOCKING = "Finishing the locked copy... Addon Manager closes when it's done."
# Save zip...: ".zip" was added to the picked name and that file is already there.
SAVE_ZIP_REPLACE_TITLE = "Replace the file?"
SAVE_ZIP_REPLACE_OK = "Replace"


def save_zip_replace(name):
    return f"{name} is already there. Replace it?"


def kept_failed(kind=None):
    """The locked copy couldn't be made after receiving: the backup is only here for now
    (why is in the log)."""
    return ("Couldn't keep a copy here. Restore it or use Save zip... before closing this "
            "window.")


def receive_locking(percent):
    return f"Locking the backup... {percent} %"


def save_zip_saving(percent):
    return f"Saving the zip... {percent} %"


def zip_saved(name, removed=True):
    if removed:
        return f"Saved as a normal zip: {name}. The locked copy was removed."
    return (f"Saved as a normal zip: {name}. The locked copy couldn't be removed; "
            "Old backups... can delete it.")


def save_zip_message(exc):
    kind = getattr(exc, "kind", "")
    if kind == "same_file":
        return "That's the backup itself: pick another name or folder."
    if kind == "inside_game":
        return "Can't save in the WoW folder. Pick another folder."
    if kind == "disk_full":
        need = getattr(exc, "need", None)
        return ("Not enough room there" + (f" (it needs {size_text(need)})" if need else "")
                + ". Pick another drive or free some space.")
    if kind == "too_big_for_drive":
        return ("That drive can't hold files over 4 GB (many USB sticks can't). Pick "
                "another drive.")
    if kind == "cancelled":
        return SAVE_ZIP_STOPPED
    return "Couldn't save it there. Pick another folder." + DETAILS


def unlocking(percent):
    return f"Unlocking... {percent} %"


UNLOCK_TITLE = "Unlock backup"
UNLOCK_TEXT = ("This backup is locked. Type the code it was sent with; " + THIS_PC
               + " remembers it after that.")
UNLOCK_EXAMPLE = RECEIVE_EXAMPLE   # one example code everywhere
UNLOCK_OK = "Unlock"
UNLOCK_WRONG = "That code doesn't open this backup. Check it and try again."
UNLOCK_CHECKING = "Checking the code..."
UNLOCK_NOT_CODE = "That doesn't look like a code."


def unlock_from(name):
    return f"Backup: {name}"


def unlock_message(exc):
    """Opening a locked kept copy didn't work (nothing was changed)."""
    kind = getattr(exc, "kind", "")
    if kind == "wrong_code":
        return UNLOCK_WRONG
    if kind == "newer":
        return "This locked backup was made by a newer Addon Manager: update this app first."
    if kind in ("damaged", "not_locked"):
        return "This locked backup is damaged. Send it again from the other PC."
    if kind == "disk_full":
        need = getattr(exc, "need", None)
        return ("Not enough room to open it" + (f" (it needs {size_text(need)})" if need else "")
                + ". Free some space and try again.")
    if kind == "cancelled":
        return "Stopped."
    if isinstance(exc, Exception) and type(exc).__name__ == "BackupError":
        return backup_message(exc)
    return "Couldn't open this locked backup." + DETAILS


CODES_BUTTON = "Codes..."
CODES_TITLE = "Your codes"
CODES_SENT = "Sent for later"
CODES_LOCKED = "Locked backups"
CODES_STOP = "Stop"
CODES_SHOW = "Show code"
CODES_HIDE = "Hide code"
CODES_FORGET = "Forget this code"   # the right-click entry
CODES_FORGET_BUTTON = "Forget"       # the row button (Steam Deck: no right-click)
CODES_NONE = "No codes are kept on " + THIS_PC + "."
CODES_NOTE = ("Kept only on this PC, for your Windows account. Sent codes are removed once "
              "they stop working.")
if os.name != "nt":
    CODES_NOTE = ("Kept only on this computer, for your account. Sent codes are removed once "
                  "they stop working.")
CODES_STOP_TITLE = "Stop this code?"
def codes_stop_text(host):
    """The Stop question; host = the code's host key ("kiwi" = file.kiwi)."""
    text = "The code stops working and the backup is deleted from its site."
    if host == "kiwi":
        text += " file.kiwi keeps a locked copy until it expires."
    return text
CODES_STOP_OK = "Stop it"
CODES_STOPPED = "Stopped. The code doesn't work any more."
CODES_STOP_FAILED = ("Couldn't reach the host. Try again later; the code stops by "
                     "itself when its time is up.")
CODES_FORGET_TITLE = "Forget this code?"
CODES_FORGET_TEXT = "After this, the backup opens only by typing its code. Write it down first."
CODES_FORGET_OK = "Forget it"
CODES_STOP_UNREADABLE = ("Couldn't read the codes saved on " + THIS_PC
                         + ", so nothing was stopped. Try again in a moment.")
CODES_COPY = "Copy"
CODES_COPIED = "Copied."
def codes_head(text, count, shown):
    """A group heading in Your codes: "(5)" with more than one, "(50 of 60, newest)" when
    not every code can be listed."""
    if count > shown:
        return f"{text} ({shown} of {count}, newest)"
    return f"{text} ({count})" if count > 1 else text


HOST_NAMES = {"adminforge": "adminForge", "visee": "send.vis.ee", "kiwi": "file.kiwi"}


def _hours_minutes(seconds):
    minutes = max(0, int(seconds) // 60)
    h, m = divmod(minutes, 60)
    return f"{h} h {m} min" if h else f"{m} min"


def codes_line(active, left, locked_count):
    """The Backup window's line beside Codes...: active sent codes (+ the soonest time left),
    else the locked backups this PC knows the code of."""
    if active:
        what = "1 code still works" if active == 1 else f"{active} codes still work"
        return f"{what} ({_hours_minutes(left)} left)" if left is not None else what
    if locked_count:
        return ("1 locked backup" if locked_count == 1
                else f"{locked_count} locked backups")
    return ""


def code_state(state, left, when=None):
    """One sent code's second line: where it is and what happened."""
    when = f" {when}" if when else ""
    if state == "taken":
        return f"Picked up{when}"
    if state == "stopped":
        return "Stopped"
    if state == "burned":
        return "Stopped: a wrong code was typed"
    if state == "ended":
        return "Ended"
    if state == "uploading":
        return "Still uploading"
    base = f"{_hours_minutes(left)} left" if left is not None else "Works"
    return base + (", not picked up yet" if state == "ready" else "")


def locked_line(saved_text, known):
    return (f"Received {saved_text}" if known else
            "Code not kept on " + THIS_PC)
