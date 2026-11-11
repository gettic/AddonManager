"""Folder detection (fake drives, fake registry) and the hidden window."""

import gc
import os
import pathlib
import time
import tkinter as tk
import unittest

from am import config, toc, ui, wowpath
from tests import fixtures


class WowPathTests(unittest.TestCase):
    def setUp(self):
        self.base = fixtures.temp_dir()

    def tearDown(self):
        fixtures.remove(self.base)

    def test_detect_common_path_on_fake_drive(self):
        drive = os.path.join(self.base, "D")
        root, addons = fixtures.make_fake_wow(os.path.join(drive, "Program Files (x86)"))
        found = wowpath.detect(drives=[drive], registry=lambda: [])
        self.assertEqual([os.path.normcase(f) for f in found], [os.path.normcase(addons)])

    def test_detect_registry(self):
        root, addons = fixtures.make_fake_wow(os.path.join(self.base, "Elsewhere"))
        found = wowpath.detect(drives=[], registry=lambda: [root])
        self.assertEqual(found, [addons])

    def test_detect_nothing(self):
        self.assertEqual(wowpath.detect(drives=[self.base], registry=lambda: []), [])

    def test_registry_failure_ignored(self):
        def boom():
            raise OSError("no registry")
        self.assertEqual(wowpath.detect(drives=[], registry=boom), [])

    def test_picked_variants(self):
        root, addons = fixtures.make_fake_wow(self.base)
        client = os.path.dirname(os.path.dirname(addons))
        for picked in (root, client, os.path.dirname(addons), addons,
                       os.path.join(addons, "TaxiTime")):
            self.assertEqual(wowpath.from_picked(picked), ("ok", addons), picked)

    def test_picked_not_wow(self):
        empty = os.path.join(self.base, "Pictures")
        os.makedirs(empty)
        self.assertEqual(wowpath.from_picked(empty), ("not_wow", None))

    def test_prefers_forever_client(self):
        root, addons = fixtures.make_fake_wow(self.base)
        os.makedirs(os.path.join(root, "_retail_", "WTF"))
        self.assertEqual(wowpath.from_picked(root), ("ok", addons))

    def test_several_clients_unclear(self):
        root = os.path.join(self.base, "World of Warcraft")
        for c in ("_retail_", "_classic_"):
            os.makedirs(os.path.join(root, c, "WTF"))
        self.assertEqual(wowpath.from_picked(root), ("choose", None))

    def test_forever_by_addon_toc(self):
        # Forever addons inside a known other client (_classic_era_) are not enough.
        root, addons = fixtures.make_fake_wow(self.base, client="_classic_era_")
        os.makedirs(os.path.join(root, "_retail_", "WTF"))
        self.assertEqual(wowpath.from_picked(root), ("choose", None))
        # ...but an unknown-named client holding Forever addons is fine.
        other = fixtures.temp_dir()
        try:
            root2, addons2 = fixtures.make_fake_wow(other, client="_guild_")
            os.makedirs(os.path.join(root2, "_retail_", "WTF"))
            self.assertEqual(wowpath.from_picked(root2), ("ok", addons2))
        finally:
            fixtures.remove(other)

    def test_client_kind(self):
        for name, kind in (("_forever_", "forever"), ("WoW Forever", "forever"),
                           ("_classic_beta_", "other"), ("_classic_beta", "other"),
                           ("_retail_", "other"), ("_ptr_", "other"), ("_xptr_", "other"),
                           ("_beta_", "other"), ("_classic_era_", "other"),
                           ("_classic_", "other"), ("_anniversary_", "other"),
                           ("World of Warcraft", "unknown"), ("_guild_", "unknown")):
            self.assertEqual(wowpath.client_kind(os.path.join(self.base, name)), kind, name)
        exe_dir = os.path.join(self.base, "Game")
        os.makedirs(exe_dir)
        open(os.path.join(exe_dir, "WowForever.exe"), "wb").close()
        self.assertEqual(wowpath.client_kind(exe_dir), "forever")

    def test_beta_with_forever_addons_not_auto_picked(self):
        drive = os.path.join(self.base, "D")
        root, beta_addons = fixtures.make_fake_wow(drive, client="_classic_beta_")
        found = wowpath.detect(drives=[drive], registry=lambda: [])
        self.assertEqual(list(found), [])
        self.assertEqual([os.path.basename(c) for c in found.others], ["_classic_beta_"])

    def test_forever_beats_beta(self):
        drive = os.path.join(self.base, "D")
        fixtures.make_fake_wow(drive, client="_classic_beta_")
        forever_addons = os.path.join(drive, "World of Warcraft", "_forever_", "Interface", "AddOns")
        os.makedirs(os.path.join(drive, "World of Warcraft", "_forever_", "WTF"))
        found = wowpath.detect(drives=[drive], registry=lambda: [])
        self.assertEqual(list(found), [forever_addons])
        root = os.path.join(drive, "World of Warcraft")
        self.assertEqual(wowpath.from_picked(root), ("ok", forever_addons))

    def test_registry_beta_path_refused_sibling_found(self):
        root, beta_addons = fixtures.make_fake_wow(self.base, client="_classic_beta")
        beta = os.path.dirname(os.path.dirname(beta_addons))
        found = wowpath.detect(drives=[], registry=lambda: [beta])
        self.assertEqual(list(found), [])
        self.assertEqual(found.others, [beta])
        os.makedirs(os.path.join(root, "_forever_", "WTF"))
        found = wowpath.detect(drives=[], registry=lambda: [beta])
        self.assertEqual(list(found), [os.path.join(root, "_forever_", "Interface", "AddOns")])

    def test_only_other_clients(self):
        drive = os.path.join(self.base, "D")
        root = os.path.join(drive, "World of Warcraft")
        for c in ("_retail_", "_ptr_", "_classic_era_"):
            os.makedirs(os.path.join(root, c, "WTF"))
        found = wowpath.detect(drives=[drive], registry=lambda: [])
        self.assertEqual(list(found), [])
        self.assertEqual(len(found.others), 3)

    def test_picked_other_client(self):
        root, addons = fixtures.make_fake_wow(self.base, client="_classic_beta_")
        client = os.path.dirname(os.path.dirname(addons))
        self.assertEqual(wowpath.from_picked(client), ("other", addons))


class WindowTests(unittest.TestCase):
    """The real window, withdrawn before anything is drawn."""

    @classmethod
    def setUpClass(cls):
        cls.base = fixtures.temp_dir()
        cls.site = fixtures.make_server_files(cls.base)

    @classmethod
    def tearDownClass(cls):
        fixtures.remove(cls.base)

    def setUp(self):
        self.work = fixtures.temp_dir()
        self.root_dir, self.addons = fixtures.make_fake_wow(self.work)
        config.save_settings({})
        self.root = tk.Tk()
        self.root.withdraw()

    def tearDown(self):
        try:
            self.root.destroy()
        except tk.TclError:
            pass
        self.root = None
        gc.collect()  # free Tk objects here, in the main thread, not in a worker thread
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

    def make_app(self, manifest=None, wow=None, game=False, detect=lambda: []):
        manifest = manifest or os.path.join(self.site, "manifest.json")
        return ui.App(self.root, manifest, True, wow or self.root_dir,
                      check_game=lambda: game, detect=detect)

    def beta_box(self, app):
        """The Show beta versions box in the Settings window (built, never shown)."""
        app.open_settings(show=False)
        return app.beta_check

    def status(self, app, folder):
        label, button, _ = app.row_widgets[folder]
        return label.cget("text"), button.cget("text") if button else None

    def test_rows_and_install_click(self):
        app = self.make_app()
        self.pump(app)
        self.assertEqual(self.status(app, "TaxiTime"), ("Update available", "Update"))
        self.assertEqual(self.status(app, "GuildHub"), ("Not installed", "Install"))
        app.row_widgets["GuildHub"][1].invoke()
        self.pump(app)
        self.assertEqual(toc.installed(self.addons, "GuildHub"), (True, "0.4.1"))
        self.assertEqual(self.status(app, "GuildHub"), ("Up to date", "Uninstall"))
        self.assertIn("v0.4.1 installed", app.message_var.get())

    def test_update_all(self):
        app = self.make_app()
        self.pump(app)
        app.all_button.invoke()
        self.pump(app)
        self.assertEqual(toc.installed(self.addons, "TaxiTime"), (True, "1.3.0"))
        self.assertEqual(toc.installed(self.addons, "GuildHub"), (True, "0.4.1"))
        self.assertIn("disabled", app.all_button.state())

    def test_game_running_banner(self):
        app = self.make_app(game=True)
        self.pump(app)
        self.assertIn("/reload", app.banner_var.get())

    def test_no_list_set_up(self):
        app = ui.App(self.root, None, False, self.root_dir, check_game=lambda: False,
                     detect=lambda: [])
        self.pump(app)
        self.assertEqual(app.message_var.get(), "No addon list is set up yet.")

    def test_rows_from_github_releases(self):
        with fixtures.LocalServer(os.path.join(self.work, "gh")) as server:
            gh = fixtures.make_github_site(self.work, lambda rel: server.url + rel)
            fixtures.write_release(os.path.join(gh, "api"), "gettic/GuildHub", "v0.5.0", [])
            app = ui.App(self.root, os.path.join(gh, "addons.json"), True, self.root_dir,
                         check_game=lambda: False, detect=lambda: [],
                         github_api=server.url + "api")
            self.pump(app)
            self.assertEqual(self.status(app, "TaxiTime"), ("Update available", "Update"))
            self.assertEqual(self.status(app, "GuildHub"), ("No download yet", None))
            self.assertIn("Guild Hub's newest release has no download yet", app.message_var.get())
            app.row_widgets["TaxiTime"][1].invoke()
            self.pump(app)
            self.assertEqual(toc.installed(self.addons, "TaxiTime"), (True, "1.3.0"))

    def test_last_known_list_shown_at_once(self):
        with fixtures.LocalServer(os.path.join(self.work, "gh")) as server:
            gh = fixtures.make_github_site(self.work, lambda rel: server.url + rel)
            api, location = server.url + "api", os.path.join(gh, "addons.json")
            # A first start fills known_addons and cache.json.
            app = ui.App(self.root, location, True, self.root_dir, check_game=lambda: False,
                         detect=lambda: [], github_api=api)
            self.pump(app)
            app.root.destroy()
            self.root = tk.Tk()
            self.root.withdraw()
            saved = {p: pathlib.Path(p).read_bytes() for p in (config.settings_path(), config.cache_path())}
            # Next start: the rows are there before any answer, buttons locked.
            app = ui.App(self.root, location, True, self.root_dir, check_game=lambda: False,
                         detect=lambda: [], github_api=api)
            self.assertTrue(app.busy)
            self.assertEqual([r.folder for r in app.rows], ["TaxiTime", "GuildHub"])
            self.assertEqual(self.latest_text(app, "TaxiTime"), "v1.3.0")
            self.assertEqual(app.message_var.get(), "Checking for updates...")
            label, button, _ = app.row_widgets["TaxiTime"]
            self.assertEqual(label.cget("text"), "Update available")
            self.assertIn("disabled", button.state())
            self.assertIn("disabled", app.all_button.state())
            self.assertIn("disabled", app.check_button.state())
            button.invoke()   # locked: nothing happens
            self.assertEqual({p: pathlib.Path(p).read_bytes() for p in saved}, saved)
            self.pump(app)
            self.assertFalse(app.busy)
            self.assertEqual(self.status(app, "TaxiTime"), ("Update available", "Update"))
            self.assertEqual(toc.installed(self.addons, "TaxiTime"), (True, "1.2.0"))

    def test_last_known_list_without_cache(self):
        config.save_settings({"known_addons": [["TaxiTime", "Taxi Time"]]})
        if os.path.exists(config.cache_path()):
            os.remove(config.cache_path())
        app = self.make_app()
        self.assertEqual([r.folder for r in app.rows], ["TaxiTime"])
        self.assertEqual(self.latest_text(app, "TaxiTime"), "?")
        # Installed, newest unknown yet: Uninstall, locked until the check is done.
        self.assertEqual(self.status(app, "TaxiTime")[1], "Uninstall")
        self.assertIn("disabled", app.row_widgets["TaxiTime"][1].state())
        self.assertFalse(os.path.exists(config.cache_path()))
        self.pump(app)
        self.assertEqual([r.folder for r in app.rows], ["TaxiTime", "GuildHub"])

    def column_xs(self, app):
        """x of the Installed, Latest and Status columns (header labels)."""
        self.root.update_idletasks()
        return [app.list_frame.grid_slaves(row=0, column=c)[0].winfo_x() for c in (1, 2, 3)]

    def latest_text(self, app, folder):
        i = [r.folder for r in app.rows].index(folder) + 1
        return app.list_frame.grid_slaves(row=i, column=2)[0].cget("text")

    def test_show_beta_versions_switch(self):
        with fixtures.LocalServer(os.path.join(self.work, "gh")) as server:
            gh = fixtures.make_github_site(self.work, lambda rel: server.url + rel)
            z = fixtures.make_zip(os.path.join(gh, "dl", "TaxiTime-v1.4.0-beta.1.zip"),
                                  "TaxiTime", "1.4.0-beta.1")
            stable_zip = os.path.join(gh, "dl", "TaxiTime-v1.3.0.zip")
            fixtures.write_releases(os.path.join(gh, "api"), "gettic/TaxiTime", [
                fixtures.release_json("v1.4.0-beta.1", [fixtures.asset(
                    "TaxiTime-v1.4.0-beta.1.zip", server.url + "dl/TaxiTime-v1.4.0-beta.1.zip",
                    z)], prerelease=True),
                fixtures.release_json("v1.3.0", [fixtures.asset(
                    "TaxiTime-v1.3.0.zip", server.url + "dl/TaxiTime-v1.3.0.zip", stable_zip)])])
            asked, answer = [], [False]
            app = ui.App(self.root, os.path.join(gh, "addons.json"), True, self.root_dir,
                         check_game=lambda: False, detect=lambda: [],
                         github_api=server.url + "api",
                         confirm=lambda *a: (asked.append(a), answer[0])[1])
            self.pump(app)
            # Off by default: the normal release.
            self.assertFalse(app.beta_var.get())
            self.assertEqual(self.latest_text(app, "TaxiTime"), "v1.3.0")
            xs_before = self.column_xs(app)
            # On: the newer beta, marked, saved, and installable.
            self.beta_box(app).invoke()
            self.pump(app)
            self.assertIs(config.load_settings()["show_betas"], True)
            self.assertEqual(self.latest_text(app, "TaxiTime"), "v1.4.0-beta.1 (beta)")
            # No wobble: the Status column (and the buttons) stay where they were.
            self.assertEqual(self.column_xs(app), xs_before)
            app.row_widgets["TaxiTime"][1].invoke()
            self.pump(app)
            self.assertEqual(toc.installed(self.addons, "TaxiTime"), (True, "1.4.0-beta.1"))
            self.assertEqual(self.status(app, "TaxiTime"), ("Up to date", "Uninstall"))
            # Off again: the beta stays (no "Update" to an older version), but the row
            # offers going back to the normal version.
            self.beta_box(app).invoke()
            self.pump(app)
            self.assertIs(config.load_settings()["show_betas"], False)
            self.assertEqual(self.latest_text(app, "TaxiTime"), "v1.3.0")
            self.assertEqual(self.status(app, "TaxiTime"), ("Beta installed", "Use v1.3.0"))
            self.assertEqual(self.column_xs(app), xs_before)
            self.assertNotIn(app.row_widgets["TaxiTime"][2], ui.ALL_STATUSES)  # "all" never goes back
            self.assertEqual(toc.installed(self.addons, "TaxiTime"), (True, "1.4.0-beta.1"))
            # Cancel: asked once, nothing changes.
            app.row_widgets["TaxiTime"][1].invoke()
            self.pump(app)
            self.assertEqual(len(asked), 1)
            title, text, ok, cancel = asked[0]
            self.assertEqual((title, ok, cancel),
                             ("Go back to the normal version?", "Go back", "Cancel"))
            self.assertIn("v1.4.0-beta.1 (beta) will be replaced by the normal version v1.3.0",
                          text)
            self.assertIn("settings are kept", text)
            self.assertEqual(toc.installed(self.addons, "TaxiTime"), (True, "1.4.0-beta.1"))
            # Yes: the normal version is installed; WTF (saved settings) untouched.
            wtf = os.path.join(os.path.dirname(os.path.dirname(self.addons)), "WTF")
            sv = os.path.join(wtf, "Account", "X", "SavedVariables")
            os.makedirs(sv, exist_ok=True)
            with open(os.path.join(sv, "TaxiTime.lua"), "w", encoding="utf-8") as fh:
                fh.write("TaxiTimeDB = {beta = true}\n")
            before = fixtures.tree_snapshot(wtf)
            answer[0] = True
            app.row_widgets["TaxiTime"][1].invoke()
            self.pump(app)
            self.assertEqual(toc.installed(self.addons, "TaxiTime"), (True, "1.3.0"))
            self.assertEqual(self.status(app, "TaxiTime"), ("Up to date", "Uninstall"))
            self.assertEqual(before, fixtures.tree_snapshot(wtf))

    def test_flagged_beta_with_plain_tag_can_go_back(self):
        with fixtures.LocalServer(os.path.join(self.work, "gh")) as server:
            gh = fixtures.make_github_site(self.work, lambda rel: server.url + rel)
            z = fixtures.make_zip(os.path.join(gh, "dl", "TaxiTime-v1.4.0.zip"), "TaxiTime", "1.4.0")
            stable_zip = os.path.join(gh, "dl", "TaxiTime-v1.3.0.zip")
            fixtures.write_releases(os.path.join(gh, "api"), "gettic/TaxiTime", [
                fixtures.release_json("v1.4.0", [fixtures.asset(
                    "TaxiTime-v1.4.0.zip", server.url + "dl/TaxiTime-v1.4.0.zip", z)],
                    prerelease=True),
                fixtures.release_json("v1.3.0", [fixtures.asset(
                    "TaxiTime-v1.3.0.zip", server.url + "dl/TaxiTime-v1.3.0.zip", stable_zip)])])
            fixtures.make_addon_folder(self.addons, "TaxiTime", "1.4.0")
            app = ui.App(self.root, os.path.join(gh, "addons.json"), True, self.root_dir,
                         check_game=lambda: False, detect=lambda: [],
                         github_api=server.url + "api", confirm=lambda *a: True)
            self.pump(app)
            self.assertEqual(self.status(app, "TaxiTime"), ("Beta installed", "Use v1.3.0"))
            app.row_widgets["TaxiTime"][1].invoke()
            self.pump(app)
            self.assertEqual(toc.installed(self.addons, "TaxiTime"), (True, "1.3.0"))
            # Box on: the same release is offered as "(beta)".
            self.beta_box(app).invoke()
            self.pump(app)
            self.assertEqual(self.latest_text(app, "TaxiTime"), "v1.4.0 (beta)")
            self.assertEqual(self.status(app, "TaxiTime"), ("Update available", "Update"))

    def test_hand_installed_beta_without_normal_version(self):
        with fixtures.LocalServer(os.path.join(self.work, "gh")) as server:
            gh = fixtures.make_github_site(self.work, lambda rel: server.url + rel)
            z = fixtures.make_zip(os.path.join(gh, "dl", "TaxiTime-v2.0.0-beta.1.zip"),
                                  "TaxiTime", "2.0.0-beta.1")
            fixtures.write_releases(os.path.join(gh, "api"), "gettic/TaxiTime", [
                fixtures.release_json("v2.0.0-beta.1", [fixtures.asset(
                    "TaxiTime-v2.0.0-beta.1.zip", server.url + "dl/TaxiTime-v2.0.0-beta.1.zip",
                    z)], prerelease=True)])
            fixtures.make_addon_folder(self.addons, "TaxiTime", "2.0.0-beta.1")
            app = ui.App(self.root, os.path.join(gh, "addons.json"), True, self.root_dir,
                         check_game=lambda: False, detect=lambda: [],
                         github_api=server.url + "api",
                         confirm=lambda *a: self.fail("asked"))
            self.pump(app)
            self.assertEqual(self.status(app, "TaxiTime"), ("Beta installed", "Uninstall"))
            # A hand-installed normal version newer than the list keeps its old status.
            fixtures.make_addon_folder(self.addons, "GuildHub", "9.0")
            app.render()
            self.assertEqual(self.status(app, "GuildHub"), ("Newer than the list", "Uninstall"))

    def test_beta_switch_no_flicker(self):
        with fixtures.LocalServer(os.path.join(self.work, "gh")) as server:
            gh = fixtures.make_github_site(self.work, lambda rel: server.url + rel)
            z = fixtures.make_zip(os.path.join(gh, "dl", "TaxiTime-v1.4.0-beta.1.zip"),
                                  "TaxiTime", "1.4.0-beta.1")
            stable_zip = os.path.join(gh, "dl", "TaxiTime-v1.3.0.zip")
            fixtures.write_releases(os.path.join(gh, "api"), "gettic/TaxiTime", [
                fixtures.release_json("v1.4.0-beta.1", [fixtures.asset(
                    "TaxiTime-v1.4.0-beta.1.zip", server.url + "dl/TaxiTime-v1.4.0-beta.1.zip",
                    z)], prerelease=True),
                fixtures.release_json("v1.3.0", [fixtures.asset(
                    "TaxiTime-v1.3.0.zip", server.url + "dl/TaxiTime-v1.3.0.zip", stable_zip)])])
            game_checks = []
            app = ui.App(self.root, os.path.join(gh, "addons.json"), True, self.root_dir,
                         check_game=lambda: game_checks.append(1) or False,
                         detect=lambda: [], github_api=server.url + "api",
                         confirm=lambda *a: False)
            self.pump(app)
            widgets = [str(w) for w in app.list_frame.winfo_children()]
            buttons = [app.row_widgets[f][1] for f in ("TaxiTime", "GuildHub")]
            buttons += [app.all_button, app.check_button, app.folder_button, self.beta_box(app),
                        app.beta_main_check]
            checks_before, renders = len(game_checks), []
            real_render = app.render
            app.render = lambda: (renders.append(1), real_render())
            self.beta_box(app).invoke()
            greyed = set()
            end = time.time() + 15
            while time.time() < end:
                self.root.update()
                greyed |= {str(b) for b in buttons if "disabled" in b.state()}
                if not app.busy:
                    break
                time.sleep(0.005)
            self.pump(app)
            self.assertEqual(greyed, set())                       # nothing greyed out
            self.assertEqual(len(renders), 1)                     # one redraw
            self.assertEqual(len(game_checks), checks_before)     # no full check
            self.assertNotEqual(app.message_var.get(), "Checking for updates...")
            self.assertEqual(self.latest_text(app, "TaxiTime"), "v1.4.0-beta.1 (beta)")
            # The same row widgets, only updated.
            self.assertEqual([str(w) for w in app.list_frame.winfo_children()], widgets)
            # "Check again" also keeps the widgets when the addons stay the same.
            app.check_button.invoke()
            self.pump(app)
            self.assertEqual([str(w) for w in app.list_frame.winfo_children()], widgets)

    def row_geometry(self, app):
        """(row y, row height, name label y) of every addon row."""
        self.root.update_idletasks()
        f, out = app.list_frame, []
        for i in range(1, len(app.rows) + 1):
            _x, y, _w, h = f.grid_bbox(0, i)
            out.append((y, h, f.grid_slaves(row=i, column=0)[0].winfo_y()))
        return out

    def test_rows_keep_height_with_or_without_button(self):
        with fixtures.LocalServer(os.path.join(self.work, "gh")) as server:
            gh = fixtures.make_github_site(self.work, lambda rel: server.url + rel)
            z = fixtures.make_zip(os.path.join(gh, "dl", "TaxiTime-v1.4.0-beta.1.zip"),
                                  "TaxiTime", "1.4.0-beta.1")
            stable_zip = os.path.join(gh, "dl", "TaxiTime-v1.3.0.zip")
            fixtures.write_releases(os.path.join(gh, "api"), "gettic/TaxiTime", [
                fixtures.release_json("v1.4.0-beta.1", [fixtures.asset(
                    "TaxiTime-v1.4.0-beta.1.zip", server.url + "dl/TaxiTime-v1.4.0-beta.1.zip",
                    z)], prerelease=True),
                fixtures.release_json("v1.3.0", [fixtures.asset(
                    "TaxiTime-v1.3.0.zip", server.url + "dl/TaxiTime-v1.3.0.zip", stable_zip)])])
            fixtures.make_addon_folder(self.addons, "TaxiTime", "1.4.0-beta.1")
            app = ui.App(self.root, os.path.join(gh, "addons.json"), True, self.root_dir,
                         check_game=lambda: False, detect=lambda: [],
                         github_api=server.url + "api", confirm=lambda *a: False)
            self.pump(app)
            # Box off: "Use v1.3.0" on Taxi Time, "Install" on Guild Hub.
            self.assertEqual(self.status(app, "TaxiTime")[1], "Use v1.3.0")
            geometry = self.row_geometry(app)
            # Box on: Taxi Time is up to date, its button says Uninstall.
            self.beta_box(app).invoke()
            self.pump(app)
            self.assertEqual(self.status(app, "TaxiTime"), ("Up to date", "Uninstall"))
            self.assertEqual(self.row_geometry(app), geometry)
            # Install Guild Hub: its button turns into Uninstall.
            app.row_widgets["GuildHub"][1].invoke()
            self.pump(app)
            self.assertEqual(self.status(app, "GuildHub"), ("Up to date", "Uninstall"))
            self.assertEqual(self.row_geometry(app), geometry)
            # Box off again: the go-back button comes back, the rows stay put.
            self.beta_box(app).invoke()
            self.pump(app)
            self.assertEqual(self.status(app, "TaxiTime")[1], "Use v1.3.0")
            self.assertEqual(self.row_geometry(app), geometry)

    def test_beta_box_in_main_window_and_settings_in_sync(self):
        app = self.make_app()
        self.pump(app)
        row = app.settings_button.master
        self.assertIs(app.beta_main_check.master, row)
        # "Show beta versions" on Windows, the shorter "Show betas" on Linux (wider fonts)
        self.assertEqual(app.beta_main_check.cget("text"),
                         "Show beta versions" if os.name == "nt" else "Show betas")
        # [box] [Backup] [Settings], checked by packing order (works for the withdrawn
        # window on X too, where a never-mapped window has no positions): all packed on the
        # right, Settings first, so each later one sits to the left of the one before.
        slaves = row.pack_slaves()
        for w in (app.beta_main_check, app.backup_main_button, app.settings_button):
            self.assertEqual(w.pack_info()["side"], "right")
        self.assertGreater(slaves.index(app.backup_main_button), slaves.index(app.settings_button))
        self.assertGreater(slaves.index(app.beta_main_check), slaves.index(app.backup_main_button))
        settings_box = self.beta_box(app)
        self.assertEqual(settings_box.cget("text"), "Show beta versions")   # full text there
        self.assertEqual(str(settings_box.cget("variable")), str(app.beta_main_check.cget("variable")))
        # main box on -> saved, and the Settings box shows it
        app.beta_main_check.invoke()
        self.pump(app)
        self.assertIs(config.load_settings()["show_betas"], True)
        self.assertIn("selected", settings_box.state())
        # Settings box off -> saved, and the main box shows it
        settings_box.invoke()
        self.pump(app)
        self.assertIs(config.load_settings()["show_betas"], False)
        self.assertNotIn("selected", app.beta_main_check.state())
        # greyed out while busy, like the other buttons
        app.busy = True
        app.render()
        self.assertIn("disabled", app.beta_main_check.state())
        app.busy = False
        app.render()
        self.assertNotIn("disabled", app.beta_main_check.state())

    def test_show_beta_versions_remembered(self):
        config.save_settings({"show_betas": True})
        app = self.make_app()
        self.pump(app)
        self.assertTrue(app.beta_var.get())

    def test_no_wow_found(self):
        app = ui.App(self.root, os.path.join(self.site, "manifest.json"), True, None,
                     check_game=lambda: False, detect=lambda: [])
        self.pump(app)
        self.assertIn("couldn't find WoW Forever", app.message_var.get())
        self.assertEqual(app.folder_button.cget("text"), "Choose folder")

    def test_choose_folder_saved(self):
        app = ui.App(self.root, os.path.join(self.site, "manifest.json"), True, None,
                     check_game=lambda: False, detect=lambda: [])
        self.pump(app)
        app.ask_folder = lambda: self.root_dir
        app.choose_folder()
        self.assertEqual(app.addons_dir, self.addons)
        self.assertEqual(config.load_settings()["wow_folder"], self.addons)
        self.assertEqual(config.load_settings()["wow_folder_source"], "picked")

    def make_beta(self):
        beta = os.path.join(self.root_dir, "_classic_beta_")
        os.makedirs(os.path.join(beta, "Interface", "AddOns"))
        return beta, os.path.join(beta, "Interface", "AddOns")

    def test_saved_beta_switches_to_forever(self):
        beta, beta_addons = self.make_beta()
        config.save_settings({"wow_folder": beta_addons})   # old settings: no source
        app = ui.App(self.root, os.path.join(self.site, "manifest.json"), True, None,
                     check_game=lambda: False, detect=lambda: [self.addons])
        self.pump(app)
        self.assertEqual(app.addons_dir, self.addons)
        self.assertEqual(config.load_settings()["wow_folder"], self.addons)
        self.assertEqual(config.load_settings()["wow_folder_source"], "auto")
        self.assertIn("Switched to your WoW Forever folder", app.message_var.get())

    def test_saved_beta_no_forever_found(self):
        beta, beta_addons = self.make_beta()
        config.save_settings({"wow_folder": beta_addons, "wow_folder_source": "auto"})
        app = ui.App(self.root, os.path.join(self.site, "manifest.json"), True, None,
                     check_game=lambda: False, detect=lambda: wowpath.Found([], [beta]))
        self.pump(app)
        self.assertIsNone(app.addons_dir)
        self.assertIn("no WoW Forever", app.message_var.get())
        self.assertIn(beta, app.message_var.get())
        self.assertEqual(app.folder_var.get(), ui.NOT_CHOSEN)

    def test_picked_beta_kept_with_warning(self):
        beta, beta_addons = self.make_beta()
        config.save_settings({"wow_folder": beta_addons, "wow_folder_source": "picked"})
        app = ui.App(self.root, os.path.join(self.site, "manifest.json"), True, None,
                     check_game=lambda: False, detect=lambda: [self.addons])
        self.pump(app)
        self.assertEqual(app.addons_dir, beta_addons)
        self.assertIn("different WoW version", app.message_var.get())

    def test_choose_beta_warns(self):
        beta, beta_addons = self.make_beta()
        app = ui.App(self.root, os.path.join(self.site, "manifest.json"), True, None,
                     check_game=lambda: False, detect=lambda: [])
        self.pump(app)
        app.ask_folder = lambda: beta
        app.choose_folder()
        self.assertEqual(app.addons_dir, beta_addons)
        self.assertIn("different WoW version", app.message_var.get())
        self.assertEqual(config.load_settings()["wow_folder_source"], "picked")

    def test_folder_field_copy(self):
        app = self.make_app()
        self.pump(app)
        copied = []
        app.clipboard = copied.append   # never the real clipboard
        client = os.path.dirname(os.path.dirname(self.addons))
        self.assertIn("readonly", app.folder_entry.state())
        self.assertEqual(app.folder_entry.get(), client)
        self.assertEqual(app.copy_folder(), client)
        app.folder_entry.selection_range(0, 3)
        self.assertEqual(app.copy_folder(), client[:3])
        self.assertEqual(copied, [client, client[:3]])
        self.assertEqual(app.folder_menu.entrycget(0, "label"), "Copy")
        app.folder_entry.selection_clear()
        app.folder_menu.invoke(0)
        self.assertEqual(copied[-1], client)

    def test_layout_no_title_version_at_bottom(self):
        app = self.make_app()
        self.pump(app)

        def labels(w):
            for c in w.winfo_children():
                if c.winfo_class() == "TLabel":
                    yield c
                yield from labels(c)
        texts = [str(lbl.cget("text")) for lbl in labels(self.root)]
        self.assertNotIn(ui.APP_NAME, texts)          # big title gone (window title keeps it)
        self.assertEqual(self.root.title(), ui.APP_NAME)
        # The app's own version is shown once outside the addon list (an addon in the
        # list may have the same version text, e.g. a fixture at v0.4.1).
        outside = [str(lbl.cget("text")) for lbl in labels(self.root)
                   if not str(lbl).startswith(str(app.list_frame) + ".")]
        self.assertEqual(outside.count(app.version_label.cget("text")), 1)
        self.assertNotEqual(app.version_label.master, app.list_frame)
        self.assertEqual(str(app.version_label.cget("text")), ui.versions.display(ui.APP_VERSION))
        self.assertEqual(str(app.version_label.cget("style")), "Muted.TLabel")
        # it sits on the message line, below the progress bar
        self.root.update_idletasks()
        self.assert_below(app.version_label, app.progress)

    def assert_below(self, low, high):
        """low is laid out under high. Checked by layout order (works for the withdrawn
        test window on every system; on X a never-mapped window has no screen
        positions), plus the positions inside the shared parent when Tk has them."""
        def chain(w):
            out = [w]
            while w.master is not None:
                w = w.master
                out.append(w)
            return out
        low_chain, high_chain = chain(low), chain(high)
        common = next(w for w in low_chain if w in high_chain)
        branch = lambda c: c[c.index(common) - 1]
        a, b = branch(low_chain), branch(high_chain)
        self.assertIsNot(a, b)
        self.assertEqual((a.winfo_manager(), b.winfo_manager()), ("pack", "pack"))
        self.assertEqual((str(a.pack_info()["side"]), str(b.pack_info()["side"])), ("top", "top"))
        slaves = common.pack_slaves()
        self.assertGreater(slaves.index(a), slaves.index(b))
        if a.winfo_y() or b.winfo_y():
            self.assertGreater(a.winfo_y(), b.winfo_y())

    def test_folder_field_nothing_to_copy(self):
        app = ui.App(self.root, os.path.join(self.site, "manifest.json"), True, None,
                     check_game=lambda: False, detect=lambda: [])
        self.pump(app)
        copied = []
        app.clipboard = copied.append
        self.assertEqual(app.copy_folder(), "")
        self.assertEqual(copied, [])


if __name__ == "__main__":
    unittest.main()
