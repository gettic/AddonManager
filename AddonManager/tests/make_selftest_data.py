"""Make a fake WoW Forever folder and a local addon list for the .exe self-test.

Usage: python -m tests.make_selftest_data <empty folder>
Prints two lines: the manifest.json path and the fake WoW folder."""

import os
import sys

from tests import fixtures


def main(base):
    os.makedirs(base, exist_ok=True)
    site = fixtures.make_server_files(base)
    root, _addons = fixtures.make_fake_wow(os.path.join(base, "wow"))
    print(os.path.join(site, "manifest.json"))
    print(root)


if __name__ == "__main__":
    main(sys.argv[1])
