"""Safe install/update of one addon into Interface\\AddOns.

Steps: download to a temp folder, check the zip, extract into a hidden staging
folder inside AddOns, then swap folders by renaming (with rollback). Only the
addon's own folder is ever replaced; other addons and WTF\\ are never touched.
"""

import logging
import os
import re
import shutil
import stat
import tempfile
import zipfile

from am import download, toc, trash, versions
from am.manifest import is_safe_folder

log = logging.getLogger(__name__)

STAGING_PREFIX = ".addonmanager-staging-"
BACKUP_PREFIX = ".addonmanager-backup-"
REMOVING_PREFIX = ".addonmanager-removing-"   # a guild addon on its way out (see remove)
DELETING_PREFIX = ".addonmanager-deleting-"   # being deleted for good: finished, never put back
RESTORE_JOURNAL = ".addonmanager-restore.json"   # in AddOns while a restore swaps (see backup)
MAX_DOWNLOAD = 50 * 1024 * 1024
MAX_UNCOMPRESSED = 200 * 1024 * 1024
MAX_RATIO = 200          # compressed -> uncompressed, per file over 1 MB
MAX_ENTRIES = 5000


class InstallError(Exception):
    """kind: bad_folder, link, unsafe, bad_zip, mismatch, locked, permission,
    disk_full, other; for remove also not_guild, missing, no_trash, trash, partly
    (deleted for good only in part: the rest is hidden and retried next start).
    download errors are re-raised as download.DownloadError."""

    def __init__(self, kind, detail=""):
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail


def _is_link(path):
    return os.path.islink(path) or os.path.isjunction(path)


def check_addons_dir(addons_dir):
    """The folder must exist and be ...\\Interface\\AddOns."""
    real = os.path.realpath(addons_dir)
    if not os.path.isdir(real):
        raise InstallError("bad_folder", addons_dir)
    if (os.path.basename(real).lower() != "addons"
            or os.path.basename(os.path.dirname(real)).lower() != "interface"):
        raise InstallError("bad_folder", addons_dir)
    return real


def target_path(addons_dir, folder):
    """addons_dir\\folder, after checking the name can't escape AddOns."""
    if not is_safe_folder(folder):
        raise InstallError("unsafe", f"folder name {folder!r}")
    path = os.path.join(addons_dir, folder)
    if os.path.dirname(os.path.abspath(path)) != os.path.abspath(addons_dir):
        raise InstallError("unsafe", f"folder name {folder!r}")
    return path


def _entry_parts(name, folder):
    """Split a zip entry name into path parts, or raise if it's unsafe."""
    if "\\" in name or name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        raise InstallError("unsafe", f"bad path {name!r}")
    parts = [p for p in name.split("/") if p != ""]
    if not parts or parts[0] != folder:
        raise InstallError("unsafe", f"{name!r} is outside {folder}/")
    for p in parts:
        if p in (".", "..") or ":" in p or p.endswith(" ") or p.endswith("."):
            raise InstallError("unsafe", f"bad path {name!r}")
    return parts


def check_zip(zf, folder, expected_version):
    """Check every entry of an open ZipFile. Returns [(info, parts)]."""
    infos = zf.infolist()
    if len(infos) > MAX_ENTRIES:
        raise InstallError("unsafe", "too many files")
    checked, total = [], 0
    for info in infos:
        parts = _entry_parts(info.filename, folder)
        if info.flag_bits & 0x1:
            raise InstallError("unsafe", f"encrypted entry {info.filename!r}")
        mode = info.external_attr >> 16
        if stat.S_ISLNK(mode):
            raise InstallError("unsafe", f"link entry {info.filename!r}")
        total += info.file_size
        if total > MAX_UNCOMPRESSED:
            raise InstallError("unsafe", "unpacks too big")
        if info.file_size > 1024 * 1024 and info.file_size > MAX_RATIO * max(info.compress_size, 1):
            raise InstallError("unsafe", f"suspicious compression {info.filename!r}")
        checked.append((info, parts))

    toc_entry = f"{folder}/{toc.toc_name(folder)}"
    try:
        toc_info = zf.getinfo(toc_entry)
    except KeyError:
        raise InstallError("bad_zip", f"missing {toc_entry}") from None
    if toc_info.file_size > 1024 * 1024:
        raise InstallError("bad_zip", "toc too big")
    try:
        bad = zf.testzip()
        toc_version = toc.parse_version(toc.decode(zf.read(toc_info)))
    except (zipfile.BadZipFile, OSError, EOFError, RuntimeError, ValueError) as e:
        raise InstallError("bad_zip", str(e)) from e
    if bad is not None:
        raise InstallError("bad_zip", f"damaged entry {bad!r}")
    if not versions.same(toc_version, expected_version):
        raise InstallError("mismatch", f"toc says {toc_version!r}, list says {expected_version!r}")
    return checked


def extract(zf, checked, staging):
    """Extract checked entries into staging, re-checking every final path."""
    staging_real = os.path.realpath(staging)
    written = 0
    for info, parts in checked:
        dest = os.path.join(staging_real, *parts)
        if os.path.commonpath([staging_real, os.path.realpath(dest)]) != staging_real:
            raise InstallError("unsafe", f"bad path {info.filename!r}")
        if info.is_dir():
            os.makedirs(dest, exist_ok=True)
            continue
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with zf.open(info) as src, open(dest, "wb") as dst:
            while True:
                chunk = src.read(64 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_UNCOMPRESSED:
                    raise InstallError("unsafe", "unpacks too big")
                dst.write(chunk)


def _remove_tree(path):
    """Delete a folder we made (staging/backup), read-only files too. Never follows links."""
    if _is_link(path):
        log.warning("Not removing link %s", path)
        return False
    try:
        shutil.rmtree(path, onexc=_clear_read_only)
        return True
    except OSError:
        log.exception("Could not remove %s (will retry next start)", path)
        return False


def _clear_read_only(func, path, exc):
    """rmtree onexc: a read-only file/folder (or its read-only parent) can't be deleted;
    make it writable and try once more. Anything else (in use...) is raised."""
    if not isinstance(exc, PermissionError):
        raise exc
    try:
        for p in (path, os.path.dirname(path)):
            mode = os.lstat(p).st_mode
            if not stat.S_ISLNK(mode):
                os.chmod(p, stat.S_IMODE(mode) | stat.S_IWRITE)
        func(path)
    except OSError:
        raise exc from None


def _force_rmtree(path, raise_errors=False):
    """Delete a folder for good, read-only files too. Never follows links (a link inside
    is removed as a link). Returns True when it's gone."""
    if _is_link(path):
        log.warning("Not removing link %s", path)
        return False
    try:
        shutil.rmtree(path, onexc=_clear_read_only)
        return True
    except OSError:
        if raise_errors:
            raise
        log.exception("Could not delete %s (will retry next start)", path)
        return False


def _os_error(e, what):
    """Map an OSError from renaming/writing to an InstallError."""
    winerror = getattr(e, "winerror", None)
    if winerror in (32, 33):
        return InstallError("locked", what)
    if winerror in (112, 39) or e.errno == 28:
        return InstallError("disk_full", what)
    if isinstance(e, PermissionError) or winerror == 5:
        return InstallError("permission", what)
    return InstallError("other", f"{what}: {e}")


_BAD_CHARS = set('<>:"/\\|?*')
_RESERVED = {"con", "prn", "aux", "nul"} | {f"com{i}" for i in range(1, 10)} \
    | {f"lpt{i}" for i in range(1, 10)}


def safe_name(name):
    """One plain file/folder name that is the same on Windows and Linux (any addon folder
    a player can have, e.g. "!BugGrabber" or names with spaces): no slashes, drive,
    '..', reserved device names, control characters or trailing dot/space."""
    if not isinstance(name, str) or not 0 < len(name) <= 255 or name in (".", ".."):
        return False
    if name.endswith((" ", ".")) or any(c in _BAD_CHARS or ord(c) < 32 for c in name):
        return False
    return name.split(".")[0].lower() not in _RESERVED


def _leftover_folder_ok(folder):
    """A folder name recover may put back: any plain addon name a backup accepts (guild
    list names, "!BugGrabber", names with spaces...); never '..', a path or a dot folder."""
    return safe_name(folder) and not folder.startswith(".")


def recover(addons_dir):
    """Clean up after an interrupted install. Only touches our own prefixed folders.
    While a restore journal is there (a restore was cut off), the .addonmanager-backup-*
    folders belong to it: backup.recover_restore undoes that restore first.
    Returns a list of (action, name)."""
    actions = []
    try:
        entries = list(os.scandir(addons_dir))
    except OSError:
        return actions
    restore_pending = os.path.lexists(os.path.join(addons_dir, RESTORE_JOURNAL))
    for entry in entries:
        name = entry.name
        if not (name.startswith(STAGING_PREFIX) or name.startswith(BACKUP_PREFIX)
                or name.startswith(REMOVING_PREFIX) or name.startswith(DELETING_PREFIX)):
            continue
        if _is_link(entry.path) or not entry.is_dir(follow_symlinks=False):
            continue
        if name.startswith(DELETING_PREFIX):
            # A delete for good that stopped half-way (app closed, read-only file): the
            # player said yes to deleting it, so finish it rather than put a broken half back.
            if _force_rmtree(entry.path):
                actions.append(("deleted", name))
            continue
        if name.startswith(REMOVING_PREFIX):
            # A remove stopped half-way: the folder is whole (only renamed), so put it back
            # (the player can remove it again). If the addon is back already, the leftover
            # is a stale copy of our own: clear it so it never blocks a later remove.
            folder = name[len(REMOVING_PREFIX):]
            target = os.path.join(addons_dir, folder)
            if not _leftover_folder_ok(folder):
                continue
            if os.path.lexists(target):
                if _force_rmtree(entry.path):
                    actions.append(("removed", name))
                continue
            try:
                os.replace(entry.path, target)
                actions.append(("restored", folder))
            except OSError:
                log.exception("Could not restore %s", name)
            continue
        if name.startswith(STAGING_PREFIX):
            if not restore_pending and _remove_tree(entry.path):
                actions.append(("removed", name))
            continue
        folder = name[len(BACKUP_PREFIX):]
        if restore_pending or not _leftover_folder_ok(folder):
            continue
        target = os.path.join(addons_dir, folder)
        if os.path.lexists(target):
            if _remove_tree(entry.path):
                actions.append(("removed", name))
        else:
            try:
                os.replace(entry.path, target)
                actions.append(("restored", folder))
            except OSError:
                log.exception("Could not restore %s", name)
    for action, name in actions:
        log.info("Recovery: %s %s", action, name)
    return actions


def install(addon, addons_dir, progress=None, timeout=30, tmp_root=None):
    """Download, check and install one addon (a manifest.Addon).
    Returns the installed version. Raises InstallError or download.DownloadError.
    On any failure the old version stays in place."""
    addons_dir = check_addons_dir(addons_dir)
    target = target_path(addons_dir, addon.folder)
    backup = os.path.join(addons_dir, BACKUP_PREFIX + addon.folder)
    if _is_link(target):
        raise InstallError("link", target)
    if os.path.lexists(target) and not os.path.isdir(target):
        raise InstallError("unsafe", f"{target} is not a folder")
    recover(addons_dir)  # a leftover backup from an earlier crash is settled first

    max_bytes = MAX_DOWNLOAD
    if addon.size:
        max_bytes = min(MAX_DOWNLOAD, addon.size + 1024 * 1024)

    tmp = tempfile.mkdtemp(prefix="AddonManager_", dir=tmp_root)
    staging = None
    try:
        zip_path = os.path.join(tmp, "addon.zip")
        download.download_file(addon.url, zip_path, max_bytes, addon.sha256 or None,
                               progress=progress, timeout=timeout,
                               url_check=download.github_host_ok
                               if getattr(addon, "strict_hosts", False) else None)
        try:
            zf = zipfile.ZipFile(zip_path)
        except (zipfile.BadZipFile, OSError) as e:
            raise InstallError("bad_zip", str(e)) from e
        with zf:
            checked = check_zip(zf, addon.folder, addon.version)
            try:
                staging = tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=addons_dir)
                extract(zf, checked, staging)
            except InstallError:
                raise
            except (zipfile.BadZipFile, EOFError, RuntimeError, ValueError) as e:
                raise InstallError("bad_zip", str(e)) from e
            except OSError as e:
                raise _os_error(e, "unpacking") from e

        staged = os.path.join(staging, addon.folder)
        if not os.path.isdir(staged):
            raise InstallError("bad_zip", "addon folder missing after unpacking")

        had_old = os.path.isdir(target)
        if had_old:
            try:
                os.replace(target, backup)
            except OSError as e:
                raise _os_error(e, f"moving old {addon.folder}") from e
        try:
            os.replace(staged, target)
        except OSError as e:
            if had_old:
                try:
                    os.replace(backup, target)
                except OSError:
                    log.exception("Rollback failed; recover() will restore %s next start", backup)
            raise _os_error(e, f"putting new {addon.folder} in place") from e
        if had_old:
            _remove_tree(backup)
        log.info("Installed %s %s into %s", addon.folder, addon.version, addons_dir)
        return toc.installed(addons_dir, addon.folder)[1] or addon.version
    finally:
        if staging:
            _remove_tree(staging)
        shutil.rmtree(tmp, ignore_errors=True)


# ---------- removing a guild addon ----------
_DEPS_RE = re.compile(r"^\s*##\s*(?:RequiredDeps|Dependencies|Dep[A-Za-z0-9_]*)\s*:\s*(.*?)\s*$",
                      re.IGNORECASE)


def needed_by(addons_dir, folder, others):
    """Folders from `others` (installed guild addons) whose _Forever.toc says they
    need `folder` (## Dependencies / ## RequiredDeps / ## Dep...). Never raises."""
    found = []
    for other in others:
        if other == folder or not is_safe_folder(other):
            continue
        try:
            with open(toc.toc_path(addons_dir, other), "rb") as f:
                text = toc.decode(f.read(1024 * 1024))
        except OSError:
            continue
        for line in text.splitlines():
            m = _DEPS_RE.match(line)
            if m and folder.lower() in {d.strip().lower() for d in m.group(1).split(",")}:
                found.append(other)
                break
    return found


MAX_IN_USE_CHECK = 5000   # files looked at when Windows says "access denied"


def _windows_in_use(path):
    """Windows: is a file in this folder open by a program (so it can't be moved)? Asks
    for delete access on each file with full sharing: a program holding the file without
    delete sharing gives a sharing violation. Never changes anything. False if unsure."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create = kernel32.CreateFileW
        create.restype = wintypes.HANDLE
        create.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                           wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
        close = kernel32.CloseHandle
        invalid = wintypes.HANDLE(-1).value
        seen = 0
        for dirpath, dirs, files in os.walk(path):
            dirs[:] = [d for d in dirs if not _is_link(os.path.join(dirpath, d))]
            for name in files:
                seen += 1
                if seen > MAX_IN_USE_CHECK:
                    return False
                h = create(os.path.join(dirpath, name), 0x00010000, 0x7, None, 3, 0, None)
                if h == invalid or h is None:
                    if ctypes.get_last_error() in (32, 33):
                        return True
                else:
                    close(h)
    except Exception:
        log.exception("In-use check failed")
    return False


def _remove_error(e, folder, path=None):
    """An OSError while moving the addon's folder: a file in use, or no permission.
    Windows says "access denied" for both when a folder can't be renamed, so the files
    are checked: only a really open file counts as "in use"."""
    err = _os_error(e, f"removing {folder}")
    if err.kind == "permission" and path and _windows_in_use(path):
        err = InstallError("locked", err.detail)
    return err


def _delete_for_good(folder, target, addons_dir):
    """send=None: rename to a hidden .addonmanager-deleting-* folder (fails at once, with
    nothing changed, if a file inside is in use), then delete it, read-only files too.
    If some files still can't be deleted, the rest stays hidden there (WoW doesn't load
    it; the next start tries again) and InstallError("partly") says so honestly."""
    doomed = os.path.join(addons_dir, DELETING_PREFIX + folder)
    n = 2
    while os.path.lexists(doomed):
        doomed = os.path.join(addons_dir, f"{DELETING_PREFIX}{folder}-{n}")
        n += 1
    try:
        os.replace(target, doomed)
    except OSError as e:
        raise _remove_error(e, folder, target) from e
    try:
        _force_rmtree(doomed, raise_errors=True)
    except OSError as e:
        log.exception("Could not delete all of %s", doomed)
        raise InstallError("partly", f"{folder}: {e}") from e
    log.info("Deleted %s from %s (no trash on this drive)", folder, addons_dir)


def remove(folder, addons_dir, guild_folders, send=trash.send):
    """Remove one guild addon's folder from AddOns. send(path) moves it to the Recycle
    Bin / Trash; send=None deletes it for good (only after the player was warned).
    Only a folder named in guild_folders, directly inside Interface\\AddOns, never a
    link, Blizzard_* or the AddOns folder itself. WTF\\ (SavedVariables) is never touched.
    A rename first checks that nothing inside is in use, so a failed remove changes nothing.
    Raises InstallError."""
    addons_dir = check_addons_dir(addons_dir)
    if folder not in set(guild_folders or ()):
        raise InstallError("not_guild", folder)
    if not is_safe_folder(folder) or folder.lower().startswith("blizzard_"):
        raise InstallError("not_guild", folder)
    target = target_path(addons_dir, folder)
    if os.path.normcase(os.path.abspath(target)) == os.path.normcase(addons_dir):
        raise InstallError("unsafe", target)
    if _is_link(target):
        raise InstallError("link", target)
    if not os.path.isdir(target):
        raise InstallError("missing", target)
    recover(addons_dir)   # also clears leftovers (old backup, stale remove/delete folders)
    if send is None:
        _delete_for_good(folder, target, addons_dir)
        return
    probe = os.path.join(addons_dir, REMOVING_PREFIX + folder)
    if os.path.lexists(probe):
        raise InstallError("other", f"{probe} is in the way")
    try:
        os.replace(target, probe)   # fails at once if a file inside is open (Windows)
    except OSError as e:
        raise _remove_error(e, folder, target) from e
    try:
        os.replace(probe, target)   # the Recycle Bin keeps the addon's real name
    except OSError as e:
        log.exception("Could not rename %s back; recover() will next start", probe)
        raise _remove_error(e, folder, probe) from e
    try:
        send(target)
    except trash.TrashError as e:
        log.warning("Could not move %s to the trash: %s", target, e)
        kind = e.kind if e.kind in ("locked", "permission", "no_trash") else "trash"
        raise InstallError(kind, str(e)) from e
    except OSError as e:
        log.exception("Could not move %s to the trash", target)
        raise _remove_error(e, folder, target) from e
    if os.path.lexists(target):
        raise InstallError("trash", f"{target} is still there")
    log.info("Moved %s from %s to the trash", folder, addons_dir)
