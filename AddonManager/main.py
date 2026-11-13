"""Guild Addon Manager: start here.

Options (for testing; players just double-click the .exe):
  --manifest <url or path>   use this addon list (allows local files / localhost)
  --github-api <url or path> with --manifest only: a local fake of the GitHub API
  --wow <folder>             use this WoW Forever folder (not saved)
  --home <folder>            keep settings and log here instead of %APPDATA%
  --hidden                   start with the window hidden
  --selftest --selftest-out <file>
                             no window: install every addon from --manifest into
                             --wow, check the result, write a report, exit 0/1
  --selftest-update --github-api <fake> --selftest-out <file>
                             built .exe only, run on a COPY in a temp folder: update
                             that copy from the fake release, check, roll back, report
  --updated-from <version>   set by the app itself when it restarts after an update
  <file>                     a .zip/.rar/.7z or unpacked folder to install (a file dropped
                             on the program's icon, or "Open with"); if the app is open
                             already, the open window gets it (am\\instance.py)
"""

import argparse
import json
import logging
import os
import sys
import threading
import time

from am import APP_NAME, APP_VERSION, config

log = logging.getLogger("main")


def parse_args(argv):
    p = argparse.ArgumentParser(prog="AddonManager", add_help=True)
    p.add_argument("--manifest")
    p.add_argument("--github-api")
    p.add_argument("--wow")
    p.add_argument("--home")
    p.add_argument("--hidden", action="store_true")
    p.add_argument("--selftest", action="store_true")
    p.add_argument("--selftest-out")
    p.add_argument("--selftest-update", action="store_true")
    p.add_argument("--updated-from")
    p.add_argument("path", nargs="?")
    return p.parse_args(argv)


def set_dpi_aware():
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


def selftest(args):
    """Headless check used on the built .exe. Never uses the real WoW folder:
    --manifest and --wow are required."""
    from am import github, installer, manifest, toc, versions, wowpath
    report = {"app": APP_VERSION, "ok": False, "steps": []}

    def step(name, ok, detail=""):
        report["steps"].append({"step": name, "ok": bool(ok), "detail": str(detail)})
        return ok

    try:
        if not (args.manifest and args.wow):
            step("arguments", False, "--manifest and --wow are required")
            return report
        m = manifest.load(args.manifest, allow_local=True)
        step("manifest", m.addons, f"{len(m.addons)} addons")
        api = github.api_base(args.github_api, True)
        m.addons, problems = github.resolve_all(m.addons, api, allow_local=True)
        releases_ok = step("releases", not problems,
                           f"{sum(1 for a in m.addons if a.repo)} from GitHub-style releases; "
                           f"problems: {sorted(problems) or 'none'}")
        status, addons_dir = wowpath.from_picked(args.wow)
        if not step("wow folder", status == "ok", addons_dir or status):
            return report
        wowpath.ensure_addons_dir(addons_dir)
        all_ok = bool(m.addons) and releases_ok
        for addon in m.addons:
            try:
                installer.install(addon, addons_dir)
                exists, v = toc.installed(addons_dir, addon.folder)
                all_ok &= step(f"install {addon.folder}", exists and versions.same(v, addon.version),
                               versions.display(v))
            except Exception as e:
                all_ok = step(f"install {addon.folder}", False, repr(e))
        all_ok &= _selftest_backup(addons_dir, step)
        all_ok &= _selftest_share(step)
        all_ok &= _selftest_later(step)
        all_ok &= _selftest_file_install(addons_dir, step)
        all_ok &= _selftest_system(step)
        # The window toolkit must be bundled: build the window hidden, load, close.
        import tkinter as tk
        from am import ui
        root = tk.Tk()
        root.withdraw()
        app = ui.App(root, args.manifest, True, args.wow, github_api=args.github_api)
        deadline = time.time() + 20
        while app.busy and time.time() < deadline:
            root.update()
            time.sleep(0.05)
        rows = len(app.rows)
        from am import icon
        icons = len(getattr(root, "_am_icons", []))
        icon_ok = step("icon", icons == len(icon.PNGS) and os.path.isfile(icon.ICO),
                       f"{icons} window icon sizes loaded")
        icon_ok &= _selftest_dnd(app, step)
        app.close()
        window_ok = step("window", not app.busy and rows == len(m.addons), f"{rows} rows")
        report["ok"] = bool(all_ok and window_ok and icon_ok)
    except Exception as e:
        step("error", False, repr(e))
    return report


def _selftest_backup(addons_dir, step):
    """Backup + restore round trip on the self-test's fake WoW folder (zip + safety copy
    in a temp folder that is removed afterwards)."""
    import shutil
    import tempfile
    from am import backup, fileinstall
    tmp = tempfile.mkdtemp(prefix="backup_restore_selftest_")
    try:
        found = backup.addon_folders(addons_dir)
        # A pretend "Other addons" record for the first addon: it must survive the round trip.
        record = ({found[0]: {"name": "Selftest", "version": "1.0", "file": "x.zip",
                              "date": "2026-01-01"}} if found else {})
        path, size, counts = backup.make(addons_dir, os.path.join(tmp, "backups"),
                                         others=record)
        info = backup.read(path)
        made = step("backup", info.addons and size > 0, f"{counts}, {size} bytes")
        amap = backup.default_map(info.accounts, backup.local_accounts(addons_dir))
        result = backup.restore(path, addons_dir, os.path.join(tmp, "safety"), account_map=amap)
        restored = step("restore", sorted(result.addons) == sorted(info.addons)
                        and result.safety and os.path.isfile(result.safety),
                        f"{len(result.addons)} addons, {result.files} settings files")
        merged = fileinstall.merge_restored({}, result.others)
        others_ok = step("backup_others",
                         set(merged) == set(record) and all(
                             "file" not in info.others.get(f, {}) for f in record),
                         f"{len(merged)} other addons kept")
        return bool(made and restored and others_ok)
    except Exception as e:
        return step("backup", False, repr(e))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _selftest_share(step):
    """The send-code crypto must be bundled (cryptography + spake2): a SPAKE2 exchange
    in memory, the key split, one sealed chunk opened and a changed one refused. No network."""
    try:
        from spake2 import SPAKE2_A, SPAKE2_B
        from am import share
        pw = share._password(37, ["amber", "lamp", "river"])
        a = SPAKE2_A(pw, idA=share.ID_A, idB=share.ID_B)
        b = SPAKE2_B(pw, idA=share.ID_A, idB=share.ID_B)
        ma, mb = a.start(), b.start()
        ka, kb = share._Keys(a.finish(mb), 37), share._Keys(b.finish(ma), 37)
        c = share.Cipher(ka.data, 37)
        box = c.seal(1, b"selftest", final=True)
        ok = ka.data == kb.data and share.Cipher(kb.data, 37).open(1, box, True) == b"selftest"
        try:
            c.open(1, box[:-1] + bytes([box[-1] ^ 1]), True)
            ok = False
        except share.ShareError:
            pass
        return step("share", ok, "SPAKE2 + ChaCha20-Poly1305")
    except Exception as e:
        return step("share", False, repr(e))


def _selftest_later(step):
    """Send later's crypto must work in the bundle: scrypt record keys, a sealed record
    opened (and a changed one refused), file.kiwi's RFC 8188 encryption both ways. No
    network."""
    try:
        import os
        from am import share, share_later
        key, verifier = share_later.record_keys(share._password(37, ["amber", "lamp", "river"]),
                                                b"s" * 16)
        rec = {"v": 1, "link": share_later.LINK_PREFIX + "abcd1234#" + "A" * 22,
               "key": "0" * 64, "size": 1, "sha256": "0" * 64, "blob": 1,
               "name": share_later.random_name()}
        sealed = share_later.seal_record(key, 37, rec)
        ok = share_later.open_record(key, 37, sealed) == rec and len(verifier) == 64
        try:
            share_later.open_record(key, 38, sealed)   # another code's channel: refused
            ok = False
        except ValueError:
            pass
        # A guild-storage record (v2: no link, the store's id and get token).
        rec2 = {"v": 2, "host": "store", "id": "a" * 32, "get": "b" * 32, "piece": 4194304,
                "slice": 943718400, "key": "0" * 64, "size": 1, "sha256": "0" * 64,
                "blob": 100, "name": share_later.random_name()}
        ok = ok and share_later.open_record(
            key, 37, share_later.seal_record(key, 37, rec2)) == rec2
        k = os.urandom(16)
        data = os.urandom(70000)                       # two RFC 8188 records
        ok = ok and share_later.ece_decrypt(k, share_later.ece_encrypt(k, data)) == data
        return step("send later", ok, "scrypt + ChaCha20-Poly1305 records + aes128gcm")
    except Exception as e:
        return step("send later", False, repr(e))


def _selftest_file_install(addons_dir, step):
    """Add from file on the self-test's fake WoW folder: a zip with two addons (one in a
    wrapper folder) is read, installed and removed again; a .rar too when this system's
    tar can read one. Plus the hand-over to an open window (lock + request). Temp only."""
    import shutil
    import tempfile
    import zipfile
    from am import archive, fileinstall, installer, instance, sysenv, toc

    def addon(prefix, name, version):
        return {f"{prefix}{name}/{name}.toc": f"## Interface: 16001\n## Version: {version}\n",
                f"{prefix}{name}/{name}.lua": "-- selftest\n"}

    tmp = tempfile.mkdtemp(prefix="fileinst_selftest_")
    ok = True
    try:
        entries = {**addon("", "SelftestFileA", "1.0.0"), **addon("Pack-main/", "SelftestFileB", "2.0.0")}
        path = os.path.join(tmp, "pack.zip")
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            for name, data in entries.items():
                z.writestr(name, data)
        plan = fileinstall.scan(path, addons_dir, [], {}, tmp_root=tmp)
        try:
            result = fileinstall.install(plan, [a.folder for a in plan.addons], addons_dir, [],
                                         {}, os.path.join(tmp, "backups"))
        finally:
            plan.close()
        got = {f: toc.describe(os.path.join(addons_dir, f), f)[2] for f, _v, _t in result.installed}
        ok &= step("file install", got == {"SelftestFileA": "1.0.0", "SelftestFileB": "2.0.0"},
                   f"{got}")
        for f in got:
            installer.remove(f, addons_dir, list(got), send=None)
        ok &= step("file remove", not any(os.path.exists(os.path.join(addons_dir, f)) for f in got))
        tar = archive.tar_path()
        if tar:
            seven = os.path.join(tmp, "s.7z")
            src = os.path.join(tmp, "src")
            os.makedirs(os.path.join(src, "SelftestSeven"))
            with open(os.path.join(src, "SelftestSeven", "SelftestSeven.toc"), "w") as f:
                f.write("## Interface: 16001\n## Version: 3.0.0\n")
            import subprocess
            made = subprocess.run([tar, "-a", "-cf", seven, "-C", src, "SelftestSeven"],
                                  capture_output=True, creationflags=archive._no_window(),
                                  env=sysenv.system_env()).returncode == 0
            if made:
                try:
                    plan = fileinstall.scan(seven, addons_dir, [], {}, tmp_root=tmp)
                    found = [a.folder for a in plan.addons]
                    plan.close()
                    step("file 7z (system tar)", found == ["SelftestSeven"], f"{found}")
                except archive.ArchiveError as e:
                    step("file 7z (system tar)", True, f"not readable here: {e.kind}")
            else:
                step("file 7z (system tar)", True, "this tar can't write 7z")
        else:
            step("file rar/7z (system tar)", True, "no system tar: .zip only here")
        home = os.path.join(tmp, "home")
        first, second = instance.Lock(home), instance.Lock(home)
        handed = (first.acquire() and not second.acquire()
                  and instance.send_request(home, path)
                  and instance.take_requests(home) == [os.path.abspath(path)])
        first.release()
        ok &= step("hand-over to the open window", handed and second.acquire())
        second.release()
    except Exception as e:
        ok = step("file install", False, repr(e))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return bool(ok)


def _selftest_system(step):
    """Built Linux program only (sysenv): HTTPS has certificates to check against (the
    build's own OpenSSL looks in Debian's places), and programs we start get the system's
    LD_LIBRARY_PATH, not our _internal folder. No network."""
    from am import sysenv
    if not (getattr(sys, "frozen", False) and os.name != "nt"):
        return step("system", True, "not the built Linux program")
    ok = True
    try:
        import ssl
        loaded = ssl.create_default_context().cert_store_stats().get("x509_ca", 0)
        chosen = next((f"{n}={os.environ[n]}" for n in ("SSL_CERT_FILE", "SSL_CERT_DIR")
                       if os.environ.get(n)), "OpenSSL default")
        has_dir = bool(ssl.get_default_verify_paths().capath)   # a folder loads lazily
        ok &= step("certificates", loaded > 0 or has_dir,
                   f"{chosen}; {loaded} loaded")
    except Exception as e:
        ok = step("certificates", False, repr(e))
    try:
        import subprocess
        out = subprocess.run(["/bin/sh", "-c", 'printf %s "${LD_LIBRARY_PATH-}"'],
                             capture_output=True, text=True, timeout=20,
                             env=sysenv.system_env()).stdout
        own = getattr(sys, "_MEIPASS", "") or "_internal"
        ok &= step("child env", own not in out, f"LD_LIBRARY_PATH={out or '(none)'}")
    except Exception as e:
        ok = step("child env", False, repr(e))
    return ok


def _selftest_dnd(app, step):
    """Drag and drop (tkinterdnd2 + tkdnd) must load in the Windows build. On Linux it is
    reported only: where tkdnd can't load, the app runs without drag and drop."""
    if os.name == "nt":
        return step("drag and drop", app.files.drop_ok, "tkdnd loaded" if app.files.drop_ok
                    else "tkdnd did not load")
    step("drag and drop", True, "tkdnd loaded" if app.files.drop_ok
         else "not available here: the app works without it")
    return True


def selftest_update(args, exe=None):
    """Headless check of the self-update on the built .exe. Only meant for a copy of the
    .exe in a temp folder: it swaps that copy for the fake release, checks, rolls back."""
    import hashlib
    from am import github, selfupdate
    report = {"app": APP_VERSION, "ok": False, "steps": []}

    def step(name, ok, detail=""):
        report["steps"].append({"step": name, "ok": bool(ok), "detail": str(detail)})
        return ok

    def sha(path):
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()

    try:
        exe = exe or selfupdate.exe_path()
        if not step("arguments", exe and args.github_api, "built .exe and --github-api needed"):
            return report
        new, old = selfupdate.side_paths(exe)
        before = sha(exe)
        upd = selfupdate.check(APP_VERSION, github.api_base(args.github_api, True), True)
        if not step("check", upd is not None, upd.version if upd else "no update found"):
            return report
        selfupdate.check_folder(exe)
        step("folder", True, os.path.dirname(exe))
        selfupdate.fetch(upd, exe, True)
        step("download", os.path.isfile(new), f"{os.path.getsize(new)} bytes")
        selfupdate.swap(exe)
        swapped = step("swap", sha(exe) == upd.sha256 and sha(old) == before
                       and not os.path.exists(new), "running exe renamed, new one in place")
        back = step("rollback", selfupdate.rollback(exe) and sha(exe) == before,
                    "old exe back")
        clean = step("cleanup", selfupdate.cleanup(exe) and not os.path.exists(new)
                     and not os.path.exists(old), "no .new/.old left")
        report["ok"] = bool(swapped and back and clean)
    except Exception as e:
        step("error", False, repr(e))
    return report


def _clean_partials():
    """At start: remove unfinished downloads left by a receive that was cut off (the app
    closed or the PC turned off) from the backup folders. Never raises."""
    try:
        from am import backup, backup_ui, share
        share.clean_partials([backup_ui.backup_folder(config.load_settings()),
                              backup.default_folder()])
    except Exception as e:
        log.info("Start-up clean-up of unfinished downloads failed (%s)", type(e).__name__)


def _clean_file_temp():
    """At start: old Add from file temp folders (%TEMP%\\AddonManager_file_*, over 2 hours)
    left by a run that was closed or killed while reading a file. Never raises."""
    try:
        from am import fileinstall
        fileinstall.clean_temp()
    except Exception as e:
        log.info("Start-up clean-up of Add from file temp folders failed (%s)", type(e).__name__)


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.home:
        os.environ[config.HOME_ENV] = os.path.abspath(args.home)
    config.setup_logging()
    log.info("%s %s starting", APP_NAME, APP_VERSION)
    from am import sysenv
    sysenv.fix_certificates()   # built Linux program: before any network call or thread
    sys.excepthook = lambda *exc: log.error("Unexpected error", exc_info=exc)

    if args.selftest or args.selftest_update:
        report = selftest_update(args) if args.selftest_update else selftest(args)
        if args.selftest_out:
            with open(args.selftest_out, "w", encoding="utf-8") as f:
                json.dump(report, f, indent=2)
        return 0 if report["ok"] else 1

    from am import instance
    open_path = os.path.abspath(args.path) if args.path else None
    lock = instance.Lock(config.home_dir())
    if not lock.acquire() and open_path:
        # The app is open already: hand the file to that window instead of a second one.
        if instance.send_request(config.home_dir(), open_path):
            log.info("Handed a file to the open window")
            return 0
    if lock.held:   # only the one open window cleans up (never another run's open files)
        threading.Thread(target=_clean_file_temp, daemon=True).start()
    from am import selfupdate
    # Leftover AddonManager.exe.old/.new from an update (the old exe may still be closing).
    selfupdate.cleanup_later(selfupdate.exe_path())
    if args.updated_from:
        log.info("Started after an update from %s", args.updated_from)
    threading.Thread(target=_clean_partials, daemon=True).start()
    set_dpi_aware()
    from am import icon, ui
    icon.set_app_id()   # before the first window, so the taskbar shows the app's icon
    ui.run(args.manifest, allow_local=bool(args.manifest), wow_override=args.wow,
           hidden=args.hidden, github_api=args.github_api if args.manifest else None,
           updated_from=args.updated_from, open_path=open_path, instance_lock=lock)
    lock.release()
    # One more quiet try on the way out (the old exe may have closed meanwhile). Only
    # <exe>.old/.new; a file still in use is simply left for the next start.
    selfupdate.cleanup(selfupdate.exe_path(), quiet=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
