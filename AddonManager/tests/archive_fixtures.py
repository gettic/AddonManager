"""Test helpers for Add from file: addon trees, zips of every layout, a tiny RAR5 writer
(stored files, no RAR tool needed) and a fake tar runner."""

import os
import struct
import zipfile
import zlib

from am import archive


def toc(folder, version="1.0", interface="16001", title=None):
    lines = []
    if interface is not None:
        lines.append(f"## Interface: {interface}")
    lines.append(f"## Title: {title or folder}")
    if version is not None:
        lines.append(f"## Version: {version}")
    lines.append(f"{folder}.lua")
    return "\n".join(lines) + "\n"


def addon_files(folder, version="1.0", prefix="", toc_name=None, interface="16001", extra=None):
    """{entry name: data} for one addon folder (toc_name None = <folder>.toc)."""
    base = f"{prefix}{folder}/" if folder is not None else prefix
    name = toc_name or f"{folder}.toc"
    out = {f"{base}{name}": toc(folder, version, interface),
           f"{base}{folder}.lua": f"-- {folder} {version}\n",
           f"{base}Libs/LibStub/LibStub.toc": "## Interface: 16001\n",
           f"{base}Libs/LibStub/LibStub.lua": "-- lib\n",
           f"{base}media/icon.tga": "TGA"}
    out.update(extra or {})
    return out


def write_zip(path, entries):
    """entries: {name: str/bytes} or [(name or ZipInfo, data)]."""
    items = entries.items() if isinstance(entries, dict) else entries
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in items:
            z.writestr(name, data)
    return path


def write_tree(root, entries):
    for name, data in entries.items():
        path = os.path.join(root, *name.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data.encode() if isinstance(data, str) else data)
    return root


# ---------- RAR5 (stored) ----------
def _vint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _block(body):
    """body = type + flags + fields. Returns CRC32 + size + body."""
    size = _vint(len(body))
    return struct.pack("<I", zlib.crc32(size + body) & 0xFFFFFFFF) + size + body


def write_rar5(path, entries):
    """A real RAR5 archive with stored (uncompressed) files: entries {name: data}; names
    ending in '/' are folders. Readable by libarchive (Windows 11 tar.exe, bsdtar)."""
    out = bytearray(b"Rar!\x1a\x07\x01\x00")
    out += _block(_vint(1) + _vint(0) + _vint(0))            # main header
    for name, data in entries.items():
        is_dir = name.endswith("/")
        name_b = name.rstrip("/").encode("utf-8")
        data = b"" if is_dir else (data.encode() if isinstance(data, str) else data)
        file_flags = 0x1 if is_dir else 0x4                  # dir / CRC32 present
        fields = _vint(file_flags) + _vint(len(data)) + _vint(0x10 if is_dir else 0x20)
        if not is_dir:
            fields += struct.pack("<I", zlib.crc32(data) & 0xFFFFFFFF)
        fields += _vint(0) + _vint(0) + _vint(len(name_b)) + name_b   # stored, Windows
        # Like WinRAR: every file header (folders and empty files too) has the data-size
        # field (flag 0x2). libarchive 3.6 (Debian bookworm's bsdtar) refuses one without it
        # ("no data found in file/service block"); newer libarchive doesn't mind.
        body = _vint(2) + _vint(0x2) + _vint(len(data)) + fields
        out += _block(body) + data
    out += _block(_vint(5) + _vint(0) + _vint(0))             # end of archive
    with open(path, "wb") as f:
        f.write(out)
    return path


def write_rar4(path, entries):
    """A real RAR 2.9-4.x archive with stored files ({name: data}, files only), ending with
    the end-of-archive block. Readable by libarchive."""
    def block(htype, flags, fields):
        size = 7 + len(fields)
        body = struct.pack("<BHH", htype, flags, size) + fields
        return struct.pack("<H", zlib.crc32(body) & 0xFFFF) + body

    out = bytearray(b"Rar!\x1a\x07\x00")
    out += block(0x73, 0, bytes(6))                                   # main header
    for name, data in entries.items():
        data = data.encode() if isinstance(data, str) else data
        name_b = name.replace("/", "\\").encode("ascii")
        fields = struct.pack("<IIBIIBBHI", len(data), len(data), 2,
                             zlib.crc32(data) & 0xFFFFFFFF, 0x21000000, 20, 0x30,
                             len(name_b), 0x20) + name_b
        out += block(0x74, 0x8000, fields) + data
    out += block(0x7B, 0x4000, b"")                                   # end of archive
    with open(path, "wb") as f:
        f.write(out)
    return path


def system_tar_reads_rar(tmp):
    """True if this system's tar can read a RAR5 file (Windows 11 tar.exe, bsdtar)."""
    exe = archive.tar_path()
    if not exe:
        return False
    path = write_rar5(os.path.join(tmp, "probe.rar"), {"a.txt": "x"})
    try:
        return archive.TarRunner().list(path, archive.RAR) == [("a.txt", False)]
    except archive.ArchiveError:
        return False


class FakeTar:
    """Stands in for archive.TarRunner: lists `names`, then writes `files` into dest."""

    def __init__(self, names=None, files=None, error=None, unpack_error=None):
        self.names = names or []
        self.files = files or {}
        self.error = error
        self.unpack_error = unpack_error
        self.unpacked = False

    def list(self, path, kind):
        if self.error:
            raise self.error
        return list(self.names)

    def unpack(self, path, dest, kind, max_bytes, max_files):
        if self.unpack_error:
            raise self.unpack_error
        self.unpacked = True
        write_tree(dest, self.files)
        size = sum(len(v) for v in self.files.values())
        if size > max_bytes:
            raise archive.ArchiveError("too_big", "unpacks too big")
