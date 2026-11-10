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
"""

import argparse
import json
import logging
import os
import sys
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
        app.close()
        window_ok = step("window", not app.busy and rows == len(m.addons), f"{rows} rows")
        report["ok"] = bool(all_ok and window_ok and icon_ok)
    except Exception as e:
        step("error", False, repr(e))
    return report


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


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.home:
        os.environ[config.HOME_ENV] = os.path.abspath(args.home)
    config.setup_logging()
    log.info("%s %s starting", APP_NAME, APP_VERSION)
    sys.excepthook = lambda *exc: log.error("Unexpected error", exc_info=exc)

    if args.selftest or args.selftest_update:
        report = selftest_update(args) if args.selftest_update else selftest(args)
        if args.selftest_out:
            with open(args.selftest_out, "w", encoding="utf-8") as f:
                json.dump(report, f, indent=2)
        return 0 if report["ok"] else 1

    from am import selfupdate
    # Leftover AddonManager.exe.old/.new from an update (the old exe may still be closing).
    selfupdate.cleanup_later(selfupdate.exe_path())
    if args.updated_from:
        log.info("Started after an update from %s", args.updated_from)
    set_dpi_aware()
    from am import icon, ui
    icon.set_app_id()   # before the first window, so the taskbar shows the app's icon
    ui.run(args.manifest, allow_local=bool(args.manifest), wow_override=args.wow,
           hidden=args.hidden, github_api=args.github_api if args.manifest else None,
           updated_from=args.updated_from)
    # One more quiet try on the way out (the old exe may have closed meanwhile). Only
    # <exe>.old/.new; a file still in use is simply left for the next start.
    selfupdate.cleanup(selfupdate.exe_path(), quiet=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
