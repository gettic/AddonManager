"""Make a fake WoW Forever folder, a local addon list and a file-based fake GitHub API
for the .exe self-test.

Usage: python -m tests.make_selftest_data <empty folder>
Prints three lines: the addon list path, the fake WoW folder and the fake GitHub API
folder (for --github-api). The list mixes both kinds of entry: TaxiTime with a direct
url/version/sha256, GuildHub from its (fake) GitHub repo's latest release."""

import os
import pathlib
import sys

from tests import fixtures


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
    main(sys.argv[1])
