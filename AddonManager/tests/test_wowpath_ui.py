"""Folder detection (fake drives, fake registry) and the hidden window."""

import gc
import os
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
        root, addons = fixtures.make_fake_wow(self.base, client="_classic_era_")
        os.makedirs(os.path.join(root, "_retail_", "WTF"))
        self.assertEqual(wowpath.from_picked(root), ("ok", addons))


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
        self.assertEqual(self.status(app, "GuildHub"), ("Up to date", None))
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


if __name__ == "__main__":
    unittest.main()
