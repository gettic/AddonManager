"""Find the WoW Forever Interface\\AddOns folder.

Order: registry (read-only), then common install paths on fixed drives. The UI
adds the saved choice before this and the folder picker after it.
"""

import logging
import os
import re

from am.gamecheck import GAME_EXES

log = logging.getLogger(__name__)

FOREVER, OTHER, UNKNOWN = "forever", "other", "unknown"
# Words in a client folder name that mean "not WoW Forever" (_retail_, _ptr_,
# _classic_beta_, _classic_era_, _anniversary_, ...).
OTHER_WORDS = {"retail", "ptr", "xptr", "beta", "alpha", "classic", "era", "anniversary",
               "event", "test", "hardcore", "sod", "plunderstorm", "wowlabs"}

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


def _forever_exe(path):
    try:
        return any(n.lower().endswith(".exe") and "forever" in n.lower()
                   for n in os.listdir(path))
    except OSError:
        return False


def client_kind(client_dir):
    """'forever' (named like Forever or has a Forever .exe), 'other' (a known other
    client such as _retail_, _ptr_, _classic_beta_, with or without the trailing
    underscore) or 'unknown'. Never 'forever' just because of the addons inside."""
    name = os.path.basename(os.path.normpath(client_dir)).lower()
    if "forever" in name or _forever_exe(client_dir):
        return FOREVER
    words = {w for w in re.split(r"[_\s\-.]+", name) if w}
    if words & OTHER_WORDS:
        return OTHER
    return UNKNOWN


def has_forever_addons(client_dir):
    """Its AddOns already hold an addon with a <Name>_Forever.toc (weak hint only)."""
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


def looks_forever(client_dir):
    """Good enough to use without asking: a Forever client, or an unknown client
    that already holds Forever addons. Known other clients never count."""
    kind = client_kind(client_dir)
    return kind == FOREVER or (kind == UNKNOWN and has_forever_addons(client_dir))


def _rank(client_dir):
    kind = client_kind(client_dir)
    if kind == FOREVER:
        return 0
    if kind == UNKNOWN:
        return 1 if has_forever_addons(client_dir) else 2
    return 3


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
        return sorted(subs, key=_rank)
    if is_client_dir(p):
        return [p]
    return []


def addons_dir_of(client_dir):
    return os.path.join(client_dir, "Interface", "AddOns")


def from_picked(picked):
    """('ok', addons_dir) | ('other', addons_dir) when the one client is a known
    non-Forever client (used, with a warning) | ('choose', None) for a root with
    several clients and none clearly Forever | ('not_wow', None)."""
    cands = candidates(picked)
    if not cands:
        return "not_wow", None
    if len(cands) == 1:
        status = "other" if client_kind(cands[0]) == OTHER else "ok"
        return status, addons_dir_of(cands[0])
    if looks_forever(cands[0]):
        return "ok", addons_dir_of(cands[0])
    return "choose", None


class Found(list):
    """detect() result: AddOns folders to use, best first. `others` lists the
    client folders that were seen but are not WoW Forever (for the message)."""

    def __init__(self, items=(), others=()):
        super().__init__(items)
        self.others = list(others)


def detect(drives=None, registry=read_registry):
    """Automatic search. Only real WoW Forever clients are picked: Forever by name
    or .exe first; an unknown client holding Forever addons only if it is the only
    one; known other clients (_retail_, _classic_beta_, ...) never."""
    roots = []
    try:
        for p in registry() or []:
            roots.append(p)
            if os.path.basename(os.path.normpath(p)).startswith("_"):
                roots.append(os.path.dirname(os.path.normpath(p)))   # its siblings too
    except Exception:
        log.exception("Registry lookup failed")
    for drive in (fixed_drives() if drives is None else drives):
        for parent in PARENTS:
            for name in ROOT_NAMES:
                roots.append(os.path.join(drive, parent, name))
    forever, maybe, others, seen, clients = [], [], [], set(), set()
    for root in roots:
        key = os.path.normcase(os.path.normpath(root))
        if key in seen or not os.path.isdir(root):
            continue
        seen.add(key)
        for client in candidates(root):
            ckey = os.path.normcase(os.path.normpath(client))
            if ckey in clients:
                continue
            clients.add(ckey)
            kind = client_kind(client)
            if kind == FOREVER:
                forever.append(client)
            elif kind == UNKNOWN and has_forever_addons(client):
                maybe.append(client)
            else:
                others.append(client)
    if forever:
        picked, others = forever, others + maybe
    elif len(maybe) == 1:
        picked = maybe
    else:
        picked, others = [], maybe + others
    found = Found([addons_dir_of(c) for c in picked], others)
    log.info("Detected: %s (other clients: %s)", list(found), found.others)
    return found


def ensure_addons_dir(addons_dir):
    """Create Interface\\AddOns in a fresh client folder. Returns True if it exists."""
    try:
        os.makedirs(addons_dir, exist_ok=True)
        return True
    except OSError:
        return False
