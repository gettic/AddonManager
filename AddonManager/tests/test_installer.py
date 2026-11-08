"""Install/update safety against a fake WoW Forever folder."""

import os
import unittest
import zipfile
from unittest import mock

from am import download, installer, manifest, toc
from am.download import DownloadError
from am.installer import InstallError
from tests import fixtures


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.base = fixtures.temp_dir()
        self.root, self.addons = fixtures.make_fake_wow(self.base)
        self.client = os.path.dirname(os.path.dirname(self.addons))
        self.zips = os.path.join(self.base, "zips")
        os.makedirs(self.zips)
        self.tmp = os.path.join(self.base, "tmp")
        os.makedirs(self.tmp)
        self.protected = {
            "other": fixtures.tree_snapshot(os.path.join(self.addons, "OtherAddon")),
            "wtf": fixtures.tree_snapshot(os.path.join(self.client, "WTF")),
        }

    def tearDown(self):
        self.assertEqual(fixtures.tree_snapshot(os.path.join(self.addons, "OtherAddon")),
                         self.protected["other"], "OtherAddon changed")
        self.assertEqual(fixtures.tree_snapshot(os.path.join(self.client, "WTF")),
                         self.protected["wtf"], "WTF changed")
        leftovers = [n for n in os.listdir(self.addons) if n.startswith(".addonmanager-")]
        self.assertEqual(leftovers, [])
        self.assertEqual(os.listdir(self.tmp), [], "temp download not cleaned")
        fixtures.remove(self.base)

    def addon(self, zpath, folder="TaxiTime", version="1.3.0", sha=None):
        return manifest.Addon(folder=folder, name=folder, version=version,
                              url=manifest.to_url(zpath), sha256=sha or "")

    def good_zip(self, folder="TaxiTime", version="1.3.0", **kw):
        return fixtures.make_zip(os.path.join(self.zips, f"{folder}-{version}.zip"),
                                 folder, version, **kw)

    def install(self, addon):
        return installer.install(addon, self.addons, tmp_root=self.tmp)

    def assert_old_kept(self):
        self.assertEqual(toc.installed(self.addons, "TaxiTime"), (True, "1.2.0"))
        self.assertTrue(os.path.isfile(os.path.join(self.addons, "TaxiTime", "old_only.lua")))

    def assert_fails(self, addon, kind):
        with self.assertRaises((InstallError, DownloadError)) as c:
            self.install(addon)
        self.assertEqual(c.exception.kind, kind, str(c.exception))
        self.assert_old_kept()

    # --- good cases ---
    def test_update_replaces_folder(self):
        z = self.good_zip()
        self.assertEqual(self.install(self.addon(z, sha=fixtures.sha256(z))), "1.3.0")
        self.assertEqual(toc.installed(self.addons, "TaxiTime"), (True, "1.3.0"))
        folder = os.path.join(self.addons, "TaxiTime")
        self.assertFalse(os.path.exists(os.path.join(folder, "old_only.lua")))
        self.assertTrue(os.path.exists(os.path.join(folder, "new_only.lua")))

    def test_fresh_install(self):
        z = self.good_zip("GuildHub", "0.4.1")
        self.install(self.addon(z, "GuildHub", "0.4.1"))
        self.assertEqual(toc.installed(self.addons, "GuildHub"), (True, "0.4.1"))

    def test_same_version_reinstall(self):
        z = self.good_zip("TaxiTime", "1.2.0")
        self.install(self.addon(z, version="v1.2.0"))
        self.assertEqual(toc.installed(self.addons, "TaxiTime"), (True, "1.2.0"))

    def test_progress_called(self):
        calls = []
        z = self.good_zip()
        installer.install(self.addon(z), self.addons, progress=lambda d, t: calls.append(d),
                          tmp_root=self.tmp)
        self.assertTrue(calls)

    # --- refused downloads/zips: old version stays ---
    def test_wrong_checksum(self):
        self.assert_fails(self.addon(self.good_zip(), sha="0" * 64), "checksum")

    def test_version_mismatch(self):
        self.assert_fails(self.addon(self.good_zip(toc_version="1.2.9")), "mismatch")

    def test_missing_toc(self):
        z = fixtures.make_raw_zip(os.path.join(self.zips, "notoc.zip"),
                                  [("TaxiTime/TaxiTime.lua", "x")])
        self.assert_fails(self.addon(z), "bad_zip")

    def test_path_traversal(self):
        z = self.good_zip(files={"TaxiTime/../evil.txt": "x"})
        self.assert_fails(self.addon(z), "unsafe")
        self.assertFalse(os.path.exists(os.path.join(self.addons, "evil.txt")))

    def test_absolute_path(self):
        z = self.good_zip(files={"/abs.txt": "x"})
        self.assert_fails(self.addon(z), "unsafe")

    def test_drive_path(self):
        z = self.good_zip(files={"C:/drive.txt": "x"})
        self.assert_fails(self.addon(z), "unsafe")

    def test_extra_top_folder(self):
        z = self.good_zip(files={"OtherAddon/OtherAddon.toc": "## Version: 0\n"})
        self.assert_fails(self.addon(z), "unsafe")

    def test_symlink_entry(self):
        info = zipfile.ZipInfo("TaxiTime/link")
        info.external_attr = 0o120777 << 16
        z = os.path.join(self.zips, "link.zip")
        fixtures.make_zip(z, "TaxiTime", "1.3.0")
        with zipfile.ZipFile(z, "a") as zf:
            zf.writestr(info, "../../outside")
        self.assert_fails(self.addon(z), "unsafe")

    def test_zip_bomb(self):
        z = self.good_zip(files={"TaxiTime/zeros.bin": b"\0" * (5 * 1024 * 1024)})
        self.assert_fails(self.addon(z), "unsafe")

    def test_not_a_zip(self):
        p = os.path.join(self.zips, "bad.zip")
        with open(p, "wb") as f:
            f.write(b"this is not a zip")
        self.assert_fails(self.addon(p), "bad_zip")

    def test_missing_file(self):
        self.assert_fails(self.addon(os.path.join(self.zips, "nope.zip")), "offline")

    def test_too_big(self):
        a = self.addon(self.good_zip())
        a.size = 10
        with mock.patch.object(installer, "MAX_DOWNLOAD", 100):
            self.assert_fails(a, "too_big")

    def test_unsafe_folder_name(self):
        a = self.addon(self.good_zip())
        a.folder = "..\\TaxiTime"
        self.assert_fails(a, "unsafe")

    def test_not_addons_folder(self):
        with self.assertRaises(InstallError) as c:
            installer.install(self.addon(self.good_zip()), self.client, tmp_root=self.tmp)
        self.assertEqual(c.exception.kind, "bad_folder")

    # --- swap failures roll back ---
    def test_swap_failure_rolls_back(self):
        real_replace = os.replace

        def flaky(src, dst):
            if os.path.basename(dst) == "TaxiTime" and ".addonmanager-staging-" in src:
                raise PermissionError(5, "Access is denied")
            return real_replace(src, dst)

        with mock.patch("am.installer.os.replace", side_effect=flaky):
            self.assert_fails(self.addon(self.good_zip()), "permission")

    def test_locked_old_folder(self):
        def locked(src, dst):
            err = OSError(13, "in use")
            err.winerror = 32
            raise err

        with mock.patch("am.installer.os.replace", side_effect=locked):
            self.assert_fails(self.addon(self.good_zip()), "locked")

    # --- start-up recovery ---
    def test_recover_restores_backup(self):
        os.replace(os.path.join(self.addons, "TaxiTime"),
                   os.path.join(self.addons, ".addonmanager-backup-TaxiTime"))
        os.makedirs(os.path.join(self.addons, ".addonmanager-staging-abc", "TaxiTime"))
        actions = installer.recover(self.addons)
        self.assertIn(("restored", "TaxiTime"), actions)
        self.assert_old_kept()

    def test_recover_removes_stale_backup(self):
        fixtures.make_addon_folder(self.addons, ".addonmanager-backup-TaxiTime", "1.0")
        installer.recover(self.addons)
        self.assert_old_kept()

    def test_link_folder_left_alone(self):
        with mock.patch("am.installer._is_link", side_effect=lambda p: p.endswith("TaxiTime")):
            self.assert_fails(self.addon(self.good_zip()), "link")


class HttpTests(unittest.TestCase):
    """The real urllib path, against a local server on 127.0.0.1."""

    @classmethod
    def setUpClass(cls):
        cls.base = fixtures.temp_dir()
        cls.site = fixtures.make_server_files(cls.base)
        cls.server = fixtures.LocalServer(cls.site).__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.server.__exit__(None, None, None)
        fixtures.remove(cls.base)

    def test_manifest_and_install(self):
        m = manifest.load(self.server.url + "manifest.json", allow_local=True)
        self.assertEqual([a.folder for a in m.addons], ["TaxiTime", "GuildHub"])
        work = fixtures.temp_dir()
        try:
            _root, addons = fixtures.make_fake_wow(work)
            for a in m.addons:
                installer.install(a, addons)
            self.assertEqual(toc.installed(addons, "GuildHub"), (True, "0.4.1"))
        finally:
            fixtures.remove(work)

    def test_redirect(self):
        data = download.fetch_bytes(self.server.url + "redirect/manifest.json")
        self.assertIn(b"TaxiTime", data)

    def test_404(self):
        with self.assertRaises(manifest.ManifestError) as c:
            manifest.load(self.server.url + "missing.json", allow_local=True)
        self.assertEqual(c.exception.kind, "bad")

    def test_timeout(self):
        with self.assertRaises(manifest.ManifestError) as c:
            manifest.load(self.server.url + "slow", allow_local=True, timeout=0.5)
        self.assertEqual(c.exception.kind, "timeout")

    def test_remote_http_refused(self):
        with self.assertRaises(manifest.ManifestError):
            manifest.load("http://example.invalid/manifest.json", allow_local=False)


if __name__ == "__main__":
    unittest.main()
