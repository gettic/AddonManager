"""The app icon: files present and bundled, loaded into hidden windows without error."""

import gc
import os
import re
import struct
import time
import tkinter as tk
import unittest
from unittest import mock

import tests  # noqa: F401  (test home folder)
from am import icon
from tests import fixtures

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def png_size(path):
    with open(path, "rb") as f:
        head = f.read(24)
    assert head[:8] == b"\x89PNG\r\n\x1a\n", path
    return struct.unpack(">II", head[16:24])


class Files(unittest.TestCase):
    def test_png_sizes(self):
        self.assertEqual([png_size(p) for p in icon.PNGS],
                         [(256, 256), (48, 48), (32, 32), (16, 16)])

    def test_ico_has_all_sizes(self):
        with open(icon.ICO, "rb") as f:
            data = f.read()
        reserved, kind, count = struct.unpack("<HHH", data[:6])
        self.assertEqual((reserved, kind), (0, 1))
        sizes = {data[6 + 16 * i] or 256 for i in range(count)}
        self.assertTrue({16, 32, 48, 256} <= sizes, sizes)

    def test_spec_bundles_every_icon_file(self):
        with open(os.path.join(HERE, "AddonManager.spec"), encoding="utf-8") as f:
            spec = f.read()
        self.assertIn("datas=assets", spec)
        self.assertIn('"am", "assets", "AddonManager.ico"', spec)
        for path in [icon.ICO] + icon.PNGS:
            self.assertIn(f'"{os.path.basename(path)}"', spec)

    def test_app_id_has_no_game_names(self):
        self.assertIsNone(re.search(r"(?i)wow|warcraft|blizzard", icon.APP_ID))


class Loading(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()

    def tearDown(self):
        try:
            self.root.destroy()
        except tk.TclError:
            pass
        self.root = None
        gc.collect()

    def test_apply_loads_all_sizes_and_keeps_them(self):
        images = icon.apply(self.root)
        self.assertEqual(len(images), len(icon.PNGS))
        self.assertIs(self.root._am_icons, images)
        self.assertEqual((images[0].width(), images[-1].width()), (256, 16))
        top = tk.Toplevel(self.root)   # later windows get it too (built, never shown)
        top.withdraw()
        top.destroy()

    def test_missing_or_damaged_files_never_stop_the_window(self):
        work = fixtures.temp_dir("icon_bad_")
        try:
            bad = os.path.join(work, "bad.png")
            with open(bad, "wb") as f:
                f.write(b"not a png")
            with mock.patch.object(icon, "PNGS", [bad]), \
                    mock.patch.object(icon, "ICO", os.path.join(work, "bad.ico")):
                self.assertEqual(icon.apply(self.root), [])
            with mock.patch.object(icon, "PNGS", [os.path.join(work, "gone.png")]):
                self.assertEqual(icon.apply(self.root), [])
        finally:
            fixtures.remove(work)

    def test_main_window_gets_the_icon(self):
        from am import ui
        work = fixtures.temp_dir("icon_app_")
        try:
            root_dir, _addons = fixtures.make_fake_wow(work)
            app = ui.App(self.root, None, False, root_dir, check_game=lambda: False,
                         detect=lambda: [])
            self.assertEqual(len(self.root._am_icons), len(icon.PNGS))
            end = time.time() + 15
            while app.busy and time.time() < end:
                self.root.update()
                time.sleep(0.02)
            app.close()
        finally:
            fixtures.remove(work)

    def test_app_id(self):
        self.assertEqual(icon.set_app_id(), os.name == "nt")


if __name__ == "__main__":
    unittest.main()
