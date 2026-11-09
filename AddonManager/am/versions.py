"""Version numbers: parse, compare and show as vX.Y.Z."""

import re

_RUNS = re.compile(r"[0-9]+|[A-Za-z]+")

UP_TO_DATE = "up_to_date"
UPDATE = "update"
NOT_INSTALLED = "not_installed"
UNKNOWN = "unknown"      # installed, but its .toc has no readable version
NEWER = "newer"          # installed version is newer than the list
NO_LATEST = "no_latest"  # addon list not available


def plain(version):
    """'v1.3.0' -> '1.3.0' (trimmed). None stays None."""
    if version is None:
        return None
    v = str(version).strip()
    if v[:1] in ("v", "V"):
        v = v[1:]
    return v


def parse(version):
    """'1.3' -> (1, 3); returns None if not purely numeric."""
    v = plain(version)
    if not v:
        return None
    parts = v.split(".")
    if not all(p.isdigit() for p in parts):
        return None
    nums = [int(p) for p in parts]
    while len(nums) > 1 and nums[-1] == 0:
        nums.pop()
    return tuple(nums)


def parse_full(version):
    """Like semver: '1.2.0-beta.1' -> ((1, 2), ('beta', 1)); '1.2' -> ((1, 2), ()).
    Build info after '+' is ignored. None if it isn't of that shape."""
    v = plain(version)
    if not v:
        return None
    v = v.split("+", 1)[0]
    core, sep, pre = v.partition("-")
    nums = parse(core)
    if nums is None:
        return None
    if not sep:
        return nums, ()
    ids = []
    for p in pre.replace("-", ".").split("."):
        if not p or not p.isascii() or not p.isalnum():
            return None
        # Letters and numbers apart, so beta10 > beta9 and RC10 > RC9 ("beta10" = beta, 10).
        for run in _RUNS.findall(p):
            ids.append(int(run) if run.isdigit() else run.lower())
    return nums, tuple(ids)


def is_prerelease(version):
    """True for '1.2.0-beta.1' and the like."""
    p = parse_full(version)
    return bool(p and p[1])


def _cmp_pre(a, b):
    """Semver pre-release order: no pre-release is newest; numbers < words; shorter first."""
    if a == b:
        return 0
    if not a or not b:
        return 1 if not a else -1
    for x, y in zip(a, b):
        if x == y:
            continue
        if isinstance(x, int) != isinstance(y, int):
            return -1 if isinstance(x, int) else 1
        return 1 if x > y else -1
    return (len(a) > len(b)) - (len(a) < len(b))


def compare(a, b):
    """-1, 0 or 1 like a <=> b ('1.2.0-beta.1' < '1.2.0' < '1.2.1').
    Versions of another shape: 0 if equal text, else None."""
    pa, pb = parse_full(a), parse_full(b)
    if pa is None or pb is None:
        return 0 if plain(a) == plain(b) else None
    if pa[0] != pb[0]:
        return 1 if pa[0] > pb[0] else -1
    return _cmp_pre(pa[1], pb[1])


def same(a, b):
    return compare(a, b) == 0


def display(version):
    """Shown in the window: 'v1.3.0', or '-' when there is none."""
    v = plain(version)
    return "v" + v if v else "-"


def status(installed_exists, installed, latest):
    """Status key for one addon row."""
    if not installed_exists:
        return NOT_INSTALLED if latest else NO_LATEST
    if not installed:
        return UNKNOWN
    if not latest:
        return NO_LATEST
    c = compare(installed, latest)
    if c == 0:
        return UP_TO_DATE
    if c == 1:
        return NEWER
    return UPDATE  # older, or different and not comparable
