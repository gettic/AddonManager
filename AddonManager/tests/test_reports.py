"""Guide reports (am\\reports.py): reading the All-in-One Guide's outbox from the saved files
as text only (never run, never written), the line checks (same as the addon and the
server), each report once, nothing while the game runs, the one-time question and quiet
sending after a yes. Everything goes to a local fake server; nothing ever goes online."""

import gc
import http.server
import json
import os
import queue
import re
import threading
import time
import tkinter as tk
import unittest

from am import config, reports, ui
from tests import fixtures

# Lines built by the addon's own code (DB\Serializer.lua + Packs\Format.lua Adler32), so the
# checksum and escaping here match the game side exactly.
LINE_A = ("AOGR2 rid=k3x9qa;v=0.2.0;c=16001 1.60.1.70959 forever enUS;lang=enUS;ab=coords;"
          "pk=demo-elwynn;sid=e5;lv=7;map=1429;x=41.2;y=65.9;"
          "note=Wrong spot%3B a%3Db %7C 100%25%0A%C3%A6%C3%B8%C3%A5;ck=2c7d356c")
LINE_B = ("AOGR2 rid=zz9abc;v=0.2.0;ab=danger;pk=demo-elwynn;sid=e7;noloc=1;"
          "note=Hi from Bob-Firemaw, you suck;ck=79e6220a")


def make_line(rid, note="x"):
    body = f"AOGR2 rid={rid};v=0.2.0;ab=other;pk=demo-elwynn;sid=e1;note={note}"
    return f"{body};ck={reports.checksum(body)}"


def saved_file(outbox, kept=(), extra=""):
    """A saved file the way WoW writes it (tabs, "-- [n]" after list entries)."""
    def lst(items):
        return "".join(f'\t\t\t\t"{s}", -- [{i}]\n' for i, s in enumerate(items, 1))
    return (
        "\nAllinOneGuideDB = {\n"
        "\t[\"dbVersion\"] = 3,\n"
        "\t[\"modules\"] = {\n"
        "\t\t[\"reports\"] = {\n"
        "\t\t\t[\"schemaVersion\"] = 1,\n"
        "\t\t\t[\"list\"] = {\n" + lst(kept) + "\t\t\t},\n"
        "\t\t\t[\"outbox\"] = {\n" + lst(outbox) + "\t\t\t},\n"
        "\t\t},\n"
        "\t\t[\"notes\"] = {\n"
        "\t\t\t[\"text\"] = \"a } brace and a \\\" quote in a string\",\n"
        "\t\t},\n"
        "\t},\n" + extra +
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


class Lines(unittest.TestCase):
    def test_real_lines_pass(self):
        self.assertTrue(reports.valid(LINE_A))
        self.assertTrue(reports.valid(LINE_B))
        self.assertEqual(reports.rid(LINE_A), "k3x9qa")

    def test_bad_lines_fail(self):
        self.assertFalse(reports.valid(LINE_A.replace("ab=coords", "ab=corrds")))   # checksum
        self.assertFalse(reports.valid(LINE_A[:-3]))                               # cut
        self.assertFalse(reports.valid("hello"))
        self.assertFalse(reports.valid(LINE_A.replace("Wrong", "Wr\"ong")))         # odd character
        self.assertFalse(reports.valid(make_line("abc", "x" * 1600)))              # too long
        self.assertFalse(reports.valid(None))

    def test_fields_and_summary(self):
        f = reports.fields(LINE_A)
        self.assertEqual(f["note"], "Wrong spot; a=b | 100%\næøå")
        self.assertEqual((f["pk"], f["sid"]), ("demo-elwynn", "e5"))
        s = reports.summary(LINE_A)
        self.assertTrue(s.startswith('Wrong place on the map, step e5: "Wrong spot; a=b'), s)
        self.assertNotIn("demo-elwynn", s)   # no raw guide ids
        self.assertIn("map position", s)
        self.assertTrue(reports.summary(LINE_B).startswith("Dangerous spot, step e7:"))
        self.assertIn("no location", reports.summary(LINE_B))

    def test_question_says_public_names_and_position(self):
        title, text = reports.question([("AllinOneGuide", LINE_A), ("AllinOneGuide", LINE_B)])
        self.assertEqual(title, "Guide reports")
        self.assertTrue(text.startswith("2 guide reports you made in the game are waiting:"))
        self.assertIn("Send guide reports automatically from now on?", text)
        self.assertIn("Anyone can read them on the guide's GitHub page", text)
        self.assertIn("keep names out of your note", text)
        for fact in ("the step", "your note", "level", "class", "race", "language",
                     "game and addon version", "map position", "Leave out my location",
                     "Never your name or account"):
            self.assertIn(fact, text)
            if fact not in ("the step", "Leave out my location", "Never your name or account"):
                self.assertIn(fact.replace("your note", "note"), ui.REPORTS_NOTE)
        self.assertIn("Never your name or account", ui.REPORTS_NOTE)
        self.assertIn("GitHub page", ui.REPORTS_NOTE)
        self.assertNotIn("Leave out my location", ui.REPORTS_NOTE)
        self.assertTrue(reports.question([("AllinOneGuide", LINE_A)])[1].startswith(
            "1 guide report you made in the game is waiting:"))
        many = [("AllinOneGuide", make_line(f"r{i}")) for i in range(5)]
        self.assertIn("and 2 more", reports.question(many)[1])


class Reading(unittest.TestCase):
    def setUp(self):
        self.work = fixtures.temp_dir(prefix="guide_reports_")
        self.root_dir, self.addons = fixtures.make_fake_wow(self.work)
        self.accounts = os.path.join(os.path.dirname(os.path.dirname(self.addons)),
                                     "WTF", "Account")

    def tearDown(self):
        fixtures.remove(self.work)

    def test_only_the_outbox(self):
        text = saved_file([LINE_A, LINE_B], kept=[make_line("kept1")])
        self.assertEqual(reports.outbox_lines(text), [LINE_A, LINE_B])

    def test_bad_and_repeated_lines_dropped(self):
        bad = LINE_A.replace("ab=coords", "ab=corrds")
        text = saved_file([LINE_A, bad, LINE_A, "not a report"])
        self.assertEqual(reports.outbox_lines(text), [LINE_A])

    def test_empty_and_old_saves(self):
        self.assertEqual(reports.outbox_lines(saved_file([])), [])
        old = saved_file([]).replace('\t\t\t["outbox"] = {\n\t\t\t},\n', "")
        self.assertNotIn("outbox", old)
        self.assertEqual(reports.outbox_lines(old), [])
        self.assertEqual(reports.outbox_lines(""), [])
        self.assertEqual(reports.outbox_lines("AllinOneGuideDB = { [\"modules\"] = { [\"reports\"] = {"), [])

    def test_many_unclosed_blocks_are_fast(self):
        """A damaged file with 20,000 blocks that never close: stops at the first one."""
        text = saved_file([LINE_A]) + '["reports"] = { ["outbox"] = { "x", ' * 20000
        start = time.perf_counter()
        self.assertEqual(reports.outbox_lines(text), [LINE_A])
        self.assertLess(time.perf_counter() - start, 2.0)
        bad = '["reports"] = { ["outbox"] = { "a", ' * 20000
        start = time.perf_counter()
        self.assertEqual(reports.outbox_lines(bad), [])
        self.assertLess(time.perf_counter() - start, 2.0)

    def test_line_outside_reports_ignored(self):
        extra = f'\t["outbox"] = {{\n\t\t"{LINE_B}",\n\t}},\n'
        self.assertEqual(reports.outbox_lines(saved_file([LINE_A], extra=extra)), [LINE_A])

    def test_every_account_each_report_once_sent_skipped(self):
        write_saved(self.addons, "X", saved_file([LINE_A, LINE_B]))
        write_saved(self.addons, "SECOND", saved_file([LINE_A, make_line("third")]))
        got = reports.pending(self.accounts)
        self.assertEqual(sorted(reports.rid(line) for _a, line in got), ["k3x9qa", "third", "zz9abc"])
        self.assertTrue(all(a == "AllinOneGuide" for a, _l in got))
        got = reports.pending(self.accounts, sent=["k3x9qa", "third"])
        self.assertEqual([line for _a, line in got], [LINE_B])

    def test_file_is_never_written(self):
        path = write_saved(self.addons, "X", saved_file([LINE_A]))
        def state():
            with open(path, "rb") as f:
                return os.path.getmtime(path), f.read()
        before = state()
        reports.pending(self.accounts)
        self.assertEqual(state(), before)

    def test_nothing_while_the_game_runs(self):
        write_saved(self.addons, "X", saved_file([LINE_A]))
        q = queue.Queue()
        reports.find_worker(q, self.accounts, [], lambda: True)
        self.assertTrue(q.empty())
        reports.find_worker(q, self.accounts, [], lambda: False)
        self.assertEqual(q.get_nowait(), ("reports_found", [("AllinOneGuide", LINE_A)]))

    def test_no_wtf_folder(self):
        self.assertEqual(reports.pending(os.path.join(self.work, "nowhere")), [])

    def test_remember_caps(self):
        s = {reports.SENT_SETTING: [f"r{i}" for i in range(reports.SENT_KEEP)]}
        reports.remember(s, ["new", "r1"])
        self.assertEqual(len(s[reports.SENT_SETTING]), reports.SENT_KEEP)
        self.assertEqual(s[reports.SENT_SETTING][-1], "new")


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        srv = self.server
        srv.requests.append({"path": self.path, "body": body})
        status = srv.status
        if srv.seen_status and json.loads(body).get("r") in srv.seen:
            status = srv.seen_status
        srv.seen.add(json.loads(body).get("r"))
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


class Server:
    def __init__(self, status=201):
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.requests, self.httpd.status = [], status
        self.httpd.seen, self.httpd.seen_status = set(), None
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/report"
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

    def tearDown(self):
        self.server.close()

    def test_payload_is_only_the_line(self):
        self.assertEqual(reports.send_one(self.server.url, "AllinOneGuide", LINE_A, True), "sent")
        self.assertEqual(self.server.payloads(), [{"v": 1, "a": "AllinOneGuide", "r": LINE_A}])
        self.assertEqual(self.server.requests[0]["path"], "/report")

    def test_answers(self):
        # Only 400 (bad line) and 409 (already filed) are final; anything else is "later"
        # (404: the address isn't live yet; 403: a block page; 5xx: busy).
        for status, want in ((409, "refused"), (400, "refused"), (404, "later"), (403, "later"),
                             (401, "later"), (405, "later"), (413, "later"), (415, "later"),
                             (429, "later"), (500, "later"), (503, "later")):
            self.server.httpd.status = status
            self.assertEqual(reports.send_one(self.server.url, "AllinOneGuide", LINE_A, True),
                             want, status)
        # a bad line is never sent
        n = len(self.server.requests)
        self.assertEqual(reports.send_one(self.server.url, "AllinOneGuide", "hello", True), "refused")
        self.assertEqual(len(self.server.requests), n)

    def test_url_rules(self):
        self.assertFalse(reports.url_ok(self.server.url))            # http only in tests
        self.assertTrue(reports.url_ok(self.server.url, True))
        self.assertFalse(reports.url_ok("https://user:pw@example.com/report"))
        self.assertTrue(reports.url_ok(config.REPORT_URL or "https://x.example/report"))
        self.assertEqual(reports.send_one("", "AllinOneGuide", LINE_A, True), "later")

    def test_404_and_403_are_not_remembered(self):
        for status in (404, 403):
            self.server.httpd.status = status
            q = queue.Queue()
            reports.send_worker(q, self.server.url, [("AllinOneGuide", LINE_A),
                                                     ("AllinOneGuide", LINE_B)], True)
            self.assertEqual(q.get_nowait(), ("reports_sent", [], 0), status)

    def test_offline_stops_and_keeps_the_rest(self):
        self.server.close()
        q = queue.Queue()
        reports.send_worker(q, self.server.url, [("AllinOneGuide", LINE_A)], True)
        self.assertEqual(q.get_nowait(), ("reports_sent", [], 0))
        self.server = Server()   # tearDown closes this one

    def test_tests_never_send_online(self):
        self.assertEqual(config.REPORT_URL, "")


class WindowTests(unittest.TestCase):
    """The real window, withdrawn before anything is drawn, with a local report server."""

    @classmethod
    def setUpClass(cls):
        cls.base = fixtures.temp_dir(prefix="guide_reports_win_")
        cls.site = fixtures.make_server_files(cls.base)

    @classmethod
    def tearDownClass(cls):
        fixtures.remove(cls.base)

    def setUp(self):
        self.work = fixtures.temp_dir(prefix="guide_reports_win_")
        self.root_dir, self.addons = fixtures.make_fake_wow(self.work)
        config.save_settings({})
        self.server = Server()
        self.asked = []
        self.answer = True
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

    def confirm(self, title, text, ok, cancel, *a, **kw):
        self.asked.append((title, text, ok, cancel))
        return self.answer

    def make_app(self, report_url=True):
        if report_url is True:
            report_url = self.server.url
        return ui.App(self.root, os.path.join(self.site, "manifest.json"), True, self.root_dir,
                      check_game=lambda: self.game, detect=lambda: [], stats_url="",
                      confirm=self.confirm, report_url=report_url)

    def new_root(self):
        self.root.destroy()
        self.root = tk.Tk()
        self.root.withdraw()

    def pump(self, app, until=None, timeout=15):
        end = time.time() + timeout
        self.root.update()
        while time.time() < end and (app.busy or app._reports_sending or (until and not until())):
            self.root.update()
            time.sleep(0.02)
        for _ in range(10):
            self.root.update()
            time.sleep(0.02)

    def test_asked_once_then_sent_quietly_each_once(self):
        write_saved(self.addons, "X", saved_file([LINE_A, LINE_B]))
        app = self.make_app()
        self.pump(app, until=lambda: len(self.server.requests) >= 2)
        self.assertEqual(len(self.asked), 1)
        title, text, ok, cancel = self.asked[0]
        self.assertEqual((title, ok, cancel), ("Guide reports", ui.REPORTS_YES, ui.REPORTS_NO))
        self.assertIn("Anyone can read them", text)
        self.assertEqual([p["r"] for p in self.server.payloads()], [LINE_A, LINE_B])
        s = config.load_settings()
        self.assertIs(s[reports.ON_SETTING], True)
        self.assertEqual(s[reports.SENT_SETTING], ["k3x9qa", "zz9abc"])
        # Next start with one new report: no question, only the new one goes.
        write_saved(self.addons, "X", saved_file([make_line("newone"), LINE_A, LINE_B]))
        self.new_root()
        app2 = self.make_app()
        self.pump(app2, until=lambda: len(self.server.requests) >= 3)
        self.assertEqual(len(self.asked), 1)
        self.assertEqual(len(self.server.requests), 3)
        self.assertEqual(self.server.payloads()[2]["r"], make_line("newone"))

    def test_no_means_never(self):
        self.answer = False
        write_saved(self.addons, "X", saved_file([LINE_A]))
        app = self.make_app()
        self.pump(app, until=lambda: self.asked)
        self.pump(app)
        self.assertEqual(self.server.requests, [])
        self.assertIs(config.load_settings()[reports.ON_SETTING], False)
        self.new_root()
        app2 = self.make_app()
        self.pump(app2)
        self.assertEqual(len(self.asked), 1)   # never asked again
        self.assertEqual(self.server.requests, [])
        # The Settings switch turns it on: sent at once.
        app2.open_settings(show=False)
        self.assertFalse(app2.report_var.get())
        app2.report_var.set(True)
        app2.toggle_reports()
        self.pump(app2, until=lambda: self.server.requests)
        self.assertEqual([p["r"] for p in self.server.payloads()], [LINE_A])
        app2.close_settings()

    def test_closing_the_question_asks_again_next_time(self):
        """X or Esc (None from the question) is not "Never send": nothing saved or sent."""
        self.answer = None
        write_saved(self.addons, "X", saved_file([LINE_A]))
        app = self.make_app()
        self.pump(app, until=lambda: self.asked)
        self.pump(app)
        self.assertEqual(len(self.asked), 1)
        self.assertEqual(self.server.requests, [])
        self.assertNotIn(reports.ON_SETTING, config.load_settings())
        self.assertFalse(app.report_var.get())
        self.new_root()
        self.answer = True
        app2 = self.make_app()
        self.pump(app2, until=lambda: self.server.requests)
        self.assertEqual(len(self.asked), 2)   # asked again
        self.assertEqual([p["r"] for p in self.server.payloads()], [LINE_A])

    def test_real_question_x_and_esc_answer_none(self):
        app = self.make_app(report_url="")
        self.pump(app)
        for how in ("x", "esc", "no", "yes"):
            got = []
            parts = app._reports_parts("Guide reports", "text", got.append)
            win = parts["win"]
            self.assertIs(parts["focus"], parts["cancel"])   # a stray key never says yes
            self.assertEqual((parts["ok"].cget("text"), parts["cancel"].cget("text")),
                             (ui.REPORTS_YES, ui.REPORTS_NO))
            if how == "x":
                self.root.tk.call(win.protocol("WM_DELETE_WINDOW"))
            elif how == "esc":
                # the key's own binding (event fields filled with 0: it ignores them)
                win.tk.eval(re.sub(r"%[#A-Za-z]", "0", win.bind("<Escape>")))
            elif how == "no":
                parts["cancel"].invoke()
            else:
                parts["ok"].invoke()
            self.assertEqual(got[:1], [{"x": None, "esc": None, "no": False, "yes": True}[how]],
                             how)
            try:
                win.destroy()
            except tk.TclError:
                pass

    def test_nothing_while_the_game_runs(self):
        self.game = True
        write_saved(self.addons, "X", saved_file([LINE_A]))
        app = self.make_app()
        self.pump(app)
        self.pump(app)
        self.assertEqual((self.asked, self.server.requests), ([], []))
        self.assertNotIn(reports.ON_SETTING, config.load_settings())

    def test_nothing_to_send_no_question(self):
        write_saved(self.addons, "X", saved_file([], kept=[LINE_A]))
        app = self.make_app()
        self.pump(app)
        self.pump(app)
        self.assertEqual((self.asked, self.server.requests), ([], []))

    def test_no_address_nothing(self):
        write_saved(self.addons, "X", saved_file([LINE_A]))
        for url in ("", None):   # None: dev run (allow_local) -> never config.REPORT_URL
            app = self.make_app(report_url=url)
            self.pump(app)
            self.assertEqual(app.report_url, "")
            self.new_root()
        self.assertEqual((self.asked, self.server.requests), ([], []))

    def test_server_already_has_it_counts_as_sent(self):
        config.save_settings({reports.ON_SETTING: True})
        self.server.httpd.status = 409
        write_saved(self.addons, "X", saved_file([LINE_A]))
        app = self.make_app()
        self.pump(app, until=lambda: config.load_settings().get(reports.SENT_SETTING))
        self.assertEqual(config.load_settings()[reports.SENT_SETTING], ["k3x9qa"])
        self.assertEqual(self.asked, [])

    def test_server_down_tries_next_time(self):
        config.save_settings({reports.ON_SETTING: True})
        self.server.httpd.status = 503
        write_saved(self.addons, "X", saved_file([LINE_A]))
        app = self.make_app()
        self.pump(app, until=lambda: self.server.requests)
        self.pump(app)
        self.assertNotIn(reports.SENT_SETTING, config.load_settings())
        self.server.httpd.status = 201
        self.new_root()
        app2 = self.make_app()
        self.pump(app2, until=lambda: config.load_settings().get(reports.SENT_SETTING))
        self.assertEqual(config.load_settings()[reports.SENT_SETTING], ["k3x9qa"])


if __name__ == "__main__":
    unittest.main()
