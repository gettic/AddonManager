"""Make a fake WoW Forever folder, a local addon list and a file-based fake GitHub API
for the .exe self-test.

Usage: python -m tests.make_selftest_data <empty folder>
Prints three lines: the addon list path, the fake WoW folder and the fake GitHub API
folder (for --github-api). The list mixes both kinds of entry: TaxiTime with a direct
url/version/sha256, GuildHub from its (fake) GitHub repo's latest release, plus a guide
with two packs in its repo ("packs" key, tag prefixes): Guide v1.0.0 and the pack release
alliance-v0.5.0 published after it (the guide must still get its own), and a horde pack
that isn't out yet (not_out: no row, not a failure).

       python -m tests.make_selftest_data <empty folder> --app-update <built .exe>
Self-update test data: copies the .exe to <folder>\\run\\AddonManager.exe (the copy that
runs and updates itself) and makes a fake gettic/AddonManager release v99.0.0 whose
AddonManager.exe is another copy with a few bytes added (never run). On Linux the same
with the AddonManager-x86_64.AppImage file and asset (the copy keeps the file's name).
Prints two lines: the copy to run and the fake GitHub API folder."""

import json
import os
import pathlib
import shutil
import sys

from tests import fixtures


def app_update(base, exe):
    run_dir = os.path.join(base, "run")
    os.makedirs(run_dir)
    # Same file name as the build (AddonManager.exe, or AddonManager on Linux); copy2 keeps
    # the Linux "executable" bit. The fake release's asset is this system's (config.APP_ASSET).
    copy = shutil.copy2(exe, os.path.join(run_dir, os.path.basename(exe)))
    api = os.path.join(base, "api")
    fixtures.make_app_release(api, os.path.join(base, "dl"), lambda p: pathlib.Path(p).as_uri(),
                              source=exe, marker=b"selftest-update")
    print(copy)
    print(api)


def _guide_packs(base, list_path):
    """gettic/Guide in the fake API (main + alliance- stream) and its entries in the list."""
    gh = os.path.join(base, "gh")
    api = os.path.join(gh, "api")
    url_of = lambda name: pathlib.Path(gh, "dl", name).as_uri()
    main_zip = fixtures.make_zip(os.path.join(gh, "dl", "Guide-v1.0.0.zip"), "Guide", "1.0.0")
    pack_zip = fixtures.make_zip(os.path.join(gh, "dl", "Guide_Alliance-v0.5.0.zip"),
                                 "Guide_Alliance", "0.5.0")
    fixtures.write_releases(api, "gettic/Guide", [   # newest first, like GitHub
        fixtures.release_json("alliance-v0.5.0", [fixtures.asset(
            "Guide_Alliance-v0.5.0.zip", url_of("Guide_Alliance-v0.5.0.zip"), pack_zip)],
            published="2026-11-02T10:00:00Z"),
        fixtures.release_json("v1.0.0", [fixtures.asset(
            "Guide-v1.0.0.zip", url_of("Guide-v1.0.0.zip"), main_zip)],
            published="2026-11-01T10:00:00Z")])
    with open(list_path, encoding="utf-8") as f:
        doc = json.load(f)
    doc["addons"].append({"folder": "Guide", "name": "Guide", "repo": "gettic/Guide"})
    doc["packs"] = [
        {"folder": "Guide_Alliance", "name": "Alliance routes", "repo": "gettic/Guide",
         "tag_prefix": "alliance-", "parent": "Guide"},
        {"folder": "Guide_Horde", "name": "Horde routes", "repo": "gettic/Guide",
         "tag_prefix": "horde-", "parent": "Guide"}]
    with open(list_path, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2)


def main(base):
    os.makedirs(base, exist_ok=True)
    site = fixtures.make_server_files(base)
    gh = fixtures.make_github_site(base, lambda rel: pathlib.Path(base, "gh", rel).as_uri())
    zpath = os.path.join(site, "TaxiTime-1.3.0.zip")
    # Made like the guild's release zips (Compress-Archive: "\" between folders), which
    # older Linux builds refused as unsafe.
    wpath = fixtures.make_windows_zip(os.path.join(site, "NotePad-1.0.0.zip"), "NotePad", "1.0.0")
    list_path = fixtures.write_manifest(os.path.join(base, "selftest.json"), [
        {"folder": "TaxiTime", "name": "Taxi Time", "version": "1.3.0",
         "url": pathlib.Path(zpath).as_uri(), "sha256": fixtures.sha256(zpath),
         "size": os.path.getsize(zpath)},
        {"folder": "NotePad", "name": "Note Pad", "version": "1.0.0",
         "url": pathlib.Path(wpath).as_uri(), "sha256": fixtures.sha256(wpath),
         "size": os.path.getsize(wpath)},
        {"folder": "GuildHub", "name": "Guild Hub", "repo": "gettic/GuildHub"}])
    _guide_packs(base, list_path)
    root, _addons = fixtures.make_fake_wow(os.path.join(base, "wow"))
    print(list_path)
    print(root)
    print(os.path.join(gh, "api"))


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[2] == "--app-update":
        os.makedirs(sys.argv[1], exist_ok=True)
        app_update(sys.argv[1], sys.argv[3])
    else:
        main(sys.argv[1])
