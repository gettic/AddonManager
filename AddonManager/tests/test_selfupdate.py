"""The app's own update: check, download, swap, rollback, clean-up and the update bar.
Only fake .exe files in temp folders; never the real running exe, never a real WoW folder."""

import contextlib
import gc
import json
import os
import pathlib
import time
import tkinter as tk
import unittest
from unittest import mock

from am import APP_VERSION, config, errors, selfupdate, ui
from tests import fixtures

URI = lambda p: pathlib.Path(p).as_uri()
GH = "https://github.com/gettic/AddonManager/releases/download/v99.0.0/AddonManager.exe"


def release(tag="v99.0.0", name=config.APP_ASSET, url=GH, digest="sha256:" + "a" * 64,
            size=5_000_000, state="uploaded"):
    return {"tag": tag, "assets": [{"name": name, "url": url, "digest": digest, "size": size,
                                    "state": state}]}


@contextlib.contextmanager
def locked(path):
    """path can't be renamed or removed while inside. Windows: a real open handle (no
    delete sharing). Linux allows renaming/removing open files, so there the same refusal
    (as from a read-only mount, an immutable file or another user's file) is simulated:
    os.replace/os.remove of exactly that path raise PermissionError."""
    if os.name == "nt":
        with open(path, "rb"):
            yield
        return
    real_replace, real_remove = os.replace, os.remove
    target = os.path.abspath(path)

    def is_target(p):
        return os.path.abspath(os.fspath(p)) == target

    def replace(src, dst, *a, **k):
        if is_target(src):
            raise PermissionError(13, "Permission denied (test lock)", src)
        return real_replace(src, dst, *a, **k)

    def remove(p, *a, **k):
        if is_target(p):
            raise PermissionError(13, "Permission denied (test lock)", p)
        return real_remove(p, *a, **k)
    with mock.patch.object(selfupdate.os, "replace", replace), \
            mock.patch.object(selfupdate.os, "remove", remove):
        yield


class FakeProc:
    pid = None

    def __init__(self, exits=False):
        self.exits, self.killed = exits, False

    def poll(self):
        return 1 if self.exits or self.killed else None

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return 0


class Pick(unittest.TestCase):
    def test_newer_release_offered(self):
        u = selfupdate.pick(release(), "0.1.0")
        self.assertEqual((u.version, u.sha256, u.size), ("99.0.0", "a" * 64, 5_000_000))
        self.assertEqual(u.page_url,
                         "https://github.com/gettic/AddonManager/releases/tag/v99.0.0")

    def test_not_newer(self):
        for tag in ("v0.1.0", "0.1", "v0.0.9"):
            self.assertIsNone(selfupdate.pick(release(tag=tag), "0.1.0"), tag)

    def test_refused(self):
        cases = {
            "other asset name": release(name="AddonManager-v99.exe"),
            "no digest": release(digest=""),
            "digest in another form": release(digest="md5:abc"),
            "no size": release(size=0),
            "not uploaded": release(state="starter"),
            "other repo": release(url="https://github.com/evil/AddonManager/releases/download/"
                                      "v99.0.0/AddonManager.exe"),
            "other host": release(url="https://example.com/gettic/AddonManager/releases/"
                                      "download/v99.0.0/AddonManager.exe"),
            "http": release(url=GH.replace("https:", "http:")),
            "bad tag": release(tag="v99.0.0/../x"),
            "text tag": release(tag="latest"),
            "pre-release tag": release(tag="v99.0.0-beta.1"),
            "pre-release flag": dict(release(), prerelease=True),
        }
        for what, rel in cases.items():
            self.assertIsNone(selfupdate.pick(rel, "0.1.0"), what)

    def test_failed_version_not_offered_again(self):
        self.assertIsNone(selfupdate.pick(release(), "0.1.0", failed="99.0.0"))
        self.assertIsNotNone(selfupdate.pick(release(tag="v99.0.1"), "0.1.0", failed="99.0.0"))


class Files(unittest.TestCase):
    def setUp(self):
        self.work = fixtures.temp_dir("selfupdate_")
        self.api = os.path.join(self.work, "api")
        self.dl = os.path.join(self.work, "dl")
        self.app_dir = os.path.join(self.work, "app")
        os.makedirs(self.app_dir)
        self.exe = fixtures.make_fake_exe(os.path.join(self.app_dir, "AddonManager.exe"),
                                          marker=b"old")
        self.old_bytes = pathlib.Path(self.exe).read_bytes()
        self.new_path = fixtures.make_app_release(self.api, self.dl, URI)
        self.new_bytes = pathlib.Path(self.new_path).read_bytes()

    def tearDown(self):
        fixtures.remove(self.work)

    def update(self):
        u = selfupdate.check("0.1.0", URI(self.api), allow_local=True)
        self.assertIsNotNone(u)
        return u

    def read(self, path):
        with open(path, "rb") as f:
            return f.read()

    # check
    def test_check_from_fake_api(self):
        self.assertEqual(self.update().version, "99.0.0")
        self.assertIsNone(selfupdate.check("99.0.0", URI(self.api), allow_local=True))

    def test_check_without_release_or_bad_json_is_silent(self):
        self.assertIsNone(selfupdate.check("0.1.0", URI(os.path.join(self.work, "none")), True))
        fixtures.write_release(self.api, "gettic/AddonManager", "", [], raw="{not json")
        self.assertIsNone(selfupdate.check("0.1.0", URI(self.api), True))

    def test_check_rate_limited_uses_cache(self):
        cache = os.path.join(self.work, "cache.json")
        with fixtures.LocalServer(self.work) as server:
            base = server.url + "limited"
            self.assertIsNone(selfupdate.check("0.1.0", base, True, cache_file=cache))
            with open(cache, "w", encoding="utf-8") as f:
                json.dump({f"{base}|app:gettic/AddonManager":
                           {"time": time.time() - 3600, "release": release(
                               url=server.url + "dl/AddonManager.exe")}}, f)
            u = selfupdate.check("0.1.0", base, True, cache_file=cache)
        self.assertEqual(u.version, "99.0.0")

    # fetch
    def test_fetch_good(self):
        new = selfupdate.fetch(self.update(), self.exe, allow_local=True)
        self.assertEqual(self.read(new), self.new_bytes)
        self.assertEqual(self.read(self.exe), self.old_bytes)

    def test_fetch_refused_keeps_exe(self):
        u = self.update()
        bad = {
            "checksum": u.__class__(u.version, u.tag, u.url, "b" * 64, u.size),
            "size": u.__class__(u.version, u.tag, u.url, u.sha256, u.size + 1),
        }
        for what, upd in bad.items():
            with self.assertRaises(selfupdate.UpdateError, msg=what) as cm:
                selfupdate.fetch(upd, self.exe, allow_local=True)
            self.assertEqual(cm.exception.kind, "damaged")
            self.assertFalse(os.path.exists(self.exe + ".new"), what)
            self.assertEqual(self.read(self.exe), self.old_bytes)
        with self.assertRaises(selfupdate.UpdateError):   # under 1 MB
            selfupdate.fetch(u, self.exe, allow_local=True, min_bytes=10 ** 9)

    def test_fetch_not_a_program(self):
        with open(self.new_path, "wb") as f:
            f.write(b"PK" + b"\0" * (1024 * 1024 + 10))
        fixtures.write_release(self.api, "gettic/AddonManager", "v99.0.0",
                               [fixtures.asset(config.APP_ASSET, URI(self.new_path),
                                               self.new_path)])
        with self.assertRaises(selfupdate.UpdateError) as cm:
            selfupdate.fetch(self.update(), self.exe, allow_local=True)
        self.assertEqual(cm.exception.kind, "damaged")
        self.assertFalse(os.path.exists(self.exe + ".new"))

    def test_fetch_strict_hosts_outside_dev(self):
        with fixtures.LocalServer(self.dl) as server:
            u = selfupdate.Update("99.0.0", "v99.0.0", server.url + "AddonManager.exe",
                                  fixtures.sha256(self.new_path), len(self.new_bytes))
            with self.assertRaises(selfupdate.UpdateError):
                selfupdate.fetch(u, self.exe, allow_local=False)
        self.assertFalse(os.path.exists(self.exe + ".new"))

    def test_fetch_offline(self):
        u = selfupdate.Update("99.0.0", "v99.0.0", URI(os.path.join(self.dl, "gone.exe")),
                              "a" * 64, 5)
        with self.assertRaises(selfupdate.UpdateError) as cm:
            selfupdate.fetch(u, self.exe, allow_local=True)
        self.assertEqual(cm.exception.kind, "offline")

    # swap, rollback, cleanup
    def test_swap_and_rollback(self):
        selfupdate.fetch(self.update(), self.exe, allow_local=True)
        selfupdate.swap(self.exe)
        self.assertEqual(self.read(self.exe), self.new_bytes)
        self.assertEqual(self.read(self.exe + ".old"), self.old_bytes)
        self.assertFalse(os.path.exists(self.exe + ".new"))
        proc = FakeProc()
        self.assertTrue(selfupdate.rollback(self.exe, proc))
        self.assertTrue(proc.killed)
        self.assertEqual(self.read(self.exe), self.old_bytes)
        self.assertTrue(selfupdate.cleanup(self.exe))
        self.assertEqual(sorted(os.listdir(self.app_dir)), ["AddonManager.exe"])

    def test_swap_locked_exe_keeps_old(self):
        selfupdate.fetch(self.update(), self.exe, allow_local=True)
        with locked(self.exe):
            with self.assertRaises(selfupdate.UpdateError) as cm:
                selfupdate.swap(self.exe, tries=2, delay=0.01)
        self.assertEqual(cm.exception.kind, "locked")
        self.assertEqual(self.read(self.exe), self.old_bytes)
        self.assertEqual(sorted(os.listdir(self.app_dir)), ["AddonManager.exe"])

    def test_swap_locked_new_puts_old_back(self):
        new = selfupdate.fetch(self.update(), self.exe, allow_local=True)
        with locked(new):
            with self.assertRaises(selfupdate.UpdateError):
                selfupdate.swap(self.exe, tries=2, delay=0.01)
        self.assertEqual(self.read(self.exe), self.old_bytes)
        self.assertFalse(os.path.exists(self.exe + ".old"))

    @unittest.skipIf(os.name == "nt", "Linux: open files can be renamed and removed")
    def test_linux_swap_and_cleanup_while_running(self):
        """On Linux the running program stays open during the update: renaming it and,
        at the next start, removing its .old copy must work (Windows would refuse)."""
        selfupdate.fetch(self.update(), self.exe, allow_local=True)
        with open(self.exe, "rb") as running:
            selfupdate.swap(self.exe, tries=1, delay=0.01)
            self.assertEqual(self.read(self.exe), self.new_bytes)
            self.assertTrue(selfupdate.cleanup(self.exe))
            self.assertEqual(running.read(), self.old_bytes)   # the old one kept running
        self.assertEqual(sorted(os.listdir(self.app_dir)), ["AddonManager.exe"])

    def test_swap_refreshes_icon(self):
        """After a swap Explorer is told the exe changed (new icon); a failing notice
        never fails the update; nothing is sent on Linux or when the swap fails."""
        selfupdate.fetch(self.update(), self.exe, allow_local=True)
        with mock.patch.object(selfupdate, "refresh_icon") as refresh:
            selfupdate.swap(self.exe)
        refresh.assert_called_once_with(self.exe)
        self.assertEqual(self.read(self.exe), self.new_bytes)
        shell = mock.Mock()
        self.assertTrue(selfupdate.refresh_icon(self.exe, windows=True, shell=shell))
        shell.SHChangeNotify.assert_called_once_with(0x2000, 0x0005, self.exe, None)
        shell.SHChangeNotify.side_effect = OSError("boom")
        self.assertFalse(selfupdate.refresh_icon(self.exe, windows=True, shell=shell))
        linux_shell = mock.Mock()
        self.assertFalse(selfupdate.refresh_icon(self.exe, windows=False, shell=linux_shell))
        linux_shell.SHChangeNotify.assert_not_called()
        with mock.patch.object(selfupdate, "_shell32", side_effect=AttributeError("no windll")):
            self.assertFalse(selfupdate.refresh_icon(self.exe, windows=True))
        with mock.patch.object(selfupdate, "refresh_icon") as refresh, \
                self.assertRaises(selfupdate.UpdateError):
            selfupdate.swap(self.exe)   # no .new any more
        refresh.assert_not_called()

    def test_swap_new_gone(self):
        with self.assertRaises(selfupdate.UpdateError) as cm:
            selfupdate.swap(self.exe)
        self.assertEqual(cm.exception.kind, "failed")
        self.assertEqual(self.read(self.exe), self.old_bytes)

    def _busy_old(self, busy_calls):
        """A _remove that refuses <exe>.old for its first busy_calls calls (the old exe
        still closing), then really removes it."""
        real, calls = selfupdate._remove, []

        def fake(path):
            if path == self.exe + ".old":
                calls.append(1)
                if len(calls) <= busy_calls:
                    return False
            return real(path)
        return fake, calls

    def test_cleanup_keeps_trying_after_the_fast_tries(self):
        # .old still in use during all 3 fast tries and the first 2 slow ones: removed later
        with open(self.exe + ".old", "wb") as f:
            f.write(b"old")
        fake, calls = self._busy_old(5)
        with mock.patch.object(selfupdate, "_remove", fake), \
                self.assertLogs("am.selfupdate", "INFO") as logs:
            t = selfupdate.cleanup_later(self.exe, tries=3, delay=0.01, slow_delay=0.01,
                                         slow_tries=10)
            t.join(5)
        self.assertFalse(t.is_alive())
        self.assertFalse(os.path.exists(self.exe + ".old"))
        self.assertEqual(len(calls), 6)
        text = "\n".join(logs.output)
        self.assertEqual(text.count("still in use"), 1)   # logged once, not every 30 s
        self.assertIn("Removed old update files: AddonManager.exe.old", text)
        self.assertTrue(os.path.exists(self.exe))          # the app itself untouched

    def test_cleanup_gives_up_quietly_when_still_in_use(self):
        with open(self.exe + ".old", "wb") as f:
            f.write(b"old")
        fake, _calls = self._busy_old(10 ** 6)
        with mock.patch.object(selfupdate, "_remove", fake):
            t = selfupdate.cleanup_later(self.exe, tries=2, delay=0.01, slow_delay=0.01,
                                         slow_tries=3)
            t.join(5)
        self.assertFalse(t.is_alive())
        self.assertTrue(os.path.exists(self.exe + ".old"))   # left for the next start

    def test_cleanup_later_nothing_to_do_and_errors(self):
        self.assertIsNone(selfupdate.cleanup_later(self.exe))
        self.assertIsNone(selfupdate.cleanup_later(None))
        with open(self.exe + ".new", "wb") as f:
            f.write(b"new")
        with mock.patch.object(selfupdate, "cleanup", side_effect=RuntimeError("boom")):
            t = selfupdate.cleanup_later(self.exe, tries=1, delay=0.01, slow_delay=0.01,
                                         slow_tries=1)
            t.join(5)   # the helper thread ends, never raises into the app
        self.assertFalse(t.is_alive())

    def test_main_tries_once_more_on_exit(self):
        import main
        for name in ("AddonManager.exe.old", "AddonManager.exe.new", "notes.txt"):
            with open(os.path.join(self.app_dir, name), "w") as f:
                f.write("x")
        with mock.patch.object(selfupdate, "exe_path", return_value=self.exe), \
                mock.patch.object(selfupdate, "cleanup_later") as later, \
                mock.patch.object(ui, "run") as run, \
                mock.patch.object(main, "set_dpi_aware"), \
                mock.patch.object(config, "setup_logging"):
            self.assertEqual(main.main([]), 0)
        later.assert_called_once_with(self.exe)
        run.assert_called_once()
        self.assertEqual(sorted(os.listdir(self.app_dir)), ["AddonManager.exe", "notes.txt"])

    def test_cleanup_only_its_own_files(self):
        for name in ("AddonManager.exe.old", "AddonManager.exe.new", "notes.txt",
                     "AddonManager.exe.older"):
            with open(os.path.join(self.app_dir, name), "w") as f:
                f.write("x")
        with locked(self.exe + ".old"):
            self.assertFalse(selfupdate.cleanup(self.exe))
        self.assertTrue(selfupdate.cleanup(self.exe))
        self.assertEqual(sorted(os.listdir(self.app_dir)),
                         ["AddonManager.exe", "AddonManager.exe.older", "notes.txt"])
        self.assertTrue(selfupdate.cleanup(None))

    # folder checks
    def test_never_inside_a_wow_folder(self):
        root, addons = fixtures.make_fake_wow(os.path.join(self.work, "wowbase"))
        client = os.path.dirname(os.path.dirname(addons))
        for folder in (root, client, os.path.join(client, "Interface")):
            exe = os.path.join(folder, "AddonManager.exe")
            with self.assertRaises(selfupdate.UpdateError, msg=folder) as cm:
                selfupdate.check_folder(exe, addons)
            self.assertEqual(cm.exception.kind, "in_wow")
            self.assertFalse(os.path.exists(exe + ".update-test"))
        # Even if WoW wasn't found: the folder itself looks like a game folder.
        with self.assertRaises(selfupdate.UpdateError):
            selfupdate.check_folder(os.path.join(client, "AddonManager.exe"), None)
        selfupdate.check_folder(self.exe, addons)   # its own folder is fine
        self.assertEqual(sorted(os.listdir(self.app_dir)), ["AddonManager.exe"])

    def test_ordinary_folders_with_interface_or_wtf_update(self):
        """An unzipped addon pack (Interface\\AddOns) or a WTF backup is not a game folder."""
        downloads = os.path.join(self.work, "Downloads")
        os.makedirs(os.path.join(downloads, "Interface", "AddOns", "Foo"))
        desktop = os.path.join(self.work, "Desktop")
        os.makedirs(os.path.join(desktop, "WTF", "Account"))
        os.makedirs(os.path.join(desktop, "Interface"))
        fixtures.make_fake_exe(os.path.join(desktop, "WoWForeverSetup-notes.txt"))
        setup_dir = os.path.join(self.work, "Setups")
        os.makedirs(setup_dir)
        fixtures.make_fake_exe(os.path.join(setup_dir, "WoWForeverSetup.exe"))
        for folder in (downloads, os.path.join(downloads, "AM"), desktop,
                       os.path.join(desktop, "AM"), setup_dir):
            os.makedirs(folder, exist_ok=True)
            self.assertFalse(selfupdate.in_wow_folder(folder), folder)
            selfupdate.check_folder(os.path.join(folder, "AddonManager.exe"))
            self.assertFalse(os.path.exists(os.path.join(folder, "AddonManager.exe.update-test")))

    def test_real_installs_are_wow(self):
        root, addons = fixtures.make_fake_wow(os.path.join(self.work, "Games"))
        tools = os.path.join(root, "Tools")    # next to _forever_ (has Wow.exe)
        os.makedirs(tools)
        self.assertTrue(selfupdate.in_wow_folder(tools))
        bnet = os.path.join(self.work, "BNet", "World of Warcraft")
        os.makedirs(os.path.join(bnet, "AM"))
        with open(os.path.join(bnet, ".build.info"), "w") as f:
            f.write("x")
        self.assertTrue(selfupdate.in_wow_folder(os.path.join(bnet, "AM")))
        forever = os.path.join(self.work, "Forever")
        os.makedirs(os.path.join(forever, "Interface"))
        fixtures.make_fake_exe(os.path.join(forever, "WowForever.exe"))
        self.assertTrue(selfupdate.in_wow_folder(forever))
        # The detected install counts even without its .exe.
        os.remove(os.path.join(os.path.dirname(os.path.dirname(addons)), "Wow.exe"))
        self.assertTrue(selfupdate.in_wow_folder(tools, addons))

    def test_bad_failed_setting_ignored(self):
        for bad in (12345, ["99.0.0"], None, {"v": 1}):
            self.assertIsNotNone(selfupdate.pick(release(), "0.1.0", failed=bad), bad)

    def test_bad_settings_types_dropped(self):
        with open(config.settings_path(), "w", encoding="utf-8") as f:
            json.dump({"wow_folder": 12345, "wow_folder_source": ["x"], "update_failed": 7,
                       "manifest_url": None, "known_addons": "x", "other": 5}, f)
        self.assertEqual(config.load_settings(), {"other": 5})
        config.save_settings({})

    def test_read_only_folder(self):
        with self.assertRaises(selfupdate.UpdateError) as cm:
            selfupdate.check_folder(os.path.join(self.work, "missing", "AddonManager.exe"))
        self.assertEqual(cm.exception.kind, "read_only")

    # restart handshake
    def test_ok_file_and_wait(self):
        selfupdate.clear_ok()
        self.assertFalse(selfupdate.wait_ok("99.0.0", timeout=0.2, step=0.05))
        self.assertFalse(selfupdate.wait_ok("99.0.0", FakeProc(exits=True), timeout=5))
        selfupdate.mark_ok("v99.0.0")
        self.assertTrue(selfupdate.wait_ok("99.0.0", timeout=1))
        self.assertFalse(selfupdate.wait_ok("98.0.0", timeout=0.2, step=0.05))
        selfupdate.clear_ok()

    def test_restart_starts_fresh(self):
        with mock.patch.object(selfupdate.subprocess, "Popen") as popen:
            selfupdate.restart(self.exe, "v0.1.0")
        args, kwargs = popen.call_args
        self.assertEqual(args[0], [self.exe, "--updated-from", "0.1.0"])
        self.assertEqual(kwargs["env"]["PYINSTALLER_RESET_ENVIRONMENT"], "1")
        self.assertNotIn("_MEIPASS2", kwargs["env"])

    def test_remember_failed(self):
        config.save_settings({"x": 1})
        selfupdate.remember_failed("v99.0.0")
        self.assertEqual(config.load_settings(), {"x": 1, "update_failed": "99.0.0"})
        config.save_settings({})

    def test_not_frozen_means_no_self_update(self):
        self.assertIsNone(selfupdate.exe_path())

    # AppImage (Linux): the .AppImage file is updated, never the read-only mount
    def test_exe_path_appimage(self):
        image = fixtures.make_fake_exe(os.path.join(self.work, "AddonManager-x86_64.AppImage"))
        mount = os.path.join(self.work, ".mount_AddonXY")
        inside = os.path.join(mount, "usr", "lib", "AddonManager", "AddonManager")
        env = {"APPIMAGE": image, "APPDIR": mount}
        path = lambda env, linux=True, exe=inside: selfupdate.exe_path(  # noqa: E731
            env, exe, frozen=True, linux=linux)
        self.assertEqual(path(env), os.path.realpath(image))
        # our AppRun's own marker works when APPDIR is missing
        self.assertEqual(path({"APPIMAGE": image, "ADDONMANAGER_APPDIR": mount}),
                         os.path.realpath(image))
        # inherited from another AppImage (program not inside its folder) -> ignored
        other = os.path.join(self.work, "elsewhere", "AddonManager")
        self.assertEqual(path(env, exe=other), os.path.abspath(other))
        # missing/relative/not-a-file APPIMAGE, no APPDIR, Windows -> the running program
        self.assertEqual(path({"APPDIR": mount}), os.path.abspath(inside))
        self.assertEqual(path({"APPIMAGE": "rel.AppImage", "APPDIR": mount}),
                         os.path.abspath(inside))
        self.assertEqual(path({"APPIMAGE": os.path.join(self.work, "gone.AppImage"),
                               "APPDIR": mount}), os.path.abspath(inside))
        self.assertEqual(path({"APPIMAGE": image}), os.path.abspath(inside))
        self.assertEqual(path(env, linux=False), os.path.abspath(inside))
        self.assertIsNone(selfupdate.exe_path(env, inside, frozen=False, linux=True))

    def test_is_program(self):
        elf = b"\x7fELF\x02\x01\x01\x00"
        self.assertTrue(selfupdate.is_program(elf + b"AI\x02" + b"\0" * 5, linux=True))
        self.assertFalse(selfupdate.is_program(elf + b"\0" * 8, linux=True))   # plain ELF
        self.assertFalse(selfupdate.is_program(b"MZ" + b"\0" * 14, linux=True))
        self.assertFalse(selfupdate.is_program(b"\x7fELF", linux=True))         # too short
        self.assertTrue(selfupdate.is_program(b"MZ" + b"\0" * 14, linux=False))
        self.assertFalse(selfupdate.is_program(elf + b"AI\x02", linux=False))
        with open(fixtures.make_fake_exe(os.path.join(self.work, "fake_prog")), "rb") as f:
            self.assertTrue(selfupdate.is_program(f.read(16)))

    def test_restart_env_drops_appimage_and_library_path(self):
        base = {"APPIMAGE": "/a/x.AppImage", "APPDIR": "/tmp/.mount_x", "ARGV0": "x",
                "OWD": "/home/u", "ADDONMANAGER_APPDIR": "/tmp/.mount_x", "_MEIPASS2": "m",
                "LD_LIBRARY_PATH": "/tmp/.mount_x/usr/lib/AddonManager/_internal",
                "HOME": "/home/u"}
        env = selfupdate.restart_env(dict(base, LD_LIBRARY_PATH_ORIG="/opt/lib"),
                                     linux=True, frozen=True)
        for name in selfupdate.APPIMAGE_ENV + ("_MEIPASS2", "LD_LIBRARY_PATH_ORIG"):
            self.assertNotIn(name, env)
        self.assertEqual(env["LD_LIBRARY_PATH"], "/opt/lib")
        self.assertEqual(env["HOME"], "/home/u")
        self.assertEqual(env["PYINSTALLER_RESET_ENVIRONMENT"], "1")
        env = selfupdate.restart_env(base, linux=True, frozen=True)
        self.assertNotIn("LD_LIBRARY_PATH", env)
        # from source or on Windows the library path is left alone
        self.assertIn("LD_LIBRARY_PATH", selfupdate.restart_env(base, linux=True, frozen=False))
        self.assertIn("LD_LIBRARY_PATH", selfupdate.restart_env(base, linux=False, frozen=True))


class Bar(unittest.TestCase):
    """The update bar in the real window, withdrawn before anything is drawn."""

    def setUp(self):
        self.work = fixtures.temp_dir("selfupdate_ui_")
        self.site = fixtures.make_server_files(self.work)
        self.root_dir, self.addons = fixtures.make_fake_wow(self.work)
        self.api = os.path.join(self.work, "api")
        self.app_dir = os.path.join(self.work, "app")
        os.makedirs(self.app_dir)
        self.exe = fixtures.make_fake_exe(os.path.join(self.app_dir, "AddonManager.exe"),
                                          marker=b"old")
        self.old_bytes = pathlib.Path(self.exe).read_bytes()
        self.new_path = fixtures.make_app_release(self.api, os.path.join(self.work, "dl"), URI)
        config.save_settings({})
        selfupdate.clear_ok()
        self.links = []
        self.root = tk.Tk()
        self.root.withdraw()

    def tearDown(self):
        try:
            self.root.destroy()
        except tk.TclError:
            pass
        self.root = None
        gc.collect()
        selfupdate.clear_ok()
        config.save_settings({})
        fixtures.remove(self.work)

    def make_app(self, exe=None, restart=None, updated_from=None):
        return ui.App(self.root, os.path.join(self.site, "manifest.json"), True, self.root_dir,
                      check_game=lambda: False, detect=lambda: [], github_api=self.api,
                      app_exe=exe or self.exe, updated_from=updated_from,
                      restart=restart or (lambda exe, v: FakeProc()),
                      open_link=self.links.append)

    def pump(self, app, until, timeout=20):
        end = time.time() + timeout
        while time.time() < end:
            try:
                self.root.update()
            except tk.TclError:
                return
            if until():
                break
            time.sleep(0.02)
        for _ in range(3):
            try:
                self.root.update()
            except tk.TclError:
                return

    def shown(self, app):
        return bool(app.update_bar.winfo_manager())

    def test_offer_later_and_whats_new(self):
        app = self.make_app()
        self.pump(app, lambda: app.update_mode == "offer" and not app.busy)
        self.assertTrue(self.shown(app))
        text = app.update_text.cget("text")
        self.assertEqual(text, "Addon Manager v99.0.0 is out.")
        for word in ("wow", "warcraft", "blizzard"):
            self.assertNotIn(word, text.lower())
            self.assertNotIn(word, errors.APP_CANT_HERE_BAR.lower())
        app.whats_new()
        self.assertEqual(self.links,
                         ["https://github.com/gettic/AddonManager/releases/tag/v99.0.0"])
        app.update_later_button.invoke()
        self.assertFalse(self.shown(app))

    def test_hidden_during_installs(self):
        app = self.make_app()
        self.pump(app, lambda: app.update_mode == "offer" and not app.busy)
        app.install_rows([r for r in app.rows if r.addon][:1])
        self.assertFalse(self.shown(app))
        self.pump(app, lambda: not app.busy)
        self.assertTrue(self.shown(app))

    def test_no_bar_without_exe_or_newer_release(self):
        app = ui.App(self.root, os.path.join(self.site, "manifest.json"), True, self.root_dir,
                     check_game=lambda: False, detect=lambda: [], github_api=self.api)
        self.pump(app, lambda: not app.busy)
        time.sleep(0.2)
        self.pump(app, lambda: False, timeout=0.2)
        self.assertIsNone(app.update_mode)
        self.assertFalse(self.shown(app))

    def test_update_now_swaps_and_closes(self):
        started = []

        def restart(exe, version):
            started.append((exe, version))
            selfupdate.mark_ok("99.0.0")   # what the new exe does once its window is up
            return FakeProc()

        app = self.make_app(restart=restart)
        self.pump(app, lambda: app.update_mode == "offer" and not app.busy)
        app.update_now_button.invoke()
        self.pump(app, lambda: app.closed)
        self.assertTrue(app.closed)
        self.assertEqual(started, [(self.exe, APP_VERSION)])
        with open(self.exe, "rb") as f:
            self.assertEqual(f.read(), pathlib.Path(self.new_path).read_bytes())
        self.assertTrue(os.path.exists(self.exe + ".old"))   # removed at the next start
        self.assertTrue(selfupdate.cleanup(self.exe))

    def test_new_exe_not_starting_rolls_back(self):
        with mock.patch.object(selfupdate, "WAIT_SECONDS", 0.5):
            app = self.make_app()
            self.pump(app, lambda: app.update_mode == "offer" and not app.busy)
            app.update_now_button.invoke()
            self.pump(app, lambda: not app.busy and not app.updating)
        self.assertFalse(app.closed)
        with open(self.exe, "rb") as f:
            self.assertEqual(f.read(), self.old_bytes)
        self.assertIn("didn't start", app.message_var.get())
        self.assertEqual(config.load_settings().get("update_failed"), "99.0.0")
        self.assertFalse(self.shown(app))
        selfupdate.cleanup(self.exe)
        self.assertEqual(sorted(os.listdir(self.app_dir)), ["AddonManager.exe"])

    def test_damaged_download_keeps_exe(self):
        with open(self.new_path, "ab") as f:
            f.write(b"tampered")
        app = self.make_app()
        self.pump(app, lambda: app.update_mode == "offer" and not app.busy)
        app.update_now_button.invoke()
        self.pump(app, lambda: not app.busy)
        self.assertIn("damaged", app.message_var.get())
        self.assertEqual(sorted(os.listdir(self.app_dir)), ["AddonManager.exe"])
        with open(self.exe, "rb") as f:
            self.assertEqual(f.read(), self.old_bytes)

    def test_exe_inside_game_folder_only_links(self):
        client = os.path.dirname(os.path.dirname(self.addons))
        exe = fixtures.make_fake_exe(os.path.join(client, "AddonManager.exe"))
        before = sorted(os.listdir(client))
        app = self.make_app(exe=exe)
        self.pump(app, lambda: app.update_mode == "offer" and not app.busy)
        app.update_now_button.invoke()
        self.assertEqual(app.update_mode, "cant")
        self.assertEqual(app.update_now_button.cget("text"), "Open download page")
        app.update_now_button.invoke()
        self.assertEqual(self.links, [selfupdate.DOWNLOAD_PAGE])
        self.assertEqual(sorted(os.listdir(client)), before)

    def test_bad_saved_folder_searches_again(self):
        for bad in (12345, ["x"], None, {"a": 1}, "", "a\0b"):
            with open(config.settings_path(), "w", encoding="utf-8") as f:
                json.dump({"wow_folder": bad, "known_addons": [1, ["A"], [2, 3], None],
                           "update_failed": 5}, f)
            calls = []
            app = ui.App(self.root, None, False, None, check_game=lambda: False,
                         detect=lambda: calls.append(1) or [self.addons], app_exe=None)
            self.pump(app, lambda: not app.busy)
            self.assertEqual(calls, [1], repr(bad))
            self.assertEqual(app.addons_dir, self.addons, repr(bad))
            app.close()
            self.root = tk.Tk()
            self.root.withdraw()

    def test_started_after_update(self):
        app = self.make_app(updated_from="0.0.9")
        self.pump(app, lambda: not app.busy)
        self.assertEqual(app.update_mode, "done")
        self.assertTrue(self.shown(app))
        self.assertIn("updated to", app.update_text.cget("text"))
        self.assertTrue(selfupdate.wait_ok(APP_VERSION, timeout=0.5))
        app.update_later_button.invoke()
        self.assertFalse(self.shown(app))


if __name__ == "__main__":
    unittest.main()
