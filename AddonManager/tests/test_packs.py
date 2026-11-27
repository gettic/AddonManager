"""Guide packs released from the guide's own repo under a tag prefix (alliance-v1.0.0):
the "packs" list key, tag streams, exact asset names, cache per stream, one request per repo,
packs that aren't out yet (no row), packs shown under the guide, Install all skipping
packs, and a pack bringing its main addon along. Fake data only, hidden window."""
import gc
import json
import os
import pathlib
import time
import tkinter as tk
import unittest
from unittest import mock

import tests  # noqa: F401  (temp home)
from am import config, errors, github, manifest, theme, toc, ui, versions
from tests import fixtures

PACKS_DOC = {"format": 1, "addons": [
    {"folder": "Guide", "name": "Guide", "repo": "gettic/Guide"},
    {"folder": "Other", "name": "Other", "repo": "gettic/Other"}],
    "packs": [
        {"folder": "Guide_Horde", "name": "Horde routes", "repo": "gettic/Guide",
         "tag_prefix": "horde-", "parent": "Guide"},
        {"folder": "Guide_Alliance", "name": "Alliance routes", "repo": "gettic/Guide",
         "tag_prefix": "alliance-", "parent": "Guide"}]}


def rel(tag, *names, prerelease=False, published=None):
    return fixtures.release_json(tag, [fixtures.asset(n, f"https://github.com/gettic/Guide/"
                                                      f"releases/download/{tag}/{n}")
                                       for n in names], prerelease=prerelease,
                                 published=published)


class ManifestTests(unittest.TestCase):
    def parse(self, doc):
        return manifest.parse(json.dumps(doc), "https://example.invalid/addons.json")

    def test_prefix_rule(self):
        for good in ("alliance-", "horde-", "a-", "pack2-", "old-world-"):
            self.assertTrue(manifest.is_safe_prefix(good), good)
        for bad in ("", "alliance", "Alliance-", "v1-", "1-", "-", "a--", "a_b-", "a b-",
                    "a/-", "x" * 40 + "-", None, 5):
            self.assertFalse(manifest.is_safe_prefix(bad), bad)

    def test_packs_key_read_and_put_under_parent(self):
        m = self.parse(PACKS_DOC)
        self.assertEqual([a.folder for a in m.addons],
                         ["Guide", "Guide_Horde", "Guide_Alliance", "Other"])
        horde = m.addons[1]
        self.assertEqual((horde.tag_prefix, horde.parent, horde.repo),
                         ("horde-", "Guide", "gettic/Guide"))
        self.assertEqual((m.addons[0].tag_prefix, m.addons[0].parent), ("", ""))

    def test_bad_entries(self):
        doc = {"addons": [{"folder": "Guide", "repo": "gettic/Guide"}], "packs": [
            {"folder": "BadPrefix", "repo": "gettic/Guide", "tag_prefix": "v2-"},
            {"folder": "SameStream", "repo": "gettic/guide"},        # = Guide's own tags
            {"folder": "A", "repo": "gettic/Guide", "tag_prefix": "a-", "parent": "Guide"},
            {"folder": "A2", "repo": "gettic/Guide", "tag_prefix": "a-"},   # same stream
            {"folder": "Orphan", "repo": "gettic/Guide", "tag_prefix": "o-", "parent": "Nope"},
            {"folder": "Deep", "repo": "gettic/Guide", "tag_prefix": "d-", "parent": "A"}]}
        m = self.parse(doc)
        by = {a.folder: a for a in m.addons}
        self.assertEqual(sorted(by), ["A", "Deep", "Guide", "Orphan"])
        self.assertEqual((by["Orphan"].parent, by["Deep"].parent), ("", ""))   # shown flat
        self.assertEqual(by["A"].parent, "Guide")   # one level only: Deep's parent has one

    def test_packs_not_a_list_ignored_and_old_lists_unchanged(self):
        m = self.parse({"addons": [{"folder": "Guide", "repo": "gettic/Guide"}],
                        "packs": {"x": 1}})
        self.assertEqual([a.folder for a in m.addons], ["Guide"])

    def test_real_list_packs(self):
        here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        with open(os.path.join(here, "addons.json"), encoding="utf-8") as f:
            m = manifest.parse(f.read(), "https://example.invalid/addons.json")
        folders = [a.folder for a in m.addons]
        i = folders.index("AllinOneGuide")
        self.assertEqual(folders[i + 1:i + 3], ["AllinOneGuide_Alliance", "AllinOneGuide_Horde"])
        for a in m.addons[i + 1:i + 3]:
            self.assertEqual((a.repo, a.parent), ("gettic/All-in-One-Guide", "AllinOneGuide"))
        self.assertEqual([a.tag_prefix for a in m.addons[i + 1:i + 3]], ["alliance-", "horde-"])


class StreamTests(unittest.TestCase):
    def test_in_stream_and_plain_tag(self):
        self.assertTrue(github.in_stream("v1.0.0"))
        self.assertTrue(github.in_stream("1.0.0-beta.2"))
        for tag in ("alliance-v1.0.0", "horde-1.0", "latest", ""):
            self.assertFalse(github.in_stream(tag), tag)
        self.assertTrue(github.in_stream("alliance-v1.0.0-beta.1", "alliance-"))
        self.assertTrue(github.in_stream("Alliance-1.2", "alliance-"))
        for tag in ("v1.0.0", "horde-v1.0.0", "alliance-vX", "alliance-", "alliance-v1.0!"):
            self.assertFalse(github.in_stream(tag, "alliance-"), tag)
        self.assertEqual(github.plain_tag("alliance-v1.0.0-beta.1", "alliance-"), "1.0.0-beta.1")
        self.assertEqual(github.plain_tag("v1.0.0", "alliance-"), "1.0.0")
        self.assertEqual(github.plain_tag("v1.0.0"), "1.0.0")

    def test_pick_asset_exact_for_packs(self):
        r = github._trim(rel("alliance-v1.0.0", "Guide-v0.2.5.zip",
                             "Guide_Alliance-v1.0.0.zip"))
        self.assertEqual(github.pick_asset(r, "Guide_Alliance", "alliance-")["name"],
                         "Guide_Alliance-v1.0.0.zip")
        only = github._trim(rel("alliance-v1.0.0", "Guide-v0.2.5.zip"))
        self.assertIsNone(github.pick_asset(only, "Guide_Alliance", "alliance-"))   # no fallback
        no_v = github._trim(rel("alliance-v1.0.0", "Guide_Alliance-1.0.0.zip"))
        self.assertIsNone(github.pick_asset(no_v, "Guide_Alliance", "alliance-"))
        self.assertEqual(github.pick_asset(only, "Guide")["name"], "Guide-v0.2.5.zip")

    def streams(self, pages, prefixes=("", "alliance-", "horde-")):
        calls = []

        def get_json(url, repo, timeout):
            calls.append(url)
            return pages[len(calls) - 1]
        with mock.patch.object(github, "_get_json", side_effect=get_json):
            out = github.newest_streams("gettic/Guide", "https://api.example", prefixes)
        return out, calls

    def test_one_list_split_into_streams(self):
        page = [   # newest first, like GitHub; beta.9 listed before beta.10
            rel("alliance-v1.0.0-beta.9", "a.zip", prerelease=True,
                published="2026-11-10T00:00:00Z"),
            rel("alliance-v1.0.0-beta.10", "a.zip", prerelease=True,
                published="2026-11-09T00:00:00Z"),
            rel("v0.3.0-beta.1", "g.zip", prerelease=True, published="2026-11-08T00:00:00Z"),
            rel("horde-v1.0.0", "h.zip", published="2026-11-07T00:00:00Z"),
            rel("horde-v1.0.0-beta.3", "h.zip", prerelease=True,
                published="2026-11-06T00:00:00Z"),   # older than horde's normal: dropped
            rel("v0.2.5", "g.zip", published="2026-11-05T00:00:00Z"),
            rel("alliance-v0.9.0", "a.zip", published="2026-11-04T00:00:00Z"),
            dict(rel("alliance-v9.0.0", "a.zip"), draft=True)]
        out, calls = self.streams([page])
        self.assertEqual(len(calls), 1)
        tags = {p: (s and s["tag"], b and b["tag"]) for p, (s, b) in out.items()}
        self.assertEqual(tags, {"": ("v0.2.5", "v0.3.0-beta.1"),
                                "alliance-": ("alliance-v0.9.0", "alliance-v1.0.0-beta.10"),
                                "horde-": ("horde-v1.0.0", None)})

    def test_main_stream_never_sees_pack_tags(self):
        page = [rel("alliance-v5.0.0", "a.zip", published="2026-11-10T00:00:00Z"),
                rel("v0.2.0", "g.zip", published="2026-11-01T00:00:00Z")]
        out, _ = self.streams([page], ("",))
        self.assertEqual(out[""][0]["tag"], "v0.2.0")

    def test_next_page_only_while_a_stream_has_nothing(self):
        full = [rel(f"v0.1.{i}", "g.zip") for i in range(github.PER_PAGE)]
        second = [rel("horde-v1.0.0", "h.zip")]
        out, calls = self.streams([full, second], ("", "horde-"))
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[1].endswith("per_page=100&page=2"))
        self.assertEqual(out["horde-"][0]["tag"], "horde-v1.0.0")
        out, calls = self.streams([full], ("",))   # every stream found something: one page
        self.assertEqual(len(calls), 1)
        out, calls = self.streams([full, full, full, full], ("", "horde-"))
        self.assertEqual(len(calls), github.MAX_PAGES)
        self.assertEqual(out["horde-"], (None, None))

    def test_cache_keys(self):
        self.assertEqual(github.cache_key("b", "g/R"), "b|g/R|releases")
        self.assertEqual(github.cache_key("b", "g/R", "horde-"), "b|g/R|releases|horde-")


class ResolveTests(unittest.TestCase):
    """resolve_all on the file-based fake API (dev mode)."""

    def setUp(self):
        self.work = fixtures.temp_dir("packtags_resolve_")
        self.api_dir = os.path.join(self.work, "api")
        self.api = pathlib.Path(self.api_dir).as_uri()
        self.cache = os.path.join(self.work, "cache.json")
        dl = pathlib.Path(self.work, "dl")
        dl.mkdir()
        url = lambda n: (dl / n).as_uri()
        g = fixtures.make_zip(str(dl / "Guide-v0.2.5.zip"), "Guide", "0.2.5")
        a = fixtures.make_zip(str(dl / "Guide_Alliance-v1.0.0-beta.1.zip"), "Guide_Alliance",
                              "1.0.0-beta.1")
        fixtures.write_releases(self.api_dir, "gettic/Guide", [
            fixtures.release_json("alliance-v1.0.0-beta.1", [fixtures.asset(
                "Guide_Alliance-v1.0.0-beta.1.zip", url("Guide_Alliance-v1.0.0-beta.1.zip"), a)],
                prerelease=True, published="2026-11-02T00:00:00Z"),
            fixtures.release_json("v0.2.5", [fixtures.asset(
                "Guide-v0.2.5.zip", url("Guide-v0.2.5.zip"), g)],
                published="2026-11-01T00:00:00Z")])
        self.addons = manifest.parse(json.dumps({"addons": PACKS_DOC["addons"][:1],
                                                 "packs": PACKS_DOC["packs"]}), self.api,
                                     True).addons

    def tearDown(self):
        fixtures.remove(self.work)

    def test_streams_resolved_with_one_request(self):
        with mock.patch.object(github, "newest_streams", wraps=github.newest_streams) as spy:
            out, problems = github.resolve_all(self.addons, self.api, True, self.cache,
                                               show_betas=True)
        self.assertEqual(spy.call_count, 1)
        by = {a.folder: a for a in out}
        self.assertEqual(by["Guide"].version, "0.2.5")
        self.assertEqual((by["Guide_Alliance"].version, by["Guide_Alliance"].beta),
                         ("1.0.0-beta.1", True))
        self.assertTrue(by["Guide_Alliance"].url.endswith("Guide_Alliance-v1.0.0-beta.1.zip"))
        self.assertEqual(problems["Guide_Horde"].kind, "not_out")
        cache = github.load_cache(self.cache)
        for p in ("", "alliance-", "horde-"):
            self.assertIn(github.cache_key(self.api, "gettic/Guide", p), cache)

    def test_betas_off_and_cache(self):
        out, problems = github.resolve_all(self.addons, self.api, True, self.cache)
        self.assertEqual(problems["Guide_Alliance"].kind, "beta_only")
        self.assertEqual(problems["Guide_Horde"].kind, "not_out")
        # Fresh cache: nothing asked again; same answers.
        with mock.patch.object(github, "newest_streams", side_effect=AssertionError("asked")):
            out2, problems2 = github.resolve_all(self.addons, self.api, True, self.cache)
        self.assertEqual(sorted(problems2), sorted(problems))

    def test_rate_limited_uses_each_streams_cache(self):
        github.resolve_all(self.addons, self.api, True, self.cache, show_betas=True)
        limited = github.ReleaseError("rate_limited", "x", wait=60)
        with mock.patch.object(github, "newest_streams", side_effect=limited):
            out, problems = github.resolve_all(self.addons, self.api, True, self.cache,
                                               use_fresh_cache=False, show_betas=True)
        by = {a.folder: a for a in out}
        self.assertEqual((by["Guide"].version, by["Guide_Alliance"].version),
                         ("0.2.5", "1.0.0-beta.1"))
        self.assertEqual(problems["Guide_Horde"].kind, "not_out")

    def test_selftest_not_out_is_fine(self):
        import main
        out, problems = github.resolve_all(self.addons, self.api, True, show_betas=True)
        left, problems, not_out = main._selftest_not_out(out, problems)
        self.assertEqual((not_out, problems), (["Guide_Horde"], {}))
        self.assertEqual([a.folder for a in left], ["Guide", "Guide_Alliance"])

    def test_cached_preview_knows_builtin_packs(self):
        github.resolve_all(self.addons, self.api, True, self.cache, show_betas=True)
        builtin = {"format": 1, "addons": PACKS_DOC["addons"][:1], "packs": PACKS_DOC["packs"]}
        with mock.patch.object(config, "BUILTIN_LIST", builtin):
            out = github.cached_preview([["Guide", "Guide"], ["Guide_Alliance", "A"]],
                                        self.api, True, self.cache, show_betas=True)
        self.assertEqual([(a.folder, a.version, a.parent) for a in out],
                         [("Guide", "0.2.5", ""), ("Guide_Alliance", "1.0.0-beta.1", "Guide")])


class WindowTests(unittest.TestCase):
    """The main window with a guide and two packs (hidden, local fake GitHub)."""

    def setUp(self):
        self.work = fixtures.temp_dir("packtags_ui_")
        self.root_dir, self.addons_dir = fixtures.make_fake_wow(os.path.join(self.work, "wow"))
        config.save_settings({})
        self.root = tk.Tk()
        self.root.withdraw()
        self.server = fixtures.LocalServer(os.path.join(self.work, "gh"))
        self.server.__enter__()
        gh = os.path.join(self.work, "gh")
        os.makedirs(os.path.join(gh, "dl"))
        self.api_dir = os.path.join(gh, "api")
        self.list_path = os.path.join(gh, "addons.json")
        with open(self.list_path, "w", encoding="utf-8") as f:
            json.dump({"format": 1, "addons": PACKS_DOC["addons"][:1],
                       "packs": PACKS_DOC["packs"]}, f)
        self.write(guide_beta=False)

    def write(self, guide_beta):
        gh = os.path.join(self.work, "gh")
        url = lambda n: self.server.url + "dl/" + n
        g = fixtures.make_zip(os.path.join(gh, "dl", "Guide-v0.2.5.zip"), "Guide", "0.2.5")
        a = fixtures.make_zip(os.path.join(gh, "dl", "Guide_Alliance-v1.0.0.zip"),
                              "Guide_Alliance", "1.0.0")
        fixtures.write_releases(self.api_dir, "gettic/Guide", [
            fixtures.release_json("alliance-v1.0.0", [fixtures.asset(
                "Guide_Alliance-v1.0.0.zip", url("Guide_Alliance-v1.0.0.zip"), a)],
                published="2026-11-02T00:00:00Z"),
            fixtures.release_json("v0.2.5", [fixtures.asset(
                "Guide-v0.2.5.zip", url("Guide-v0.2.5.zip"), g)], prerelease=guide_beta,
                published="2026-11-01T00:00:00Z")])

    def tearDown(self):
        try:
            self.root.destroy()
        except tk.TclError:
            pass
        self.root = None
        gc.collect()
        self.server.__exit__(None, None, None)
        fixtures.remove(self.work)

    def pump(self, app, timeout=15):
        end = time.time() + timeout
        self.root.update()
        while app.busy and time.time() < end:
            self.root.update()
            time.sleep(0.02)
        for _ in range(5):
            self.root.update()
            time.sleep(0.02)

    def make_app(self):
        app = ui.App(self.root, self.list_path, True, self.root_dir, check_game=lambda: False,
                     detect=lambda: [], github_api=self.server.url + "api")
        self.pump(app)
        return app

    def cells(self, app, folder):
        return app._cells[[r.folder for r in app.rows].index(folder)]

    def test_rows_pack_under_guide_and_not_out_hidden(self):
        app = self.make_app()
        self.assertEqual([r.folder for r in app.rows], ["Guide", "Guide_Alliance"])
        self.assertEqual(config.load_settings()["known_addons"],
                         [["Guide", "Guide"], ["Guide_Alliance", "Alliance routes"]])
        pad = str(self.cells(app, "Guide_Alliance")["name"].cget("padding"))
        self.assertTrue(pad and pad.split()[0] != "0", pad)
        self.assertIn(str(self.cells(app, "Guide")["name"].cget("padding")), ("", "0"))
        # An installed pack that isn't out shows (so it can still be uninstalled).
        fixtures.make_addon_folder(self.addons_dir, "Guide_Horde", "0.1.0")
        app.check_again()
        self.pump(app)
        self.assertIn("Guide_Horde", [r.folder for r in app.rows])
        self.assertEqual(self.cells(app, "Guide_Horde")["status"].cget("text"), "Not out yet")

    def test_install_all_skips_packs_update_all_updates_them(self):
        app = self.make_app()
        app.all_button.invoke()
        self.pump(app)
        self.assertEqual(toc.installed(self.addons_dir, "Guide"), (True, "0.2.5"))
        self.assertFalse(toc.installed(self.addons_dir, "Guide_Alliance")[0])
        self.assertIn("disabled", app.all_button.state())   # only the pack's own button left
        fixtures.make_addon_folder(self.addons_dir, "Guide_Alliance", "0.9.0")
        app.render()
        self.assertEqual(app.all_button.cget("text"), "Update all")
        app.all_button.invoke()
        self.pump(app)
        self.assertEqual(toc.installed(self.addons_dir, "Guide_Alliance"), (True, "1.0.0"))

    def test_pack_install_brings_the_guide(self):
        app = self.make_app()
        app.row_widgets["Guide_Alliance"][1].invoke()
        self.pump(app)
        self.assertEqual(toc.installed(self.addons_dir, "Guide"), (True, "0.2.5"))
        self.assertEqual(toc.installed(self.addons_dir, "Guide_Alliance"), (True, "1.0.0"))

    def test_pack_waits_when_guide_cant_be_installed(self):
        self.write(guide_beta=True)   # the guide is only out as a beta; betas are off
        app = self.make_app()
        app.row_widgets["Guide_Alliance"][1].invoke()
        self.pump(app)
        self.assertFalse(toc.installed(self.addons_dir, "Guide_Alliance")[0])
        cells = self.cells(app, "Guide_Alliance")
        self.assertEqual(cells["status"].cget("text"), "Needs Guide")
        self.assertIn("Show beta versions", cells["status_tip"].text)
        self.assertIn("only together with Guide", app.message_var.get())

    def test_needs_row_look(self):
        self.write(guide_beta=True)
        app = self.make_app()
        app.row_widgets["Guide_Alliance"][1].invoke()
        self.pump(app)
        cells = self.cells(app, "Guide_Alliance")
        self.assertEqual(str(cells["button"].cget("style")), "Item.TButton")   # not red
        self.assertEqual(str(cells["status"].cget("foreground")),
                         theme.STATUS[versions.UPDATE])
        self.assertNotIn("bold", str(cells["name"].cget("font")))
        self.assertIn("bold", str(self.cells(app, "Guide")["name"].cget("font")))
        probe = ui.ttk.Label(app.list_frame, text=errors.NEEDS_PARENT.format(parent="Guide"))
        width = probe.winfo_reqwidth()
        probe.destroy()
        self.assertGreaterEqual(int(app.list_frame.grid_columnconfigure(3)["minsize"]), width)

    def test_update_all_keeps_an_uninstalled_guide_out(self):
        fixtures.make_addon_folder(self.addons_dir, "Guide_Alliance", "0.9.0")
        app = self.make_app()
        app._set_removed({"Guide"})   # the player uninstalled the guide
        app.render()
        app.all_button.invoke()   # Update all
        self.pump(app)
        self.assertFalse(toc.installed(self.addons_dir, "Guide")[0])
        self.assertEqual(toc.installed(self.addons_dir, "Guide_Alliance"), (True, "0.9.0"))
        self.assertIn("Guide", app._removed())
        self.assertEqual(self.cells(app, "Guide_Alliance")["status"].cget("text"), "Needs Guide")
        self.assertIn("You uninstalled Guide", app.message_var.get())
        # The pack's own button brings the guide back.
        app.row_widgets["Guide_Alliance"][1].invoke()
        self.pump(app)
        self.assertEqual(toc.installed(self.addons_dir, "Guide"), (True, "0.2.5"))
        self.assertEqual(toc.installed(self.addons_dir, "Guide_Alliance"), (True, "1.0.0"))

    def test_uninstalled_pack_that_is_not_out_leaves_at_once(self):
        fixtures.make_addon_folder(self.addons_dir, "Guide_Horde", "0.1.0")
        app = self.make_app()
        self.assertIn("Guide_Horde", [r.folder for r in app.rows])
        fixtures.remove(os.path.join(self.addons_dir, "Guide_Horde"))
        app._handle(("removed", "Guide_Horde", "bin"))
        self.assertNotIn("Guide_Horde", [r.folder for r in app.rows])
        self.assertNotIn(["Guide_Horde", "Horde routes"],
                         config.load_settings()["known_addons"])
        self.assertEqual(len(app._cells), len(app.rows))

    def test_offline_first_start_shows_no_packs(self):
        problem = github.ReleaseError("offline")
        a = manifest.parse(json.dumps({"addons": PACKS_DOC["addons"][:1],
                                       "packs": PACKS_DOC["packs"]}), "file:///x", True).addons
        self.assertTrue(ui._hidden_pack(a[1], problem, set(), False))
        self.assertFalse(ui._hidden_pack(a[1], problem, {a[1].folder}, False))
        self.assertFalse(ui._hidden_pack(a[1], problem, set(), True))
        self.assertFalse(ui._hidden_pack(a[0], problem, set(), False))   # never the guide
        self.assertFalse(ui._hidden_pack(a[1], None, set(), False))


class TextTests(unittest.TestCase):
    def test_texts(self):
        self.assertEqual(errors.release_message(github.ReleaseError("not_out"), "X")[0],
                         "Not out yet")
        short, full = errors.needs_parent("All-in-One Guide", beta_only=True)
        self.assertEqual(short, "Needs All-in-One Guide")
        self.assertIn("Show beta versions", full)
        self.assertNotIn("beta", errors.needs_parent("G")[1])


if __name__ == "__main__":
    unittest.main()
