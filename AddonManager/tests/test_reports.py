"""Guide reports (am\\reports.py): reading the All-in-One Guide's outbox from the saved files
as text only (never run, never written), the line checks (same as the addon and the
server), each report once, nothing while the game runs, on by default and sent quietly
(no question), an old "Never send" (False) stays off.
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

    def test_fields(self):
        f = reports.fields(LINE_A)
        self.assertEqual(f["note"], "Wrong spot; a=b | 100%\næøå")
        self.assertEqual((f["pk"], f["sid"]), ("demo-elwynn", "e5"))
        self.assertEqual(reports.fields(LINE_B)["noloc"], "1")

    def test_settings_note_says_public_and_what_is_sent(self):
        self.assertEqual(ui.REPORTS_TEXT, "Send guide reports")
        note = ui.REPORTS_NOTE
        for fact in ("On unless you switch it off",
                     "after you /reload, log out or close the game",
                     "Anyone can read them on the guide's GitHub page",
                     "keep names out of your note", "the step", "your note", "level", "class",
                     "race", "language", "game and addon version", "map position if you allowed it",
                     "Never your name or account"):
            self.assertIn(fact, note)
        self.assertFalse(hasattr(reports, "question"))
        self.assertFalse(hasattr(ui, "REPORTS_YES"))


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

    def test_complete(self):
        """WoW still writing: a block or string left open = not complete."""
        text = saved_file([LINE_A, LINE_B], kept=[make_line("kept1")])
        self.assertTrue(reports.complete(text))
        self.assertTrue(reports.complete(""))
        cuts = [text.index(LINE_A) + 20, text.index('["outbox"]') + 14,
                text.index("a } brace") + 3, text.index("AllinOneGuideRecDB") + 22,
                len(text) // 2]
        for cut in cuts:
            self.assertFalse(reports.complete(text[:cut]), cut)
        self.assertFalse(reports.complete("A = { } }"))
        self.assertTrue(reports.complete('A = { "-- not a comment {", -- a { comment\n}\n'))
        start = time.perf_counter()
        self.assertFalse(reports.complete("A = {" * 200000))
        self.assertFalse(reports.complete('A = { "' + "x" * 2000000))
        self.assertLess(time.perf_counter() - start, 2.0)

    def test_unfinished_file_skipped(self):
        text = saved_file([LINE_A])
        path = write_saved(self.addons, "X", text[: text.index("AllinOneGuideRecDB") + 22])
        write_saved(self.addons, "SECOND", saved_file([LINE_B]))
        unfinished = []
        got = reports.pending(self.accounts, (), unfinished)
        self.assertEqual([line for _a, line in got], [LINE_B])
        self.assertEqual(unfinished, [path])

    def test_stamp_is_stat_only(self):
        path = write_saved(self.addons, "X", saved_file([LINE_A]))
        st = os.stat(path)
        self.assertEqual(reports.stamp(self.accounts), {path: (st.st_size, st.st_mtime_ns)})
        self.assertEqual(reports.stamp(os.path.join(self.work, "nowhere")), {})

    def find(self, last=None, settle=0, sleep=None, sent=()):
        q = queue.Queue()
        reports.find_worker(q, self.accounts, list(sent), last, settle,
                            sleep or (lambda s: None))
        msg = q.get_nowait()
        self.assertTrue(q.empty())
        self.assertEqual(msg[0], "reports_found")
        return msg[1], msg[2]

    def test_find_worker_reads_only_changed_settled_complete(self):
        """The game may run: no game check; the stamp decides."""
        self.assertEqual(self.find(), ([], {}))   # no saved file yet
        path = write_saved(self.addons, "X", saved_file([LINE_A]))
        found, st = self.find()
        self.assertEqual(found, [("AllinOneGuide", LINE_A)])
        self.assertEqual(st, reports.stamp(self.accounts))
        # Unchanged since the last look: not opened at all.
        real_read = reports._read
        reads = []
        reports._read = lambda p: reads.append(p) or real_read(p)
        try:
            self.assertEqual(self.find(st, sent=["k3x9qa"]), ([], st))
            self.assertEqual(reads, [])
            # Written again (a /reload), still changing during the settle step: wait.
            write_saved(self.addons, "X", saved_file([LINE_B, LINE_A]))
            def still_writing(_s):
                with open(path, "a", encoding="utf-8") as f:
                    f.write("\n")
            self.assertEqual(self.find(st, settle=5, sleep=still_writing), ([], st))
            self.assertEqual(reads, [])
            # Settled now: read, only the new one.
            found, st2 = self.find(st, settle=5, sent=["k3x9qa"])
            self.assertEqual(found, [("AllinOneGuide", LINE_B)])
            self.assertEqual(st2, reports.stamp(self.accounts))
            # Settled but not complete (a cut write): nothing, same stamp kept (try again).
            text = saved_file([make_line("cut1"), LINE_B, LINE_A])
            write_saved(self.addons, "X", text[: text.index("cut1") + 10])
            self.assertEqual(self.find(st2, settle=5), ([], st2))
            write_saved(self.addons, "X", text)
            found, _st3 = self.find(st2, settle=5, sent=["k3x9qa", "zz9abc"])
            self.assertEqual([line for _a, line in found], [make_line("cut1")])
        finally:
            reports._read = real_read

    def test_saved_file_can_be_replaced_while_open(self):
        """WoW renames the old file to .bak and writes a new one: never blocked by us."""
        path = write_saved(self.addons, "X", saved_file([LINE_A]))
        new = path + ".new"
        with open(new, "w", encoding="utf-8") as f:
            f.write(saved_file([LINE_B]))
        with reports._open_shared(path) as f:
            head = f.read(10)
            with open(path, "ab") as w:   # written while open
                w.write(b"\n")
            os.replace(path, path + ".bak")   # renamed while open
            os.replace(new, path)             # replaced while open
        self.assertTrue(head)
        self.assertEqual(reports.outbox_lines(reports._read(path)), [LINE_B])
        if os.name == "nt":   # Python's own open() would have blocked the rename
            with open(path, "rb"):
                with self.assertRaises(PermissionError):
                    os.replace(path, path + ".bak2")
        # While the app reads, a write from "WoW" still goes through.
        with reports._open_shared(path):
            os.remove(path + ".bak")
            os.remove(path)
        self.assertFalse(os.path.exists(path))
        self.assertEqual(reports._read(path), "")

    def test_never_read_beyond_the_saved_file(self):
        """Only WTF\\Account\\*\\SavedVariables\\AllinOneGuide.lua is opened."""
        write_saved(self.addons, "X", saved_file([LINE_A]))
        sv = os.path.dirname(write_saved(self.addons, "Y", saved_file([LINE_B])))
        with open(os.path.join(sv, "Other.lua"), "w") as f:
            f.write(saved_file([make_line("other")]))
        opened = []
        real = reports._open_shared
        reports._open_shared = lambda p: opened.append(p) or real(p)
        try:
            self.find()
        finally:
            reports._open_shared = real
        self.assertEqual(sorted(os.path.relpath(p, self.accounts).lower() for p in opened),
                         [os.path.join("x", "savedvariables", "allinoneguide.lua"),
                          os.path.join("y", "savedvariables", "allinoneguide.lua")])

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
            self.assertEqual(q.get_nowait(), ("reports_sent", [], 0, False), status)

    def test_offline_stops_and_keeps_the_rest(self):
        self.server.close()
        q = queue.Queue()
        reports.send_worker(q, self.server.url, [("AllinOneGuide", LINE_A)], True)
        self.assertEqual(q.get_nowait(), ("reports_sent", [], 0, False))
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

    def make_app(self, report_url=True, watch_ms=None, settle_s=0):
        if report_url is True:
            report_url = self.server.url
        app = ui.App(self.root, os.path.join(self.site, "manifest.json"), True, self.root_dir,
                     check_game=lambda: self.game, detect=lambda: [], stats_url="",
                     confirm=self.confirm, report_url=report_url)
        self.assertEqual(app.report_watch_ms, 600000)   # every 10 minutes in the app
        self.assertEqual(app.report_settle_s, 5)
        if watch_ms:
            app.report_watch_ms = watch_ms
        app.report_settle_s = settle_s
        return app

    def new_root(self):
        self.root.destroy()
        self.root = tk.Tk()
        self.root.withdraw()

    def pump(self, app, until=None, timeout=15):
        end = time.time() + timeout
        self.root.update()
        while time.time() < end and (app.busy or app._reports_busy or (until and not until())):
            self.root.update()
            time.sleep(0.02)
        for _ in range(10):
            self.root.update()
            time.sleep(0.02)

    def test_on_by_default_sent_quietly_each_once(self):
        write_saved(self.addons, "X", saved_file([LINE_A, LINE_B]))
        app = self.make_app()
        self.assertTrue(app.report_var.get())
        self.pump(app, until=lambda: len(self.server.requests) >= 2)
        self.assertEqual(self.asked, [])   # never asked
        self.assertEqual([p["r"] for p in self.server.payloads()], [LINE_A, LINE_B])
        s = config.load_settings()
        self.assertNotIn(reports.ON_SETTING, s)   # nothing saved: still the default
        self.assertEqual(s[reports.SENT_SETTING], ["k3x9qa", "zz9abc"])
        # Next start with one new report: only the new one goes.
        write_saved(self.addons, "X", saved_file([make_line("newone"), LINE_A, LINE_B]))
        self.new_root()
        app2 = self.make_app()
        self.pump(app2, until=lambda: len(self.server.requests) >= 3)
        self.assertEqual(self.asked, [])
        self.assertEqual(len(self.server.requests), 3)
        self.assertEqual(self.server.payloads()[2]["r"], make_line("newone"))

    def test_old_never_send_stays_off(self):
        """A False saved by the old question ("Never send") stays off for good."""
        config.save_settings({reports.ON_SETTING: False})
        write_saved(self.addons, "X", saved_file([LINE_A]))
        for _ in range(2):
            app = self.make_app()
            self.assertFalse(app.report_var.get())
            self.pump(app)
            self.pump(app)
            self.new_root()
        self.assertEqual((self.asked, self.server.requests), ([], []))
        self.assertIs(config.load_settings()[reports.ON_SETTING], False)
        # Only the Settings switch turns it back on: sent at once.
        app = self.make_app()
        self.pump(app)
        app.open_settings(show=False)
        self.assertFalse(app.report_var.get())
        app.report_var.set(True)
        app.toggle_reports()
        self.assertIs(config.load_settings()[reports.ON_SETTING], True)
        self.pump(app, until=lambda: self.server.requests)
        self.assertEqual([p["r"] for p in self.server.payloads()], [LINE_A])
        app.close_settings()

    def test_old_send_automatically_carries_on(self):
        config.save_settings({reports.ON_SETTING: True})
        write_saved(self.addons, "X", saved_file([LINE_A]))
        app = self.make_app()
        self.pump(app, until=lambda: self.server.requests)
        self.assertEqual(self.asked, [])
        self.assertEqual([p["r"] for p in self.server.payloads()], [LINE_A])
        self.assertIs(config.load_settings()[reports.ON_SETTING], True)

    def test_switched_off_in_settings_nothing_next_run(self):
        app = self.make_app(report_url="")   # this run sends nothing (no address)
        self.pump(app)
        app.open_settings(show=False)
        self.assertTrue(app.report_var.get())
        app.report_var.set(False)
        app.toggle_reports()
        app.close_settings()
        self.assertIs(config.load_settings()[reports.ON_SETTING], False)
        write_saved(self.addons, "X", saved_file([LINE_A]))
        self.new_root()
        app2 = self.make_app()
        self.pump(app2)
        self.pump(app2)
        self.assertEqual((self.asked, self.server.requests), ([], []))

    def test_sent_while_the_game_runs(self):
        """WoW wrote the file at a /reload: sent with the game still open."""
        self.game = True
        write_saved(self.addons, "X", saved_file([LINE_A]))
        app = self.make_app(settle_s=0.1)
        self.pump(app, until=lambda: config.load_settings().get(reports.SENT_SETTING))
        self.assertEqual([p["r"] for p in self.server.payloads()], [LINE_A])
        self.assertEqual(self.asked, [])

    def test_watch_sends_new_reports_while_open(self):
        """Every check (10 minutes in the app; short here): a file WoW rewrote while the
        app is open is read once it settled and complete; each report goes once."""
        self.game = True
        app = self.make_app(watch_ms=150, settle_s=0.05)
        self.pump(app)
        self.assertIsNotNone(app._reports_after)
        self.assertEqual(self.server.requests, [])
        # A /reload: written.
        write_saved(self.addons, "X", saved_file([LINE_A]))
        self.pump(app, until=lambda: self.server.requests)
        self.pump(app, until=lambda: config.load_settings().get(reports.SENT_SETTING))
        # A cut write (WoW still writing): nothing; then finished: only the new one.
        text = saved_file([LINE_B, LINE_A])
        write_saved(self.addons, "X", text[: text.index(LINE_B) + 30])
        time.sleep(0.6)
        self.pump(app)
        self.assertEqual(len(self.server.requests), 1)
        self.game = False   # logged out and closed the game: the final write
        write_saved(self.addons, "X", text)
        self.pump(app, until=lambda: len(self.server.requests) >= 2)
        time.sleep(0.6)   # more checks: nothing sent twice
        self.pump(app)
        self.assertEqual([p["r"] for p in self.server.payloads()], [LINE_A, LINE_B])
        self.assertEqual(config.load_settings()[reports.SENT_SETTING], ["k3x9qa", "zz9abc"])
        self.assertEqual(self.asked, [])

    def test_no_overlapping_looks_during_a_slow_send(self):
        write_saved(self.addons, "X", saved_file([LINE_A]))
        gate = threading.Event()
        real = reports.send_one
        calls = []
        def slow(*a, **kw):
            calls.append(a[2])
            gate.wait(10)
            return real(*a, **kw)
        reports.send_one = slow
        finds = []
        real_find = reports.find_worker
        reports.find_worker = lambda *a, **kw: finds.append(1) or real_find(*a, **kw)
        try:
            app = self.make_app(watch_ms=50)
            end = time.time() + 10
            while not calls and time.time() < end:
                self.root.update()
                time.sleep(0.02)
            for _ in range(20):   # many checks while the send hangs
                self.root.update()
                time.sleep(0.02)
            self.assertEqual(len(finds), 1)
            gate.set()
            self.pump(app, until=lambda: config.load_settings().get(reports.SENT_SETTING))
            for _ in range(15):
                self.root.update()
                time.sleep(0.02)
        finally:
            reports.send_one = real
            reports.find_worker = real_find
            gate.set()
        self.assertEqual(calls, [LINE_A])
        self.assertEqual(len(self.server.requests), 1)

    def test_switched_off_nothing_looked_at(self):
        """Switch off: the files aren't even stat'ed, let alone read."""
        config.save_settings({reports.ON_SETTING: False})
        write_saved(self.addons, "X", saved_file([LINE_A]))
        looks = []
        real_stamp, real_read = reports.stamp, reports._read
        reports.stamp = lambda d: looks.append("stat") or real_stamp(d)
        reports._read = lambda p: looks.append("read") or real_read(p)
        try:
            app = self.make_app(watch_ms=50)
            self.pump(app)
            time.sleep(0.3)
            self.pump(app)
            self.assertEqual(looks, [])
            # Switched on: looked at and sent at once.
            app.open_settings(show=False)
            app.report_var.set(True)
            app.toggle_reports()
            app.close_settings()
            self.pump(app, until=lambda: self.server.requests)
            self.assertIn("read", looks)
            # Off again: no more looks.
            self.pump(app, until=lambda: config.load_settings().get(reports.SENT_SETTING))
            app.open_settings(show=False)
            app.report_var.set(False)
            app.toggle_reports()
            app.close_settings()
            n = len(looks)
            time.sleep(0.3)
            self.pump(app)
            self.assertEqual(len(looks), n)
        finally:
            reports.stamp, reports._read = real_stamp, real_read
        self.assertEqual([p["r"] for p in self.server.payloads()], [LINE_A])

    def test_nothing_to_send(self):
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
