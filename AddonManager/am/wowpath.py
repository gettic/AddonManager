"""Find the WoW Forever Interface\\AddOns folder.

Order: registry (read-only), then common install paths on fixed drives. The UI
adds the saved choice before this and the folder picker after it.
"""

import logging
import os

from am.gamecheck import GAME_EXES

log = logging.getLogger(__name__)

ROOT_NAMES = ("World of Warcraft", "World of Warcraft Forever", "WoW Forever")
PARENTS = ("Program Files (x86)", "Program Files", "", "Games",
           "Blizzard", "Battle.net", os.path.join("Program Files (x86)", "Blizzard"),
           os.path.join("Program Files", "Blizzard"))

_BLIZZARD_KEY = r"SOFTWARE\WOW6432Node\Blizzard Entertainment\World of Warcraft"
_UNINSTALL_KEYS = (
    r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
    r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
)


def fixed_drives():
    """Root paths of fixed (local hard) drives, e.g. ['C:\\\\', 'D:\\\\']."""
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        mask = kernel32.GetLogicalDrives()
        drives = []
        for i in range(26):
            if mask & (1 << i):
                root = f"{chr(65 + i)}:\\"
                if kernel32.GetDriveTypeW(root) == 3:  # DRIVE_FIXED
                    drives.append(root)
        return drives
    except Exception:
        return ["C:\\"]


def read_registry():
    """Install folders from the registry. Read-only (KEY_READ); never writes."""
    paths = []
    try:
        import winreg
    except ImportError:
        return paths

    def value(key, name):
        try:
            v, _ = winreg.QueryValueEx(key, name)
            return v if isinstance(v, str) else ""
        except OSError:
            return ""

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _BLIZZARD_KEY, 0, winreg.KEY_READ) as k:
            p = value(k, "InstallPath")
            if p:
                paths.append(p)
    except OSError:
        pass
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for sub in _UNINSTALL_KEYS:
            try:
                with winreg.OpenKey(hive, sub, 0, winreg.KEY_READ) as parent:
                    i = 0
                    while True:
                        try:
                            name = winreg.EnumKey(parent, i)
                        except OSError:
                            break
                        i += 1
                        try:
                            with winreg.OpenKey(parent, name, 0, winreg.KEY_READ) as k:
                                display = value(k, "DisplayName").lower()
                                if "world of warcraft" in display or "forever" in display:
                                    loc = value(k, "InstallLocation")
                                    if loc:
                                        paths.append(loc)
                        except OSError:
                            continue
            except OSError:
                continue
    return paths


def _has_game_exe(path):
    try:
        return any(name.lower() in GAME_EXES for name in os.listdir(path))
    except OSError:
        return False


def is_client_dir(path):
    """A game client folder: has the game .exe, or WTF\\ or Interface\\."""
    return os.path.isdir(path) and (
        _has_game_exe(path)
        or os.path.isdir(os.path.join(path, "WTF"))
        or os.path.isdir(os.path.join(path, "Interface")))


def client_subdirs(root):
    """Battle.net-style client folders inside a root, like _retail_ or _forever_."""
    try:
        names = os.listdir(root)
    except OSError:
        return []
    return [os.path.join(root, n) for n in sorted(names)
            if n.startswith("_") and n.endswith("_") and len(n) > 2
            and is_client_dir(os.path.join(root, n))]


def looks_forever(client_dir):
    """Named like Forever, or its AddOns already hold a *_Forever.toc addon."""
    if "forever" in os.path.basename(client_dir).lower():
        return True
    addons = os.path.join(client_dir, "Interface", "AddOns")
    try:
        with os.scandir(addons) as it:
            for entry in it:
                if entry.is_dir() and os.path.isfile(
                        os.path.join(entry.path, f"{entry.name}_Forever.toc")):
                    return True
    except OSError:
        pass
    return False


def _client_of(path):
    """If path is a client folder or inside one (Interface, AddOns, an addon), return it."""
    p = path
    for _ in range(4):
        low = os.path.basename(p).lower()
        if low == "addons" and os.path.basename(os.path.dirname(p)).lower() == "interface":
            return os.path.dirname(os.path.dirname(p))
        if low == "interface" and is_client_dir(os.path.dirname(p)):
            return os.path.dirname(p)
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    return None


def candidates(picked):
    """Client folders a picked/found folder could mean, best first."""
    p = os.path.normpath(os.path.abspath(picked))
    if not os.path.isdir(p):
        return []
    inner = _client_of(p)
    if inner:
        return [inner]
    subs = client_subdirs(p)
    if subs:
        return sorted(subs, key=lambda d: not looks_forever(d))
    if is_client_dir(p):
        return [p]
    return []


def addons_dir_of(client_dir):
    return os.path.join(client_dir, "Interface", "AddOns")


def from_picked(picked):
    """('ok', addons_dir) | ('choose', None) for a root with several clients and
    none clearly Forever | ('not_wow', None)."""
    cands = candidates(picked)
    if not cands:
        return "not_wow", None
    if len(cands) == 1 or looks_forever(cands[0]):
        return "ok", addons_dir_of(cands[0])
    return "choose", None


def detect(drives=None, registry=read_registry):
    """Automatic search. Returns a list of AddOns folders that look like WoW Forever."""
    roots = []
    try:
        roots.extend(registry() or [])
    except Exception:
        log.exception("Registry lookup failed")
    for drive in (fixed_drives() if drives is None else drives):
        for parent in PARENTS:
            for name in ROOT_NAMES:
                roots.append(os.path.join(drive, parent, name))
    found, seen = [], set()
    for root in roots:
        key = os.path.normcase(os.path.normpath(root))
        if key in seen or not os.path.isdir(root):
            continue
        seen.add(key)
        for client in candidates(root):
            if looks_forever(client):
                addons = addons_dir_of(client)
                if os.path.normcase(addons) not in {os.path.normcase(f) for f in found}:
                    found.append(addons)
    log.info("Detected: %s", found)
    return found


def ensure_addons_dir(addons_dir):
    """Create Interface\\AddOns in a fresh client folder. Returns True if it exists."""
    try:
        os.makedirs(addons_dir, exist_ok=True)
        return True
    except OSError:
        return False
