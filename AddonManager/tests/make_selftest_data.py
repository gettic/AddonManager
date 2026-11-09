"""Make a fake WoW Forever folder, a local addon list and a file-based fake GitHub API
for the .exe self-test.

Usage: python -m tests.make_selftest_data <empty folder>
Prints three lines: the addon list path, the fake WoW folder and the fake GitHub API
folder (for --github-api). The list mixes both kinds of entry: TaxiTime with a direct
url/version/sha256, GuildHub from its (fake) GitHub repo's latest release.

       python -m tests.make_selftest_data <empty folder> --app-update <built .exe>
Self-update test data: copies the .exe to <folder>\\run\\AddonManager.exe (the copy that
runs and updates itself) and makes a fake gettic/AddonManager release v99.0.0 whose
AddonManager.exe is another copy with a few bytes added (never run). On Linux the same
with the AddonManager program and the AddonManager-linux-x86_64 asset.
Prints two lines: the copy to run and the fake GitHub API folder."""

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


def main(base):
    os.makedirs(base, exist_ok=True)
    site = fixtures.make_server_files(base)
    gh = fixtures.make_github_site(base, lambda rel: pathlib.Path(base, "gh", rel).as_uri())
    zpath = os.path.join(site, "TaxiTime-1.3.0.zip")
    list_path = fixtures.write_manifest(os.path.join(base, "selftest.json"), [
        {"folder": "TaxiTime", "name": "Taxi Time", "version": "1.3.0",
         "url": pathlib.Path(zpath).as_uri(), "sha256": fixtures.sha256(zpath),
         "size": os.path.getsize(zpath)},
        {"folder": "GuildHub", "name": "Guild Hub", "repo": "gettic/GuildHub"}])
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
