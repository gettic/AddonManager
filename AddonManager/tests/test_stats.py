"""The daily anonymous usage count (am\\stats.py): payload, once a day, first-of-week
flag, silent failures, and the hidden window sending it after the update check.
Everything goes to a local fake server; nothing ever goes online."""

import datetime
import gc
import http.server
import json
import os
import threading
import time
import tkinter as tk
import unittest

from am import APP_VERSION, config, stats, toc, ui
from tests import fixtures


class _PingHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        srv = self.server
        srv.requests.append({"path": self.path, "body": body,
                             "type": self.headers.get("Content-Type"),
                             "agent": self.headers.get("User-Agent")})
        if srv.status in (301, 302, 307):
            self.send_response(srv.status)
            self.send_header("Location", "http://127.0.0.1:9/elsewhere")
        else:
            self.send_response(srv.status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


class PingServer:
    """http://127.0.0.1:<port>/ping recording every POST; replies with .status."""

    def __init__(self, status=204):
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _PingHandler)
        self.httpd.requests, self.httpd.status = [], status
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/ping"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def requests(self):
        return self.httpd.requests

    def payloads(self):
        return [json.loads(r["body"]) for r in self.requests]

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


MON = datetime.date(2026, 11, 9)    # ISO week 2026-W46
SUN = datetime.date(2026, 11, 15)   # same week
NEXT_MON = datetime.date(2026, 11, 16)


class Basics(unittest.TestCase):
    def test_week_and_day(self):
        self.assertEqual(stats.week(MON), "2026-W46")
        self.assertEqual(stats.week(SUN), "2026-W46")
        self.assertEqual(stats.week(NEXT_MON), "2026-W47")
        self.assertEqual(stats.week(datetime.date(2027, 1, 1)), "2026-W53")
        self.assertEqual(stats.week(datetime.date(2025, 12, 29)), "2026-W01")
        self.assertEqual(stats.day(MON), "2026-11-09")

    def test_due_and_first_this_week(self):
        self.assertTrue(stats.due({}, MON))
        self.assertTrue(stats.first_this_week({}, MON))
        sent = {stats.DAY_SETTING: "2026-11-09", stats.WEEK_SETTING: "2026-W46"}
        self.assertFalse(stats.due(sent, MON))
        self.assertTrue(stats.due(sent, SUN))
        self.assertFalse(stats.first_this_week(sent, SUN))
        self.assertTrue(stats.first_this_week(sent, NEXT_MON))

    def test_state(self):
        cases = [("1.3.0", "1.3.0", "", "latest"), ("1.4.0", "1.3.0", "", "latest"),
                 ("1.2.0", "1.3.0", "", "outdated"), ("v1.3.0", "1.3.0", "", "latest"),
                 ("1.4.0-beta.1", "1.3.0", "", "beta"),
                 ("1.3.0-beta.2", "1.3.0", "", "outdated"),   # the normal one came out after
                 ("1.4.0", "1.3.0", "1.4.0", "beta"),         # flagged beta, plain tag
                 ("1.4.0-beta.1", "", "1.4.0-beta.1", "beta"),  # only betas so far
                 ("1.2.0", "", "", None), ("", "1.3.0", "", None), (None, "1.3.0", "", None),
                 ("abc", "1.3.0", "", None)]
        for installed, stable, beta_v, want in cases:
            self.assertEqual(stats.state(installed, stable, beta_v), want,
                             (installed, stable, beta_v))

    def test_entry_refuses_odd_values(self):
        self.assertEqual(stats.entry("TaxiTime", "1.2.0", "1.3.0"),
                         {"a": "TaxiTime", "ver": "1.2.0", "s": "outdated"})
        self.assertIsNone(stats.entry("Taxi Time", "1.2.0", "1.3.0"))      # bad name
        self.assertIsNone(stats.entry("../x", "1.2.0", "1.3.0"))
        self.assertIsNone(stats.entry("TaxiTime", "1.2.0.1.5", "1.3.0"))   # too many parts
        self.assertIsNone(stats.entry("TaxiTime", "1.2.0+build7", "1.3.0"))

    def test_payload(self):
        p = stats.build_payload([("TaxiTime", "1.2.0", "1.3.0", ""),
                                 ("GuildHub", "0.2.0-beta.6", "0.1.2", "0.2.0-beta.6"),
                                 ("TaxiTime", "1.3.0", "1.3.0", ""),     # duplicate: dropped
                                 ("AddonManager", "9.0.0", "9.0.0", ""),  # only the app's own
                                 ("NotePad", "", "1.0.0", "")],          # unknown version
                                "0.3.0", app_newer=True, first_week=True)
        self.assertEqual(p, {"v": 1, "first_this_week": True, "addons": [
            {"a": "TaxiTime", "ver": "1.2.0", "s": "outdated"},
            {"a": "GuildHub", "ver": "0.2.0-beta.6", "s": "beta"},
            {"a": "AddonManager", "ver": "0.3.0", "s": "outdated"}]})
        p = stats.build_payload([], "v0.3.0", app_newer=False, first_week=False)
        self.assertEqual(p, {"v": 1, "first_this_week": False, "addons": [
            {"a": "AddonManager", "ver": "0.3.0", "s": "latest"}]})

    def test_payload_has_nothing_personal(self):
        p = stats.build_payload([("TaxiTime", "1.2.0", "1.3.0", "")], "0.3.0", False, True)
        self.assertEqual(set(p), {"v", "first_this_week", "addons"})
        for e in p["addons"]:
            self.assertEqual(set(e), {"a", "ver", "s"})
        text = json.dumps(p)
        words = [os.environ.get(k) or "" for k in ("USERNAME", "USER", "COMPUTERNAME")]
        for word in [w for w in words if len(w) >= 4] + ["\\", "/", ":\\"]:
            self.assertNotIn(word, text)

    def test_payload_max_entries_keeps_app(self):
        many = [(f"Addon{i}", "1.0.0", "1.0.0", "") for i in range(30)]
        p = stats.build_payload(many, "0.3.0", False, True)
        self.assertEqual(len(p["addons"]), stats.MAX_ENTRIES)
        self.assertEqual(p["addons"][-1]["a"], stats.APP_ENTRY)

    def test_url_ok(self):
        self.assertTrue(stats.url_ok("https://x.example.workers.dev/ping"))
        for bad in ("", None, "http://x.example/ping", "ftp://x/ping",
                    "https://user:pw@x.example/ping", "file:///C:/x"):
            self.assertFalse(stats.url_ok(bad), bad)
        self.assertFalse(stats.url_ok("http://127.0.0.1:5/ping"))
        self.assertTrue(stats.url_ok("http://127.0.0.1:5/ping", allow_local=True))
        self.assertFalse(stats.url_ok("http://10.0.0.1/ping", allow_local=True))

    def test_tests_never_ping_online(self):
        import tests
        self.assertEqual(config.STATS_URL, "")
        # The real address (built app only): https, accepted by url_ok, ends in /ping.
        self.assertEqual(tests.REAL_STATS_URL, "https://guild-stats.gtd-relay.workers.dev/ping")
        self.assertTrue(stats.url_ok(tests.REAL_STATS_URL))


class SendTests(unittest.TestCase):
    def setUp(self):
        self.server = PingServer()

    def tearDown(self):
        self.server.close()

    def test_send_ok(self):
        p = stats.build_payload([], "0.3.0", False, True)
        self.assertTrue(stats.send(self.server.url, p, allow_local=True))
        r = self.server.requests[0]
        self.assertEqual(r["path"], "/ping")
        self.assertEqual(r["type"], "application/json")
        self.assertTrue(r["agent"].startswith("AddonManager/"))
        self.assertEqual(json.loads(r["body"]), p)

    def test_send_failures_are_silent(self):
        p = stats.build_payload([], "0.3.0", False, True)
        for status in (500, 400, 429, 302):
            self.server.httpd.status = status
            self.assertFalse(stats.send(self.server.url, p, allow_local=True), status)
        # a redirect is never followed (only the 302 itself reached the server)
        self.assertEqual([r["path"] for r in self.server.requests], ["/ping"] * 4)
        self.assertFalse(stats.send("http://127.0.0.1:9/ping", p, allow_local=True))
        self.assertFalse(stats.send(self.server.url, p))   # http outside dev mode: not sent
        self.assertEqual(len(self.server.requests), 4)

    def test_worker_reports_only_success(self):
        import queue
        q = queue.Queue()
        p = stats.build_payload([], "0.3.0", False, True)
        stats.worker(q, self.server.url, p, True, "2026-11-09", "2026-W46")
        self.assertEqual(q.get_nowait(), ("stats_sent", "2026-11-09", "2026-W46"))
        self.server.httpd.status = 500
        stats.worker(q, self.server.url, p, True, "2026-11-09", "2026-W46")
        self.assertTrue(q.empty())


class WindowTests(unittest.TestCase):
    """The real window, withdrawn before anything is drawn, with a local ping server."""

    @classmethod
    def setUpClass(cls):
        cls.base = fixtures.temp_dir(prefix="usage_counts_")
        cls.site = fixtures.make_server_files(cls.base)

    @classmethod
    def tearDownClass(cls):
        fixtures.remove(cls.base)

    def setUp(self):
        self.work = fixtures.temp_dir(prefix="usage_counts_")
        self.root_dir, self.addons = fixtures.make_fake_wow(self.work)
        config.save_settings({})
        self.server = PingServer()
        self.root = tk.Tk()
        self.root.withdraw()

    def tearDown(self):
        try:
            self.root.destroy()
        except tk.TclError:
            pass
        self.root = None
        gc.collect()
        self.server.close()
        fixtures.remove(self.work)

    def make_app(self, stats_url=True, **kw):
        if stats_url is True:
            stats_url = self.server.url
        return ui.App(self.root, os.path.join(self.site, "manifest.json"), True, self.root_dir,
                      check_game=lambda: False, detect=lambda: [], stats_url=stats_url, **kw)

    def pump(self, app, until=None, timeout=15):
        end = time.time() + timeout
        self.root.update()
        while time.time() < end and (app.busy or (until and not until())):
            self.root.update()
            time.sleep(0.02)
        for _ in range(10):
            self.root.update()
            time.sleep(0.02)

    def test_sent_once_a_day_with_week_flag(self):
        app = self.make_app()
        self.pump(app, until=lambda: config.load_settings().get(stats.DAY_SETTING))
        self.assertEqual(self.server.payloads(), [{"v": 1, "first_this_week": True, "addons": [
            {"a": "TaxiTime", "ver": "1.2.0", "s": "outdated"},
            {"a": "AddonManager", "ver": stats.versions.plain(APP_VERSION), "s": "latest"}]}])
        s = config.load_settings()
        self.assertEqual((s[stats.DAY_SETTING], s[stats.WEEK_SETTING]),
                         (stats.day(), stats.week()))
        # Same day: a second start sends nothing; "check again" sends nothing either.
        app.check_again()
        self.pump(app)
        self.root.destroy()
        self.root = tk.Tk()
        self.root.withdraw()
        app2 = self.make_app()
        self.pump(app2)
        self.assertEqual(len(self.server.requests), 1)

    def test_later_day_same_week_not_first(self):
        config.save_settings({stats.DAY_SETTING: "2000-01-01", stats.WEEK_SETTING: stats.week()})
        app = self.make_app()
        self.pump(app, until=lambda: self.server.requests)
        self.assertEqual(len(self.server.requests), 1)
        self.assertFalse(self.server.payloads()[0]["first_this_week"])

    def test_after_install_counts_new_version_next_time(self):
        app = self.make_app()
        self.pump(app, until=lambda: self.server.requests)
        app.all_button.invoke()
        self.pump(app)
        self.assertEqual(toc.installed(self.addons, "GuildHub"), (True, "0.4.1"))
        self.assertEqual(len(self.server.requests), 1)   # still only one ping this run

    def test_server_down_is_silent_and_retried(self):
        self.server.httpd.status = 500
        app = self.make_app()
        self.pump(app, until=lambda: self.server.requests)
        self.pump(app)
        self.assertEqual(len(self.server.requests), 1)
        self.assertNotIn(stats.DAY_SETTING, config.load_settings())
        # Nothing about the failed count: only the normal summary of the list.
        self.assertEqual(app.message_var.get(),
                         "1 update available. 1 addon isn't installed yet.")
        self.root.destroy()
        self.root = tk.Tk()
        self.root.withdraw()
        self.server.httpd.status = 204
        app2 = self.make_app()
        self.pump(app2, until=lambda: config.load_settings().get(stats.DAY_SETTING))
        self.assertEqual(len(self.server.requests), 2)
        self.assertTrue(self.server.payloads()[1]["first_this_week"])

    def test_no_address_no_ping(self):
        for url in ("", None):   # None: dev run (allow_local) -> never config.STATS_URL
            app = self.make_app(stats_url=url)
            self.pump(app)
            self.assertEqual(app.stats_url, "")
        self.assertEqual(self.server.requests, [])

    def test_no_folder_no_ping(self):
        app = ui.App(self.root, os.path.join(self.site, "manifest.json"), True, None,
                     check_game=lambda: False, detect=lambda: [], stats_url=self.server.url)
        self.pump(app)
        self.assertEqual(self.server.requests, [])

    # ---------- the Share anonymous counts switch (Settings window) ----------
    def test_settings_window_defaults(self):
        app = self.make_app()
        self.pump(app)
        self.assertEqual(app.settings_button.cget("text"), ui.SETTINGS_BUTTON)
        win = app.open_settings(show=False)
        self.assertEqual(win.state(), "withdrawn")          # never shown in tests
        self.assertIs(app.open_settings(show=False), win)   # only one window
        self.assertTrue(app.share_var.get())                # missing = on
        self.assertFalse(app.beta_var.get())
        self.assertIn("app's maker", ui.SHARE_NOTE)
        app.close_settings()
        self.assertIsNone(app.settings_win)
        self.assertIsNone(app.beta_check)
        app.render()   # works with the window closed

    def test_switch_off_sends_nothing_and_on_sends(self):
        config.save_settings({ui.SHARE_SETTING: False})
        app = self.make_app()
        self.pump(app)
        time.sleep(0.3)
        self.pump(app)
        self.assertEqual(self.server.requests, [])
        app.open_settings(show=False)
        self.assertFalse(app.share_var.get())
        message = app.message_var.get()
        app.share_check.invoke()          # on: today's count goes now
        self.assertFalse(app.busy)        # no list reload
        self.pump(app, until=lambda: self.server.requests)
        self.assertEqual(len(self.server.requests), 1)
        self.assertIs(config.load_settings()[ui.SHARE_SETTING], True)
        self.assertEqual(app.message_var.get(), message)

    def test_switched_off_before_check_finishes(self):
        app = self.make_app()
        app.open_settings(show=False)
        app.share_check.invoke()          # off, before the list has arrived
        self.assertIs(config.load_settings()[ui.SHARE_SETTING], False)
        self.pump(app)
        time.sleep(0.3)
        self.pump(app)
        self.assertEqual(self.server.requests, [])
        self.assertNotIn(stats.DAY_SETTING, config.load_settings())

    def test_bad_setting_counts_as_on(self):
        with open(config.settings_path(), "w", encoding="utf-8") as f:
            json.dump({ui.SHARE_SETTING: "no"}, f)
        app = self.make_app()
        self.pump(app, until=lambda: self.server.requests)
        self.assertEqual(len(self.server.requests), 1)

    def test_thread_checks_switch_again(self):
        import queue
        q = queue.Queue()
        p = stats.build_payload([], "0.3.0", False, True)
        config.save_settings({ui.SHARE_SETTING: False})
        ui._stats_worker(q, self.server.url, p, True, "2026-11-09", "2026-W46")
        self.assertEqual(self.server.requests, [])
        config.save_settings({})
        ui._stats_worker(q, self.server.url, p, True, "2026-11-09", "2026-W46")
        self.assertEqual(len(self.server.requests), 1)

    def test_bottom_row_fits_smallest_window(self):
        app = self.make_app()
        self.pump(app)
        self.root.update_idletasks()
        row = app.settings_button.master
        self.assertIs(app.beta_main_check.master, row)   # incl. the Show beta versions box
        self.assertIs(app.backup_main_button.master, row)   # and Backup
        # Add file...: on its own line above the list (every system), so the bottom row
        # keeps real room (at least 20 px) inside the smallest window.
        self.assertIsNot(app.files.button.master, row)
        self.assertLessEqual(row.winfo_reqwidth(), 560 - 20 - 20)

    def test_waits_for_app_update_check(self):
        exe = os.path.join(self.work, "AddonManager.exe")
        open(exe, "wb").close()
        app = self.make_app(app_exe=exe)   # self-update check: closed local port -> None
        self.assertFalse(app._app_checked)
        self.pump(app, until=lambda: self.server.requests)
        self.assertTrue(app._app_checked)
        self.assertEqual(self.server.payloads()[0]["addons"][-1]["s"], "latest")


if __name__ == "__main__":
    unittest.main()
