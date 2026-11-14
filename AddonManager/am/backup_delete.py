"""Delete old backups (the Backup window's "Old backups..." list): which files may be
deleted and the delete itself. Standard library only; no file is ever opened (an
online-only cloud file is never downloaded just to delete it).

Only this app's own backup files in the searched backup folders: "AddonManager backup
....zip" (also " received") and the safety copies "Before restore/Before install ....zip".
Never anything in the WoW install (AddOns, WTF, the client or the WoW root). Each file
is checked again right before it goes: still a plain file (no link), same size and
date as listed, still in one of the searched folders. Files go to the Recycle Bin /
Trash; only the ones the player was told have no bin are deleted for good."""

import logging
import os
import stat

from am import backup, installer, selfupdate

log = logging.getLogger(__name__)

# Result kinds, one per file.
MOVED = "moved"            # in the Recycle Bin / Trash
DELETED = "deleted"        # deleted for good (no bin on that drive; the player was told)
IN_USE = "in_use"          # open in another program (or being synced)
PERMISSION = "permission"  # no right to delete it (read-only shared/cloud folder)
CHANGED = "changed"        # size or date changed since the list was made
GONE = "gone"              # not there any more
RESTORE = "restore"        # a safety copy while a cut-off restore may still need it
NO_BIN = "no_bin"          # the bin went away since the question (never deleted for good unasked)
REFUSED = "refused"        # not one of this app's backups, a link, or in the game folder
OTHER = "other"

DONE = (MOVED, DELETED)
MTIME_SLACK = 0.01         # seconds: the listing's date vs a fresh lstat
# Windows reparse tags that point somewhere else (cloud placeholders are fine).
_LINK_TAGS = (0xA000000C, 0xA0000003)   # IO_REPARSE_TAG_SYMLINK, IO_REPARSE_TAG_MOUNT_POINT


def own_name(name):
    """True for a file name this app writes for backups and safety copies."""
    low = os.path.basename(name).lower()
    if low.endswith(backup.LOCKED_EXT):   # a locked kept copy of a received backup
        return low.startswith(backup.BACKUP_PREFIX)
    return low.endswith(".zip") and low.startswith((backup.BACKUP_PREFIX, *backup.SAFETY_PREFIXES))


def _key(folder):
    try:
        return os.path.normcase(os.path.realpath(folder))
    except (OSError, ValueError):
        return None


def in_game(folder, addons_dir):
    """True if folder is in the WoW install (both checks), or if that can't be checked."""
    try:
        if addons_dir and backup.in_game_folder(folder, addons_dir):
            return True
        return bool(selfupdate.in_wow_folder(os.path.realpath(folder), addons_dir or None))
    except Exception as e:
        log.info("Game folder check failed (%s): place skipped", type(e).__name__)
        return True


def safe_places(places, addons_dir):
    """[(label, folder)] without folders in the WoW install."""
    return [(label, folder) for label, folder in places
            if folder and not in_game(folder, addons_dir)]


def find(places, addons_dir):
    """[FoundBackup] (newest first) the list may offer: backup.find_backups on the safe
    places, own names only (the listing only: nothing is opened)."""
    places = safe_places(places, addons_dir)
    found = backup.find_backups(places)
    return [f for f in found if own_name(f.path)
            and not in_game(os.path.dirname(f.path), addons_dir)]


def restore_pending(addons_dir):
    """A cut-off restore's journal is still in AddOns (its safety copy may be needed)."""
    try:
        return bool(addons_dir) and os.path.lexists(
            os.path.join(addons_dir, installer.RESTORE_JOURNAL))
    except (OSError, ValueError):
        return True


def check(f, places, addons_dir):
    """None if the listed FoundBackup f may be deleted now, else a result kind."""
    if not own_name(f.path):
        return REFUSED
    folder = os.path.dirname(os.path.abspath(f.path))
    allowed = {_key(p) for _l, p in safe_places(places, addons_dir)}
    key = _key(folder)
    if key is None or key not in allowed:
        return REFUSED
    if in_game(folder, addons_dir) or in_game(f.path, addons_dir):
        return REFUSED
    try:
        st = os.lstat(f.path)
    except FileNotFoundError:
        return GONE
    except OSError:
        return OTHER
    if (not stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode)
            or getattr(st, "st_reparse_tag", 0) in _LINK_TAGS):
        return REFUSED
    if st.st_size != f.size or abs(st.st_mtime - f.mtime) > MTIME_SLACK:
        return CHANGED
    if f.safety and restore_pending(addons_dir):
        return RESTORE
    return None


def _os_kind(e):
    if isinstance(e, FileNotFoundError):
        return GONE
    if getattr(e, "winerror", None) in (32, 33):
        return IN_USE
    if isinstance(e, PermissionError):
        return PERMISSION
    return OTHER


def delete_for_good(path):
    """Remove one file; a read-only file is made writable and tried once more."""
    try:
        os.remove(path)
        return
    except PermissionError as e:
        if getattr(e, "winerror", None) in (32, 33):
            raise
        try:
            mode = os.lstat(path).st_mode
        except OSError:
            raise e from None
        if stat.S_ISLNK(mode) or mode & stat.S_IWRITE:
            raise
        os.chmod(path, stat.S_IMODE(mode) | stat.S_IWRITE)
    os.remove(path)


def _trash_kind(e):
    kind = getattr(e, "kind", None)
    if kind == "locked":
        return IN_USE
    if kind == "permission":
        return PERMISSION
    if kind == "no_trash":
        return NO_BIN
    if isinstance(e, OSError):
        return _os_kind(e)
    return OTHER


def delete_one(f, places, addons_dir, trash_api, for_good=()):
    """Delete one listed backup: the bin when there is one, else for good only when its
    path is in for_good (the player was told). Returns a result kind; never raises."""
    try:
        kind = check(f, places, addons_dir)
        if kind:
            return kind
        if trash_api.available(f.path):
            try:
                trash_api.send(f.path)
            except Exception as e:
                log.warning("Backup not moved to the bin (%s)", type(e).__name__)
                return _trash_kind(e)
            return MOVED
        if f.path not in for_good:
            return NO_BIN
        try:
            delete_for_good(f.path)
        except OSError as e:
            log.warning("Backup not deleted (%s)", type(e).__name__)
            return _os_kind(e)
        return DELETED
    except Exception:
        log.exception("Backup delete failed")
        return OTHER


def delete(items, places, addons_dir, trash_api, for_good=()):
    """[(FoundBackup, kind)], one file at a time; one failure never stops the rest. A locked
    kept copy that went also takes its remembered code (by name + size: no file is opened),
    unless another locked copy of that name and size is still in the searched places."""
    out = [(f, delete_one(f, places, addons_dir, trash_api, for_good)) for f in items]
    gone = {(os.path.basename(f.path), f.size) for f, kind in out
            if kind in DONE and getattr(f, "locked", False)}
    if gone:
        try:
            left = {(os.path.basename(f.path), f.size)
                    for f in find(places, addons_dir) if getattr(f, "locked", False)}
        except Exception as e:   # noqa: BLE001 (can't tell: keep the codes)
            log.info("Backups not listed again (%s)", type(e).__name__)
            left = gone
        for name, size in gone - left:
            try:
                from am import codes
                codes.forget_named(name, size)
            except Exception as e:   # noqa: BLE001
                log.info("Code of a deleted locked backup not forgotten (%s)", type(e).__name__)
    return out


def but_newest(found):
    """Every FoundBackup except the newest (list order is newest first)."""
    rest = sorted(found, key=lambda f: f.mtime, reverse=True)
    return rest[1:]
