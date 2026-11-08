"""Version numbers: parse, compare and show as vX.Y.Z."""

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


def compare(a, b):
    """-1, 0 or 1 like a <=> b. Non-numeric versions: 0 if equal text, else None."""
    pa, pb = parse(a), parse(b)
    if pa is None or pb is None:
        return 0 if plain(a) == plain(b) else None
    return (pa > pb) - (pa < pb)


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
