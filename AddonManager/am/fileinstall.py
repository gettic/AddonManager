"""Install any addon from a file (.zip/.rar/.7z) or an unpacked folder the player picked:
from CurseForge, Wago or a friend, not just the guild's list.

scan() unpacks into our own temp folder (am\\archive.py checks everything), finds the
addon folders by their .toc files and says what each one would do (new / update /
replaces a copy put there by hand). install() then puts the chosen ones into AddOns with
the same staging + rename swap as guild installs. Rules:
  - only files WoW uses are installed (archive.INSTALL_EXT); programs refuse the whole file
  - never a Blizzard_* folder, never a guild-list addon (those come from the list)
  - a folder that is already there is zipped into the backups folder first ("Before
    install <date>.zip", a backup safety copy Restore can put back)
  - anything that fails leaves the old folder in place
The addons installed this way are remembered in the settings (tracked: folder -> info);
only those get Uninstall and an "Other addons" row. They are never in the usage ping.
"""

import dataclasses
import datetime
import logging
import os
import shutil
import tempfile
import time

from am import archive, backup, installer, toc, versions
from am.archive import ArchiveError
from am.installer import InstallError

log = logging.getLogger(__name__)

MAX_ADDON_DEPTH = 3           # root=0: addon folders up to two wrapper folders down
MAX_LEFT_OUT_NAMES = 50
SAFETY_STEM = "Before install"
SETTING = "file_addons"       # settings key: {folder: {"name", "version", "file", "date"}}
TEMP_PREFIX = "AddonManager_file_"   # scan()'s temp folders (see clean_temp)
TEMP_MAX_AGE = 2 * 60 * 60           # older ones are leftovers of a run that was closed/killed

NEW, UPDATE, SAME, HAND, GUILD = "new", "update", "same", "hand", "guild"
BLIZZARD, BAD_NAME = "blizzard", "bad_name"


@dataclasses.dataclass
class FoundAddon:
    folder: str                 # the folder name in AddOns
    title: str
    version: str                # from the toc WoW Forever loads, or None
    status: str                 # toc.FOREVER / OTHER_VERSION / NOT_FOREVER
    source: str                 # its folder in our temp tree
    renamed_from: str = None    # "Addon-main" -> "Addon"; "" = tocs at the top of the file
    files: list = dataclasses.field(default_factory=list)       # path parts to install
    left_out: list = dataclasses.field(default_factory=list)    # names not installed
    left_out_count: int = 0
    size: int = 0
    state: str = NEW            # NEW / UPDATE / SAME / HAND / GUILD
    installed_folder: str = None    # the existing folder's spelling, if there is one
    installed_version: str = None

    @property
    def can_install(self):
        return self.state != GUILD

    @property
    def tick(self):
        """Ticked at the start: everything allowed, except addons WoW Forever won't load."""
        return self.can_install and self.status != toc.NOT_FOREVER


@dataclasses.dataclass
class Plan:
    name: str                   # the picked file's (or folder's) name, for the window
    kind: str                   # archive.ZIP / RAR / SEVEN_Z / FOLDER
    work: str                   # our temp folder (close() removes it)
    addons: list = dataclasses.field(default_factory=list)       # [FoundAddon]
    skipped: list = dataclasses.field(default_factory=list)      # [(folder, BLIZZARD/BAD_NAME)]
    unused_archives: list = dataclasses.field(default_factory=list)

    def close(self):
        if self.work:
            shutil.rmtree(self.work, ignore_errors=True)
            self.work = None

    def addon(self, folder):
        return next((a for a in self.addons if a.folder == folder), None)


@dataclasses.dataclass
class Result:
    installed: list = dataclasses.field(default_factory=list)   # [(folder, version, title)]
    failed: tuple = None        # (folder, InstallError) of the one that stopped it
    safety: str = None          # the "Before install" zip, if one was made
    not_done: list = dataclasses.field(default_factory=list)    # chosen, not tried after a failure


# ---------- finding addon folders ----------
def _listing(path):
    try:
        with os.scandir(path) as it:
            entries = list(it)
    except OSError:
        return [], []
    files = sorted(e.name for e in entries if e.is_file(follow_symlinks=False))
    dirs = sorted(e.name for e in entries if e.is_dir(follow_symlinks=False))
    return files, dirs


def _skip_dir(name):
    return name.startswith(".") or name.lower() in archive.SKIP_DIRS


def find_addons(root):
    """[(folder, path, renamed_from)] of the addon folders in an unpacked tree.
    A folder is an addon when it holds a toc of its own name (any suffix, any case); its
    insides are never searched (libraries inside an addon stay part of it). A folder (or
    the top of the file) holding tocs of one other name becomes that addon ("Addon-main"
    with Addon.toc -> Addon). Tocs of several names there: ArchiveError("ambiguous")."""
    found = []

    def visit(path, name, depth):
        files, dirs = _listing(path)
        tocs = [f for f in files if f.lower().endswith(".toc")]
        if name is not None and any(toc.belongs(t, name) for t in tocs):
            found.append((name, path, None))
            return
        if tocs:
            bases = {}
            for t in tocs:
                base = toc.split_toc_name(t)[0]
                bases.setdefault(base, t[:len(base)])
            if len(bases) != 1:
                raise ArchiveError("ambiguous", name or "")
            found.append((next(iter(bases.values())), path, name or ""))
            return
        if depth >= MAX_ADDON_DEPTH:
            return
        for d in dirs:
            if not _skip_dir(d):
                visit(os.path.join(path, d), d, depth + 1)

    visit(root, None, 0)
    seen = set()
    for folder, _p, _r in found:
        if folder.lower() in seen:
            raise ArchiveError("ambiguous", f"{folder} twice")
        seen.add(folder.lower())
    return found


def _inner_archives(root):
    out = []
    for parts, _size, is_dir in archive.walk_tree(root):
        if not is_dir and archive.file_rule(parts[-1]) == "archive" \
                and not any(_skip_dir(p) for p in parts[:-1]):
            out.append(os.path.join(root, *parts))
    return out


def _addon_files(path):
    """(install parts, left-out names, left-out count, size) for one addon folder."""
    files, left, count, size = [], [], 0, 0
    for parts, fsize, is_dir in archive.walk_tree(path):
        if is_dir:
            continue
        if any(_skip_dir(p) for p in parts[:-1]) or archive.file_rule(parts[-1]) != "install":
            count += 1
            if len(left) < MAX_LEFT_OUT_NAMES:
                left.append("/".join(parts))
            continue
        files.append(parts)
        size += fsize
    return files, left, count, size


def _existing(addons_dir, folder):
    """The spelling of an existing folder with this name in AddOns (any case), or None."""
    if not addons_dir:
        return None
    try:
        with os.scandir(addons_dir) as entries:
            for e in entries:
                if e.name.lower() == folder.lower() and e.is_dir(follow_symlinks=False) \
                        and not installer._is_link(e.path):
                    return e.name
    except OSError:
        pass
    return None


def _lower_set(names):
    return {n.lower() for n in (names or ()) if isinstance(n, str)}


def _set_state(a, addons_dir, guild_folders, tracked):
    if a.folder.lower() in _lower_set(guild_folders):
        a.state = GUILD
        return
    existing = _existing(addons_dir, a.folder)
    a.installed_folder = existing
    if existing is None:
        a.state = NEW
        return
    a.installed_version = toc.describe(os.path.join(addons_dir, existing), existing)[2]
    if existing.lower() not in _lower_set(tracked):
        a.state = HAND
    elif a.version and a.installed_version and versions.same(a.version, a.installed_version):
        a.state = SAME
    else:
        a.state = UPDATE


def _in_game_pick(path, addons_dir):
    """A picked folder inside the game's AddOns, or one holding it (the WoW folder)."""
    if not addons_dir or not os.path.isdir(path):
        return False
    return backup.is_under(path, addons_dir) or backup.is_under(addons_dir, path)


def scan(path, addons_dir, guild_folders=(), tracked=None, runner=None, tmp_root=None):
    """Read a picked file/folder into a Plan (the caller must plan.close() it).
    Raises ArchiveError (nothing is left behind then)."""
    if _in_game_pick(path, addons_dir):
        raise ArchiveError("in_game", path)
    work = tempfile.mkdtemp(prefix=TEMP_PREFIX, dir=tmp_root)
    try:
        budget = archive.Budget()
        tree = archive.open_source(path, work, runner=runner, budget=budget)
        plan = Plan(os.path.basename(os.path.normpath(path)), tree.kind, work)
        found = find_addons(tree.root)
        inner = _inner_archives(tree.root)
        if not found and inner:
            if len(inner) > archive.MAX_INNER:
                raise ArchiveError("too_big", "too many files inside")
            for p in inner:
                sub = archive.open_source(p, work, runner=runner, budget=budget)
                found += find_addons(sub.root)   # one level only: archives in these stay unused
            names = [f for f, _p, _r in found]
            if len({n.lower() for n in names}) != len(names):
                raise ArchiveError("ambiguous", "same addon twice")
        elif inner:
            plan.unused_archives = [os.path.relpath(p, tree.root) for p in inner]
        for folder, src, renamed in found:
            if folder.lower().startswith("blizzard_"):
                plan.skipped.append((folder, BLIZZARD))
                continue
            if not installer.addon_name_ok(folder):
                plan.skipped.append((folder, BAD_NAME))
                continue
            chosen, title, version, status = toc.describe(src, folder)
            files, left, count, size = _addon_files(src)
            a = FoundAddon(folder, title, version, status, src, renamed, files, left, count, size)
            _set_state(a, addons_dir, guild_folders, tracked or {})
            plan.addons.append(a)
        if not plan.addons and not plan.skipped:
            raise ArchiveError("empty", plan.name)
        plan.addons.sort(key=lambda a: a.folder.lower())
        return plan
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise


def clean_temp(tmp_root=None, now=None, max_age=TEMP_MAX_AGE):
    """At start: remove our own scan folders (TEMP_PREFIX*) older than max_age from the
    temp folder, left when the app was closed or killed while reading a file or with the
    confirm window open (up to a 200 MB copy + 500 MB unpacked). Only real folders with our
    name, never links. Returns the names removed. Never raises."""
    root = tmp_root or tempfile.gettempdir()
    now = time.time() if now is None else now
    removed = []
    try:
        with os.scandir(root) as entries:
            old = [e for e in entries if e.name.startswith(TEMP_PREFIX)
                   and e.is_dir(follow_symlinks=False) and not installer._is_link(e.path)
                   and now - e.stat(follow_symlinks=False).st_mtime > max_age]
    except OSError:
        return removed
    for e in old:
        if installer._force_rmtree(e.path):
            removed.append(e.name)
    if removed:
        log.info("Removed %d old Add from file temp folder(s)", len(removed))
    return removed


# ---------- installing ----------
def safety_path(folder, now=None):
    """<folder>\\Before install 2026-11-10 2130.zip (" (2)" if it exists)."""
    now = now or datetime.datetime.now()
    return backup._unique(folder, f"{SAFETY_STEM} {now:%Y-%m-%d %H%M}")


def make_safety_copy(addons_dir, folders, dest_folder, now=None):
    """Zip the addon folders about to be replaced into dest_folder as a backup safety copy
    (Settings > Restore can put them back). Raises InstallError("safety")."""
    try:
        p = backup.places(addons_dir)
        dest_folder = os.path.abspath(dest_folder)
        if backup.in_game_folder(dest_folder, p.addons):
            raise backup.BackupError("inside_game", dest_folder)
        os.makedirs(dest_folder, exist_ok=True)
        files = backup._addon_files(p, folders)
        path = backup._write_zip(safety_path(dest_folder, now), files, "safety", [backup.ADDONS])
    except backup.BackupError as e:
        raise InstallError("safety", e.kind) from e
    except OSError as e:
        err = installer._os_error(e, "safety copy")
        raise InstallError("safety", err.kind) from e
    log.info("Safety copy before install: %s", path)
    return path


def _install_one(a, addons_dir):
    """Copy one found addon into a staging folder in AddOns, then swap it in."""
    name = a.installed_folder or a.folder
    target = installer.addon_target(addons_dir, name)
    if installer._is_link(target):
        raise InstallError("link", target)
    if os.path.lexists(target) and not os.path.isdir(target):
        raise InstallError("unsafe", f"{target} is not a folder")
    staging = None
    try:
        try:
            staging = tempfile.mkdtemp(prefix=installer.STAGING_PREFIX, dir=addons_dir)
            staged = os.path.join(staging, name)
            os.makedirs(staged)
            for parts in a.files:
                dest = os.path.join(staged, *parts)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                shutil.copyfile(os.path.join(a.source, *parts), dest, follow_symlinks=False)
        except OSError as e:
            raise installer._os_error(e, "unpacking") from e
        installer.swap_in(staged, target, os.path.join(addons_dir, installer.BACKUP_PREFIX + name),
                          name)
    finally:
        if staging:
            installer._remove_tree(staging)
    log.info("Installed %s %s from a file into %s", name, a.version, addons_dir)
    return name


def install(plan, folders, addons_dir, guild_folders, tracked, safety_folder, progress=None,
            now=None):
    """Install the chosen folders of a Plan. Guild-list and Blizzard_ folders are refused
    (nothing done). Folders already there are zipped into safety_folder first. Stops at the
    first failure (that folder keeps its old version). Returns Result."""
    addons_dir = installer.check_addons_dir(addons_dir)
    chosen = [a for a in plan.addons if a.folder in set(folders)]
    for a in chosen:
        _set_state(a, addons_dir, guild_folders, tracked or {})   # it may have changed
        if not a.can_install or not installer.addon_name_ok(a.folder):
            raise InstallError("not_allowed", a.folder)
    installer.recover(addons_dir)
    result = Result()
    replace = [a.installed_folder for a in chosen if a.installed_folder]
    if replace:
        result.safety = make_safety_copy(addons_dir, replace, safety_folder, now)
    for i, a in enumerate(chosen):
        if progress:
            progress(i, len(chosen))
        try:
            name = _install_one(a, addons_dir)
        except InstallError as e:
            log.warning("Install from file stopped at %s: %s", a.folder, e)
            result.failed = (a.folder, e)
            result.not_done = [b.folder for b in chosen[i + 1:]]
            break
        result.installed.append((name, a.version, a.title))
    if progress:
        progress(len(chosen), len(chosen))
    if result.safety and not result.installed:
        backup._discard(result.safety)   # nothing was replaced: no safety copy needed
        result.safety = None
    return result


# ---------- remembering what came from a file ----------
def clean_tracked(value):
    """The settings value as {folder: info dict}, dropping anything malformed."""
    out = {}
    if isinstance(value, dict):
        for folder, info in value.items():
            if isinstance(folder, str) and installer.addon_name_ok(folder) \
                    and isinstance(info, dict):
                out[folder] = {k: v for k, v in info.items()
                               if k in ("name", "version", "file", "date")
                               and isinstance(v, str) and len(v) <= 200}
    return out


def track(tracked, result, source_name, today=None):
    """A new tracked dict with the addons just installed added."""
    out = clean_tracked(tracked)
    today = today or datetime.date.today().isoformat()
    for folder, version, title in result.installed:
        for old in [f for f in out if f.lower() == folder.lower()]:
            del out[old]
        out[folder] = {"name": title or folder, "version": version or "",
                       "file": (source_name or "")[:100], "date": today}
    return out


def untrack(tracked, folder):
    return {f: i for f, i in clean_tracked(tracked).items() if f.lower() != folder.lower()}


@dataclasses.dataclass
class OtherRow:
    folder: str
    title: str
    version: str
    status: str     # toc.FOREVER / OTHER_VERSION / NOT_FOREVER


def other_rows(addons_dir, tracked, guild_folders=()):
    """(rows, tracked without folders that are gone) for the "Other addons" section:
    title and version from the toc on disk now. A folder that is on the guild list now
    (the guild added an addon a player had installed from a file) is dropped too: it shows
    once, as the guild's row. Never raises."""
    rows, kept = [], {}
    guild = _lower_set(guild_folders)
    for folder, info in sorted(clean_tracked(tracked).items(), key=lambda x: x[0].lower()):
        if folder.lower() in guild:
            continue
        path = os.path.join(addons_dir or "", folder)
        try:
            ok = bool(addons_dir) and os.path.isdir(path) and not installer._is_link(path)
        except OSError:
            ok = False
        if not ok:
            continue
        kept[folder] = info
        _chosen, title, version, status = toc.describe(path, folder)
        rows.append(OtherRow(folder, title or info.get("name") or folder, version, status))
    return rows, kept
