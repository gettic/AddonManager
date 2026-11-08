"""Test helpers: a fake WoW Forever folder, addon zips (good and evil),
manifests and a quiet local web server. Everything lives in a temp folder."""

import hashlib
import http.server
import json
import os
import shutil
import tempfile
import threading
import time
import zipfile

TEST_TMP_ENV = "ADDONMANAGER_TEST_TMP"


def temp_dir(prefix="v1core_"):
    """A new temp folder (under $ADDONMANAGER_TEST_TMP if set)."""
    base = os.environ.get(TEST_TMP_ENV)
    if base:
        os.makedirs(base, exist_ok=True)
    return tempfile.mkdtemp(prefix=prefix, dir=base)


def toc_text(folder, version):
    return (f"## Interface: 16001\n## Title: {folder}\n## Version: {version}\n"
            f"{folder}.lua\n")


def make_addon_folder(addons_dir, folder, version, extra="old"):
    path = os.path.join(addons_dir, folder)
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, f"{folder}_Forever.toc"), "w", encoding="utf-8") as f:
        f.write(toc_text(folder, version))
    with open(os.path.join(path, f"{folder}.lua"), "w", encoding="utf-8") as f:
        f.write(f"-- {folder} {version}\n")
    with open(os.path.join(path, f"{extra}.lua"), "w", encoding="utf-8") as f:
        f.write("-- file only in this version\n")
    return path


def make_fake_wow(base, client="_forever_"):
    """base\\World of Warcraft\\<client>\\ with Wow.exe, WTF and Interface\\AddOns
    holding TaxiTime 1.2.0 and an unrelated OtherAddon. Returns (root, addons_dir)."""
    root = os.path.join(base, "World of Warcraft")
    client_dir = os.path.join(root, client)
    addons = os.path.join(client_dir, "Interface", "AddOns")
    os.makedirs(addons)
    open(os.path.join(client_dir, "Wow.exe"), "wb").close()
    sv = os.path.join(client_dir, "WTF", "Account", "X", "SavedVariables")
    os.makedirs(sv)
    with open(os.path.join(sv, "TaxiTime.lua"), "w") as f:
        f.write("TaxiTimeDB = {}\n")
    make_addon_folder(addons, "TaxiTime", "1.2.0", extra="old_only")
    other = os.path.join(addons, "OtherAddon")
    os.makedirs(other)
    with open(os.path.join(other, "OtherAddon.toc"), "w") as f:
        f.write("## Version: 9.9\n")
    return root, addons


def make_zip(path, folder, version, files=None, toc_version=None):
    """A good addon zip: <folder>/<folder>_Forever.toc + <folder>/<folder>.lua."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"{folder}/", "")
        z.writestr(f"{folder}/{folder}_Forever.toc",
                   toc_text(folder, toc_version if toc_version is not None else version))
        z.writestr(f"{folder}/{folder}.lua", f"-- {folder} {version}\n")
        z.writestr(f"{folder}/new_only.lua", "-- new\n")
        for name, data in (files or {}).items():
            z.writestr(name, data)
    return path


def make_raw_zip(path, entries):
    """entries: list of (ZipInfo or name, data)."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for info, data in entries:
            z.writestr(info, data)
    return path


def sha256(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def write_manifest(path, addons, fmt=1, app=None):
    doc = {"format": fmt, "title": "Test Guild", "addons": addons}
    if app:
        doc["app"] = app
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2)
    return path


def make_server_files(base):
    """base\\site\\ with TaxiTime 1.3.0 and GuildHub 0.4.1 zips and manifest.json
    (relative urls, real sha256). Returns the site folder."""
    site = os.path.join(base, "site")
    os.makedirs(site)
    entries = []
    for folder, name, version in (("TaxiTime", "Taxi Time", "1.3.0"),
                                  ("GuildHub", "Guild Hub", "0.4.1")):
        zname = f"{folder}-{version}.zip"
        zpath = make_zip(os.path.join(site, zname), folder, version)
        entries.append({"folder": folder, "name": name, "description": f"{name} test",
                        "version": version, "url": zname, "sha256": sha256(zpath),
                        "size": os.path.getsize(zpath)})
    write_manifest(os.path.join(site, "manifest.json"), entries)
    return site


def asset(name, url, path=None, digest=True, state="uploaded"):
    """One release asset as GitHub's API shows it. digest=True: real sha256 of path;
    a string: that digest; None: no digest (older GitHub assets)."""
    if digest is True:
        digest = "sha256:" + sha256(path) if path else None
    return {"name": name, "browser_download_url": url, "state": state,
            "size": os.path.getsize(path) if path else 123, "digest": digest,
            "content_type": "application/zip"}


def write_release(api_dir, repo, tag, assets, raw=None):
    """A fake GitHub API answer: <api_dir>/repos/<repo>/releases/latest (a JSON file)."""
    folder = os.path.join(api_dir, "repos", *repo.split("/"), "releases")
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "latest"), "w", encoding="utf-8") as f:
        if raw is not None:
            f.write(raw)
        else:
            json.dump({"tag_name": tag, "name": tag, "draft": False, "prerelease": False,
                       "assets": assets}, f, indent=2)
    return api_dir


def make_fake_exe(path, marker=b"new", size=1024 * 1024 + 100, source=None):
    """A file that passes the update checks ('MZ', over 1 MB). source: copy that file and
    append marker (never run). Returns path."""
    with open(path, "wb") as f:
        if source:
            with open(source, "rb") as src:
                shutil.copyfileobj(src, f)
            f.write(marker)
        else:
            f.write(b"MZ" + marker + b"\0" * max(0, size - 2 - len(marker)))
    return path


def make_app_release(api_dir, dl_dir, url_of, tag="v99.0.0", source=None, digest=True,
                     name="AddonManager.exe", marker=b"new"):
    """A fake latest release of gettic/AddonManager with one .exe asset in dl_dir.
    url_of(file path) -> its URL. Returns the asset file path."""
    os.makedirs(dl_dir, exist_ok=True)
    path = make_fake_exe(os.path.join(dl_dir, name), marker=marker, source=source)
    write_release(api_dir, "gettic/AddonManager", tag,
                  [asset(name, url_of(path), path, digest=digest)])
    return path


def make_github_site(base, url_of):
    """base\\gh\\: zips for gettic/TaxiTime (tag v1.3.0, asset TaxiTime-v1.3.0.zip, like
    the real one) and gettic/GuildHub (tag 0.4.1, asset GuildHub-0.4.1.zip), the fake API
    under gh\\api and addons.json (repo entries). url_of(path relative to gh) -> URL.
    Returns the gh folder."""
    gh = os.path.join(base, "gh")
    api = os.path.join(gh, "api")
    os.makedirs(os.path.join(gh, "dl"))
    for repo, folder, tag, zname, version in (
            ("gettic/TaxiTime", "TaxiTime", "v1.3.0", "TaxiTime-v1.3.0.zip", "1.3.0"),
            ("gettic/GuildHub", "GuildHub", "0.4.1", "GuildHub-0.4.1.zip", "0.4.1")):
        zpath = make_zip(os.path.join(gh, "dl", zname), folder, version)
        write_release(api, repo, tag, [
            asset(zname, url_of(f"dl/{zname}"), zpath),
            {"name": "notes.txt", "browser_download_url": url_of("dl/notes.txt"),
             "state": "uploaded", "size": 1, "digest": None}])
    write_manifest(os.path.join(gh, "addons.json"), [
        {"folder": "TaxiTime", "name": "Taxi Time", "repo": "gettic/TaxiTime"},
        {"folder": "GuildHub", "name": "Guild Hub", "repo": "gettic/GuildHub"}])
    return gh


def tree_snapshot(path):
    """{relative path: bytes} of every file under path, to prove nothing changed."""
    snap = {}
    for dirpath, _dirs, files in os.walk(path):
        for name in files:
            full = os.path.join(dirpath, name)
            with open(full, "rb") as f:
                snap[os.path.relpath(full, path)] = f.read()
    return snap


class _Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path.startswith("/slow"):
            time.sleep(2)
            self.send_response(200)
            self.end_headers()
            return
        if self.path.startswith("/limited"):
            # GitHub's "rate limit reached" reply (unauthenticated: 60 an hour).
            self.send_response(403)
            self.send_header("x-ratelimit-remaining", "0")
            self.send_header("x-ratelimit-reset", str(int(time.time()) + 150))
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"message": "API rate limit exceeded"}')
            return
        if self.path.startswith("/forbidden"):
            self.send_response(403)
            self.end_headers()
            return
        if self.path.startswith("/redirect/"):
            self.send_response(302)
            self.send_header("Location", "/" + self.path[len("/redirect/"):])
            self.end_headers()
            return
        super().do_GET()


class LocalServer:
    """http://127.0.0.1:<port>/ serving a folder, in a background thread."""

    def __init__(self, directory):
        handler = lambda *a, **k: _Handler(*a, directory=directory, **k)
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


def remove(path):
    shutil.rmtree(path, ignore_errors=True)
