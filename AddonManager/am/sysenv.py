"""The built Linux program (PyInstaller, made on Debian) on a different Linux system.

Two things it carries over from the Debian build machine:
- Its own OpenSSL looks for certificates where Debian keeps them (/usr/lib/ssl). SteamOS and
  other systems keep them elsewhere, so every HTTPS call failed: fix_certificates points
  OpenSSL at the system's own bundle (SSL_CERT_FILE / SSL_CERT_DIR) once at start.
- PyInstaller sets LD_LIBRARY_PATH to its own libraries (_internal). Programs we start
  (xdg-open, kde-open, bsdtar, the browser) would load our old libraries instead of the
  system's and fail: system_env gives them the system's environment back (and
  _browser_clean does the same for Python's own browser search, which has no env argument).
On Windows and when run from source nothing here changes anything.
"""

import logging
import os
import ssl
import subprocess
import sys
import threading
import webbrowser

log = logging.getLogger(__name__)

# Where Linux systems keep their certificate bundle, in the order we look.
CA_FILES = ("/etc/ssl/certs/ca-certificates.crt",                # Debian, Ubuntu, Arch, SteamOS
            "/etc/pki/tls/certs/ca-bundle.crt",                  # Fedora, RHEL
            "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem",  # Fedora, RHEL (newer)
            "/etc/ssl/ca-bundle.pem",                            # openSUSE
            "/etc/ssl/cert.pem")                                 # Alpine, others
CA_DIR = "/etc/ssl/certs"   # a hashed folder: used only when no bundle file exists

# Variables PyInstaller and the AppImage set for this program only; programs we start
# must not see them (another AppImage would think it's ours).
OWN_ENV = ("_MEIPASS2", "_PYI_APPLICATION_HOME_DIR", "_PYI_ARCHIVE_FILE",
           "_PYI_PARENT_PROCESS_LEVEL", "_PYI_LINUX_PROCESS_NAME", "TCL_LIBRARY",
           "TK_LIBRARY", "APPIMAGE", "APPDIR", "ARGV0", "OWD", "ADDONMANAGER_APPDIR")


def _built_linux(linux, frozen):
    linux = os.name != "nt" if linux is None else linux
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    return bool(linux and frozen)


def _has_files(folder):
    """A certificate folder that exists and has something in it (an empty one, like a
    leftover Debian-style /usr/lib/ssl/certs, checks nothing)."""
    try:
        with os.scandir(folder) as entries:
            return any(True for _ in entries)
    except OSError:
        return False


def fix_certificates(environ=None, linux=None, frozen=None, defaults=None, files=CA_FILES,
                     ca_dir=CA_DIR):
    """Built Linux program only: when OpenSSL's own certificate places don't exist here,
    point it at the system's bundle (SSL_CERT_FILE, else SSL_CERT_DIR). Call once at start,
    before any network call. Returns (variable, path) when it set one, else None. A
    variable the player (or the system) set already is never changed. Never raises."""
    try:
        environ = os.environ if environ is None else environ
        if not _built_linux(linux, frozen):
            return None
        if environ.get("SSL_CERT_FILE") or environ.get("SSL_CERT_DIR"):
            return None
        paths = (defaults or ssl.get_default_verify_paths)()
        # None when the place doesn't exist; an empty folder doesn't count either
        if paths.cafile or (paths.capath and _has_files(paths.capath)):
            return None
        choice = next((("SSL_CERT_FILE", f) for f in files if os.path.isfile(f)), None)
        if choice is None and ca_dir and _has_files(ca_dir):
            choice = ("SSL_CERT_DIR", ca_dir)
        if choice is None:
            log.warning("Certificates: no system certificates found")
            return None
        environ[choice[0]] = choice[1]
        log.info("Certificates: %s=%s", choice[0], choice[1])
        return choice
    except Exception:
        log.exception("Certificates: check failed")
        return None


def system_env(environ=None, linux=None, frozen=None):
    """The environment for a program we start: for the built Linux program the library
    path from before PyInstaller changed it (LD_LIBRARY_PATH_ORIG, else none) and none of
    our own variables (OWN_ENV). Otherwise an unchanged copy."""
    env = dict(os.environ if environ is None else environ)
    if _built_linux(linux, frozen):
        orig = env.pop("LD_LIBRARY_PATH_ORIG", None)
        if orig:
            env["LD_LIBRARY_PATH"] = orig
        else:
            env.pop("LD_LIBRARY_PATH", None)
        for name in OWN_ENV:
            env.pop(name, None)
    return env


_ENV_LOCK = threading.Lock()


def _browser_clean(browser, url, environ=None, linux=None, frozen=None):
    """browser(url) (Python's own browser search) with the system's environment, so the
    browser it starts doesn't get our libraries (LD_LIBRARY_PATH) or variables. webbrowser
    has no env argument: os.environ is swapped just for the call, then put back."""
    environ = os.environ if environ is None else environ
    if not _built_linux(linux, frozen):
        return bool(browser(url))
    with _ENV_LOCK:
        saved = dict(environ)
        clean = system_env(environ, linux, frozen)
        try:
            for name in [n for n in environ if n not in clean]:
                del environ[name]
            environ.update(clean)
            return bool(browser(url))
        finally:
            for name in [n for n in environ if n not in saved]:
                del environ[name]
            environ.update(saved)


def _xdg_open(target, popen):
    """xdg-open <target> with the system's environment, detached, no output. True when
    it started; FileNotFoundError when there's no xdg-open."""
    popen(["xdg-open", target], env=system_env(), stdin=subprocess.DEVNULL,
          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
          start_new_session=True)
    return True


def open_link(url, popen=None, browser=None, windows=None):
    """Open a web link in the player's browser. True when something was started. Never
    raises (the caller shows the link to copy when this is False)."""
    browser = browser or webbrowser.open
    windows = os.name == "nt" if windows is None else windows
    try:
        if windows:
            return bool(browser(url))
        try:
            return _xdg_open(url, popen or subprocess.Popen)
        except FileNotFoundError:   # no xdg-open: Python's own browser search
            return _browser_clean(browser, url)
    except Exception:
        log.exception("Could not open a link")
        return False


def open_folder(path, popen=None, windows=None):
    """Show a folder in Explorer / the file manager. True when started. Never raises."""
    windows = os.name == "nt" if windows is None else windows
    try:
        if windows:
            os.startfile(path)   # noqa: S606 (a folder the player chose)
            return True
        return _xdg_open(path, popen or subprocess.Popen)
    except Exception:
        log.exception("Could not open %s", path)
        return False
