"""Addon versions and downloads from (fake) GitHub releases. No internet."""

import json
import os
import pathlib
import time
import unittest
from unittest import mock

import tests
from am import config, download, errors, github, installer, manifest, toc
from tests import fixtures


def _addon(folder="TaxiTime", repo="gettic/TaxiTime"):
    return manifest.Addon(folder=folder, name=folder, version="", url="", repo=repo)


class Basics(unittest.TestCase):
    def test_real_defaults(self):
        self.assertEqual(tests.REAL_GITHUB_API, "https://api.github.com")
        self.assertEqual(tests.REAL_MANIFEST_URL,
                         "https://raw.githubusercontent.com/gettic/AddonManager/main/addons.json")
        self.assertEqual(download.USER_AGENT.split("/")[0], "AddonManager")

    def test_builtin_list_matches_addons_json(self):
        here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        with open(os.path.join(here, "addons.json"), encoding="utf-8") as f:
            doc = json.load(f)
        strip = lambda d: [(a["folder"], a["repo"]) for a in d["addons"]]
        self.assertEqual(strip(doc), strip(config.BUILTIN_LIST))
        m = manifest.parse(json.dumps(doc), "https://example.invalid/addons.json")
        self.assertEqual([a.repo for a in m.addons], ["gettic/GuildHub", "gettic/TaxiTime"])

    def test_repo_entries(self):
        m = manifest.parse(json.dumps({"addons": [
            {"folder": "A", "repo": "gettic/A"},
            {"folder": "B", "repo": "gettic/../x"},
            {"folder": "C", "repo": "https://github.com/gettic/C"},
            {"folder": "D", "repo": "gettic/.."},
            {"folder": "E", "version": "1.0", "url": "https://x.invalid/E.zip"}]}),
            "https://x.invalid/list.json")
        self.assertEqual([(a.folder, a.repo) for a in m.addons], [("A", "gettic/A"), ("E", "")])

    def test_tags_and_digest(self):
        self.assertEqual(github.plain_tag("v0.0.1"), "0.0.1")
        self.assertEqual(github.plain_tag("1.3.0"), "1.3.0")
        self.assertEqual(github.parse_digest("sha256:" + "AB" * 32), "ab" * 32)
        for bad in (None, "", "md5:abc", "sha256:xyz", "ab" * 32):
            self.assertEqual(github.parse_digest(bad), "")

    def test_pick_asset(self):
        def rel(tag, *names, state="uploaded"):
            return {"tag": tag, "assets": [{"name": n, "url": "u/" + n, "digest": "", "size": 1,
                                             "state": state} for n in names]}
        # with and without the "v", in the tag and in the asset name
        self.assertEqual(github.pick_asset(rel("v0.0.1", "TaxiTime-v0.0.1.zip", "x.zip"),
                                           "TaxiTime")["name"], "TaxiTime-v0.0.1.zip")
        self.assertEqual(github.pick_asset(rel("v0.0.1", "TaxiTime-0.0.1.zip", "x.zip"),
                                           "TaxiTime")["name"], "TaxiTime-0.0.1.zip")
        self.assertEqual(github.pick_asset(rel("0.0.1", "TaxiTime-v0.0.1.zip", "x.zip"),
                                           "TaxiTime")["name"], "TaxiTime-v0.0.1.zip")
        self.assertEqual(github.pick_asset(rel("0.0.1", "TaxiTime-0.0.1.zip", "x.zip"),
                                           "TaxiTime")["name"], "TaxiTime-0.0.1.zip")
        # only one zip: take it; several unknown zips or none: no download
        self.assertEqual(github.pick_asset(rel("1.0", "Other.zip", "a.txt"), "TaxiTime")["name"],
                         "Other.zip")
        self.assertIsNone(github.pick_asset(rel("1.0", "a.zip", "b.zip"), "TaxiTime"))
        self.assertIsNone(github.pick_asset(rel("1.0", "a.txt"), "TaxiTime"))
        self.assertIsNone(github.pick_asset(rel("1.0", "TaxiTime-1.0.zip", state="open"),
                                            "TaxiTime"))

    def test_download_host_check(self):
        ok = "https://github.com/gettic/TaxiTime/releases/download/v1.0/TaxiTime-v1.0.zip"
        self.assertTrue(github.download_allowed(ok, "gettic/TaxiTime", False))
        for bad in ("https://github.com/someone/TaxiTime/releases/download/v1/T.zip",
                    "https://evil.example/gettic/TaxiTime/releases/download/v1/T.zip",
                    "http://github.com/gettic/TaxiTime/releases/download/v1/T.zip",
                    "https://github.com/gettic/TaxiTime/releases/download/../../x/T.zip",
                    "file:///C:/x/T.zip", "http://127.0.0.1:8/T.zip"):
            self.assertFalse(github.download_allowed(bad, "gettic/TaxiTime", False), bad)
        self.assertTrue(github.download_allowed("http://127.0.0.1:8/T.zip", "gettic/TaxiTime", True))

    def test_api_base_override_only_in_dev_mode(self):
        self.assertEqual(github.api_base("http://127.0.0.1:5/", False), config.GITHUB_API)
        self.assertEqual(github.api_base("http://127.0.0.1:5/", True), "http://127.0.0.1:5")
        self.assertEqual(github.api_base("http://evil.example/", True), config.GITHUB_API)

    def test_trailing_newline_refused(self):
        # finding 2: "$" in a regex also matches before a final newline
        self.assertFalse(manifest.is_safe_repo("gettic/TaxiTime\n"))
        self.assertFalse(manifest.is_safe_folder("TaxiTime\n"))
        m = manifest.parse(json.dumps({"addons": [
            {"folder": "A", "version": "1.0", "url": "https://x.invalid/A.zip",
             "sha256": "a" * 64 + "\n"},
            {"folder": "B", "repo": "gettic/B\n"}]}), "https://x.invalid/l.json")
        self.assertEqual(m.addons, [])
        self.assertEqual(github.parse_digest("sha256:" + "a" * 64 + "\n"), "")

    def test_digest_forms(self):
        # finding 3: upper-case prefix accepted; other algorithms logged, no hash check
        self.assertEqual(github.parse_digest("SHA256:" + "Ab" * 32), "ab" * 32)
        with self.assertLogs("am.github", "WARNING"):
            self.assertEqual(github.parse_digest("sha512:" + "a" * 128), "")

    def test_download_host_check_strict(self):
        # finding 4: port, user name, %-escapes, backslash
        for bad in ("https://github.com:8443/gettic/TaxiTime/releases/download/v1/T.zip",
                    "https://evil@github.com/gettic/TaxiTime/releases/download/v1/T.zip",
                    "https://github.com/gettic/TaxiTime/releases/download/%2e%2e/x/T.zip",
                    "https://github.com/gettic/TaxiTime/releases/download/v1\\..\\T.zip",
                    "https://github.com/gettic/TaxiTime/releases/download/v1/T.zip#x"):
            self.assertFalse(github.download_allowed(bad, "gettic/TaxiTime", False), bad)
        ok = ("https://github.com/x", "https://objects.githubusercontent.com/a?sig=1",
              "https://release-assets.githubusercontent.com/a")
        bad = ("http://objects.githubusercontent.com/a", "https://githubusercontent.com.evil.example/a",
               "https://evilgithubusercontent.com/a", "https://x@objects.githubusercontent.com/a",
               "https://objects.githubusercontent.com:444/a", "https://example.com/a",
               "file:///C:/a.zip")
        for u in ok:
            self.assertTrue(download.github_host_ok(u), u)
        for u in bad:
            self.assertFalse(download.github_host_ok(u), u)

    def test_redirect_never_downgrades_to_http(self):
        class Req:
            full_url = "https://github.com/gettic/T/releases/download/v1/T.zip"
        handler = download._CheckedRedirects()
        with self.assertRaises(download.DownloadError) as c:
            handler.redirect_request(Req(), None, 302, "Found", {}, "http://objects.githubusercontent.com/a")
        self.assertEqual(c.exception.kind, "redirect")
        strict = download._CheckedRedirects(download.github_host_ok)
        with self.assertRaises(download.DownloadError):
            strict.redirect_request(Req(), None, 302, "Found", {}, "https://evil.example/a")

    def test_messages(self):
        self.assertIn("3 minutes", errors.release_message(github.ReleaseError("rate_limited", wait=150), "X")[1])
        self.assertEqual(errors.release_message(github.ReleaseError("no_release"), "X")[0],
                         "No release yet")


class WithServer(unittest.TestCase):
    """A local web server plays GitHub: API JSON files plus the zips."""

    def setUp(self):
        self.work = fixtures.temp_dir("hosting_gh_")
        self.server = fixtures.LocalServer(os.path.join(self.work, "gh"))
        self.server.__enter__()
        self.gh = fixtures.make_github_site(self.work, lambda rel: self.server.url + rel)
        self.api = self.server.url + "api"
        self.api_dir = os.path.join(self.gh, "api")
        self.cache = os.path.join(self.work, "cache.json")
        _root, self.addons = fixtures.make_fake_wow(os.path.join(self.work, "wow"))

    def tearDown(self):
        self.server.__exit__(None, None, None)
        fixtures.remove(self.work)

    def resolve(self, addons=None, base=None, **kw):
        return github.resolve_all(addons or [_addon(), _addon("GuildHub", "gettic/GuildHub")],
                                  base or self.api, True, **kw)

    def test_resolve_and_install(self):
        addons, problems = self.resolve()
        self.assertEqual(problems, {})
        taxi, hub = addons
        self.assertEqual((taxi.version, hub.version), ("1.3.0", "0.4.1"))
        self.assertTrue(taxi.url.endswith("/dl/TaxiTime-v1.3.0.zip"))
        self.assertEqual(taxi.sha256, fixtures.sha256(os.path.join(self.gh, "dl", "TaxiTime-v1.3.0.zip")))
        self.assertGreater(taxi.size, 0)
        before = fixtures.tree_snapshot(os.path.join(self.addons, "OtherAddon"))
        for a in addons:
            installer.install(a, self.addons)
        self.assertEqual(toc.installed(self.addons, "TaxiTime"), (True, "1.3.0"))
        self.assertEqual(toc.installed(self.addons, "GuildHub"), (True, "0.4.1"))
        self.assertEqual(before, fixtures.tree_snapshot(os.path.join(self.addons, "OtherAddon")))

    def test_wrong_digest_keeps_old_version(self):
        z = os.path.join(self.gh, "dl", "TaxiTime-v1.3.0.zip")
        fixtures.write_release(self.api_dir, "gettic/TaxiTime", "v1.3.0", [fixtures.asset(
            "TaxiTime-v1.3.0.zip", self.server.url + "dl/TaxiTime-v1.3.0.zip", z,
            digest="sha256:" + "0" * 64)])
        (taxi,), problems = self.resolve([_addon()])
        self.assertEqual(problems, {})
        with self.assertRaises(download.DownloadError) as c:
            installer.install(taxi, self.addons)
        self.assertEqual(c.exception.kind, "checksum")
        self.assertEqual(toc.installed(self.addons, "TaxiTime"), (True, "1.2.0"))

    def test_no_digest_still_installs(self):
        z = os.path.join(self.gh, "dl", "TaxiTime-v1.3.0.zip")
        fixtures.write_release(self.api_dir, "gettic/TaxiTime", "1.3.0", [fixtures.asset(
            "TaxiTime-v1.3.0.zip", self.server.url + "dl/TaxiTime-v1.3.0.zip", z, digest=None)])
        (taxi,), problems = self.resolve([_addon()])
        self.assertEqual((problems, taxi.sha256), ({}, ""))
        installer.install(taxi, self.addons)
        self.assertEqual(toc.installed(self.addons, "TaxiTime"), (True, "1.3.0"))

    def test_tag_not_matching_toc_is_refused(self):
        z = os.path.join(self.gh, "dl", "TaxiTime-v1.3.0.zip")
        fixtures.write_release(self.api_dir, "gettic/TaxiTime", "v1.4.0", [fixtures.asset(
            "TaxiTime-v1.4.0.zip", self.server.url + "dl/TaxiTime-v1.3.0.zip", z)])
        (taxi,), _ = self.resolve([_addon()])
        with self.assertRaises(installer.InstallError):
            installer.install(taxi, self.addons)
        self.assertEqual(toc.installed(self.addons, "TaxiTime"), (True, "1.2.0"))

    def test_problems(self):
        fixtures.write_release(self.api_dir, "gettic/TaxiTime", "v2.0", [])
        fixtures.write_release(self.api_dir, "gettic/Broken", "", [], raw="not json")
        addons, problems = self.resolve([_addon(), _addon("Missing", "gettic/Missing"),
                                         _addon("Broken", "gettic/Broken")])
        self.assertEqual({k: v.kind for k, v in problems.items()},
                         {"TaxiTime": "no_asset", "Missing": "no_release", "Broken": "bad"})
        self.assertEqual([a.version for a in addons], ["", "", ""])

    def test_offline(self):
        (_a,), problems = self.resolve([_addon()], base="http://127.0.0.1:9")
        self.assertEqual(problems["TaxiTime"].kind, "offline")

    def test_non_github_download_refused_outside_dev_mode(self):
        (a,), problems = github.resolve_all([_addon()], self.api, allow_local=False)
        self.assertEqual(problems["TaxiTime"].kind, "bad")

    def test_cache_and_rate_limit(self):
        now = time.time()
        _, problems = self.resolve(cache_file=self.cache, now=now)
        self.assertEqual(problems, {})
        # Fresh cache: GitHub isn't asked at all.
        with mock.patch.object(github, "latest_release", side_effect=AssertionError("called")):
            (taxi, _hub), problems = self.resolve(cache_file=self.cache, now=now + 60)
        self.assertEqual((problems, taxi.version), ({}, "1.3.0"))
        # "Check again" asks GitHub even with a fresh cache.
        with mock.patch.object(github, "latest_release", wraps=github.latest_release) as spy:
            self.resolve(cache_file=self.cache, now=now + 60, use_fresh_cache=False)
        self.assertEqual(spy.call_count, 2)

    def test_rate_limited(self):
        limited = self.server.url + "limited"
        _, problems = self.resolve([_addon()], base=limited, cache_file=self.cache)
        err = problems["TaxiTime"]
        self.assertEqual(err.kind, "rate_limited")
        self.assertTrue(100 <= err.wait <= 150, err.wait)
        self.assertIn("3 minutes", errors.release_message(err, "Taxi Time")[1])
        # With an old cached answer the app carries on with it.
        cache = {f"{limited}|gettic/TaxiTime": {"time": time.time() - 3600, "release": {
            "tag": "v1.3.0", "assets": [{"name": "TaxiTime-v1.3.0.zip", "size": 5, "digest": "",
                                         "state": "uploaded",
                                         "url": self.server.url + "dl/TaxiTime-v1.3.0.zip"}]}}}
        github.save_cache(self.cache, cache)
        (taxi,), problems = self.resolve([_addon()], base=limited, cache_file=self.cache)
        self.assertEqual((problems, taxi.version), ({}, "1.3.0"))
        # A plain 403 is not a rate limit.
        _, problems = self.resolve([_addon()], base=self.server.url + "forbidden")
        self.assertEqual(problems["TaxiTime"].kind, "bad")

    def _write_cache_entry(self, release, when=None):
        github.save_cache(self.cache, {f"{self.api}|gettic/TaxiTime": {
            "time": time.time() if when is None else when, "release": release}})

    def test_damaged_cache_entries_refetched(self):
        # findings 1 and 5: dropped and fetched again in the same check, no problem shown
        direct = manifest.Addon(folder="Direct", name="Direct", version="1.0",
                                url=self.server.url + "dl/x.zip")
        for release, when in (({"tag": 130, "assets": []}, None),
                              ({"tag": "1.3.0", "assets": ["x"]}, None),
                              ({"tag": "1.3.0"}, None),
                              ({"tag": "v1.3.0", "assets": []}, "yesterday"),
                              ("not a dict", None)):
            self._write_cache_entry(release, when)
            addons, problems = self.resolve([direct, _addon()], cache_file=self.cache)
            self.assertEqual(problems, {}, release)
            self.assertEqual([a.version for a in addons], ["1.0", "1.3.0"])
            entry = github.load_cache(self.cache)[f"{self.api}|gettic/TaxiTime"]
            self.assertEqual(entry["release"]["tag"], "v1.3.0")

    def test_damaged_cache_while_rate_limited_only_that_row(self):
        limited = self.server.url + "limited"
        github.save_cache(self.cache, {f"{limited}|gettic/TaxiTime": {
            "time": time.time() - 3600, "release": {"tag": 130, "assets": []}}})
        direct = manifest.Addon(folder="Direct", name="Direct", version="1.0",
                                url=self.server.url + "dl/x.zip")
        addons, problems = github.resolve_all([direct, _addon()], limited, True,
                                              cache_file=self.cache)
        self.assertEqual(list(problems), ["TaxiTime"])
        self.assertEqual(problems["TaxiTime"].kind, "rate_limited")
        self.assertEqual(addons[0].version, "1.0")
        self.assertEqual(github.load_cache(self.cache), {})

    def test_load_worker_with_damaged_cache(self):
        # finding 1 as the player saw it: the whole list went blank
        import queue
        from am import ui
        github.save_cache(config.cache_path(), {f"{self.api}|gettic/TaxiTime": {
            "time": time.time(), "release": {"tag": 130, "assets": []}}})
        q = queue.Queue()
        ui._load_worker(q, lambda: False, os.path.join(self.gh, "addons.json"), True,
                        self.api, force=False)
        q.get()  # game check
        kind, m, error, problems = q.get()
        self.assertEqual((kind, error, problems), ("manifest", None, {}))
        self.assertEqual([a.version for a in m.addons], ["1.3.0", "0.4.1"])

    def test_unexpected_error_only_that_row(self):
        with mock.patch.object(github, "apply_release", side_effect=AttributeError("boom")):
            addons, problems = self.resolve([_addon()], cache_file=self.cache)
        self.assertEqual(problems["TaxiTime"].kind, "bad")
        self.assertEqual(errors.release_message(problems["TaxiTime"], "Taxi Time")[0],
                         "Can't check now")

    def test_strict_download_refuses_redirect_off_github(self):
        z = os.path.join(self.gh, "dl", "TaxiTime-v1.3.0.zip")
        addon = manifest.Addon(folder="TaxiTime", name="Taxi Time", version="1.3.0",
                               url=self.server.url + "redirect/dl/TaxiTime-v1.3.0.zip",
                               sha256=fixtures.sha256(z), strict_hosts=True)
        with self.assertRaises(download.DownloadError) as c:
            installer.install(addon, self.addons)
        self.assertEqual(c.exception.kind, "redirect")
        self.assertEqual(toc.installed(self.addons, "TaxiTime"), (True, "1.2.0"))
        # First address allowed, the redirect hop not.
        with self.assertRaises(download.DownloadError) as c:
            download.download_file(addon.url, os.path.join(self.work, "y.zip"), 10**7,
                                   url_check=lambda u: "/redirect/" in u)
        self.assertEqual(c.exception.kind, "redirect")
        # The same redirect is fine when the check allows it.
        got = download.download_file(addon.url, os.path.join(self.work, "x.zip"), 10**7,
                                     url_check=lambda u: u.startswith(self.server.url))
        self.assertEqual(got, addon.sha256)

    def test_real_mode_sets_strict_hosts(self):
        release = {"tag": "v1.0", "assets": [{
            "name": "TaxiTime-v1.0.zip", "size": 5, "digest": "", "state": "uploaded",
            "url": "https://github.com/gettic/TaxiTime/releases/download/v1.0/TaxiTime-v1.0.zip"}]}
        self.assertTrue(github.apply_release(_addon(), release, False).strict_hosts)
        self.assertFalse(github.apply_release(_addon(), release, True).strict_hosts)

    def test_empty_online_list_uses_builtin(self):
        # finding 6: a valid list with no usable addon falls back to the built-in list
        fixtures.write_manifest(os.path.join(self.gh, "empty.json"),
                                [{"folder": "bad/name", "repo": "x/y"}])
        url = self.server.url + "empty.json"
        with mock.patch.object(config, "DEFAULT_MANIFEST_URL", url):
            m = github.load_list(url, allow_local=True)
        self.assertEqual([a.repo for a in m.addons], ["gettic/GuildHub", "gettic/TaxiTime"])
        self.assertEqual(github.load_list(url, allow_local=True).addons, [])  # other lists: as is

    def test_fallback_to_builtin_list(self):
        with mock.patch.object(config, "DEFAULT_MANIFEST_URL", "https://127.0.0.1:9/addons.json"):
            m = github.load_list(config.DEFAULT_MANIFEST_URL)
            self.assertEqual([a.repo for a in m.addons], ["gettic/GuildHub", "gettic/TaxiTime"])
            with self.assertRaises(manifest.ManifestError):   # other lists don't fall back
                github.load_list("https://127.0.0.1:9/other.json")

    def test_file_based_fake_api(self):
        """The layout the .exe self-test uses: everything as local files, no server."""
        base = os.path.join(self.work, "files")
        os.makedirs(base)
        gh = fixtures.make_github_site(
            base, lambda rel: pathlib.Path(base, "gh", rel).as_uri())
        api = github.api_base(os.path.join(gh, "api"), True)
        m = manifest.load(os.path.join(gh, "addons.json"), allow_local=True)
        addons, problems = github.resolve_all(m.addons, api, True)
        self.assertEqual(problems, {})
        for a in addons:
            installer.install(a, self.addons)
        self.assertEqual(toc.installed(self.addons, "TaxiTime"), (True, "1.3.0"))


if __name__ == "__main__":
    unittest.main()
