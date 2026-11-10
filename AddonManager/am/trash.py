"""Move a folder to the Recycle Bin (Windows) or the Trash (Linux, freedesktop.org
standard), so a removed addon can be put back by hand. Standard library only.

available(path) -> "bin", "trash" or None (no bin on that drive: deleting would be for good).
send(path) moves the folder, or raises TrashError. Never deletes for good by itself.
"""

import datetime
import logging
import os
import stat
import urllib.parse

log = logging.getLogger(__name__)


class TrashError(Exception):
    """kind: no_trash, locked, permission, other."""

    def __init__(self, kind, detail=""):
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail


# ---------- Windows: the Recycle Bin ----------
FO_DELETE = 3
FOF_SILENT = 0x0004
FOF_NOCONFIRMATION = 0x0010
FOF_ALLOWUNDO = 0x0040
FOF_NOERRORUI = 0x0400
FOF_WANTNUKEWARNING = 0x4000   # Windows asks before deleting for good (safety net)
DRIVE_FIXED = 3


def _drive_root(path):
    """'C:\\' for a local drive path, None for network (UNC) paths."""
    drive = os.path.splitdrive(os.path.abspath(path))[0]
    if len(drive) != 2 or drive[1] != ":":
        return None
    return drive + "\\"


def _windows_available(path):
    import ctypes
    from ctypes import wintypes
    root = _drive_root(path)
    if not root:
        return None
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
        if kernel32.GetDriveTypeW(root) != DRIVE_FIXED:
            return None   # USB sticks, network and CD drives have no Recycle Bin

        class SHQUERYRBINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("i64Size", ctypes.c_longlong),
                        ("i64NumItems", ctypes.c_longlong)]
        info = SHQUERYRBINFO()
        info.cbSize = ctypes.sizeof(info)
        shell32 = ctypes.WinDLL("shell32")
        shell32.SHQueryRecycleBinW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(SHQUERYRBINFO)]
        if shell32.SHQueryRecycleBinW(root, ctypes.byref(info)) != 0:
            return None
    except (OSError, AttributeError):
        log.exception("Could not check the Recycle Bin for %s", root)
        return None
    return "bin"


def _windows_send(path):
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT),
                    ("pFrom", wintypes.LPCWSTR), ("pTo", wintypes.LPCWSTR),
                    ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]
    op = SHFILEOPSTRUCTW()
    op.wFunc = FO_DELETE
    op.pFrom = os.path.abspath(path) + "\0"   # list ends with two zero characters
    op.fFlags = (FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_NOERRORUI | FOF_SILENT
                 | FOF_WANTNUKEWARNING)
    shell32 = ctypes.WinDLL("shell32")
    shell32.SHFileOperationW.argtypes = [ctypes.POINTER(SHFILEOPSTRUCTW)]
    code = shell32.SHFileOperationW(ctypes.byref(op))
    if code in (32, 33):
        raise TrashError("locked", f"code {code}")
    if code == 5:
        raise TrashError("permission", f"code {code}")
    if code != 0 or op.fAnyOperationsAborted:
        raise TrashError("other", f"code {code}, aborted {bool(op.fAnyOperationsAborted)}")


# ---------- Linux: the freedesktop.org Trash ----------
def _data_home():
    return os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"),
                                                          ".local", "share")


def _device(path):
    return os.stat(path).st_dev


def _mount_point(path, device=_device):
    """The top folder of the drive that path is on."""
    path = os.path.abspath(path)
    dev = device(path)
    while True:
        parent = os.path.dirname(path)
        if parent == path:
            return path
        try:
            if device(parent) != dev:
                return path
        except OSError:
            return path
        path = parent


def _existing_parent(path):
    path = os.path.abspath(path)
    while not os.path.exists(path):
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    return path


def linux_trash_dir(path, data_home=None, uid=None, device=_device):
    """(trash folder, top folder or None) for the item at path, or None if there is
    no usable Trash on its drive. Home Trash when on the same drive as the home data
    folder; otherwise <drive top>/.Trash/<uid> (sticky, not a link) or <drive top>/.Trash-<uid>."""
    data_home = data_home or _data_home()
    uid = os.getuid() if uid is None and hasattr(os, "getuid") else (uid or 0)
    item_parent = os.path.dirname(os.path.abspath(path))
    try:
        item_dev = device(item_parent)
        if device(_existing_parent(data_home)) == item_dev:
            return os.path.join(data_home, "Trash"), None
        top = _mount_point(item_parent, device)
    except OSError:
        return None
    shared = os.path.join(top, ".Trash")
    try:
        st = os.lstat(shared)
        if stat.S_ISDIR(st.st_mode) and not stat.S_ISLNK(st.st_mode) and st.st_mode & stat.S_ISVTX:
            own = os.path.join(shared, str(uid))
            if not os.path.islink(own):
                return own, top
    except OSError:
        pass
    own = os.path.join(top, f".Trash-{uid}")
    if os.path.islink(own):
        return None
    if os.path.isdir(own) or os.access(top, os.W_OK):
        return own, top
    return None


def linux_send(path, trash_dir, top=None, now=None):
    """Move path into trash_dir/files with its trash_dir/info/<name>.trashinfo note."""
    files, info = os.path.join(trash_dir, "files"), os.path.join(trash_dir, "info")
    for d in (trash_dir, files, info):
        if os.path.islink(d):
            raise TrashError("other", f"{d} is a link")
        os.makedirs(d, mode=0o700, exist_ok=True)
    base = os.path.basename(os.path.abspath(path))
    info_path = name = None
    for n in range(1, 1000):
        candidate = base if n == 1 else f"{base}.{n}"
        if os.path.lexists(os.path.join(files, candidate)):
            continue
        try:
            fd = os.open(os.path.join(info, candidate + ".trashinfo"),
                         os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            continue
        name, info_path = candidate, os.path.join(info, candidate + ".trashinfo")
        break
    if not name:
        raise TrashError("other", "no free name in the Trash")
    stored = os.path.abspath(path) if top is None else os.path.relpath(os.path.abspath(path), top)
    when = (now or datetime.datetime.now()).strftime("%Y-%m-%dT%H:%M:%S")
    text = f"[Trash Info]\nPath={urllib.parse.quote(stored.replace(os.sep, '/'), safe='/')}\n" \
           f"DeletionDate={when}\n"
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.rename(path, os.path.join(files, name))
    except OSError as e:
        try:
            os.remove(info_path)
        except OSError:
            pass
        if isinstance(e, PermissionError):
            raise TrashError("permission", str(e)) from e
        raise TrashError("other", str(e)) from e
    return os.path.join(files, name)


# ---------- this system ----------
def available(path):
    """"bin" (Windows Recycle Bin), "trash" (Linux Trash) or None (no bin on that drive)."""
    try:
        if os.name == "nt":
            return _windows_available(path)
        return "trash" if linux_trash_dir(path) else None
    except Exception:
        log.exception("Could not check the trash for %s", path)
        return None


def send(path):
    """Move the folder at path to the Recycle Bin / Trash. Raises TrashError."""
    if os.name == "nt":
        _windows_send(path)
        return
    where = linux_trash_dir(path)
    if not where:
        raise TrashError("no_trash", path)
    linux_send(path, where[0], where[1])
