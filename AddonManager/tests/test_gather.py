"""Game data (am\\gather.py, am\\gather_ui.py): All-in-One Guide's sealed AOGD1 pages read from
the saved file as text only (never run, never written), only while WoW is closed, the page
rule + Adler-32, a random sender code per account, each page once, the daily limit waits for
the next UTC day, on by default with its own Settings switch (separate from guide reports).
Everything goes to a local fake server; nothing ever goes online."""

import gc
import http.server
import json
import os
import queue
import threading
import time
import tkinter as tk
import unittest
from unittest import mock

from am import config, gather, gather_ui, reports, ui
from tests import fixtures

# Pages made by All-in-One Guide's own code (DB\Serializer.lua Build "D1" + Packs\Format.lua
# Adler32, run with LuaJIT the way Data\Gather.lua Header/Finish does).
PAGE_A = ("AOGD1 rid=k3x9q2ab;h=1;v=0.3.0;b=1.60.1.70959;fac=A;cls=WARRIOR;race=Human;"
          "realm=Forever Test;loc=enUS;day=2026-11-26;cnt=3 AOGD1 k=qxp;q=783;xp=85;pl=2;ql=1 "
          "AOGD1 k=camp;npc=80;m=1429;x=27;y=46;n=11 AOGD1 k=drop;npc=38;it=752;loot=14;got=3 "
          "AOGD1 ck=bfc547a4")
PAGE_B = ("AOGD1 rid=zz9abc12;h=1;v=0.3.0;b=1.60.1.70959;fac=A;cls=WARRIOR;race=Human;"
          "realm=Forever Test;loc=enUS;day=2026-11-26;cnt=2 AOGD1 k=err;msg=Gather.lua:12: "
          "attempt%3B a%3Db %7C 100%25 AOGD1 k=fly;fn=2;tn=4;sec=95 AOGD1 ck=50513f3a")


def make_page(rid, filler="n=1"):
    core = (f"AOGD1 rid={rid};h=1;v=0.3.0;fac=A;cnt=1 AOGD1 k=camp;npc=80;m=1429;x=27;y=46;"
            f"{filler}")
    return f"{core} AOGD1 ck={reports.checksum(core)}"


def saved_file(pages, live=(), extra_module=""):
    """A saved file the way WoW writes it (tabs, "-- [n]" after list entries)."""
    def lst(items, tabs):
        return "".join(f'{tabs}"{s}", -- [{i}]\n' for i, s in enumerate(items, 1))
    return (
        "\nAllinOneGuideDB = {\n"
        "\t[\"dbVersion\"] = 3,\n"
        "\t[\"modules\"] = {\n"
        "\t\t[\"reports\"] = {\n"
        "\t\t\t[\"pages\"] = {\n" + lst([make_page("inreports")], "\t\t\t\t") + "\t\t\t},\n"
        "\t\t\t[\"outbox\"] = {\n\t\t\t},\n"
        "\t\t},\n" + extra_module +
        "\t\t[\"gather\"] = {\n"
        "\t\t\t[\"v\"] = 1,\n"
        "\t\t\t[\"live\"] = {\n"
        "\t\t\t\t[\"order\"] = {\n" + lst(live, "\t\t\t\t\t") + "\t\t\t\t},\n"
        "\t\t\t\t[\"recs\"] = {\n\t\t\t\t\t[\"pages\"] = {\n"
        + lst([make_page("deeper")], "\t\t\t\t\t\t") + "\t\t\t\t\t},\n\t\t\t\t},\n"
        "\t\t\t},\n"
        "\t\t\t[\"_badPages\"] = {\n" + lst([make_page("badpages")], "\t\t\t\t") + "\t\t\t},\n"
        "\t\t\t[\"note\"] = \"a } brace, a \\\" quote and [\\\"pages\\\"] = { in a string\",\n"
        "\t\t\t[\"pages\"] = {\n" + lst(pages, "\t\t\t\t") + "\t\t\t},\n"
        "\t\t},\n"
        "\t},\n"
        "}\n"
        "AllinOneGuideRecDB = {\n}\n")


def write_saved(addons_dir, account, text):
    client = os.path.dirname(os.path.dirname(addons_dir))
    sv = os.path.join(client, "WTF", "Account", account, "SavedVariables")
    os.makedirs(sv, exist_ok=True)
    path = os.path.join(sv, "AllinOneGuide.lua")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return path


def accounts_dir(addons_dir):
    return os.path.join(os.path.dirname(os.path.dirname(addons_dir)), "WTF", "Account")


class Pages(unittest.TestCase):
    def test_real_pages_pass(self):
        for p in (PAGE_A, PAGE_B):
            self.assertTrue(gather.valid_page(p), p)
        self.assertEqual(gather.page_rid(PAGE_A), "k3x9q2ab")

    def test_bad_pages_fail(self):
        self.assertFalse(gather.valid_page(PAGE_A.replace("xp=85", "xp=86")))   # checksum
        self.assertFalse(gather.valid_page(PAGE_A[:-2]))                          # cut
        self.assertFalse(gather.valid_page(PAGE_A.replace(";h=1;", ";h=2;")))     # header
        self.assertFalse(gather.valid_page(PAGE_A.replace("Human", 'Hu"man')))    # character
        self.assertFalse(gather.valid_page(make_page("long", "n=" + "1" * 3000)))  # > 3000
        self.assertFalse(gather.valid_page("AOGR2 rid=abc;ck=00000000"))
        self.assertFalse(gather.valid_page(None))

    def test_only_modules_gather_pages(self):
        text = saved_file([PAGE_A, PAGE_B, PAGE_A, "junk", PAGE_A.replace("xp=85", "xp=1")],
                          live=[make_page("liveone")])
        self.assertEqual(gather.pages_in(text), [PAGE_A, PAGE_B])

    def test_escaped_strings_and_comments(self):
        # the WoW escaping is undone only for \" and \\ (a valid page holds neither)
        text = saved_file([PAGE_A]).replace('"note"', '"no\\\\te"')
        self.assertEqual(gather.pages_in(text), [PAGE_A])
        self.assertEqual(gather.pages_in(saved_file([])), [])

    def test_damaged_files(self):
        good = saved_file([PAGE_A])
        self.assertEqual(gather.pages_in(good[: good.index(PAGE_A) + 50]), [])   # cut off
        self.assertEqual(gather.pages_in(""), [])
        self.assertEqual(gather.pages_in("OtherDB = { [\"modules\"] = {} }"), [])
        t = time.time()
        self.assertEqual(gather.pages_in("AllinOneGuideDB = {" + "{" * 20000), [])
        self.assertEqual(gather.pages_in('AllinOneGuideDB = {["modules"] = {' + "{" * 20000), [])
        self.assertLess(time.time() - t, 5)

    def test_batch_limits(self):
        many = [make_page(f"p{i:03d}") for i in range(45)]
        self.assertEqual(len(gather.batch(many)), gather.MAX_PAGES)
        self.assertEqual(gather.batch(many, sent=["p000", "p001"])[0], many[2])
        big = [make_page(f"b{i:02d}", "n=1;c=" + "x" * 2800) for i in range(30)]
        self.assertEqual(len(gather.batch(big)), 30)   # 30 full pages always fit 100 KB
        with mock.patch.object(gather, "MAX_BODY", 20000):
            b = gather.batch(big)
        self.assertLess(len(json.dumps({"v": 1, "sid": "a" * 12, "pages": b})), 20000)
        self.assertEqual(len(b), 6)

    def test_sid_and_keys(self):
        sid = gather.new_sid()
        self.assertRegex(sid, r"^[a-z0-9]{12}$")
        self.assertNotEqual(sid, gather.new_sid())
        s = {}
        salt = gather.salt(s)
        self.assertEqual(gather.salt(s), salt)
        k = gather.account_key("C:\\WoW\\WTF\\Account", "MYACCOUNT", salt)
        self.assertNotIn("myaccount", k.lower())
        self.assertEqual(k, gather.account_key("C:\\WoW\\WTF\\Account", "myaccount", salt))
        self.assertNotEqual(k, gather.account_key("C:\\WoW\\WTF\\Account", "other", salt))


class Reading(unittest.TestCase):
    def setUp(self):
        self.work = fixtures.temp_dir(prefix="gather_read_")
        self.root_dir, self.addons = fixtures.make_fake_wow(self.work)
        self.accounts = accounts_dir(self.addons)
        self.settings = {}
        gather.salt(self.settings)

    def tearDown(self):
        fixtures.remove(self.work)

    def test_never_written(self):
        path = write_saved(self.addons, "ACC1", saved_file([PAGE_A]))

        def state():
            with open(path, "rb") as f:
                return f.read(), os.stat(path).st_mtime_ns
        before = state()
        q = queue.Queue()
        gather.find_worker(q, self.accounts, self.settings, lambda: False, settle_s=0)
        self.assertEqual(q.get_nowait()[1][0][1], [PAGE_A])
        self.assertEqual(state(), before)

    def test_game_running_nothing_opened(self):
        write_saved(self.addons, "ACC1", saved_file([PAGE_A]))
        q = queue.Queue()
        with mock.patch.object(reports, "_open_shared", side_effect=AssertionError("opened")), \
                mock.patch.object(os, "stat", side_effect=AssertionError("stat")):
            gather.find_worker(q, self.accounts, self.settings, lambda: True, settle_s=0)
        self.assertEqual(q.get_nowait(), ("gather_found", [], None, True))

    def test_unchanged_files_not_read_again(self):
        write_saved(self.addons, "ACC1", saved_file([PAGE_A]))
        q = queue.Queue()
        gather.find_worker(q, self.accounts, self.settings, lambda: False, settle_s=0)
        _, found, keep, _game = q.get_nowait()
        self.assertTrue(found and keep)
        with mock.patch.object(reports, "_open_shared", side_effect=AssertionError("opened")):
            gather.find_worker(q, self.accounts, self.settings, lambda: False, last=keep,
                               settle_s=0)
        self.assertEqual(q.get_nowait()[1], [])

    def test_unfinished_file_waits(self):
        text = saved_file([PAGE_A])
        write_saved(self.addons, "ACC1", text[:-30])
        q = queue.Queue()
        gather.find_worker(q, self.accounts, self.settings, lambda: False, settle_s=0)
        self.assertEqual(q.get_nowait()[1:3], ([], None))

    def test_stuck_unfinished_file_not_read_every_time(self):
        gather._UNFINISHED.clear()
        write_saved(self.addons, "ACC1", saved_file([PAGE_A])[:-30])
        q = queue.Queue()
        gather.find_worker(q, self.accounts, self.settings, lambda: False, settle_s=0)
        self.assertEqual(q.get_nowait()[1:3], ([], None))   # first look: may still be written
        gather.find_worker(q, self.accounts, self.settings, lambda: False, settle_s=0)
        keep = q.get_nowait()[2]
        self.assertTrue(keep)   # same size+time: stuck, stamp kept
        with mock.patch.object(reports, "_open_shared", side_effect=AssertionError("opened")):
            gather.find_worker(q, self.accounts, self.settings, lambda: False, last=keep,
                               settle_s=0)
        self.assertEqual(q.get_nowait()[1], [])
        # the file changes and is finished: read and found
        write_saved(self.addons, "ACC1", saved_file([PAGE_A]))
        gather.find_worker(q, self.accounts, self.settings, lambda: False, last=keep,
                           settle_s=0)
        self.assertEqual(q.get_nowait()[1][0][1], [PAGE_A])

    def test_pending_per_account_sent_and_wait(self):
        write_saved(self.addons, "ACC1", saved_file([PAGE_A, PAGE_B]))
        write_saved(self.addons, "ACC2", saved_file([PAGE_B]))
        found = gather.pending(self.accounts, self.settings)
        self.assertEqual(len(found), 2)
        keys = [k for k, _ in found]
        gather.give_sids(self.settings, keys)
        gather.record(self.settings, [(keys[0], ["k3x9q2ab"], []), (keys[1], [], ["zz9abc12"])],
                      day="2026-11-26")
        found = gather.pending(self.accounts, self.settings, day="2026-11-26")
        self.assertEqual(found, [(keys[0], [PAGE_B])])   # ACC2 waits for tomorrow
        found = gather.pending(self.accounts, self.settings, day="2026-11-27")
        self.assertEqual(found, [(keys[0], [PAGE_B]), (keys[1], [PAGE_B])])

    def test_preview_labels_and_game(self):
        write_saved(self.addons, "ZEBRA", saved_file([PAGE_B]))
        write_saved(self.addons, "ALPHA", saved_file([PAGE_A]))
        self.assertEqual(gather.preview(self.accounts, lambda: False),
                         [("WoW account 1", [PAGE_A]), ("WoW account 2", [PAGE_B])])
        self.assertIsNone(gather.preview(self.accounts, lambda: True))

    def test_forget_sids(self):
        s = {}
        gather.give_sids(s, ["k1", "k2"])
        gather.record(s, [("k1", ["r1"], [])])
        gather.forget_sids(s)
        accs = gather.accounts(s)
        self.assertNotIn("sid", accs["k1"])
        self.assertEqual(accs["k1"]["sent"], ["r1"])
        gather.record(s, [("k1", ["r2"], [])])   # after off: nothing kept
        self.assertEqual(gather.accounts(s)["k1"]["sent"], ["r1"])


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        srv = self.server
        srv.requests.append({"path": self.path, "body": body, "headers": dict(self.headers)})
        if srv.status != 200:
            self.send_response(srv.status)
            if srv.status in (301, 302):
                self.send_header("Location", "http://127.0.0.1:1/elsewhere")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        data = json.loads(body)
        took, over = [], []
        for p in data["pages"]:
            rid = gather.page_rid(p)
            (over if srv.over_after is not None and srv.count >= srv.over_after else took).append(rid)
            if rid in took:
                srv.count += 1
        out = json.dumps({"took": took, "dup": [], "bad": [], "over": over}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *args):
        pass


class Server:
    def __init__(self):
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.requests, self.httpd.status = [], 200
        self.httpd.over_after, self.httpd.count = None, 0
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/data"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def requests(self):
        return self.httpd.requests

    def payloads(self):
        return [json.loads(r["body"]) for r in self.requests]

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class Sending(unittest.TestCase):
    def setUp(self):
        self.server = Server()
        self.sid = "abcdefghij12"

    def tearDown(self):
        self.server.close()

    def test_payload_is_only_sid_and_pages(self):
        r = gather.send_batch(self.server.url, self.sid, [PAGE_A, PAGE_B], True)
        self.assertEqual(r, ("ok", ["k3x9q2ab", "zz9abc12"], []))
        self.assertEqual(self.server.payloads(), [{"v": 1, "sid": self.sid,
                                                   "pages": [PAGE_A, PAGE_B]}])
        req = self.server.requests[0]
        self.assertEqual(req["path"], "/data")
        self.assertTrue(req["headers"]["User-Agent"].startswith("AddonManager/"))

    def test_over_the_limit(self):
        self.server.httpd.over_after = 1
        self.assertEqual(gather.send_batch(self.server.url, self.sid, [PAGE_A, PAGE_B], True),
                         ("ok", ["k3x9q2ab"], ["zz9abc12"]))
        self.server.httpd.status = 429
        self.assertEqual(gather.send_batch(self.server.url, self.sid, [PAGE_A], True),
                         ("over", [], ["k3x9q2ab"]))

    def test_other_answers_try_later(self):
        for status in (400, 403, 404, 500, 503, 302):
            self.server.httpd.status = status
            self.assertEqual(gather.send_batch(self.server.url, self.sid, [PAGE_A], True),
                             ("later", [], []), status)
        self.assertEqual(len(self.server.requests), 6)   # the redirect was never followed

    def test_refusals_without_a_request(self):
        self.assertEqual(gather.send_batch(self.server.url, "BAD", [PAGE_A], True)[0], "later")
        self.assertEqual(gather.send_batch(self.server.url, self.sid, [PAGE_A])[0], "later")
        self.assertEqual(gather.send_batch("", self.sid, [PAGE_A], True)[0], "later")
        self.assertEqual(self.server.requests, [])

    def test_offline(self):
        self.server.close()
        self.assertEqual(gather.send_batch(self.server.url, self.sid, [PAGE_A], True),
                         ("later", [], []))
        self.server = Server()

    def test_switched_off_stops_before_the_next_request(self):
        q = queue.Queue()
        answers = iter([True, False])
        gather.send_worker(q, self.server.url, [("k1", self.sid, [PAGE_A]),
                                                ("k2", "zyxwvutsrq98", [PAGE_B])], True,
                           lambda: next(answers))
        self.assertEqual(q.get_nowait(), ("gather_sent", [("k1", ["k3x9q2ab"], [])], 1, False))
        self.assertEqual(len(self.server.requests), 1)

    def test_tests_never_send_online(self):
        self.assertEqual(config.DATA_URL, "")
        from tests import REAL_DATA_URL
        self.assertTrue(REAL_DATA_URL.startswith("https://") and REAL_DATA_URL.endswith("/data"))


class WindowTests(unittest.TestCase):
    """The real window, withdrawn before anything is drawn, with a local data server."""

    @classmethod
    def setUpClass(cls):
        cls.base = fixtures.temp_dir(prefix="gather_win_")
        cls.site = fixtures.make_server_files(cls.base)

    @classmethod
    def tearDownClass(cls):
        fixtures.remove(cls.base)

    def setUp(self):
        self.work = fixtures.temp_dir(prefix="gather_win_")
        self.root_dir, self.addons = fixtures.make_fake_wow(self.work)
        config.save_settings({})
        self.server = Server()
        self.asked = []
        self.game = False
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

    def confirm(self, *a, **kw):
        self.asked.append(a)
        return True

    def make_app(self, data_url=True):
        if data_url is True:
            data_url = self.server.url
        app = ui.App(self.root, os.path.join(self.site, "manifest.json"), True, self.root_dir,
                     check_game=lambda: self.game, detect=lambda: [], stats_url="",
                     confirm=self.confirm, report_url="", data_url=data_url)
        self.assertEqual(app.gather_settle_s, 5)
        app.gather_settle_s = 0
        return app

    def new_root(self):
        self.root.destroy()
        self.root = tk.Tk()
        self.root.withdraw()

    def pump(self, app, until=None, timeout=15):
        end = time.time() + timeout
        self.root.update()
        while time.time() < end and (app.busy or app._gather_busy or (until and not until())):
            self.root.update()
            time.sleep(0.02)
        for _ in range(10):
            self.root.update()
            time.sleep(0.02)

    def sent_pages(self):
        return [p for body in self.server.payloads() for p in body["pages"]]

    def test_fresh_settings_send_quietly_each_once(self):
        write_saved(self.addons, "MYACCOUNT", saved_file([PAGE_A, PAGE_B]))
        app = self.make_app()
        self.assertTrue(app.gather_var.get())
        self.pump(app, until=lambda: self.server.requests)
        self.assertEqual(self.asked, [])
        self.assertEqual(self.sent_pages(), [PAGE_A, PAGE_B])
        sid = self.server.payloads()[0]["sid"]
        self.assertRegex(sid, r"^[a-z0-9]{12}$")
        s = config.load_settings()
        self.assertNotIn(gather.ON_SETTING, s)   # still the default
        (entry,) = gather.accounts(s).values()
        self.assertEqual((entry["sid"], entry["sent"]), (sid, ["k3x9q2ab", "zz9abc12"]))
        with open(config.settings_path(), encoding="utf-8") as f:
            self.assertNotIn("myaccount", f.read().lower())   # the folder name is never kept
        # Next start with one new page: only that one goes, with the same code.
        new = make_page("newpage1")
        write_saved(self.addons, "MYACCOUNT", saved_file([PAGE_A, PAGE_B, new]))
        self.new_root()
        app2 = self.make_app()
        self.pump(app2, until=lambda: len(self.server.requests) >= 2)
        self.assertEqual(self.server.payloads()[1], {"v": 1, "sid": sid, "pages": [new]})

    def test_send_reports_has_no_effect(self):
        """Game data has its own switch: the guide reports switch changes nothing."""
        write_saved(self.addons, "ACC1", saved_file([PAGE_A]))
        config.save_settings({reports.ON_SETTING: False})
        app = self.make_app()
        self.pump(app, until=lambda: self.server.requests)
        self.assertEqual(self.sent_pages(), [PAGE_A])
        # off + send_reports True: nothing
        config.save_settings({reports.ON_SETTING: True, gather.ON_SETTING: False})
        write_saved(self.addons, "ACC1", saved_file([PAGE_A, PAGE_B]))
        self.new_root()
        app2 = self.make_app()
        self.pump(app2)
        self.pump(app2)
        self.assertEqual(len(self.server.requests), 1)

    def test_switch_off_removes_codes_and_on_makes_new(self):
        write_saved(self.addons, "ACC1", saved_file([PAGE_A]))
        app = self.make_app()
        self.pump(app, until=lambda: self.server.requests)
        first_sid = self.server.payloads()[0]["sid"]
        app.open_settings(show=False)
        self.assertEqual(app.gather_check.cget("text"), ui.GATHER_TEXT)
        self.assertEqual(app.gather_link.cget("text"), ui.GATHER_LINK)
        app.gather_var.set(False)
        app.toggle_gather()
        s = config.load_settings()
        self.assertIs(s[gather.ON_SETTING], False)
        self.assertTrue(all("sid" not in e for e in gather.accounts(s).values()))
        app.close_settings()
        write_saved(self.addons, "ACC1", saved_file([PAGE_A, PAGE_B]))
        self.new_root()
        app2 = self.make_app()
        self.pump(app2)
        self.pump(app2)
        self.assertEqual(len(self.server.requests), 1)   # off: nothing next run
        app2.open_settings(show=False)
        self.assertFalse(app2.gather_var.get())
        app2.gather_var.set(True)
        app2.toggle_gather()
        self.pump(app2, until=lambda: len(self.server.requests) >= 2)
        body = self.server.payloads()[1]
        self.assertEqual(body["pages"], [PAGE_B])
        self.assertNotEqual(body["sid"], first_sid)
        app2.close_settings()

    def test_nothing_while_the_game_runs(self):
        path = write_saved(self.addons, "ACC1", saved_file([PAGE_A]))
        self.game = True
        app = self.make_app()
        opened = []
        real = reports._open_shared
        with mock.patch.object(reports, "_open_shared",
                               side_effect=lambda p: (opened.append(p), real(p))[1]):
            self.pump(app)
            self.pump(app)
        self.assertNotIn(path, opened)
        self.assertEqual(self.server.requests, [])
        self.game = False
        app._reports_tick()   # the next 10-minute look, WoW closed now
        self.pump(app, until=lambda: self.server.requests)
        self.assertEqual(self.sent_pages(), [PAGE_A])

    def test_over_the_limit_waits_for_the_next_day(self):
        write_saved(self.addons, "ACC1", saved_file([PAGE_A, PAGE_B]))
        self.server.httpd.over_after = 1
        with mock.patch.object(gather, "today", return_value="2026-11-26"):
            app = self.make_app()
            self.pump(app, until=lambda: self.server.requests)
            self.assertEqual(self.server.payloads()[0]["pages"], [PAGE_A, PAGE_B])
            app._reports_tick()   # same day: nothing
            self.pump(app)
            self.assertEqual(len(self.server.requests), 1)
        self.server.httpd.over_after = None
        with mock.patch.object(gather, "today", return_value="2026-11-27"):
            app._reports_tick()
            self.pump(app, until=lambda: len(self.server.requests) >= 2)
        self.assertEqual(self.server.payloads()[1]["pages"], [PAGE_B])

    def test_offline_tries_again(self):
        write_saved(self.addons, "ACC1", saved_file([PAGE_A]))
        self.server.httpd.status = 503
        app = self.make_app()
        self.pump(app, until=lambda: self.server.requests)
        self.assertEqual(gather.accounts(config.load_settings()).popitem()[1]["sent"], [])
        self.server.httpd.status = 200
        app._reports_tick()
        self.pump(app, until=lambda: len(self.server.requests) >= 2)
        self.assertEqual(self.server.payloads()[1]["pages"], [PAGE_A])

    def test_no_address_no_folder_nothing(self):
        write_saved(self.addons, "ACC1", saved_file([PAGE_A]))
        app = self.make_app(data_url="")
        self.pump(app)
        self.assertEqual(self.server.requests, [])
        self.assertNotIn(gather.SALT_SETTING, config.load_settings())

    def test_preview_shows_the_exact_pages(self):
        write_saved(self.addons, "ACC1", saved_file([PAGE_A, PAGE_B]))
        app = self.make_app(data_url="")
        self.pump(app)
        app.open_settings(show=False)
        dialog = app.open_game_data(show=False)
        self.assertIsInstance(dialog, gather_ui.GameDataDialog)
        self.pump(app, until=lambda: dialog.pages is not None)
        self.assertEqual(dialog.page_lines(), [PAGE_A, PAGE_B])
        text = dialog.text.get("1.0", "end")
        for part in (gather_ui.NEVER, gather_ui.IP, gather_ui.SENDER, gather_ui.RIGHTS,
                     "group size (a number)", "WoW account 1"):
            self.assertIn(part, text)
        self.assertNotIn("ACC1", text)
        dialog.close()
        self.game = True
        dialog = app.open_game_data(show=False)
        self.pump(app, until=lambda: gather_ui.GAME_OPEN in dialog.text.get("1.0", "end"))
        self.assertEqual(dialog.page_lines(), [])
        dialog.close()
        app.close_settings()


if __name__ == "__main__":
    unittest.main()
