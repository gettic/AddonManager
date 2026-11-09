"""Find the WoW Forever Interface\\AddOns folder.

Order: registry (read-only), then common install paths on fixed drives. On Linux
(SteamOS) the game runs in a Wine/Proton prefix: the prefixes' system.reg (read-only)
and the usual paths inside their drive_c. The UI adds the saved choice before this
and the folder picker after it.
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


# Linux folders care about upper/lower case; Wine (and so the game) doesn't.
CASE_SENSITIVE = os.name != "nt"


def child(path, name):
    """path/name, using the name as it is really spelled on disk (Addons, interface...)
    when the file system cares about case. Falls back to path/name if none exists."""
    exact = os.path.join(path, name)
    if not CASE_SENSITIVE:
        return exact
    try:
        names = sorted(os.listdir(path))
    except OSError:
        return exact
    if name in names:
        return exact
    low = name.lower()
    for n in names:
        if n.lower() == low:
            return os.path.join(path, n)
    return exact


def child_path(path, *parts):
    for part in parts:
        path = child(path, part)
    return path


def _has_game_exe(path):
    try:
        return any(name.lower() in GAME_EXES for name in os.listdir(path))
    except OSError:
        return False


def is_client_dir(path):
    """A game client folder: has the game .exe, or WTF\\ or Interface\\."""
    return os.path.isdir(path) and (
        _has_game_exe(path)
        or os.path.isdir(child(path, "WTF"))
        or os.path.isdir(child(path, "Interface")))


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
    addons = addons_dir_of(client_dir)
    try:
        with os.scandir(addons) as it:
            for entry in it:
                if entry.is_dir() and os.path.isfile(
                        child(entry.path, f"{entry.name}_Forever.toc")):
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
    return _prefix_clients(p)


def _prefix_clients(p):
    """A picked Wine prefix (compatdata/<id>, pfx or drive_c): the clients in its
    usual install folders, best first."""
    if os.path.basename(p).lower() == "drive_c":
        drive_c = p
    elif os.path.isdir(os.path.join(p, "drive_c")):
        drive_c = os.path.join(p, "drive_c")
    else:
        drive_c = os.path.join(p, "pfx", "drive_c")
    if not os.path.isdir(drive_c):
        return []
    found, seen = [], set()
    for parent in PARENTS:
        parts = [x for x in parent.split(os.sep) if x]
        for name in ROOT_NAMES:
            root = child_path(drive_c, *parts, name)
            if not os.path.isdir(root):
                continue
            for c in client_subdirs(root) or ([root] if is_client_dir(root) else []):
                key = os.path.normcase(os.path.realpath(c))
                if key not in seen:
                    seen.add(key)
                    found.append(c)
    return sorted(found, key=_rank)


def addons_dir_of(client_dir):
    """client/Interface/AddOns, reusing an existing folder spelled another way on Linux
    (never a second Interface or AddOns folder next to the real one)."""
    return child_path(client_dir, "Interface", "AddOns")


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


# ---------- Linux / SteamOS: the game runs inside a Wine/Proton prefix ----------
# Places where removable drives / SD cards are mounted (SteamOS: /run/media/...).
MEDIA_ROOTS = ("/run/media", "/media", "/mnt")
MAX_PREFIXES = 500
MAX_REG_BYTES = 64 * 1024 * 1024
_STEAM_FLATPAK = os.path.join(".var", "app", "com.valvesoftware.Steam")
_WINE_KEYS = ("software\\wow6432node\\blizzard entertainment\\world of warcraft",
              "software\\blizzard entertainment\\world of warcraft")
_WINE_UNINSTALL = ("software\\microsoft\\windows\\currentversion\\uninstall\\",
                   "software\\wow6432node\\microsoft\\windows\\currentversion\\uninstall\\")
_REG_VALUE_RE = re.compile(r'^"((?:[^"\\]|\\.)*)"="((?:[^"\\]|\\.)*)"\s*$')
_VDF_PATH_RE = re.compile(r'"path"\s+"((?:[^"\\]|\\.)*)"', re.IGNORECASE)


def _subdirs(path):
    try:
        with os.scandir(path) as it:
            return sorted(e.path for e in it if e.is_dir())
    except OSError:
        return []


def _vdf_paths(vdf_file):
    """Library folders listed in Steam's libraryfolders.vdf ("path" "...")."""
    try:
        with open(vdf_file, encoding="utf-8", errors="replace") as f:
            text = f.read(1024 * 1024)
    except OSError:
        return []
    return [m.group(1).replace("\\\\", "\\") for m in _VDF_PATH_RE.finditer(text)]


def steam_libraries(home, media_roots=MEDIA_ROOTS):
    """Steam folders (normal, ~/.steam links, Flatpak), the libraries they list, and
    SteamLibrary folders on SD cards / other drives."""
    steams = [os.path.join(home, ".local", "share", "Steam"),
              os.path.join(home, ".steam", "steam"),
              os.path.join(home, ".steam", "root"),
              os.path.join(home, _STEAM_FLATPAK, ".local", "share", "Steam"),
              os.path.join(home, _STEAM_FLATPAK, "data", "Steam")]
    libs = list(steams)
    for s in steams:
        libs += _vdf_paths(os.path.join(s, "steamapps", "libraryfolders.vdf"))
        libs += _vdf_paths(os.path.join(s, "config", "libraryfolders.vdf"))
    for media in media_roots:
        for d in _subdirs(media):            # /run/media/mmcblk0p1, /run/media/deck
            for base in [d] + _subdirs(d):   # /run/media/deck/<label>
                libs += [base, os.path.join(base, "SteamLibrary")]
    return libs


def wine_prefixes(home=None, media_roots=MEDIA_ROOTS):
    """Wine/Proton prefixes (folders holding drive_c), newest first: Steam's
    compatdata/<id>/pfx (non-Steam games run with Proton), Lutris (~/Games/<name>),
    Heroic, Bottles (Flatpak and normal), PlayOnLinux and ~/.wine."""
    home = home or os.path.expanduser("~")
    found = []
    for lib in steam_libraries(home, media_roots):
        found += [os.path.join(d, "pfx") for d in _subdirs(os.path.join(lib, "steamapps", "compatdata"))]
    games = os.path.join(home, "Games")
    flat = os.path.join(home, ".var", "app")
    found += _subdirs(games)
    for heroic in (os.path.join(games, "Heroic", "Prefixes"),
                   os.path.join(flat, "com.heroicgameslauncher.hgl", "Games", "Heroic", "Prefixes")):
        found += _subdirs(heroic) + _subdirs(os.path.join(heroic, "default"))
    found += _subdirs(os.path.join(flat, "com.usebottles.bottles", "data", "bottles", "bottles"))
    found += _subdirs(os.path.join(home, ".local", "share", "bottles", "bottles"))
    found += _subdirs(os.path.join(home, ".PlayOnLinux", "wineprefix"))
    found.append(os.path.join(home, ".wine"))
    prefixes, seen = [], set()
    for p in found:
        if not os.path.isdir(os.path.join(p, "drive_c")):
            continue
        key = os.path.normcase(os.path.realpath(p))
        if key not in seen:
            seen.add(key)
            prefixes.append(p)

    def mtime(p):
        try:
            return os.path.getmtime(p)
        except OSError:
            return 0
    prefixes.sort(key=mtime, reverse=True)
    return prefixes[:MAX_PREFIXES]


def _reg_unescape(s):
    return re.sub(r"\\(.)", lambda m: m.group(1), s)


def read_wine_registry(prefix):
    """Install folders (Windows paths) from a prefix's system.reg, Wine's registry as
    plain text. Read-only; same keys as read_registry."""
    path = os.path.join(prefix, "system.reg")
    try:
        if os.path.getsize(path) > MAX_REG_BYTES:
            return []
        f = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return []
    paths, section, vals = [], None, {}

    def flush():
        if section in _WINE_KEYS:
            if vals.get("installpath"):
                paths.append(vals["installpath"])
        elif section and section.startswith(_WINE_UNINSTALL):
            display = vals.get("displayname", "").lower()
            if ("world of warcraft" in display or "forever" in display) \
                    and vals.get("installlocation"):
                paths.append(vals["installlocation"])

    with f:
        for line in f:
            line = line.rstrip("\r\n")
            if line.startswith("["):
                flush()
                end = line.rfind("]")
                name = _reg_unescape(line[1:end]).lower() if end > 0 else ""
                wanted = name in _WINE_KEYS or name.startswith(_WINE_UNINSTALL)
                section, vals = (name if wanted else None), {}
            elif section and line.startswith('"'):
                m = _REG_VALUE_RE.match(line)
                if m:
                    vals[_reg_unescape(m.group(1)).lower()] = _reg_unescape(m.group(2))
        flush()
    return paths


def wine_to_linux(prefix, win_path):
    """C:\\Program Files (x86)\\World of Warcraft -> <prefix>/drive_c/Program Files (x86)/...
    Other drive letters through the prefix's dosdevices links. None if not mappable."""
    m = re.match(r"^([A-Za-z]):[\\/]*(.*)$", (win_path or "").strip())
    if not m:
        return None
    parts = [p for p in re.split(r"[\\/]+", m.group(2)) if p]
    if any(p in (".", "..") for p in parts):
        return None
    letter = m.group(1).lower()
    dev = os.path.join(prefix, "dosdevices", letter + ":")
    if os.path.isdir(dev):
        base = os.path.realpath(dev)
    elif letter == "c":
        base = os.path.join(prefix, "drive_c")
    else:
        return None
    return child_path(base, *parts)


def _has_battlenet(drive_c):
    return any(os.path.isdir(child_path(drive_c, *parts)) for parts in (
        ("Program Files (x86)", "Battle.net"), ("Program Files", "Battle.net"),
        ("ProgramData", "Battle.net")))


def linux_roots(home=None, media_roots=MEDIA_ROOTS):
    """(registry paths, common install paths) found in the Wine prefixes, plus a game
    folder copied straight into ~/Games or ~."""
    home = home or os.path.expanduser("~")
    registry_paths, roots = [], []
    for prefix in wine_prefixes(home, media_roots):
        drive_c = os.path.join(prefix, "drive_c")
        if _has_battlenet(drive_c):   # only then is system.reg worth reading
            for win in read_wine_registry(prefix):
                p = wine_to_linux(prefix, win)
                if p:
                    registry_paths.append(p)
        for parent in PARENTS:
            parts = [x for x in parent.split(os.sep) if x]
            for name in ROOT_NAMES:
                roots.append(child_path(drive_c, *parts, name))
    for base in (os.path.join(home, "Games"), home):
        for name in ROOT_NAMES:
            roots.append(os.path.join(base, name))
    return registry_paths, roots


def detect(drives=None, registry=read_registry, linux=None, home=None,
           media_roots=MEDIA_ROOTS):
    """Automatic search. Only real WoW Forever clients are picked: Forever by name
    or .exe first; an unknown client holding Forever addons only if it is the only
    one; known other clients (_retail_, _classic_beta_, ...) never.
    Windows: registry + fixed drives. Linux (SteamOS): Wine/Proton prefixes (their
    system.reg + common paths). Passing drives means the Windows search (tests)."""
    if linux is None:
        linux = os.name != "nt" and drives is None
    if linux:
        try:
            reg_paths, common = linux_roots(home, media_roots)
        except Exception:
            log.exception("Linux folder search failed")
            reg_paths, common = [], []
    else:
        common = []
        try:
            reg_paths = list(registry() or [])
        except Exception:
            log.exception("Registry lookup failed")
            reg_paths = []
        for drive in (fixed_drives() if drives is None else drives):
            for parent in PARENTS:
                for name in ROOT_NAMES:
                    common.append(os.path.join(drive, parent, name))
    roots = []
    for p in reg_paths:
        roots.append(p)
        if os.path.basename(os.path.normpath(p)).startswith("_"):
            roots.append(os.path.dirname(os.path.normpath(p)))   # its siblings too
    roots += common
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
