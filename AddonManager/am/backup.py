"""Backup and restore of the player's addons (Interface\\AddOns) and their settings
(WTF\\Account\\...) as one .zip, to play with the same setup on another PC.

A pure file copy: never reads the game's memory or data files, never changes the game
install. Only these are ever read or written:
  - <client>\\Interface\\AddOns\\<addon folder>\\...   (not Blizzard_*, not .addonmanager-*)
  - <client>\\WTF\\Account\\<account>\\...             (settings files only: .lua .wtf .txt .md5;
                                                    never chat-cache.txt or WTF\\Config.wtf)
Zip layout: backup.json, AddOns/<folder>/..., WTF/Account/<account>/...

Restore merges: what the backup holds replaces the same addon folder / settings file here;
everything else stays. A safety copy of what gets replaced is written first; every new
file is unpacked into a staging folder first, then swapped in by renaming with rollback,
so if anything fails the old files stay.
"""

import ctypes
import dataclasses
import datetime
import json
import logging
import os
import stat
import tempfile
import zipfile
import zlib

from am import APP_VERSION, installer, selfupdate, wowpath

log = logging.getLogger(__name__)

FORMAT = 1
BACKUP_APP = "AddonManager"   # in backup.json and the file name (never changes)
INFO_NAME = "backup.json"
DOCUMENTS_ENV = "ADDONMANAGER_DOCUMENTS"   # tests: a temp folder instead of Documents
FOLDER_NAME = "AddonManager backups"
SETTINGS_EXT = (".lua", ".wtf", ".txt", ".md5")
SKIP_FILES = {"chat-cache.txt"}            # chat history: private and not needed
BINDING_FILES = {"bindings-cache.wtf", "macros-cache.txt"}
RESTORE_PREFIX = ".addonmanager-restore-"  # staging folder inside WTF during a restore
MAX_ENTRIES = 200_000
MAX_TOTAL = 4 * 1024 * 1024 * 1024         # unpacked size of a whole backup
MAX_RATIO = 200                            # compressed -> unpacked, per file over 1 MB
MAX_WTF_DEPTH = 4                          # realm/character/SavedVariables/file

ADDONS, SETTINGS, BINDINGS = "addons", "settings", "bindings"


class BackupError(Exception):
    """kind: bad_folder, nothing, inside_game, not_backup, newer_format, unsafe, bad_zip,
    no_account, same_account, locked, permission, disk_full, other."""

    def __init__(self, kind, detail=""):
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail


# ---------- names and places ----------
def safe_part(name):
    """One path part that is the same plain name on Windows and Linux (the same rule
    installer.recover uses for the folders it may put back)."""
    return installer.safe_name(name)


def _addon_name_ok(folder):
    return (safe_part(folder) and not folder.startswith(".")
            and not folder.lower().startswith("blizzard_"))


def _is_link(path):
    return os.path.islink(path) or os.path.isjunction(path)


def _windows_documents():
    """Documents via the Known Folder API (follows a moved or OneDrive Documents)."""
    try:
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("a", wintypes.DWORD), ("b", wintypes.WORD), ("c", wintypes.WORD),
                        ("d", ctypes.c_ubyte * 8)]
        folder_id = GUID(0xFDD39AD0, 0x238F, 0x46AF,
                         (ctypes.c_ubyte * 8)(0xAD, 0xB4, 0x6C, 0x85, 0x48, 0x03, 0x69, 0xC7))
        out = ctypes.c_wchar_p()
        shell32 = ctypes.windll.shell32
        if shell32.SHGetKnownFolderPath(ctypes.byref(folder_id), 0, None,
                                        ctypes.byref(out)) != 0:
            return None
        try:
            return out.value
        finally:
            ctypes.windll.ole32.CoTaskMemFree(out)
    except Exception:
        log.info("Documents folder lookup failed", exc_info=True)
        return None


def _linux_documents(home):
    """XDG_DOCUMENTS_DIR from ~/.config/user-dirs.dirs, else ~/Documents."""
    try:
        with open(os.path.join(home, ".config", "user-dirs.dirs"), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("XDG_DOCUMENTS_DIR="):
                    value = line.split("=", 1)[1].strip().strip('"')
                    value = value.replace("$HOME", home)
                    if os.path.isabs(value) and os.path.normpath(value) != os.path.normpath(home):
                        return value
    except OSError:
        pass
    return os.path.join(home, "Documents")


def documents_dir():
    """The player's Documents folder ($ADDONMANAGER_DOCUMENTS in tests)."""
    override = os.environ.get(DOCUMENTS_ENV)
    if override:
        return override
    home = os.path.expanduser("~")
    if os.name == "nt":
        return _windows_documents() or os.path.join(home, "Documents")
    return _linux_documents(home)


def default_folder():
    """Where backups go unless the player picked another folder."""
    return os.path.join(documents_dir(), FOLDER_NAME)


def cloud_folders(home=None, env=None):
    """Synced cloud folders that exist on this PC: [(label, folder)]. Only folders the
    cloud's own program syncs; the app never talks to a cloud service itself."""
    env = os.environ if env is None else env
    if home is None:
        home = os.path.expanduser("~")
    found, seen = [], set()

    def add(label, path):
        if path and os.path.isdir(path):
            key = os.path.normcase(os.path.realpath(path))
            if key not in seen:
                seen.add(key)
                found.append((label, path))
    if os.name == "nt":
        add("OneDrive", env.get("OneDriveConsumer"))
        add("OneDrive", env.get("OneDriveCommercial"))
        add("OneDrive", env.get("OneDrive"))
        add("Google Drive", os.path.join(home, "Google Drive"))
        add("Google Drive", os.path.join(home, "My Drive"))
        add("iCloud Drive", os.path.join(home, "iCloudDrive"))
    add("Dropbox", os.path.join(home, "Dropbox"))
    return found


def _unique(folder, stem):
    path = os.path.join(folder, stem + ".zip")
    n = 2
    while os.path.lexists(path) or os.path.lexists(path + ".part"):
        path = os.path.join(folder, f"{stem} ({n}).zip")
        n += 1
    return path


def backup_path(folder, now=None):
    """<folder>\\AddonManager backup 2026-11-10.zip (" (2)" etc. if it exists)."""
    now = now or datetime.datetime.now()
    return _unique(folder, f"{BACKUP_APP} backup {now:%Y-%m-%d}")


def safety_path(folder, now=None):
    """<folder>\\Before restore 2026-11-10 2130.zip."""
    now = now or datetime.datetime.now()
    return _unique(folder, f"Before restore {now:%Y-%m-%d %H%M}")


@dataclasses.dataclass
class Places:
    addons: str        # real Interface\AddOns
    client: str
    wtf: str           # client\WTF (may not exist yet)
    accounts: str      # client\WTF\Account (may not exist yet)


def places(addons_dir):
    try:
        real = installer.check_addons_dir(addons_dir)
    except installer.InstallError as e:
        raise BackupError("bad_folder", addons_dir) from e
    client = os.path.dirname(os.path.dirname(real))
    wtf = wowpath.child(client, "WTF")
    return Places(real, client, wtf, wowpath.child(wtf, "Account"))


def local_accounts(addons_dir):
    """Account folders in WTF\\Account on this PC (made when the player first logs in)."""
    p = places(addons_dir)
    try:
        entries = sorted(os.scandir(p.accounts), key=lambda e: e.name.lower())
    except OSError:
        return []
    return [e.name for e in entries if e.is_dir(follow_symlinks=False)
            and not _is_link(e.path) and safe_part(e.name) and not e.name.startswith(".")
            and e.name != "SavedVariables"]


def _inside(path, folder):
    try:
        return os.path.commonpath([os.path.normcase(os.path.realpath(path)),
                                   os.path.normcase(os.path.realpath(folder))]) \
            == os.path.normcase(os.path.realpath(folder))
    except ValueError:   # other drive
        return False


# ---------- what goes into a backup ----------
def wtf_kind(rest):
    """rest: path parts below WTF/Account/<account>/. SETTINGS, BINDINGS or None (left out)."""
    if not rest or len(rest) > MAX_WTF_DEPTH or not all(safe_part(p) for p in rest):
        return None
    name = rest[-1].lower()
    if name in SKIP_FILES or not name.endswith(SETTINGS_EXT) or rest[0].startswith("."):
        return None
    return BINDINGS if name in BINDING_FILES else SETTINGS


def _has_toc(folder_path):
    try:
        with os.scandir(folder_path) as it:
            return any(e.is_file(follow_symlinks=False) and e.name.lower().endswith(".toc")
                       for e in it)
    except OSError:
        return False


def _walk(root, depth=0, max_depth=None):
    """(parts, path) of every plain file below root. Never follows links; skips
    names that couldn't be restored on another system."""
    try:
        entries = sorted(os.scandir(root), key=lambda e: e.name)
    except OSError as e:
        raise _os_error(e, f"reading {root}") from e
    for e in entries:
        if not safe_part(e.name) or _is_link(e.path):
            if not safe_part(e.name):
                log.info("Leaving out %s (name can't be restored)", e.path)
            continue
        if e.is_dir(follow_symlinks=False):
            if max_depth is None or depth + 1 < max_depth:
                for parts, path in _walk(e.path, depth + 1, max_depth):
                    yield [e.name] + parts, path
        elif e.is_file(follow_symlinks=False):
            yield [e.name], e.path


def addon_folders(addons_dir):
    """Addon folders in AddOns that a backup holds: a real folder with a .toc inside;
    never Blizzard_*, links or the app's own .addonmanager-* folders."""
    p = places(addons_dir)
    out = []
    for e in sorted(os.scandir(p.addons), key=lambda e: e.name.lower()):
        if (_addon_name_ok(e.name) and e.is_dir(follow_symlinks=False)
                and not _is_link(e.path) and _has_toc(e.path)):
            out.append(e.name)
    return out


def _addon_files(p, folders):
    files = []
    for folder in folders:
        for parts, path in _walk(os.path.join(p.addons, folder)):
            files.append(("/".join(["AddOns", folder] + parts), path))
    return files


def _settings_files(p, accounts=None, kinds=(SETTINGS, BINDINGS)):
    files = []
    for account in (local_accounts(p.addons) if accounts is None else accounts):
        acc_dir = os.path.join(p.accounts, account)
        if not os.path.isdir(acc_dir) or _is_link(acc_dir):
            continue
        for rest, path in _walk(acc_dir, max_depth=MAX_WTF_DEPTH):
            if wtf_kind(rest) in kinds:
                files.append(("/".join(["WTF", "Account", account] + rest), path))
    return files


def _size(files):
    total = 0
    for _arc, path in files:
        try:
            total += os.path.getsize(path)
        except OSError:
            pass
    return total


def addons_size(addons_dir):
    """Bytes of the addon folders a backup would hold (for the window's label)."""
    try:
        return _size(_addon_files(places(addons_dir), addon_folders(addons_dir)))
    except (BackupError, OSError):
        return 0


def _os_error(e, what):
    err = installer._os_error(e, what)
    return BackupError(err.kind if err.kind in ("locked", "permission", "disk_full") else "other",
                       err.detail)


def _write_zip(path, files, kind, parts, progress=None):
    """Write files [(arcname, source)] + backup.json into path (via path.part)."""
    part = path + ".part"
    total = _size(files) or 1
    done = 0
    info = {"app": BACKUP_APP, "format": FORMAT, "version": APP_VERSION, "kind": kind,
            "created": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
            "parts": sorted(parts)}
    try:
        with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED, compresslevel=6,
                             strict_timestamps=False) as zf:
            zf.writestr(INFO_NAME, json.dumps(info, indent=2))
            for arc, src in files:
                zf.write(src, arc)
                try:
                    done += os.path.getsize(src)
                except OSError:
                    pass
                if progress:
                    progress(done, total)
        os.replace(part, path)
    except OSError as e:
        _discard(part)
        raise _os_error(e, f"writing {path}") from e
    except BaseException:
        _discard(part)
        raise
    return path


def _discard(path):
    try:
        if os.path.lexists(path):
            os.remove(path)
    except OSError:
        log.exception("Could not remove %s", path)


def make(addons_dir, dest_folder, addons=True, settings=True, now=None, progress=None):
    """Make a backup in dest_folder. Returns (path, size in bytes, counts dict).
    settings: everything restorable below WTF\\Account (incl. key bindings and macros)."""
    p = places(addons_dir)
    dest_folder = os.path.abspath(dest_folder)
    if in_game_folder(dest_folder, p.addons):
        raise BackupError("inside_game", dest_folder)
    try:
        folders = addon_folders(addons_dir) if addons else []
        files = _addon_files(p, folders) if addons else []
        wtf_files = _settings_files(p) if settings else []
    except OSError as e:
        raise _os_error(e, "reading the game folder") from e
    if not files and not wtf_files:
        raise BackupError("nothing", addons_dir)
    try:
        os.makedirs(dest_folder, exist_ok=True)
    except OSError as e:
        raise _os_error(e, f"making {dest_folder}") from e
    parts = ([ADDONS] if files else []) + ([SETTINGS] if wtf_files else [])
    path = _write_zip(backup_path(dest_folder, now), files + wtf_files, "backup", parts, progress)
    counts = {"addons": len(folders), "settings_files": len(wtf_files)}
    log.info("Backup made: %s (%s)", path, counts)
    return path, os.path.getsize(path), counts


# ---------- reading a backup ----------
@dataclasses.dataclass
class BackupInfo:
    path: str
    created: str = ""
    kind: str = "backup"
    addons: list = dataclasses.field(default_factory=list)     # folder names
    accounts: dict = dataclasses.field(default_factory=dict)   # name -> set of (realm, char)
    settings_files: int = 0
    binding_files: int = 0
    skipped: list = dataclasses.field(default_factory=list)    # addon folders without a .toc
    entries: list = dataclasses.field(default_factory=list)    # (ZipInfo, parts, kind)

    @property
    def characters(self):
        return sum(len(c) for c in self.accounts.values())


def _entry_parts(name):
    if "\\" in name or name.startswith("/") or (len(name) > 1 and name[1] == ":"):
        raise BackupError("unsafe", f"bad path {name!r}")
    parts = name.rstrip("/").split("/")
    if not all(safe_part(p) for p in parts):
        raise BackupError("unsafe", f"bad path {name!r}")
    return parts


def read(zip_path):
    """Open and check a backup. Every entry must be backup.json, AddOns/<folder>/... or
    WTF/Account/<account>/<settings file>; anything else and the whole file is refused.
    Returns BackupInfo. Raises BackupError."""
    try:
        zf = zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, OSError, ValueError) as e:
        raise BackupError("not_backup", str(e)) from e
    with zf:
        try:
            raw = zf.read(INFO_NAME) if INFO_NAME in zf.NameToInfo else b""
            meta = json.loads(raw.decode("utf-8")) if raw else None
        except (ValueError, OSError, zipfile.BadZipFile, RuntimeError, EOFError) as e:
            raise BackupError("not_backup", str(e)) from e
        if not isinstance(meta, dict) or meta.get("app") != BACKUP_APP:
            raise BackupError("not_backup", "no backup.json")
        fmt = meta.get("format")
        if not isinstance(fmt, int) or fmt < 1:
            raise BackupError("not_backup", f"format {fmt!r}")
        if fmt > FORMAT:
            raise BackupError("newer_format", str(fmt))
        info = BackupInfo(path=zip_path)
        created = meta.get("created")
        if isinstance(created, str) and len(created) <= 20:
            info.created = created
        if meta.get("kind") == "safety":
            info.kind = "safety"
        infos = zf.infolist()
        if len(infos) > MAX_ENTRIES:
            raise BackupError("unsafe", "too many files")
        total, seen, tocs, folders = 0, set(), set(), []
        for zi in infos:
            if zi.filename == INFO_NAME:
                continue
            parts = _entry_parts(zi.filename)
            if zi.is_dir():
                continue   # folders are made as needed; only files are restored
            key = "/".join(parts).lower()
            if zi.flag_bits & 0x1:
                raise BackupError("unsafe", f"encrypted {zi.filename!r}")
            if stat.S_ISLNK(zi.external_attr >> 16):
                raise BackupError("unsafe", f"link {zi.filename!r}")
            total += zi.file_size
            if total > MAX_TOTAL:
                raise BackupError("unsafe", "unpacks too big")
            if zi.file_size > 1024 * 1024 and zi.file_size > MAX_RATIO * max(zi.compress_size, 1):
                raise BackupError("unsafe", f"suspicious compression {zi.filename!r}")
            if parts[0] == "AddOns" and len(parts) >= 2 and _addon_name_ok(parts[1]):
                if len(parts) < 3:
                    raise BackupError("unsafe", f"bad path {zi.filename!r}")
                kind = ADDONS
                if parts[1] not in folders:
                    folders.append(parts[1])
                if len(parts) == 3 and parts[2].lower().endswith(".toc"):
                    tocs.add(parts[1])
            elif (len(parts) >= 3 and parts[0] == "WTF" and parts[1] == "Account"
                  and not parts[2].startswith(".")):
                kind = wtf_kind(parts[3:])
                if kind is None:
                    raise BackupError("unsafe", f"not a settings file {zi.filename!r}")
                rest = parts[3:]
                chars = info.accounts.setdefault(parts[2], set())
                if len(rest) >= 3 and rest[0] != "SavedVariables":
                    chars.add((rest[0], rest[1]))
                if kind == BINDINGS:
                    info.binding_files += 1
                else:
                    info.settings_files += 1
            else:
                raise BackupError("unsafe", f"unexpected entry {zi.filename!r}")
            if key in seen:   # same file twice (or only in letter case): refuse
                raise BackupError("unsafe", f"duplicate {zi.filename!r}")
            seen.add(key)
            info.entries.append((zi, parts, kind))
        if info.kind == "safety":
            # A safety copy puts back exactly what was replaced, folders without a .toc too.
            info.addons, info.skipped = list(folders), []
        else:
            info.addons = [f for f in folders if f in tocs]
            info.skipped = [f for f in folders if f not in tocs]
        lower = {}
        for f in info.addons:
            if lower.setdefault(f.lower(), f) != f:
                raise BackupError("unsafe", f"duplicate folder {f!r}")
        if not info.addons and not info.accounts:
            raise BackupError("nothing", zip_path)
    return info


# ---------- restoring ----------
@dataclasses.dataclass
class RestoreResult:
    addons: list
    files: int
    safety: str = None


def default_map(backup_accounts, local):
    """Backup account -> account on this PC. Each account here gets at most one backup
    account: the same name first; then, if exactly one backup account and one account here
    are left, those two; everything else None (the player picks; None = skipped)."""
    out = {acc: (acc if acc in local else None) for acc in backup_accounts}
    left = [a for a in backup_accounts if out[a] is None]
    free = [a for a in local if a not in out.values()]
    if len(left) == 1 and len(free) == 1:
        out[left[0]] = free[0]
    return out


def _extract(zf, zi, dest, budget):
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with zf.open(zi) as src, open(dest, "wb") as dst:
        while True:
            chunk = src.read(256 * 1024)
            if not chunk:
                break
            budget[0] += len(chunk)
            if budget[0] > MAX_TOTAL:
                raise BackupError("unsafe", "unpacks too big")
            dst.write(chunk)


def plan(info, addons_dir, addons=True, settings=True, bindings=True, account_map=None):
    """What a restore would write: (addon folders, [(entry, local account, rest parts)]).
    Two backup accounts going to the same account here is refused (kind same_account):
    their files would overwrite each other."""
    folders = list(info.addons) if addons else []
    kinds = {k for k, on in ((SETTINGS, settings), (BINDINGS, bindings)) if on}
    files = []
    if kinds:
        local = set(local_accounts(addons_dir))
        used = {}
        for zi, parts, kind in info.entries:
            if kind in kinds:
                target_acc = (account_map or {}).get(parts[2])
                if target_acc and target_acc in local:
                    if used.setdefault(target_acc.lower(), parts[2]) != parts[2]:
                        raise BackupError("same_account", target_acc)
                    files.append((zi, target_acc, parts[3:]))
    return folders, files


def in_game_folder(folder, addons_dir):
    """True if folder is anywhere inside the WoW install (client folder, the WoW root
    above it, or any real game install): backups and safety copies never go there."""
    p = places(addons_dir)
    real = os.path.realpath(folder)
    return (_inside(real, p.client) or _inside(real, p.addons) or _inside(real, p.wtf)
            or selfupdate.in_wow_folder(real, p.addons))


# The restore journal (installer.RESTORE_JOURNAL in AddOns) is written after everything
# is unpacked and before the first rename; it is removed once every rename is done (the
# commit). If it is still there at the next start, the restore was cut off: _undo puts
# everything back as it was before (recover_restore).
def _journal_path(p):
    return os.path.join(p.addons, installer.RESTORE_JOURNAL)


def _write_journal(p, data):
    path = _journal_path(p)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _read_journal(p):
    try:
        with open(_journal_path(p), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _stage_dir(parent, name, prefix):
    if isinstance(name, str) and name.startswith(prefix) and safe_part(name):
        return os.path.join(parent, name)
    return None


def _undo(p, journal):
    """Put back everything a (cut off or failed) restore swapped, newest first.
    Works from what is on disk, so it is right whether a rename happened or not.
    True if all was undone."""
    ok = True
    staging = _stage_dir(p.addons, journal.get("staging"), installer.STAGING_PREFIX)
    stage = _stage_dir(p.wtf, journal.get("wtf_stage"), RESTORE_PREFIX)
    files = journal.get("files") if isinstance(journal.get("files"), list) else []
    for item in reversed(files):
        try:
            i, rel, had_old = item
            parts = rel.split("/")
            if not (isinstance(i, int) and stage and all(safe_part(x) for x in parts)):
                continue
            target = os.path.join(p.accounts, *parts)
            if not _inside(os.path.dirname(target), p.accounts):
                continue
            aside = os.path.join(stage, "old", str(i))
            new = os.path.join(stage, "new", str(i))
            if had_old:
                if os.path.isfile(aside):
                    os.replace(aside, target)
            elif not os.path.lexists(new) and os.path.isfile(target):
                os.remove(target)
        except (OSError, ValueError, TypeError, AttributeError):
            ok = False
            log.exception("Undo of %r failed", item)
    addons = journal.get("addons") if isinstance(journal.get("addons"), list) else []
    for item in reversed(addons):
        try:
            folder, had_old = item
            if not _addon_name_ok(folder):
                continue
            target = os.path.join(p.addons, folder)
            aside = os.path.join(p.addons, installer.BACKUP_PREFIX + folder)
            staged = os.path.join(staging, folder) if staging else None
            if _is_link(target):
                continue
            new_in_place = os.path.isdir(target) and (
                (had_old and os.path.isdir(aside))
                or (not had_old and not (staged and os.path.lexists(staged))))
            if new_in_place:
                doomed = os.path.join(p.addons, installer.DELETING_PREFIX + folder)
                n = 2
                while os.path.lexists(doomed):
                    doomed = os.path.join(p.addons, f"{installer.DELETING_PREFIX}{folder}-{n}")
                    n += 1
                os.replace(target, doomed)
                installer._force_rmtree(doomed)   # if it fails, recover finishes it
            if had_old and os.path.isdir(aside) and not os.path.lexists(target):
                os.replace(aside, target)
        except (OSError, ValueError, TypeError):
            ok = False
            log.exception("Undo of %r failed", item)
    return ok


def _clean_up(p, journal, swapped_ok):
    """Remove the staging folders (and, after a finished restore, the old folders)."""
    staging = _stage_dir(p.addons, journal.get("staging"), installer.STAGING_PREFIX)
    stage = _stage_dir(p.wtf, journal.get("wtf_stage"), RESTORE_PREFIX)
    if swapped_ok:
        for folder, had_old in journal.get("addons", []):
            aside = os.path.join(p.addons, installer.BACKUP_PREFIX + folder)
            if had_old and os.path.isdir(aside) and _addon_name_ok(folder):
                installer._remove_tree(aside)
    for d in (staging, stage):
        if d and os.path.isdir(d):
            installer._remove_tree(d)


def recover_restore(addons_dir):
    """At start (and before a restore): a restore that was cut off (app killed, power cut)
    is undone, so nothing is left half-restored; leftover staging folders in WTF are
    removed. Call before installer.recover. Never raises. Returns [(action, what)]."""
    try:
        p = places(addons_dir)
    except BackupError:
        return []
    actions = []
    journal = _read_journal(p)
    if journal is not None:
        if _undo(p, journal):
            _clean_up(p, journal, swapped_ok=False)
            try:
                os.remove(_journal_path(p))
            except OSError:
                log.exception("Could not remove the restore journal")
            actions.append(("undone", "restore"))
        else:
            actions.append(("undo_failed", "restore"))   # tried again next start
    elif os.path.lexists(_journal_path(p)):   # damaged journal: nothing to go on
        _discard(_journal_path(p))
    if not os.path.lexists(_journal_path(p)):
        try:
            entries = list(os.scandir(p.wtf))
        except OSError:
            entries = []
        for e in entries:
            if e.name.startswith(RESTORE_PREFIX) and e.is_dir(follow_symlinks=False) \
                    and not _is_link(e.path) and installer._remove_tree(e.path):
                actions.append(("removed", e.name))
    for a in actions:
        log.info("Restore recovery: %s %s", *a)
    return actions


def restore(zip_path, addons_dir, safety_folder, addons=True, settings=True, bindings=True,
            account_map=None, now=None, progress=None):
    """Restore a backup into this PC's AddOns and WTF\\Account (merge). Returns
    RestoreResult. Raises BackupError; on any failure nothing is changed (and the safety
    copy made for it is removed again)."""
    p = places(addons_dir)
    info = read(zip_path)
    folders, files = plan(info, addons_dir, addons, settings, bindings, account_map)
    if not folders and not files:
        raise BackupError("nothing", zip_path)
    recover_restore(p.addons)
    installer.recover(p.addons)
    if os.path.lexists(_journal_path(p)):
        raise BackupError("other", "an earlier restore couldn't be undone yet")

    # Where everything goes, checked before anything is written.
    addon_targets = []
    for folder in folders:
        target = os.path.join(p.addons, folder)
        if _is_link(target) or (os.path.lexists(target) and not os.path.isdir(target)):
            raise BackupError("unsafe", f"{target} is not a plain folder")
        if os.path.lexists(os.path.join(p.addons, installer.BACKUP_PREFIX + folder)):
            raise BackupError("other", f"leftover backup of {folder}")
        addon_targets.append((folder, target))
    file_targets, seen = [], set()
    for zi, account, rest in files:
        acc_dir = os.path.join(p.accounts, account)
        target = os.path.join(acc_dir, *rest)
        if _is_link(acc_dir) or not _inside(os.path.dirname(target), acc_dir) \
                or _is_link(target) or (os.path.lexists(target) and not os.path.isfile(target)):
            raise BackupError("unsafe", f"{target} is not a plain file in {acc_dir}")
        key = os.path.normcase(target).lower()
        if key in seen:   # never two files onto one (and never twice in the safety copy)
            raise BackupError("same_account", target)
        seen.add(key)
        file_targets.append((zi, target, "/".join([account] + list(rest))))

    # 1. The safety copy of everything that will be replaced.
    old_addons = [f for f, t in addon_targets if os.path.isdir(t)]
    old_files = [(f"WTF/Account/{rel}", t) for _zi, t, rel in file_targets if os.path.isfile(t)]
    safety = None
    if old_addons or old_files:
        if in_game_folder(safety_folder, p.addons):
            raise BackupError("inside_game", safety_folder)
        try:
            os.makedirs(safety_folder, exist_ok=True)
        except OSError as e:
            raise _os_error(e, f"making {safety_folder}") from e
        parts = ([ADDONS] if old_addons else []) + ([SETTINGS] if old_files else [])
        safety = _write_zip(safety_path(safety_folder, now),
                            _addon_files(p, old_addons) + old_files, "safety", parts)
        log.info("Safety copy before restore: %s", safety)

    steps = len(addon_targets) + len(file_targets)
    journal = {"staging": None, "wtf_stage": None,
               "addons": [[f, os.path.isdir(t)] for f, t in addon_targets],
               "files": [[i, rel, os.path.isfile(t)]
                         for i, (_zi, t, rel) in enumerate(file_targets)]}
    journal_written = False
    try:
        # 2. Unpack everything into staging folders (AddOns and WTF; same drive = renames).
        budget = [0]
        try:
            with zipfile.ZipFile(zip_path) as zf:
                if addon_targets:
                    staging = tempfile.mkdtemp(prefix=installer.STAGING_PREFIX + "restore-",
                                               dir=p.addons)
                    journal["staging"] = os.path.basename(staging)
                    wanted = {f for f, _t in addon_targets}
                    for zi, parts, kind in info.entries:
                        if kind == ADDONS and parts[1] in wanted:
                            dest = os.path.join(staging, *parts[1:])
                            if not _inside(os.path.dirname(dest), staging):
                                raise BackupError("unsafe", zi.filename)
                            _extract(zf, zi, dest, budget)
                if file_targets:
                    os.makedirs(p.wtf, exist_ok=True)
                    stage = tempfile.mkdtemp(prefix=RESTORE_PREFIX, dir=p.wtf)
                    journal["wtf_stage"] = os.path.basename(stage)
                    os.makedirs(os.path.join(stage, "old"))
                    for i, (zi, _target, _rel) in enumerate(file_targets):
                        _extract(zf, zi, os.path.join(stage, "new", str(i)), budget)
        except BackupError:
            raise
        except (zipfile.BadZipFile, EOFError, RuntimeError, ValueError, zlib.error) as e:
            raise BackupError("bad_zip", str(e)) from e
        except OSError as e:
            raise _os_error(e, "unpacking") from e

        # 3. Swap in by renaming (journal first, so a cut-off restore is undone at start).
        try:
            _write_journal(p, journal)
            journal_written = True
            done = 0
            for folder, target in addon_targets:
                if os.path.isdir(target):
                    os.replace(target, os.path.join(p.addons, installer.BACKUP_PREFIX + folder))
                os.replace(os.path.join(p.addons, journal["staging"], folder), target)
                done += 1
                if progress:
                    progress(done, steps)
            stage = os.path.join(p.wtf, journal["wtf_stage"]) if file_targets else None
            for i, (_zi, target, _rel) in enumerate(file_targets):
                if os.path.isfile(target):
                    os.replace(target, os.path.join(stage, "old", str(i)))
                os.makedirs(os.path.dirname(target), exist_ok=True)
                os.replace(os.path.join(stage, "new", str(i)), target)
                done += 1
                if progress:
                    progress(done, steps)
            os.remove(_journal_path(p))   # the commit: from here on, the restore stays
            journal_written = False
        except OSError as e:
            raise _os_error(e, "putting restored files in place") from e
    except BaseException:
        undone = _undo(p, journal) if journal_written else True
        if undone:
            _clean_up(p, journal, swapped_ok=False)
            if journal_written:
                _discard(_journal_path(p))
            if safety:
                _discard(safety)   # nothing was changed, so no safety copy is needed
        raise
    _clean_up(p, journal, swapped_ok=True)
    log.info("Restored %d addons and %d settings files from %s", len(addon_targets),
             len(file_targets), zip_path)
    return RestoreResult([f for f, _t in addon_targets], len(file_targets), safety)
