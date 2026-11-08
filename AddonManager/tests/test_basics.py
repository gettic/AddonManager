"""versions, toc, manifest, gamecheck, config, errors."""

import json
import os
import unittest

from am import config, errors, gamecheck, toc, versions
from am.download import DownloadError
from am.installer import InstallError
from am.manifest import ManifestError, is_safe_folder, parse, url_allowed
from tests import fixtures


class VersionTests(unittest.TestCase):
    def test_compare(self):
        self.assertEqual(versions.compare("1.10.0", "1.9.2"), 1)
        self.assertEqual(versions.compare("v1.3", "1.3.0"), 0)
        self.assertEqual(versions.compare("1.2.0", "1.3.0"), -1)
        self.assertIsNone(versions.compare("1.2-beta", "1.2"))
        self.assertEqual(versions.compare("1.2-beta", "v1.2-beta"), 0)

    def test_display(self):
        self.assertEqual(versions.display("1.3.0"), "v1.3.0")
        self.assertEqual(versions.display("v1.3.0"), "v1.3.0")
        self.assertEqual(versions.display(None), "-")

    def test_status(self):
        s = versions.status
        self.assertEqual(s(False, None, "1.0"), versions.NOT_INSTALLED)
        self.assertEqual(s(True, None, "1.0"), versions.UNKNOWN)
        self.assertEqual(s(True, "1.0", "1.0.0"), versions.UP_TO_DATE)
        self.assertEqual(s(True, "0.9", "1.0"), versions.UPDATE)
        self.assertEqual(s(True, "2.0", "1.0"), versions.NEWER)
        self.assertEqual(s(True, "x", "y"), versions.UPDATE)
        self.assertEqual(s(True, "1.0", None), versions.NO_LATEST)


class TocTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(toc.parse_version("## Title: X\n##Version:  1.2.3 \n"), "1.2.3")
        self.assertEqual(toc.parse_version("## Version: |cff00ff001.4|r"), "1.4")
        self.assertIsNone(toc.parse_version("## Version: @project-version@"))
        self.assertIsNone(toc.parse_version("## Title: none"))

    def test_installed(self):
        base = fixtures.temp_dir()
        try:
            _root, addons = fixtures.make_fake_wow(base)
            self.assertEqual(toc.installed(addons, "TaxiTime"), (True, "1.2.0"))
            self.assertEqual(toc.installed(addons, "GuildHub"), (False, None))
            self.assertEqual(toc.installed(addons, "OtherAddon"), (True, None))
        finally:
            fixtures.remove(base)


class ManifestTests(unittest.TestCase):
    BASE = "https://example.invalid/list/manifest.json"

    def doc(self, **over):
        entry = {"folder": "TaxiTime", "name": "Taxi Time", "version": "1.3.0",
                 "url": "TaxiTime-1.3.0.zip"}
        entry.update(over)
        return json.dumps({"format": 1, "addons": [entry]})

    def test_relative_url(self):
        m = parse(self.doc(), self.BASE)
        self.assertEqual(m.addons[0].url, "https://example.invalid/list/TaxiTime-1.3.0.zip")

    def test_bad_entries_skipped(self):
        for bad in ({"folder": "..\\evil"}, {"folder": "../x"}, {"folder": ".hidden"},
                    {"folder": "C:x"}, {"version": "1.0; rm"}, {"url": ""},
                    {"url": "http://example.invalid/x.zip"}, {"sha256": "abc"}):
            self.assertEqual(parse(self.doc(**bad), self.BASE).addons, [], bad)

    def test_local_only_in_dev(self):
        self.assertFalse(url_allowed("file:///C:/x.zip", False))
        self.assertTrue(url_allowed("file:///C:/x.zip", True))
        self.assertTrue(url_allowed("http://127.0.0.1:8000/x", True))
        self.assertFalse(url_allowed("http://example.com/x", True))

    def test_format(self):
        with self.assertRaises(ManifestError) as c:
            parse(json.dumps({"format": 2, "addons": []}), self.BASE)
        self.assertEqual(c.exception.kind, "too_new")
        with self.assertRaises(ManifestError):
            parse("not json", self.BASE)

    def test_safe_folder(self):
        self.assertTrue(is_safe_folder("Taxi_Time-2"))
        for bad in ("", ".", "..", "a/b", "a\\b", "C:", "x.", None):
            self.assertFalse(is_safe_folder(bad), bad)

    def test_not_set(self):
        from am import manifest
        with self.assertRaises(ManifestError) as c:
            manifest.load("")
        self.assertEqual(c.exception.kind, "not_set")
        self.assertEqual(errors.manifest_message(c.exception), "No addon list is set up yet.")


class GameCheckTests(unittest.TestCase):
    def test_names_only(self):
        out = '"System","4","Services","0","100 K"\r\n"Wow.exe","1234","Console","1","2 G"\r\n'
        self.assertTrue(gamecheck.game_running(run=lambda: out))
        self.assertFalse(gamecheck.game_running(run=lambda: '"WowUp.exe","1","C","1","1 K"'))

    def test_failure_is_false(self):
        def boom():
            raise OSError("no tasklist")
        self.assertFalse(gamecheck.game_running(run=boom))


class ConfigTests(unittest.TestCase):
    def test_home_is_temp(self):
        self.assertIn("v1core_home_", config.home_dir())

    def test_roundtrip(self):
        self.assertTrue(config.save_settings({"wow_folder": "X"}))
        self.assertEqual(config.load_settings()["wow_folder"], "X")
        self.assertEqual(config.manifest_url({}), config.DEFAULT_MANIFEST_URL)
        self.assertEqual(config.manifest_url({"manifest_url": "https://a"}), "https://a")


class ErrorTests(unittest.TestCase):
    def test_plain_messages(self):
        for exc in (DownloadError("offline"), DownloadError("timeout"), DownloadError("checksum"),
                    DownloadError("http"), InstallError("unsafe"), InstallError("permission"),
                    InstallError("locked"), InstallError("disk_full"), InstallError("link"),
                    InstallError("other"), ValueError("x")):
            msg = errors.install_message(exc, "Taxi Time")
            self.assertTrue(msg and "Traceback" not in msg and "Error" not in msg, msg)


if __name__ == "__main__":
    unittest.main()
