"""Read an addon file the player picked (.zip, .rar, .7z) or an unpacked folder into a
checked tree in our own temp folder. Nothing here writes into the game folder.

Zip: Python's zipfile; every entry is checked before anything is unpacked.
Rar / 7z: the system's own tar (Windows 11 tar.exe = libarchive; Linux bsdtar); no RAR
code ships with the app. The listing is checked first, tar unpacks into an empty temp
folder under a size/time watchdog, then the unpacked tree is checked again with lstat.
Folder: copied file by file (never through links) with the same limits.
"""

import dataclasses
import logging
import os
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import time
import zipfile
import zlib

from am import installer

log = logging.getLogger(__name__)

MAX_FILE = 200 * 1024 * 1024        # the picked archive
MAX_UNPACKED = 500 * 1024 * 1024    # everything unpacked (inner archives share it)
MAX_ENTRIES = 20_000
MAX_RATIO = 200                     # compressed -> unpacked, per file over 1 MB
MAX_DEPTH = 24                      # folder levels inside an archive
MAX_INNER = 20                      # archives inside the archive (one level only)
TAR_TIMEOUT = 120                   # seconds for tar to list or unpack

ZIP, RAR, SEVEN_Z, FOLDER = "zip", "rar", "7z", "folder"
EXTENSIONS = {".zip": ZIP, ".rar": RAR, ".7z": SEVEN_Z}
_MAGIC = ((b"PK\x03\x04", ZIP), (b"PK\x05\x06", ZIP), (b"Rar!\x1a\x07", RAR),
          (b"7z\xbc\xaf\x27\x1c", SEVEN_Z))

# What gets installed into AddOns (everything WoW uses, plus readme/licence text).
INSTALL_EXT = {".toc", ".lua", ".xml", ".tga", ".blp", ".png", ".jpg", ".jpeg", ".ttf",
               ".otf", ".ogg", ".mp3", ".wav", ".m2", ".skin", ".txt", ".md"}
# Programs: one of these anywhere in the file refuses the whole file.
PROGRAM_EXT = {".exe", ".dll", ".com", ".scr", ".bat", ".cmd", ".ps1", ".psm1", ".psd1",
               ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh", ".hta", ".msi", ".msp",
               ".msix", ".appx", ".appxbundle", ".lnk", ".url", ".reg", ".cpl", ".sys",
               ".ocx", ".jar", ".pif", ".scf", ".inf", ".so", ".dylib", ".appimage",
               ".desktop", ".application", ".gadget", ".msc", ".xll", ".drv"}
PROGRAM_MAGIC = (b"MZ", b"\x7fELF")
ARCHIVE_EXT = set(EXTENSIONS)
SKIP_DIRS = {"__macosx"}            # macOS zip clutter: never unpacked


class ArchiveError(Exception):
    """kind: not_found, too_big, bad_archive, encrypted, unsafe, program (detail = the
    program's name), no_tar (no tar that can read this kind: rar/7z), timeout, empty,
    in_game (a folder inside the game's AddOns), locked, permission, disk_full, other."""

    def __init__(self, kind, detail=""):
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail


def _os_error(e, what):
    err = installer._os_error(e, what)
    kind = err.kind if err.kind in ("locked", "permission", "disk_full") else "other"
    return ArchiveError(kind, err.detail)


def ext_of(name):
    return os.path.splitext(name)[1].lower()


def kind_of(path):
    """ZIP / RAR / SEVEN_Z / FOLDER from the first bytes (so a renamed file still works),
    else from the extension; None if it's neither."""
    if os.path.isdir(path):
        return FOLDER
    try:
        with open(path, "rb") as f:
            head = f.read(8)
    except OSError:
        head = b""
    for magic, kind in _MAGIC:
        if head.startswith(magic):
            return kind
    return EXTENSIONS.get(ext_of(path))


def is_program_name(name):
    return ext_of(name) in PROGRAM_EXT


def file_rule(name):
    """'install' (WoW uses it), 'archive' (an inner .zip/.rar/.7z) or 'leave' (left out)."""
    e = ext_of(name)
    if e in INSTALL_EXT or (e == "" and not name.startswith(".")):
        return "install"
    if e in ARCHIVE_EXT:
        return "archive"
    return "leave"


def entry_parts(name):
    """An archive entry name -> its path parts, or ArchiveError("unsafe"). Every part
    must be a plain name that is the same on Windows and Linux (installer.safe_name)."""
    if (not name or "\\" in name or name.startswith("/")
            or (len(name) > 1 and name[1] == ":") or "\0" in name):
        raise ArchiveError("unsafe", f"bad path {name!r}")
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if not parts or len(parts) > MAX_DEPTH or not all(installer.safe_name(p) for p in parts):
        raise ArchiveError("unsafe", f"bad path {name!r}")
    return parts


class Budget:
    """Unpacked bytes and files across one pick (inner archives included)."""

    def __init__(self, max_bytes=None, max_entries=None):
        self.bytes, self.entries = 0, 0
        self.max_bytes = MAX_UNPACKED if max_bytes is None else max_bytes
        self.max_entries = MAX_ENTRIES if max_entries is None else max_entries

    def add(self, size=0, entries=0):
        self.bytes += size
        self.entries += entries
        if self.bytes > self.max_bytes:
            raise ArchiveError("too_big", "unpacks too big")
        if self.entries > self.max_entries:
            raise ArchiveError("too_big", "too many files")


def _check_names(names, budget):
    """Shared listing checks: safe parts, no programs, no duplicates (any letter case)."""
    seen = set()
    checked = []
    for name, is_dir in names:
        parts = entry_parts(name)
        if parts[0].lower() in SKIP_DIRS:
            continue
        if not is_dir and is_program_name(parts[-1]):
            raise ArchiveError("program", parts[-1])
        key = "/".join(parts).lower()
        if key in seen:
            if is_dir:
                continue   # tar lists a folder again when a file in it comes later
            raise ArchiveError("unsafe", f"duplicate {name!r}")
        seen.add(key)
        budget.add(entries=1)
        checked.append((parts, is_dir))
    return checked


# ---------- zip ----------
def _unpack_zip(path, dest, budget):
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError, ValueError) as e:
        raise ArchiveError("bad_archive", str(e)) from e
    with zf:
        infos = zf.infolist()
        if len(infos) > MAX_ENTRIES:
            raise ArchiveError("too_big", "too many files")
        checked = _check_names([(i.filename, i.is_dir()) for i in infos], budget)
        by_key = {}
        for info in infos:
            if info.flag_bits & 0x1:
                raise ArchiveError("encrypted", info.filename)
            if stat.S_ISLNK(info.external_attr >> 16):
                raise ArchiveError("unsafe", f"link {info.filename!r}")
            if (info.file_size > 1024 * 1024
                    and info.file_size > MAX_RATIO * max(info.compress_size, 1)):
                raise ArchiveError("too_big", f"suspicious compression {info.filename!r}")
            by_key.setdefault("/".join(p for p in info.filename.split("/")
                                       if p not in ("", ".")).lower(), info)
        planned = sum(by_key[("/".join(p)).lower()].file_size for p, d in checked if not d)
        if budget.bytes + planned > budget.max_bytes:
            raise ArchiveError("too_big", "unpacks too big")
        dest_real = os.path.realpath(dest)
        try:
            for parts, is_dir in checked:
                target = os.path.join(dest_real, *parts)
                if os.path.commonpath([dest_real, os.path.realpath(target)]) != dest_real:
                    raise ArchiveError("unsafe", "/".join(parts))
                if is_dir:
                    os.makedirs(target, exist_ok=True)
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                info = by_key["/".join(parts).lower()]
                with zf.open(info) as src, open(target, "xb") as dst:
                    while True:
                        chunk = src.read(64 * 1024)
                        if not chunk:
                            break
                        budget.add(size=len(chunk))
                        dst.write(chunk)
        except ArchiveError:
            raise
        except (zipfile.BadZipFile, EOFError, RuntimeError, ValueError, zipfile.LargeZipFile,
                NotImplementedError) as e:
            raise ArchiveError("bad_archive", str(e)) from e
        except FileExistsError as e:
            raise ArchiveError("unsafe", f"duplicate {e.filename!r}") from e
        except OSError as e:
            raise _os_error(e, "unpacking") from e


# ---------- rar / 7z through the system's tar ----------
def tar_path():
    """Windows: %SystemRoot%\\System32\\tar.exe (full path, never from PATH); Linux/other:
    bsdtar (GNU tar can't read rar). None if there is none."""
    if os.name == "nt":
        root = os.environ.get("SystemRoot") or r"C:\Windows"
        path = os.path.join(root, "System32", "tar.exe")
        return path if os.path.isfile(path) else None
    return shutil.which("bsdtar")


def _tar_env():
    """The system's environment for tar: a PyInstaller build changes LD_LIBRARY_PATH to its
    own libraries, which can break the system's libarchive (the _ORIG copy is the real one)."""
    env = dict(os.environ)
    if getattr(sys, "frozen", False) and os.name != "nt":
        orig = env.pop("LD_LIBRARY_PATH_ORIG", None)
        if orig is not None:
            env["LD_LIBRARY_PATH"] = orig
        else:
            env.pop("LD_LIBRARY_PATH", None)
    return env


def _no_window():
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def _tar_error(stderr, kind):
    low = (stderr or "").lower()
    if "encrypt" in low or "passphrase" in low or "password" in low:
        return ArchiveError("encrypted", "")
    if "unrecognized archive format" in low or "not supported" in low \
            or "unsupported" in low:
        return ArchiveError("no_tar", kind)
    return ArchiveError("bad_archive", (stderr or "").strip()[-300:])


def _dir_usage(path, stop_at_bytes, stop_at_files):
    total = files = 0
    for dirpath, dirs, names in os.walk(path):
        files += len(names) + len(dirs)
        for n in names:
            try:
                total += os.lstat(os.path.join(dirpath, n)).st_size
            except OSError:
                pass
        if total > stop_at_bytes or files > stop_at_files:
            break
    return total, files


class TarRunner:
    """Runs the system's tar. Tests swap it for a fake (same two methods)."""

    def __init__(self, exe=None, timeout=TAR_TIMEOUT):
        self.exe = exe
        self.timeout = timeout

    def _exe(self, kind):
        exe = self.exe or tar_path()
        if not exe:
            raise ArchiveError("no_tar", kind)
        return exe

    def _run_list(self, path, kind, flag):
        try:
            done = subprocess.run([self._exe(kind), flag, path], capture_output=True,
                                  stdin=subprocess.DEVNULL, timeout=self.timeout,
                                  env=_tar_env(), creationflags=_no_window())
        except subprocess.TimeoutExpired as e:
            raise ArchiveError("timeout", "listing") from e
        except OSError as e:
            raise ArchiveError("no_tar", f"{kind}: {e}") from e
        if done.returncode != 0:
            raise _tar_error(done.stderr.decode("utf-8", "replace"), kind)
        lines = [ln for ln in done.stdout.decode("utf-8", "replace").splitlines() if ln.strip()]
        if len(lines) > MAX_ENTRIES:
            raise ArchiveError("too_big", "too many files")
        return lines

    def list(self, path, kind):
        """[(name, is_dir)]: names from 'tar -tf', each entry's type from the first letter of
        'tar -tvf' (same order): d = folder, - = file; links (l, h) and anything else are
        refused (a rar lists its folders without a trailing '/')."""
        names = self._run_list(path, kind, "-tf")
        long = self._run_list(path, kind, "-tvf")
        if len(long) != len(names):
            raise ArchiveError("bad_archive", "listing mismatch")
        out = []
        for name, line in zip(names, long):
            t = line[:1]
            if t not in ("d", "-"):
                raise ArchiveError("unsafe", f"link or special entry {name!r}")
            out.append((name.rstrip("/"), t == "d" or name.endswith("/")))
        return out

    def unpack(self, path, dest, kind, max_bytes, max_files):
        """'tar -xf path -C dest' under a watchdog: stopped when dest grows past the limits
        or after the timeout."""
        exe = self._exe(kind)
        with tempfile.TemporaryFile() as errf:
            try:
                proc = subprocess.Popen([exe, "-xf", path, "-C", dest], stdin=subprocess.DEVNULL,
                                        stdout=subprocess.DEVNULL, stderr=errf,
                                        env=_tar_env(), creationflags=_no_window())
            except OSError as e:
                raise ArchiveError("no_tar", f"{kind}: {e}") from e
            start, problem = time.monotonic(), None
            while proc.poll() is None:
                time.sleep(0.1)
                size, files = _dir_usage(dest, max_bytes, max_files)
                if size > max_bytes or files > max_files:
                    problem = ArchiveError("too_big", "unpacks too big")
                elif time.monotonic() - start > self.timeout:
                    problem = ArchiveError("timeout", "unpacking")
                if problem:
                    proc.kill()
                    proc.wait()
                    raise problem
            errf.seek(0)
            err = errf.read(64 * 1024).decode("utf-8", "replace")
        if proc.returncode != 0:
            raise _tar_error(err, kind)


RAR5_SIGNATURE = b"Rar!\x1a\x07\x01\x00"
RAR4_SIGNATURE = b"Rar!\x1a\x07\x00"


def _vint(data, pos):
    """A RAR5 variable-length number at pos: (value, next pos). Raises IndexError at the end."""
    value = shift = 0
    while True:
        b = data[pos]
        pos += 1
        value |= (b & 0x7F) << shift
        if not b & 0x80:
            return value, pos
        shift += 7
        if shift > 63:
            raise ValueError("vint too long")


def _rar5_complete(data):
    """Walk every RAR5 header (CRC checked) to the end-of-archive header. libarchive says
    nothing when a cut-off file ends exactly between two headers, so this is our check."""
    pos = len(RAR5_SIGNATURE)
    while True:
        crc = struct.unpack_from("<I", data, pos)[0]
        size, body = _vint(data, pos + 4)
        if size < 2 or body + size > len(data) or size > 2 * 1024 * 1024:
            return False
        if zlib.crc32(data[pos + 4:body + size]) & 0xFFFFFFFF != crc:
            return False
        htype, p = _vint(data, body)
        flags, p = _vint(data, p)
        if flags & 0x1:
            _extra, p = _vint(data, p)
        data_size = 0
        if flags & 0x2:
            data_size, p = _vint(data, p)
        if htype == 5:                       # end of archive
            end_flags, _p = _vint(data, p)
            return not end_flags & 0x1       # 0x1: more volumes follow (a part only)
        if htype == 4:                       # headers encrypted: refused anyway
            raise ArchiveError("encrypted", "")
        pos = body + size + data_size
        if pos > len(data):
            return False


def _rar4_complete(data):
    """Walk RAR 1.5-4.x blocks (header CRC checked) to the end-of-archive block (0x7B)."""
    pos = len(RAR4_SIGNATURE)
    while True:
        head_crc, htype, flags, head_size = struct.unpack_from("<HBHH", data, pos)
        if head_size < 7 or pos + head_size > len(data):
            return False
        if zlib.crc32(data[pos + 2:pos + head_size]) & 0xFFFF != head_crc:
            return False
        if htype == 0x7B:
            return not flags & 0x1           # 0x1: more volumes follow
        add = 0
        if flags & 0x8000 or htype == 0x74:
            add = struct.unpack_from("<I", data, pos + 7)[0]
            if htype == 0x74 and flags & 0x100:
                add += struct.unpack_from("<I", data, pos + 32)[0] << 32
        pos += head_size + add
        if pos > len(data):
            return False


def check_rar_complete(path):
    """A .rar must be whole: the end-of-archive header must be there (a cut-off download
    would otherwise install part of an addon). Raises ArchiveError("bad_archive")."""
    try:
        with open(path, "rb") as f:
            data = f.read(MAX_FILE + 1)
    except OSError as e:
        raise _os_error(e, "reading the file") from e
    try:
        if data.startswith(RAR5_SIGNATURE):
            ok = _rar5_complete(data)
        elif data.startswith(RAR4_SIGNATURE):
            ok = _rar4_complete(data)
        else:
            ok = False
    except (IndexError, struct.error, ValueError):
        ok = False
    if not ok:
        raise ArchiveError("bad_archive", "damaged or cut off rar")


def _unpack_tar(path, dest, kind, budget, runner):
    if kind == RAR:
        check_rar_complete(path)
    # The listing first: '..', absolute paths, links, programs, duplicates refuse before
    # unpacking.
    names = runner.list(path, kind)
    listed_files = sum(1 for _n, is_dir in names if not is_dir)
    _check_names(names, Budget(max_entries=budget.max_entries - budget.entries))
    runner.unpack(path, dest, kind, budget.max_bytes - budget.bytes,
                  budget.max_entries - budget.entries)
    # Then the tree on disk is what counts: no links, plain names, within the limits, and
    # exactly as many files as listed (fewer = damaged, more = not what was checked).
    files = 0
    for parts, size, is_dir in walk_tree(dest):
        if not is_dir and is_program_name(parts[-1]):
            raise ArchiveError("program", parts[-1])
        files += not is_dir
        budget.add(size=size, entries=1)
    if files != listed_files:
        raise ArchiveError("bad_archive", f"{files} files unpacked, {listed_files} listed")


# ---------- folders ----------
def walk_tree(root, depth=0):
    """(parts, size, is_dir) of everything below root, checked with lstat: a link,
    junction or other special file, or a name that isn't plain, raises ArchiveError."""
    try:
        entries = sorted(os.scandir(root), key=lambda e: e.name)
    except OSError as e:
        raise _os_error(e, f"reading {root}") from e
    if depth >= MAX_DEPTH and entries:
        raise ArchiveError("unsafe", "too deep")
    for e in entries:
        if not installer.safe_name(e.name):
            raise ArchiveError("unsafe", f"bad name {e.name!r}")
        try:
            st = os.lstat(e.path)
        except OSError as err:
            raise _os_error(err, e.path) from err
        reparse = getattr(st, "st_file_attributes", 0) & 0x400   # FILE_ATTRIBUTE_REPARSE_POINT
        if stat.S_ISLNK(st.st_mode) or reparse or installer._is_link(e.path):
            raise ArchiveError("unsafe", f"link {e.name!r}")
        if stat.S_ISDIR(st.st_mode):
            yield [e.name], 0, True
            for parts, size, is_dir in walk_tree(e.path, depth + 1):
                yield [e.name] + parts, size, is_dir
        elif stat.S_ISREG(st.st_mode):
            yield [e.name], st.st_size, False
        else:
            raise ArchiveError("unsafe", f"special file {e.name!r}")


def _copy_folder(src, dest, budget):
    """Copy a picked folder into dest (our temp), checked like an archive."""
    for parts, size, is_dir in walk_tree(src):
        if parts[0].lower() in SKIP_DIRS:
            continue
        if not is_dir and is_program_name(parts[-1]):
            raise ArchiveError("program", parts[-1])
        budget.add(size=size, entries=1)
        target = os.path.join(dest, *parts)
        try:
            if is_dir:
                os.makedirs(target, exist_ok=True)
            else:
                os.makedirs(os.path.dirname(target), exist_ok=True)
                shutil.copyfile(os.path.join(src, *parts), target, follow_symlinks=False)
        except OSError as e:
            raise _os_error(e, "copying") from e


# ---------- after unpacking: no programs ----------
def check_programs(root):
    """A file starting like a program (MZ / ELF), whatever its name, refuses the whole
    file (a fake "addon"). Returns nothing; raises ArchiveError("program")."""
    for parts, _size, is_dir in walk_tree(root):
        if is_dir:
            continue
        if is_program_name(parts[-1]):
            raise ArchiveError("program", parts[-1])
        try:
            with open(os.path.join(root, *parts), "rb") as f:
                head = f.read(4)
        except OSError as e:
            raise _os_error(e, "reading") from e
        if head.startswith(PROGRAM_MAGIC):
            raise ArchiveError("program", parts[-1])


@dataclasses.dataclass
class Tree:
    root: str            # our temp folder holding the checked files
    kind: str            # ZIP / RAR / SEVEN_Z / FOLDER
    budget: Budget


def open_source(path, work_dir, runner=None, budget=None):
    """Unpack/copy what the player picked into a new folder in work_dir (ours). Archives are
    copied into work_dir first, so a file that changes while it's read can't fool the
    checks. Returns Tree. Raises ArchiveError."""
    runner = runner or TarRunner()
    budget = budget or Budget()
    if not os.path.lexists(path):
        raise ArchiveError("not_found", path)
    if installer._is_link(path):
        raise ArchiveError("unsafe", "link")
    kind = kind_of(path)
    if kind is None:
        raise ArchiveError("bad_archive", "not a zip, rar or 7z file")
    root = tempfile.mkdtemp(prefix="tree_", dir=work_dir)
    if kind == FOLDER:
        _copy_folder(path, root, budget)
    else:
        try:
            if os.path.getsize(path) > MAX_FILE:
                raise ArchiveError("too_big", "file too big")
            copy = os.path.join(work_dir, "picked" + (".zip" if kind == ZIP else "." + kind))
            n = 2
            while os.path.lexists(copy):
                copy = os.path.join(work_dir, f"picked{n}.{kind}")
                n += 1
            shutil.copyfile(path, copy)
            if os.path.getsize(copy) > MAX_FILE:
                raise ArchiveError("too_big", "file too big")
        except ArchiveError:
            raise
        except OSError as e:
            raise _os_error(e, "reading the file") from e
        if kind == ZIP:
            _unpack_zip(copy, root, budget)
        else:
            _unpack_tar(copy, root, kind, budget, runner)
    check_programs(root)
    return Tree(root, kind, budget)
