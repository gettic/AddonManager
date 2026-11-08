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
        self.assertEqual(texts.count(app.version_label.cget("text")), 1)
        self.assertEqual(str(app.version_label.cget("text")), ui.versions.display(ui.APP_VERSION))
        self.assertEqual(str(app.version_label.cget("style")), "Muted.TLabel")
        # it sits on the message line, below the progress bar
        self.root.update_idletasks()
        self.assertGreater(app.version_label.winfo_rooty(), app.progress.winfo_rooty())

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
