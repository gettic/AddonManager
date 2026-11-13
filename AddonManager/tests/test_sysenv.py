"""The built Linux program on a non-Debian system (am\\sysenv.py): certificates found in
the system's own places, the system's environment for programs we start, links and
folders opened through xdg-open, and certificate errors told apart from "offline".
Runs on any system: temp folders stand in for /etc and /usr/lib/ssl, the environment is a
plain dict, programs are never really started."""

import os
import shutil
import ssl
import subprocess
import types
import unittest
import urllib.error
from unittest import mock

from am import download, errors, github, manifest, selfupdate, share, share_later, sysenv
from am.download import DownloadError
from am.share import ShareError
from tests.fixtures import temp_dir


def no_defaults():
    """ssl.get_default_verify_paths() when Debian's /usr/lib/ssl isn't there."""
    return types.SimpleNamespace(cafile=None, capath=None)


def cert_error():
    return ssl.SSLCertVerificationError(1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate "
                                           "verify failed: unable to get local issuer")


class Certificates(unittest.TestCase):
    def setUp(self):
        self.tmp = temp_dir("sysenv_certs_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.files = tuple(os.path.join(self.tmp, n) for n in ("a.crt", "b.crt", "c.pem"))
        self.ca_dir = os.path.join(self.tmp, "certs")

    def fix(self, env, **kw):
        kw.setdefault("defaults", no_defaults)
        return sysenv.fix_certificates(env, linux=True, frozen=True, files=self.files,
                                       ca_dir=self.ca_dir, **kw)

    def make(self, path):
        with open(path, "w") as f:
            f.write("x")

    def test_set_by_player_is_left_alone(self):
        self.make(self.files[0])
        for name in ("SSL_CERT_FILE", "SSL_CERT_DIR"):
            env = {name: "/mine"}
            self.assertIsNone(self.fix(env))
            self.assertEqual(env, {name: "/mine"})

    def test_working_defaults_are_left_alone(self):
        self.make(self.files[0])
        lib_certs = os.path.join(self.tmp, "usr_lib_ssl_certs")
        os.makedirs(lib_certs)
        self.make(os.path.join(lib_certs, "0a1b2c3d.0"))
        env = {}
        self.assertIsNone(self.fix(env, defaults=lambda: types.SimpleNamespace(
            cafile=None, capath=lib_certs)))
        self.assertEqual(env, {})
        self.assertIsNone(self.fix(env, defaults=lambda: types.SimpleNamespace(
            cafile=self.files[0], capath=None)))
        self.assertEqual(env, {})

    def test_empty_default_folder_is_not_trusted(self):
        lib_certs = os.path.join(self.tmp, "usr_lib_ssl_certs")
        os.makedirs(lib_certs)   # exists, but nothing in it
        self.make(self.files[2])
        env = {}
        self.assertEqual(self.fix(env, defaults=lambda: types.SimpleNamespace(
            cafile=None, capath=lib_certs)), ("SSL_CERT_FILE", self.files[2]))
        self.assertEqual(env, {"SSL_CERT_FILE": self.files[2]})

    def test_empty_system_folder_is_not_used(self):
        os.makedirs(self.ca_dir)
        env = {}
        self.assertIsNone(self.fix(env))
        self.assertEqual(env, {})

    def test_first_existing_bundle_is_used(self):
        self.make(self.files[1])
        self.make(self.files[2])
        os.makedirs(self.ca_dir)
        env = {}
        self.assertEqual(self.fix(env), ("SSL_CERT_FILE", self.files[1]))
        self.assertEqual(env, {"SSL_CERT_FILE": self.files[1]})

    def test_folder_when_no_bundle(self):
        os.makedirs(self.ca_dir)
        self.make(os.path.join(self.ca_dir, "ca-certificates.crt"))
        env = {}
        self.assertEqual(self.fix(env), ("SSL_CERT_DIR", self.ca_dir))
        self.assertEqual(env, {"SSL_CERT_DIR": self.ca_dir})

    def test_nothing_found_sets_nothing(self):
        env = {}
        self.assertIsNone(self.fix(env))
        self.assertEqual(env, {})

    def test_never_raises(self):
        def broken():
            raise OSError("no")
        env = {}
        self.assertIsNone(self.fix(env, defaults=broken))
        self.assertEqual(env, {})

    def test_only_the_built_linux_program(self):
        self.make(self.files[0])
        for linux, frozen in ((True, False), (False, True)):
            env = {}
            self.assertIsNone(sysenv.fix_certificates(
                env, linux=linux, frozen=frozen, defaults=no_defaults, files=self.files,
                ca_dir=self.ca_dir))
            self.assertEqual(env, {})

    def test_default_list(self):
        self.assertEqual(sysenv.CA_FILES[0], "/etc/ssl/certs/ca-certificates.crt")
        self.assertEqual(len(sysenv.CA_FILES), 5)
        self.assertEqual(sysenv.CA_DIR, "/etc/ssl/certs")


class SystemEnv(unittest.TestCase):
    BASE = {"PATH": "/usr/bin", "LD_LIBRARY_PATH": "/tmp/.mount_x/_internal",
            "_MEIPASS2": "/tmp/_MEI", "TCL_LIBRARY": "/tmp/_internal/tcl",
            "APPIMAGE": "/home/deck/AM.AppImage", "APPDIR": "/tmp/.mount_x",
            "ADDONMANAGER_APPDIR": "/tmp/.mount_x"}

    def test_orig_is_restored(self):
        env = sysenv.system_env(dict(self.BASE, LD_LIBRARY_PATH_ORIG="/opt/lib"),
                                linux=True, frozen=True)
        self.assertEqual(env["LD_LIBRARY_PATH"], "/opt/lib")
        self.assertNotIn("LD_LIBRARY_PATH_ORIG", env)
        for name in ("_MEIPASS2", "TCL_LIBRARY", "APPIMAGE", "APPDIR", "ADDONMANAGER_APPDIR"):
            self.assertNotIn(name, env)
        self.assertEqual(env["PATH"], "/usr/bin")

    def test_without_orig_it_is_removed(self):
        env = sysenv.system_env(dict(self.BASE), linux=True, frozen=True)
        self.assertNotIn("LD_LIBRARY_PATH", env)

    def test_unchanged_when_not_built_or_windows(self):
        for linux, frozen in ((True, False), (False, True)):
            self.assertEqual(sysenv.system_env(dict(self.BASE), linux=linux, frozen=frozen),
                             self.BASE)

    def test_a_copy(self):
        base = dict(self.BASE)
        sysenv.system_env(base, linux=True, frozen=True)
        self.assertEqual(base, self.BASE)

    def test_selfupdate_restart_uses_it(self):
        env = selfupdate.restart_env(dict(self.BASE), linux=True, frozen=True)
        self.assertNotIn("LD_LIBRARY_PATH", env)
        self.assertEqual(env["PYINSTALLER_RESET_ENVIRONMENT"], "1")


class Opening(unittest.TestCase):
    real_system_env = staticmethod(sysenv.system_env)

    def setUp(self):
        self.calls = []
        p = mock.patch.object(sysenv, "system_env", return_value={"SYSTEM": "1"})
        p.start()
        self.addCleanup(p.stop)

    def popen(self, args, **kw):
        self.calls.append((args, kw))

    def check_call(self, target):
        args, kw = self.calls[-1]
        self.assertEqual(args, ["xdg-open", target])
        self.assertEqual(kw["env"], {"SYSTEM": "1"})
        for name in ("stdin", "stdout", "stderr"):
            self.assertEqual(kw[name], subprocess.DEVNULL)
        self.assertTrue(kw["start_new_session"])

    def test_link_on_linux(self):
        browser = mock.Mock()
        self.assertTrue(sysenv.open_link("https://file.kiwi/x", popen=self.popen,
                                         browser=browser, windows=False))
        self.check_call("https://file.kiwi/x")
        browser.assert_not_called()

    def test_folder_on_linux(self):
        self.assertTrue(sysenv.open_folder("/home/deck/backups", popen=self.popen,
                                           windows=False))
        self.check_call("/home/deck/backups")

    def test_no_xdg_open_falls_back(self):
        def missing(*a, **k):
            raise FileNotFoundError("xdg-open")
        browser = mock.Mock(return_value=True)
        self.assertTrue(sysenv.open_link("https://x.example", popen=missing, browser=browser,
                                         windows=False))
        browser.assert_called_once_with("https://x.example")

    def test_failures_return_false(self):
        def broken(*a, **k):
            raise OSError("no")
        self.assertFalse(sysenv.open_link("https://x.example", popen=broken,
                                          browser=mock.Mock(side_effect=OSError),
                                          windows=False))
        self.assertFalse(sysenv.open_folder("/nowhere", popen=broken, windows=False))
        self.assertFalse(sysenv.open_link("https://x.example", browser=mock.Mock(
            return_value=False), windows=True))

    def test_fallback_browser_gets_the_system_environment(self):
        env = {"PATH": "/usr/bin", "LD_LIBRARY_PATH": "/tmp/.mount_x/_internal",
               "LD_LIBRARY_PATH_ORIG": "/opt/lib", "_MEIPASS2": "/tmp/_MEI"}
        before = dict(env)
        seen = []
        with mock.patch.object(sysenv, "system_env", self.real_system_env):
            ok = sysenv._browser_clean(lambda url: seen.append(dict(env)) or True,
                                       "https://x.example", environ=env, linux=True,
                                       frozen=True)
        self.assertTrue(ok)
        self.assertEqual(seen, [{"PATH": "/usr/bin", "LD_LIBRARY_PATH": "/opt/lib"}])
        self.assertEqual(env, before)   # put back after the call

    def test_fallback_browser_environment_put_back_on_error(self):
        env = {"PATH": "/usr/bin", "LD_LIBRARY_PATH": "/tmp/.mount_x/_internal"}
        before = dict(env)

        def broken(url):
            raise OSError("no browser")
        with mock.patch.object(sysenv, "system_env", self.real_system_env):
            with self.assertRaises(OSError):
                sysenv._browser_clean(broken, "https://x.example", environ=env, linux=True,
                                      frozen=True)
        self.assertEqual(env, before)

    def test_fallback_browser_unchanged_when_not_built(self):
        env = {"LD_LIBRARY_PATH": "/x"}
        seen = []
        self.assertTrue(sysenv._browser_clean(lambda url: seen.append(dict(env)) or True,
                                              "https://x.example", environ=env, linux=True,
                                              frozen=False))
        self.assertEqual(seen, [{"LD_LIBRARY_PATH": "/x"}])

    def test_no_xdg_open_fallback_uses_clean_environment(self):
        def missing(*a, **k):
            raise FileNotFoundError("xdg-open")
        with mock.patch.object(sysenv, "_browser_clean", return_value=True) as clean:
            self.assertTrue(sysenv.open_link("https://x.example", popen=missing,
                                             browser=mock.sentinel.browser, windows=False))
        clean.assert_called_once_with(mock.sentinel.browser, "https://x.example")

    def test_windows_link_uses_browser(self):
        browser = mock.Mock(return_value=True)
        self.assertTrue(sysenv.open_link("https://x.example", popen=self.popen,
                                         browser=browser, windows=True))
        self.assertEqual(self.calls, [])


class CertificateErrors(unittest.TestCase):
    def test_detect(self):
        self.assertTrue(download.is_cert_error(cert_error()))
        self.assertTrue(download.is_cert_error(urllib.error.URLError(cert_error())))
        self.assertFalse(download.is_cert_error(urllib.error.URLError(OSError("down"))))
        self.assertFalse(download.is_cert_error(TimeoutError()))

    def test_relay_gives_certificate_and_does_not_retry(self):
        naps = []
        relay = share.Relay("https://relay.example", sleep=naps.append)
        tries = []

        def fail(*a, **k):
            tries.append(1)
            raise urllib.error.URLError(cert_error())
        relay._opener = types.SimpleNamespace(open=fail)
        with self.assertRaises(ShareError) as cm:
            relay._call("POST", "/open")
        self.assertEqual(cm.exception.kind, "certificate")
        self.assertIn("CERTIFICATE_VERIFY_FAILED", cm.exception.detail)
        self.assertEqual((len(tries), naps), (1, []))

    def test_relay_offline_detail_has_the_reason(self):
        relay = share.Relay("https://relay.example", sleep=lambda s: None)

        def fail(*a, **k):
            raise urllib.error.URLError(OSError("Name or service not known"))
        relay._opener = types.SimpleNamespace(open=fail)
        with self.assertRaises(ShareError) as cm:
            relay._call("POST", "/open")
        self.assertEqual(cm.exception.kind, "offline")
        self.assertIn("Name or service not known", cm.exception.detail)
        self.assertNotIn("relay.example", cm.exception.detail)

    def test_file_kiwi_gives_certificate(self):
        class FailingConnection:
            def __init__(self, *a, **k):
                pass

            def putrequest(self, *a, **k):
                raise cert_error()

            def close(self):
                pass
        kiwi = share_later.FileKiwi("https://kiwi.example/api", sleep=lambda s: None)
        with mock.patch.object(share_later.http.client, "HTTPSConnection", FailingConnection):
            with self.assertRaises(share_later.LaterError) as cm:
                kiwi._request("GET", "https://kiwi.example/api/x")
        self.assertEqual(cm.exception.kind, "certificate")
        self.assertEqual(share._later_error(cm.exception).kind, "certificate")

    def test_player_message(self):
        msg = errors.share_message(ShareError("certificate"))
        self.assertEqual(msg, errors.SHARE_CERTIFICATE)
        self.assertIn("date and time", msg)
        self.assertNotIn("Check your internet", msg)


class DownloadCertificateErrors(unittest.TestCase):
    """Addon list, GitHub release checks, addon downloads and the app's own update: a
    certificate failure gets the date-and-time text, not "Can't reach the internet"."""

    def open_failing(self, exc):
        def build(*a, **k):
            def fail(*a, **k):
                raise exc
            return types.SimpleNamespace(open=fail)
        return mock.patch.object(download.urllib.request, "build_opener", build)

    def test_open_url_gives_certificate(self):
        for exc in (urllib.error.URLError(cert_error()), cert_error()):
            with self.open_failing(exc), self.assertRaises(DownloadError) as cm:
                download.fetch_bytes("https://example.invalid/list.json")
            self.assertEqual(cm.exception.kind, "certificate")
            self.assertIn("CERTIFICATE_VERIFY_FAILED", cm.exception.detail)

    def test_other_failures_stay_offline(self):
        with self.open_failing(urllib.error.URLError(OSError("down"))), \
                self.assertRaises(DownloadError) as cm:
            download.fetch_bytes("https://example.invalid/list.json")
        self.assertEqual(cm.exception.kind, "offline")

    def test_addon_list(self):
        with self.open_failing(urllib.error.URLError(cert_error())), \
                self.assertRaises(manifest.ManifestError) as cm:
            manifest.load("https://example.invalid/list.json")
        self.assertEqual(cm.exception.kind, "certificate")
        self.assertEqual(errors.manifest_message(cm.exception), errors.CERTIFICATE)

    def test_release_check(self):
        with self.open_failing(urllib.error.URLError(cert_error())), \
                self.assertRaises(github.ReleaseError) as cm:
            github._get_json("https://api.github.com/repos/a/b/releases", "a/b", 5)
        self.assertEqual(cm.exception.kind, "certificate")
        self.assertEqual(errors.release_message(cm.exception, "X"),
                         ("Can't check now", errors.CERTIFICATE))

    def test_addon_download_message(self):
        msg = errors.install_message(DownloadError("certificate", "x"), "X")
        self.assertEqual(msg, errors.CERTIFICATE)

    def test_app_update(self):
        tmp = temp_dir("linuxssl_fix_selfupdate_")
        self.addCleanup(shutil.rmtree, tmp, True)
        update = selfupdate.Update("1.0.0", "v1.0.0", "https://github.com/a/b/x.exe",
                                   "0" * 64, 1)
        with self.open_failing(urllib.error.URLError(cert_error())), \
                self.assertRaises(selfupdate.UpdateError) as cm:
            selfupdate.fetch(update, os.path.join(tmp, "AddonManager.exe"))
        self.assertEqual(cm.exception.kind, "certificate")
        self.assertEqual(errors.app_update_message(cm.exception), errors.CERTIFICATE)

    def test_text(self):
        self.assertIn("date and time", errors.CERTIFICATE)
        self.assertNotIn("internet", errors.CERTIFICATE)


if __name__ == "__main__":
    unittest.main()
