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

from am import download, toc, versions
from am.manifest import is_safe_folder

log = logging.getLogger(__name__)

STAGING_PREFIX = ".addonmanager-staging-"
BACKUP_PREFIX = ".addonmanager-backup-"
MAX_DOWNLOAD = 50 * 1024 * 1024
MAX_UNCOMPRESSED = 200 * 1024 * 1024
MAX_RATIO = 200          # compressed -> uncompressed, per file over 1 MB
MAX_ENTRIES = 5000


class InstallError(Exception):
    """kind: bad_folder, link, unsafe, bad_zip, mismatch, locked, permission,
    disk_full, other. download errors are re-raised as download.DownloadError."""

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
    """Delete a folder we made (staging/backup). Never follows links."""
    if _is_link(path):
        log.warning("Not removing link %s", path)
        return False
    try:
        shutil.rmtree(path)
        return True
    except OSError:
        log.exception("Could not remove %s (will retry next start)", path)
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


def recover(addons_dir):
    """Clean up after an interrupted install. Only touches our own prefixed folders.
    Returns a list of (action, name)."""
    actions = []
    try:
        entries = list(os.scandir(addons_dir))
    except OSError:
        return actions
    for entry in entries:
        name = entry.name
        if not (name.startswith(STAGING_PREFIX) or name.startswith(BACKUP_PREFIX)):
            continue
        if _is_link(entry.path) or not entry.is_dir(follow_symlinks=False):
            continue
        if name.startswith(STAGING_PREFIX):
            if _remove_tree(entry.path):
                actions.append(("removed", name))
            continue
        folder = name[len(BACKUP_PREFIX):]
        if not is_safe_folder(folder):
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
