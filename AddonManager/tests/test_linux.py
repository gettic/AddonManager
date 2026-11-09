"""Linux / SteamOS paths: Wine/Proton prefixes, Wine's system.reg, upper/lower case
folder names, the /proc game check, the settings folder and the Linux self-update.
Runs on any system: a fake home folder, fake media folder and fake /proc in temp
folders; never the real home, Steam or game folders."""

import os
import stat
import unittest
from unittest import mock

from am import config, gamecheck, selfupdate, wowpath
from tests import fixtures

PF86 = "Program Files (x86)"


def make_client(drive_c_or_base, client="_forever_", parents=(PF86,), root="World of Warcraft"):
    """<base>/<parents>/<root>/<client> with WTF and Interface/AddOns. Returns the AddOns."""
    client_dir = os.path.join(drive_c_or_base, *parents, root, client)
    addons = os.path.join(client_dir, "Interface", "AddOns")
    os.makedirs(addons)
    os.makedirs(os.path.join(client_dir, "WTF"))
    return addons


def make_prefix(path):
    os.makedirs(os.path.join(path, "drive_c"), exist_ok=True)
    return path


class Base(unittest.TestCase):
    def setUp(self):
        self.work = fixtures.temp_dir("linux_paths_")
        self.home = os.path.join(self.work, "home")
        self.media = os.path.join(self.work, "media")
        os.makedirs(self.home)
        os.makedirs(self.media)

    def tearDown(self):
        fixtures.remove(self.work)

    def detect(self):
        return wowpath.detect(linux=True, home=self.home, media_roots=(self.media,))

    def same(self, found, expected):
        self.assertEqual([os.path.normcase(os.path.normpath(p)) for p in found],
                         [os.path.normcase(os.path.normpath(p)) for p in expected])


class Prefixes(Base):
    def steam_pfx(self, steam_root, appid="3141592653"):
        return make_prefix(os.path.join(steam_root, "steamapps", "compatdata", appid, "pfx"))

    def test_launchers_found(self):
        h = self.home
        cases = {
            "steam": self.steam_pfx(os.path.join(h, ".local", "share", "Steam")),
            "steam flatpak": self.steam_pfx(os.path.join(
                h, ".var", "app", "com.valvesoftware.Steam", ".local", "share", "Steam")),
            "sd card": self.steam_pfx(os.path.join(self.media, "mmcblk0p1")),
            "sd card SteamLibrary": self.steam_pfx(os.path.join(self.media, "deck", "SD",
                                                                "SteamLibrary")),
            "lutris": make_prefix(os.path.join(h, "Games", "battlenet")),
            "heroic": make_prefix(os.path.join(h, "Games", "Heroic", "Prefixes", "default",
                                               "Battle.net")),
            "bottles flatpak": make_prefix(os.path.join(
                h, ".var", "app", "com.usebottles.bottles", "data", "bottles", "bottles", "BNet")),
            "wine": make_prefix(os.path.join(h, ".wine")),
        }
        for what, prefix in cases.items():
            with self.subTest(what):
                addons = make_client(os.path.join(prefix, "drive_c"))
                self.same(self.detect(), [addons])
                fixtures.remove(os.path.dirname(os.path.dirname(os.path.dirname(addons))))

    def test_library_from_vdf(self):
        lib = os.path.join(self.work, "elsewhere", "Lib")
        steam = os.path.join(self.home, ".local", "share", "Steam", "steamapps")
        os.makedirs(steam)
        with open(os.path.join(steam, "libraryfolders.vdf"), "w", encoding="utf-8") as f:
            f.write('"libraryfolders"\n{\n\t"0"\n\t{\n\t\t"path"\t\t"%s"\n\t}\n}\n'
                    % lib.replace("\\", "\\\\"))
        addons = make_client(os.path.join(self.steam_pfx(lib), "drive_c"))
        self.same(self.detect(), [addons])

    def test_forever_picked_other_clients_never(self):
        pfx = self.steam_pfx(os.path.join(self.home, ".local", "share", "Steam"))
        drive_c = os.path.join(pfx, "drive_c")
        addons = make_client(drive_c)
        make_client(drive_c, client="_retail_")
        found = self.detect()
        self.same(found, [addons])
        self.assertEqual(len(found.others), 1)
        fixtures.remove(os.path.dirname(os.path.dirname(addons)))
        found = self.detect()
        self.assertEqual(list(found), [])
        self.assertEqual(len(found.others), 1)

    def test_nothing_found_and_no_crash_without_home(self):
        self.assertEqual(list(self.detect()), [])
        found = wowpath.detect(linux=True, home=os.path.join(self.work, "missing"),
                               media_roots=(os.path.join(self.work, "nope"),))
        self.assertEqual(list(found), [])

    def test_picked_prefix_folders(self):
        compat = os.path.join(self.home, ".local", "share", "Steam", "steamapps", "compatdata", "77")
        pfx = make_prefix(os.path.join(compat, "pfx"))
        addons = make_client(os.path.join(pfx, "drive_c"))
        for picked in (compat, pfx, os.path.join(pfx, "drive_c")):
            with self.subTest(picked):
                status, got = wowpath.from_picked(picked)
                self.assertEqual(status, "ok")
                self.same([got], [addons])
        self.assertEqual(wowpath.from_picked(self.home), ("not_wow", None))


class WineRegistry(Base):
    REG = (
        'WINE REGISTRY Version 2\n'
        ';; All keys relative to \\\\Machine\n\n'
        '[Software\\\\Wow6432Node\\\\Blizzard Entertainment\\\\World of Warcraft] 1700000000\n'
        '#time=1da0000000000000\n'
        '"InstallPath"="C:\\\\MyGames\\\\WF\\\\_forever_\\\\"\n'
        '"Other"=dword:00000001\n\n'
        '[Software\\\\Microsoft\\\\Windows\\\\CurrentVersion\\\\Uninstall\\\\Battle.net] 1700000000\n'
        '"DisplayName"="Battle.net"\n'
        '"InstallLocation"="C:\\\\Program Files (x86)\\\\Battle.net"\n\n'
        '[Software\\\\Wow6432Node\\\\Microsoft\\\\Windows\\\\CurrentVersion\\\\Uninstall\\\\WF] 1\n'
        '"DisplayName"="World of Warcraft \\"Forever\\""\n'
        '"InstallLocation"="C:\\\\Other Place\\\\WoW"\n'
    )

    def make(self):
        pfx = make_prefix(os.path.join(self.home, ".wine"))
        with open(os.path.join(pfx, "system.reg"), "w", encoding="utf-8") as f:
            f.write(self.REG)
        return pfx

    def test_parse(self):
        pfx = self.make()
        self.assertEqual(wowpath.read_wine_registry(pfx),
                         ["C:\\MyGames\\WF\\_forever_\\", "C:\\Other Place\\WoW"])
        self.assertEqual(wowpath.read_wine_registry(os.path.join(self.work, "none")), [])

    def test_registry_install_found(self):
        pfx = self.make()
        drive_c = os.path.join(pfx, "drive_c")
        os.makedirs(os.path.join(drive_c, PF86, "Battle.net"))
        addons = make_client(drive_c, parents=("MyGames",), root="WF")
        self.same(self.detect(), [addons])

    def test_registry_skipped_without_battlenet(self):
        pfx = self.make()
        make_client(os.path.join(pfx, "drive_c"), parents=("MyGames",), root="WF")
        self.assertEqual(list(self.detect()), [])

    def test_wine_to_linux(self):
        pfx = make_prefix(os.path.join(self.work, "pfx"))
        got = wowpath.wine_to_linux(pfx, "C:\\Program Files (x86)\\World of Warcraft\\")
        self.same([got], [os.path.join(pfx, "drive_c", PF86, "World of Warcraft")])
        for bad in ("", "relative\\path", "C:\\a\\..\\..\\etc", "Q:\\x", "\\\\server\\share"):
            self.assertIsNone(wowpath.wine_to_linux(pfx, bad), bad)


class CaseInsensitive(Base):
    def test_existing_spelling_reused(self):
        client = os.path.join(self.work, "_forever_")
        os.makedirs(os.path.join(client, "interface", "Addons"))
        os.makedirs(os.path.join(client, "wtf"))
        with mock.patch.object(wowpath, "CASE_SENSITIVE", True):
            self.assertTrue(wowpath.is_client_dir(client))
            got = wowpath.addons_dir_of(client)
            self.assertEqual(os.path.basename(got), "Addons")
            self.assertEqual(os.path.basename(os.path.dirname(got)), "interface")
            fresh = os.path.join(self.work, "_new_")
            os.makedirs(fresh)
            self.assertEqual(wowpath.addons_dir_of(fresh),
                             os.path.join(fresh, "Interface", "AddOns"))

    def test_lowercase_install_folders(self):
        pfx = make_prefix(os.path.join(self.home, ".wine"))
        drive_c = os.path.join(pfx, "drive_c")
        client = os.path.join(drive_c, "program files (x86)", "world of warcraft", "_forever_")
        os.makedirs(os.path.join(client, "Interface", "AddOns"))
        with mock.patch.object(wowpath, "CASE_SENSITIVE", True):
            found = self.detect()
        self.assertEqual(len(found), 1)
        self.assertEqual(os.path.normcase(os.path.dirname(os.path.dirname(found[0]))),
                         os.path.normcase(client))


class ProcGameCheck(unittest.TestCase):
    def setUp(self):
        self.proc = fixtures.temp_dir("linux_proc_")

    def tearDown(self):
        fixtures.remove(self.proc)

    def pid(self, n, comm, cmdline=b"", environ=b"SECRET=1"):
        d = os.path.join(self.proc, str(n))
        os.makedirs(d)
        for name, data in (("comm", comm + b"\n"), ("cmdline", cmdline), ("environ", environ)):
            with open(os.path.join(d, name), "wb") as f:
                f.write(data)

    def test_names(self):
        self.pid(1, b"systemd", b"/sbin/init\0")
        self.pid(2, b"bash", b"bash\0-c\0Wow.exe\0")     # a plain program named Wow.exe in its args
        self.assertFalse(gamecheck.linux_game_running(self.proc))
        self.pid(3, b"wine64-preloade",
                 b"C:\\Program Files (x86)\\World of Warcraft\\_forever_\\WowForever.exe\0-x\0")
        self.assertTrue(gamecheck.linux_game_running(self.proc))

    def test_comm_name(self):
        self.pid(5, b"Wow.exe")
        self.assertTrue(gamecheck.linux_game_running(self.proc))
        fixtures.remove(os.path.join(self.proc, "5"))
        self.pid(6, b"WowClassicT.exe")
        self.assertTrue(gamecheck.linux_game_running(self.proc))

    def test_only_comm_and_cmdline_read(self):
        self.pid(1, b"steam", b"/usr/bin/steam\0")
        self.pid(2, b"wineserver", b"wineserver\0")
        read = []
        real = gamecheck._read_small
        with mock.patch.object(gamecheck, "_read_small",
                               side_effect=lambda p, n: read.append(os.path.basename(p)) or real(p, n)):
            self.assertFalse(gamecheck.linux_game_running(self.proc))
        self.assertTrue(set(read) <= {"comm", "cmdline"}, read)
        self.assertEqual(read.count("cmdline"), 1)   # only the wine process

    def test_missing_proc(self):
        self.assertFalse(gamecheck.linux_game_running(os.path.join(self.proc, "none")))


class HomeDir(unittest.TestCase):
    def test_linux_config_folder(self):
        env = {k: v for k, v in os.environ.items() if k != config.HOME_ENV}
        with mock.patch.object(config.os, "name", "posix"):
            with mock.patch.dict(os.environ, dict(env, XDG_CONFIG_HOME=os.path.abspath("cfg")),
                                 clear=True):
                self.assertEqual(config.home_dir(),
                                 os.path.join(os.path.abspath("cfg"), "AddonManager"))
            with mock.patch.dict(os.environ, dict(env, XDG_CONFIG_HOME="relative"), clear=True):
                self.assertEqual(config.home_dir(), os.path.join(
                    os.path.expanduser("~"), ".config", "AddonManager"))

    def test_asset_per_system(self):
        self.assertEqual(config.APP_ASSET,
                         "AddonManager.exe" if os.name == "nt" else "AddonManager-linux-x86_64")
        self.assertEqual(selfupdate.PROGRAM_MAGIC, b"MZ" if os.name == "nt" else b"\x7fELF")

    def test_other_systems_asset_refused(self):
        other = (config.LINUX_ASSET if config.APP_ASSET == config.WINDOWS_ASSET
                 else config.WINDOWS_ASSET)
        rel = {"tag": "v99.0.0", "assets": [{
            "name": other, "url": "https://github.com/gettic/AddonManager/releases/download/"
                                  f"v99.0.0/{other}",
            "digest": "sha256:" + "a" * 64, "size": 5_000_000, "state": "uploaded"}]}
        self.assertIsNone(selfupdate.pick(rel, "0.1.0"))


@unittest.skipIf(os.name == "nt", "Linux only: the executable bit")
class LinuxFetch(unittest.TestCase):
    def test_new_file_is_executable(self):
        import pathlib
        work = fixtures.temp_dir("linux_fetch_")
        try:
            exe = fixtures.make_fake_exe(os.path.join(work, "AddonManager"), marker=b"old")
            api = os.path.join(work, "api")
            fixtures.make_app_release(api, os.path.join(work, "dl"),
                                      lambda p: pathlib.Path(p).as_uri())
            u = selfupdate.check("0.1.0", pathlib.Path(api).as_uri(), allow_local=True)
            new = selfupdate.fetch(u, exe, allow_local=True)
            self.assertTrue(os.stat(new).st_mode & stat.S_IXUSR)
        finally:
            fixtures.remove(work)


if __name__ == "__main__":
    unittest.main()
