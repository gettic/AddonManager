"""The Addon Manager updates its own .exe from gettic/AddonManager's GitHub releases.

Only after the player clicks Update now. Steps: check (latest normal release, newer than
this one, asset AddonManager.exe with a sha256 digest) -> download next to the exe as
AddonManager.exe.new (sha256 + size + 'MZ' checked) -> swap (running exe -> .old, .new ->
exe; put back on failure) -> start the new exe -> it writes update-ok.txt -> the old one
exits. If the new one doesn't confirm in time: roll back and never offer that version again.
Leftover .old/.new files (exactly those two names) are removed at the next start.
Never writes inside a WoW folder, never uses a token, nothing from any other host.
Linux (SteamOS) works the same with its own asset (config.APP_ASSET, an AppImage), an
ELF + AppImage check instead of 'MZ', and the .new file made executable before the swap
(Linux lets a running program be renamed, so the swap needs no locks). Inside an AppImage
the running program sits in a read-only mount, so the file that is updated is the
.AppImage itself ($APPIMAGE), see exe_path.
"""

import dataclasses
import logging
import os
import subprocess
import sys
import threading
import time

from am import config, download, github, versions, wowpath
from am import manifest as manifest_mod

log = logging.getLogger(__name__)

MAX_BYTES = 200 * 1024 * 1024
MIN_BYTES = 1024 * 1024          # a real one-file build is far bigger
OK_FILE = "update-ok.txt"
WAIT_SECONDS = 30
SETTINGS_FAILED = "update_failed"
# First bytes of a real program for this system: Windows .exe, Linux ELF.
PROGRAM_MAGIC = b"MZ" if os.name == "nt" else b"\x7fELF"
# A Linux update is a type 2 AppImage: an ELF with "AI\x02" at byte 8.
APPIMAGE_MAGIC = b"AI\x02"
APPIMAGE_MAGIC_AT = 8
# Set by the AppImage runtime (APPIMAGE, APPDIR, ARGV0, OWD) and by our AppRun; never
# passed on to the restarted program (its own runtime sets them again).
APPIMAGE_ENV = ("APPIMAGE", "APPDIR", "ARGV0", "OWD", "ADDONMANAGER_APPDIR")


def is_program(head, linux=None):
    """True if head (the file's first bytes) is a program for this system: 'MZ' on
    Windows; an ELF AppImage on Linux."""
    linux = os.name != "nt" if linux is None else linux
    if linux:
        return (head[:4] == b"\x7fELF" and
                head[APPIMAGE_MAGIC_AT:APPIMAGE_MAGIC_AT + len(APPIMAGE_MAGIC)] == APPIMAGE_MAGIC)
    return head[:2] == b"MZ"


class UpdateError(Exception):
    """kind: in_wow, read_only, offline, timeout, rate_limited, damaged, locked, failed."""

    def __init__(self, kind, detail=""):
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail


@dataclasses.dataclass(frozen=True)
class Update:
    version: str
    tag: str
    url: str
    sha256: str
    size: int

    @property
    def page_url(self):
        """The release page (the "What's new" link); built here, never taken from the API."""
        return f"https://github.com/{config.APP_REPO}/releases/tag/{self.tag}"


DOWNLOAD_PAGE = f"https://github.com/{config.APP_REPO}/releases/latest"


def exe_path(env=None, executable=None, frozen=None, linux=None):
    """The file to update: the running .exe, or on Linux inside an AppImage the .AppImage
    file ($APPIMAGE; the program itself is in a read-only mount). None when not running
    as the built app (from source). $APPIMAGE only counts when the running program lies
    inside this AppImage's folder ($APPDIR, or ADDONMANAGER_APPDIR from our AppRun), so a
    value inherited from some other AppImage is ignored."""
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    if not frozen:
        return None
    env = os.environ if env is None else env
    linux = os.name != "nt" if linux is None else linux
    executable = os.path.abspath(executable or sys.executable)
    appimage = env.get("APPIMAGE")
    if linux and appimage and os.path.isabs(appimage) and os.path.isfile(appimage):
        for appdir in (env.get("APPDIR"), env.get("ADDONMANAGER_APPDIR")):
            if appdir and os.path.isabs(appdir) and _inside(executable, appdir):
                return os.path.realpath(appimage)
    return executable


def side_paths(exe):
    return exe + ".new", exe + ".old"


# ---------- 1. is there a newer version? ----------
def pick(release, current, allow_local=False, failed=None):
    """An Update from a (trimmed) release, or None if it isn't newer or isn't safe."""
    tag = release["tag"]
    version = github.plain_tag(tag)
    if not version or not manifest_mod._VERSION_RE.fullmatch(version) or not tag.isprintable() \
            or "/" in tag or "\\" in tag or ".." in tag:
        log.warning("App release tag not usable: %r", tag)
        return None
    if release.get("prerelease") or versions.is_prerelease(version):
        log.info("App pre-release %s not offered (the app updates to normal releases only)", tag)
        return None
    if versions.compare(version, current) != 1:
        return None
    if not isinstance(failed, str):
        failed = None   # damaged setting: ignore it
    if failed and versions.same(version, failed):
        log.info("App update %s failed before; not offered again", version)
        return None
    asset = next((a for a in release["assets"]
                  if a["name"] == config.APP_ASSET and a.get("state", "uploaded") == "uploaded"),
                 None)
    if asset is None:
        log.warning("App release %s has no %s", tag, config.APP_ASSET)
        return None
    if not github.download_allowed(asset["url"], config.APP_REPO, allow_local):
        log.warning("App download not allowed: %s", asset["url"])
        return None
    digest = github.parse_digest(asset["digest"])
    if not digest:
        log.warning("App release %s has no sha256 digest; refused", tag)
        return None
    if not isinstance(asset["size"], int) or asset["size"] < 1:
        log.warning("App release %s has no size; refused", tag)
        return None
    return Update(version, tag, asset["url"], digest, asset["size"])


def check(current, base=None, allow_local=False, cache_file=None, use_fresh_cache=True,
          failed=None, now=None):
    """The newer Update or None. Never raises: problems only go to the log."""
    base = base or config.GITHUB_API
    now = time.time() if now is None else now
    try:
        cache = github.load_cache(cache_file) if cache_file else {}
        key = f"{base}|app:{config.APP_REPO}"
        cached = github._cached(cache, key)
        release = None
        if cached and use_fresh_cache and 0 <= now - cached["time"] < github.CACHE_SECONDS:
            release = cached["release"]
        if release is None:
            try:
                release = github.latest_release(config.APP_REPO, base)
                if cache_file:
                    cache[key] = {"time": now, "release": release}
                    github.save_cache(cache_file, cache)
            except github.ReleaseError as e:
                if e.kind == "rate_limited" and cached:
                    release = cached["release"]
                else:
                    log.info("App update check: %s", e)
                    return None
        return pick(release, current, allow_local, failed)
    except Exception:
        log.exception("App update check failed")
        return None


# ---------- 2. may we write here? ----------
def _inside(path, folder):
    try:
        path, folder = os.path.normcase(os.path.abspath(path)), os.path.normcase(os.path.abspath(folder))
        return os.path.commonpath([path, folder]) == folder
    except ValueError:   # other drive
        return False


def _real_client(path):
    """A real game client folder: holds the game .exe (Wow.exe & co., or a Forever .exe
    next to Interface\\ or WTF\\). Interface\\ or WTF\\ alone (an unzipped addon pack, a
    settings backup) doesn't count."""
    if wowpath._has_game_exe(path):
        return True
    return wowpath._forever_exe(path) and (os.path.isdir(wowpath.child(path, "Interface"))
                                           or os.path.isdir(wowpath.child(path, "WTF")))


def _real_root(path):
    """A real install root: Battle.net's .build.info, or _xxx_ folders that are real clients."""
    if os.path.isfile(os.path.join(path, ".build.info")):
        return True
    return any(_real_client(c) for c in wowpath.client_subdirs(path))


def in_wow_folder(folder, addons_dir=None):
    """True if folder is inside the WoW install in use, or inside a real game install
    (a client with the game .exe, or a root with .build.info / real _xxx_ clients)."""
    if addons_dir:
        client = os.path.dirname(os.path.dirname(addons_dir))
        roots = [client]
        parent = os.path.dirname(client)
        if parent != client and (wowpath.client_subdirs(parent)
                                 or os.path.basename(parent).lower()
                                 in {n.lower() for n in wowpath.ROOT_NAMES}):
            roots.append(parent)
        if any(_inside(folder, r) for r in roots):
            return True
    p = os.path.abspath(folder)
    while True:
        if _real_client(p) or _real_root(p):
            return True
        parent = os.path.dirname(p)
        if parent == p:
            return False
        p = parent


def check_folder(exe, addons_dir=None):
    """Raise UpdateError(in_wow / read_only) if we must not update in the exe's folder."""
    folder = os.path.dirname(exe)
    if in_wow_folder(folder, addons_dir):
        raise UpdateError("in_wow", folder)
    probe = exe + ".update-test"
    try:
        with open(probe, "wb") as f:
            f.write(b"ok")
        os.remove(probe)
    except OSError as e:
        raise UpdateError("read_only", str(e)) from e


# ---------- 3. download ----------
def _remove(path):
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


def fetch(update, exe, allow_local=False, progress=None, min_bytes=MIN_BYTES):
    """Download the update to <exe>.new and check it. Returns the .new path.
    On any problem the .new file is removed and UpdateError raised; the exe is untouched."""
    new, _old = side_paths(exe)
    if not _remove(new):
        raise UpdateError("locked", new)
    url_check = None if allow_local else download.github_host_ok
    try:
        download.download_file(update.url, new, MAX_BYTES, expected_sha256=update.sha256,
                               progress=progress, timeout=30, url_check=url_check)
        size = os.path.getsize(new)
        if size != update.size or size < min_bytes:
            raise UpdateError("damaged", f"size {size}, expected {update.size}")
        with open(new, "rb") as f:
            if not is_program(f.read(16)):
                raise UpdateError("damaged", "not a program for this system")
        if os.name != "nt":
            os.chmod(new, 0o755)   # downloads aren't executable on Linux
        return new
    except download.DownloadError as e:
        _remove(new)
        kind = e.kind if e.kind in ("offline", "timeout", "rate_limited") else "damaged"
        raise UpdateError(kind, str(e)) from e
    except UpdateError:
        _remove(new)
        raise
    except OSError as e:
        _remove(new)
        raise UpdateError("failed", str(e)) from e


# ---------- 4. swap ----------
def _retry(action, tries, delay):
    for i in range(tries):
        try:
            return action()
        except OSError:
            if i == tries - 1:
                raise
            time.sleep(delay)


def swap(exe, tries=5, delay=0.6):
    """exe -> exe.old, exe.new -> exe. On failure the old exe is put back and
    UpdateError('locked') raised."""
    new, old = side_paths(exe)
    if not os.path.isfile(new):
        raise UpdateError("failed", "the downloaded file is gone (antivirus?)")
    if not _remove(old):
        raise UpdateError("locked", old)
    try:
        _retry(lambda: os.replace(exe, old), tries, delay)
    except OSError as e:
        _remove(new)
        raise UpdateError("locked", str(e)) from e
    try:
        _retry(lambda: os.replace(new, exe), tries, delay)
    except OSError as e:
        try:
            _retry(lambda: os.replace(old, exe), tries, delay)
        except OSError:
            log.exception("Could not put the old exe back: %s", old)
        _remove(new)
        raise UpdateError("locked", str(e)) from e


def rollback(exe, proc=None, tries=10, delay=0.5):
    """Stop the new exe (if started) and put exe.old back. True if it worked.
    The new exe is renamed to exe.new first (renaming works even if it still runs);
    cleanup removes it at the next start."""
    if proc is not None:
        _stop(proc)
    new, old = side_paths(exe)
    if not os.path.isfile(old):
        log.error("Rollback: %s is missing", old)
        return False
    try:
        if os.path.exists(exe):
            if not _remove(new):
                _retry(lambda: os.remove(new), tries, delay)
            _retry(lambda: os.replace(exe, new), tries, delay)
        _retry(lambda: os.replace(old, exe), tries, delay)
        return True
    except OSError:
        log.exception("Rollback failed: %s", old)
        return False


def _stop(proc):
    """Stop the new exe and its child (a one-file build runs as two processes)."""
    try:
        if os.name == "nt" and getattr(proc, "pid", None):
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=15,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            proc.kill()
    except Exception:
        pass
    try:
        proc.wait(timeout=10)
    except Exception:
        pass


# ---------- 5. restart and confirm ----------
def ok_path():
    return os.path.join(config.home_dir(), OK_FILE)


def clear_ok():
    _remove(ok_path())


def mark_ok(version):
    """Called by the new exe once its window is up."""
    try:
        os.makedirs(config.home_dir(), exist_ok=True)
        with open(ok_path(), "w", encoding="utf-8") as f:
            f.write(versions.plain(version))
    except OSError:
        log.exception("Could not write %s", ok_path())


def restart_env(environ=None, linux=None, frozen=None):
    """The environment for the new program: a fresh PyInstaller start
    (PYINSTALLER_RESET_ENVIRONMENT instead of reusing this process's unpacked files), no
    AppImage variables of this run, and for the built Linux program the library path from
    before PyInstaller changed it (LD_LIBRARY_PATH_ORIG; as archive._tar_env)."""
    env = dict(os.environ if environ is None else environ)
    linux = os.name != "nt" if linux is None else linux
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    for name in ("_MEIPASS2", "_PYI_APPLICATION_HOME_DIR", "_PYI_ARCHIVE_FILE",
                 "_PYI_PARENT_PROCESS_LEVEL") + APPIMAGE_ENV:
        env.pop(name, None)
    if linux and frozen:
        orig = env.pop("LD_LIBRARY_PATH_ORIG", None)
        if orig:
            env["LD_LIBRARY_PATH"] = orig
        else:
            env.pop("LD_LIBRARY_PATH", None)
    return env


def restart(exe, old_version):
    """Start the new exe (on Linux the .AppImage file) with restart_env."""
    env = restart_env()
    return subprocess.Popen([exe, "--updated-from", versions.plain(old_version)], env=env,
                            close_fds=True, cwd=os.path.dirname(exe))


def wait_ok(version, proc=None, timeout=None, step=0.25):
    """True once update-ok.txt holds version; False on timeout (WAIT_SECONDS) or if the
    new exe exits first."""
    end = time.time() + (WAIT_SECONDS if timeout is None else timeout)
    while time.time() < end:
        try:
            with open(ok_path(), "r", encoding="utf-8") as f:
                if versions.same(f.read().strip(), version):
                    return True
        except OSError:
            pass
        if proc is not None and proc.poll() is not None:
            return False
        time.sleep(step)
    return False


def remember_failed(version):
    s = config.load_settings()
    s[SETTINGS_FAILED] = versions.plain(version)
    config.save_settings(s)


# ---------- 6. clean up at start ----------
def cleanup(exe, tries=1, delay=3.0, quiet=False):
    """Remove <exe>.old and <exe>.new (only those two names). Returns True when both are
    gone. quiet: no "still in use" log line (the slow retries log it once themselves)."""
    if not exe:
        return True
    for i in range(tries):
        existed = [p for p in side_paths(exe) if os.path.exists(p)]
        if all(_remove(p) for p in side_paths(exe)):
            if existed:
                log.info("Removed old update files: %s", ", ".join(
                    os.path.basename(p) for p in existed))
            return True
        if i < tries - 1:
            time.sleep(delay)
    if not quiet:
        log.info("Old update files still in use: %s", exe)
    return False


def _cleanup_loop(exe, tries, delay, slow_delay, slow_tries):
    """Fast tries first (the old exe is usually closed within seconds), then one try every
    slow_delay seconds for as long as this app runs (slow_tries=None) or slow_tries times.
    The "still in use" line is logged once. Never raises."""
    try:
        if cleanup(exe, tries, delay):
            return True
        n = 0
        while slow_tries is None or n < slow_tries:
            time.sleep(slow_delay)
            n += 1
            if cleanup(exe, quiet=True):
                return True
        return False
    except Exception:   # a background helper: never take the app down
        log.exception("Cleaning up old update files failed")
        return False


def cleanup_later(exe, tries=20, delay=3.0, slow_delay=30.0, slow_tries=None):
    """Clean up in a background thread (the old exe may still be closing; Windows can't
    delete a running .exe). Keeps trying every slow_delay seconds while the app runs.
    Returns the thread (None if there is nothing to clean)."""
    if exe and any(os.path.exists(p) for p in side_paths(exe)):
        t = threading.Thread(target=_cleanup_loop, daemon=True,
                             args=(exe, tries, delay, slow_delay, slow_tries))
        t.start()
        return t
    return None
