"""Locked kept copies of received backups (".amlocked") and Save zip.

A backup received from another PC (Send code, Send servers, file.kiwi) is kept in the backup
folder LOCKED with the code it was received with: "AddonManager backup <date> received.amlocked".
This PC remembers the code (am\\codes.py), so Restore opens it without asking; another PC asks
for the code once. Save zip... writes the plain backup zip where the player picks and only then
removes the locked copy.

File: MAGIC, header length (4 bytes, big-endian), header JSON (plain: version, random file id,
salt, scrypt settings, plain size, chunk size), a check box (index 0), then the plain zip in
CHUNK pieces, each sealed with ChaCha20-Poly1305 (index = nonce; AAD = index + last flag, so
pieces can't be moved, repeated, cut or made the last). Key = HMAC-SHA256(scrypt(code, salt),
"AM-lock-v1|" + header): a changed header opens like a wrong code. A wrong code fails at the
check box before anything is written.

While a Restore window shows a locked (or just received) backup, its plain zip sits next to it
as a hidden ".AddonManager unlocked <hex>.part": removed when that window closes, and any
leftover at the next start (clean_parts, under the instance lock).

Only hashlib and the bundled `cryptography` package; never logs a code."""

import errno
import hashlib
import hmac
import json
import logging
import math
import os
import re
import secrets
import shutil
import struct

from am import backup

log = logging.getLogger(__name__)

EXT = backup.LOCKED_EXT
MAGIC = b"AMLOCK1\n"
VERSION = 1
CHUNK = 1024 * 1024
TAG = 16
CHECK = b"AM-lock-check"
MAX_HEADER = 1024
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 16, 8, 1   # as share_later's send-later records
SCRYPT_MAXMEM = 128 * 1024 * 1024
SPARE_DISK = 20 * 1024 * 1024
FAT32_MAX = 4 * 1024 ** 3 - 1
UNLOCKED_PREFIX = ".AddonManager unlocked "    # a plain copy while Restore shows it
LOCKING_PREFIX = ".AddonManager locking "      # a locked copy being written
SAVING_PREFIX = ".AddonManager saving "        # Save zip's copy being written
PART_EXT = ".part"


class LockError(Exception):
    """kind: not_locked, newer, damaged, wrong_code, disk_full, cancelled, file, same_file,
    inside_game, too_big_for_drive. need = bytes needed (disk_full)."""

    def __init__(self, kind, detail="", need=None):
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind, self.detail, self.need = kind, detail, need


def is_locked(path):
    return str(path).lower().endswith(EXT)


def _password(code):
    """The code as the relay's password bytes (any case/separators; raises LockError)."""
    from am import share
    try:
        channel, words = share.parse_code(code)
    except share.ShareError as e:
        raise LockError("wrong_code", "format") from e
    return share._password(channel, words)


def normal_code(code):
    """The code written the normal way ("37-amber-lamp-river"), or None."""
    from am import share
    try:
        return share.format_code(*share.parse_code(code))
    except share.ShareError:
        return None


def _key(password, header_bytes, h):
    secret = hashlib.scrypt(password, salt=bytes.fromhex(h["salt"]), n=SCRYPT_N, r=SCRYPT_R,
                            p=SCRYPT_P, maxmem=SCRYPT_MAXMEM, dklen=32)
    return hmac.new(secret, b"AM-lock-v1|" + header_bytes, hashlib.sha256).digest()


class _Cipher:
    def __init__(self, key):
        from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
        self._aead = ChaCha20Poly1305(key)

    @staticmethod
    def _parts(index, final):
        return b"AMl1" + index.to_bytes(8, "big"), b"AM-lock-v1|%d|%d" % (index, 1 if final else 0)

    def seal(self, index, data, final=False):
        nonce, aad = self._parts(index, final)
        return self._aead.encrypt(nonce, data, aad)

    def open(self, index, box, final=False):
        from cryptography.exceptions import InvalidTag
        nonce, aad = self._parts(index, final)
        try:
            return self._aead.decrypt(nonce, box, aad)
        except (InvalidTag, ValueError, TypeError):
            return None


def _chunks(size):
    return max(1, math.ceil(size / CHUNK))


def locked_size(size):
    """Bytes of a locked file holding `size` plain bytes (header not counted: <= MAX_HEADER)."""
    return len(MAGIC) + 4 + MAX_HEADER + len(CHECK) + TAG + size + TAG * _chunks(size)


def _free(folder, disk_usage=None):
    try:
        return (disk_usage or shutil.disk_usage)(folder).free
    except OSError:
        return None


def _os_error(e):
    full = getattr(e, "errno", None) in (errno.ENOSPC, errno.EFBIG) or getattr(
        e, "winerror", None) in (39, 112, 223)
    return LockError("disk_full" if full else "file", type(e).__name__)


def _hidden_part(folder, prefix):
    return os.path.join(folder, f"{prefix}{secrets.token_hex(4)}{PART_EXT}")


def _remove(path):
    if path:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError:
            log.warning("Could not remove a temporary file")


# ---------- lock ----------
def lock(plain_path, dest_path, code, progress=None, cancel=None, disk_usage=None):
    """Write the locked copy of plain_path to dest_path (via a hidden part, then renamed).
    Returns the file id (hex). Raises LockError; nothing is left behind on failure."""
    password = _password(code)
    size = os.path.getsize(plain_path)
    folder = os.path.dirname(dest_path) or "."
    os.makedirs(folder, exist_ok=True)
    free = _free(folder, disk_usage)
    need = locked_size(size) + SPARE_DISK
    if free is not None and free < need:
        raise LockError("disk_full", "lock", need=need)
    h = {"v": VERSION, "id": secrets.token_hex(16), "salt": secrets.token_hex(16),
         "n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P, "size": size, "chunk": CHUNK}
    header = json.dumps(h, separators=(",", ":")).encode("ascii")
    cipher = _Cipher(_key(password, header, h))
    chunks = _chunks(size)
    part = _hidden_part(folder, LOCKING_PREFIX)
    try:
        with open(plain_path, "rb") as src, open(part, "xb") as out:
            out.write(MAGIC + struct.pack(">I", len(header)) + header)
            out.write(cipher.seal(0, CHECK))
            done = 0
            if progress:
                progress(0, size)
            for i in range(1, chunks + 1):
                if cancel is not None and cancel.is_set():
                    raise LockError("cancelled")
                data = src.read(CHUNK)
                if i < chunks and len(data) != CHUNK:
                    raise LockError("file", "changed")
                out.write(cipher.seal(i, data, final=(i == chunks)))
                done += len(data)
                if progress:
                    progress(done, size)
            if done != size or src.read(1):
                raise LockError("file", "changed")
            out.flush()
            os.fsync(out.fileno())
        os.replace(part, dest_path)
        part = None
        return h["id"]
    except OSError as e:
        raise _os_error(e) from e
    finally:
        _remove(part)


# ---------- read / unlock ----------
def read_header(path):
    """(header dict, header bytes, offset of the check box). Raises LockError."""
    try:
        with open(path, "rb") as f:
            head = f.read(len(MAGIC) + 4)
            if len(head) != len(MAGIC) + 4 or head[:len(MAGIC)] != MAGIC:
                if head.startswith(b"AMLOCK"):   # ours, from a newer app
                    raise LockError("newer")
                raise LockError("not_locked")
            n = struct.unpack(">I", head[len(MAGIC):])[0]
            if not 10 < n <= MAX_HEADER:
                raise LockError("damaged", "header size")
            raw = f.read(n)
            total = os.fstat(f.fileno()).st_size
    except OSError as e:
        raise LockError("file", type(e).__name__) from e
    if len(raw) != n:
        raise LockError("damaged", "cut off")
    try:
        h = json.loads(raw.decode("ascii"))
    except (ValueError, UnicodeDecodeError) as e:
        raise LockError("damaged", "header") from e
    if not isinstance(h, dict):
        raise LockError("damaged", "header")
    if isinstance(h.get("v"), int) and h.get("v") > VERSION:
        raise LockError("newer")
    size = h.get("size")
    if (h.get("v") != VERSION or not isinstance(h.get("id"), str)
            or not re.fullmatch(r"[0-9a-f]{32}", h["id"])
            or not isinstance(h.get("salt"), str) or not re.fullmatch(r"[0-9a-f]{32}", h["salt"])
            or (h.get("n"), h.get("r"), h.get("p"), h.get("chunk"))
            != (SCRYPT_N, SCRYPT_R, SCRYPT_P, CHUNK)
            or type(size) is not int or not 0 < size <= backup.MAX_TOTAL):
        raise LockError("damaged", "header")
    start = len(MAGIC) + 4 + n
    if total != start + len(CHECK) + TAG + size + TAG * _chunks(size):
        raise LockError("damaged", "cut off")
    return h, raw, start


def file_id(path):
    """The locked file's id (hex) or None (never raises)."""
    try:
        return read_header(path)[0]["id"]
    except LockError:
        return None


def check_code(path, code):
    """True when code opens this locked file (checks the header and the check box only)."""
    try:
        h, raw, start = read_header(path)
        cipher = _Cipher(_key(_password(code), raw, h))
        with open(path, "rb") as f:
            f.seek(start)
            return cipher.open(0, f.read(len(CHECK) + TAG)) == CHECK
    except (LockError, OSError):
        return False


def unlock(path, code, dest_folder, progress=None, cancel=None, disk_usage=None):
    """Write the plain zip of a locked file into dest_folder as a hidden
    ".AddonManager unlocked <hex>.part". Returns its path. Raises LockError (wrong_code
    before anything is written); nothing is left behind on failure."""
    h, raw, start = read_header(path)
    cipher = _Cipher(_key(_password(code), raw, h))
    size, chunks = h["size"], _chunks(h["size"])
    out_path = None
    try:
        with open(path, "rb") as f:
            f.seek(start)
            if cipher.open(0, f.read(len(CHECK) + TAG)) != CHECK:
                raise LockError("wrong_code")
            os.makedirs(dest_folder, exist_ok=True)
            free = _free(dest_folder, disk_usage)
            if free is not None and free < size + SPARE_DISK:
                raise LockError("disk_full", "unlock", need=size + SPARE_DISK)
            out_path = _hidden_part(dest_folder, UNLOCKED_PREFIX)
            with open(out_path, "xb") as out:
                done = 0
                if progress:
                    progress(0, size)
                for i in range(1, chunks + 1):
                    if cancel is not None and cancel.is_set():
                        raise LockError("cancelled")
                    want = (CHUNK if i < chunks else size - (chunks - 1) * CHUNK) + TAG
                    box = f.read(want)
                    data = cipher.open(i, box, final=(i == chunks)) if len(box) == want else None
                    if data is None:
                        raise LockError("damaged", f"piece {i}")
                    out.write(data)
                    done += len(data)
                    if progress:
                        progress(done, size)
            if done != size:
                raise LockError("damaged", "size")
        result, out_path = out_path, None
        return result
    except OSError as e:
        raise _os_error(e) from e
    finally:
        _remove(out_path)


def open_locked(path, code, dest_folder, progress=None, cancel=None, disk_usage=None):
    """unlock() + the received-backup check (backup.read(received=True), as a live receive):
    (plain part path, BackupInfo). A file that fails the check is refused (BackupError) and
    its plain part removed, so a hand-made .amlocked can't carry what a send can't."""
    plain = unlock(path, code, dest_folder, progress, cancel, disk_usage)
    try:
        info = backup.read(plain, received=True)
        if info.kind != "backup":
            raise backup.BackupError("not_backup")
        return plain, info
    except BaseException:
        _remove(plain)
        raise


# ---------- keep a received backup locked ----------
class Kept:
    """A received (or unlocked) backup shown in the Restore window.
    locked: the .amlocked kept copy (None if it couldn't be made: lock_error says why).
    plain: the hidden plain part Restore and Save zip use (removed by discard()).
    code: the code, only right after receiving (shown once; never logged).
    remembered: False when this PC couldn't save the code (the window shows it to write
    down); None when nothing was asked to remember it."""

    def __init__(self, locked, plain, code=None, file_id=None, lock_error=None,
                 remembered=None):
        self.locked, self.plain, self.code = locked, plain, code
        self.file_id, self.lock_error = file_id, lock_error
        self.remembered = remembered
        self.saved_to = None

    def __repr__(self):
        return f"Kept(locked={self.locked!r}, file_id={self.file_id!r})"

    def discard(self):
        """Remove the hidden plain copy (the Restore window closed). Never raises."""
        p, self.plain = self.plain, None
        _remove(p)


def keep(received_zip, code, remember=None, progress=None, now=None):
    """After a receive: lock received_zip (the checked file: a hidden UNLOCKED_PREFIX part
    from share.receive(hidden=True), or a "... received.zip") into "... received.amlocked"
    next to it; the plain file is (or becomes) the hidden plain part.
    remember(file_id, name, size, code) stores the code (am\\codes.py). Never raises: if the
    locked copy can't be made, Kept.lock_error says why and the plain part is still there."""
    folder = os.path.dirname(received_zip)
    hidden = os.path.basename(received_zip).startswith(UNLOCKED_PREFIX)
    plain = received_zip if hidden else _hidden_part(folder, UNLOCKED_PREFIX)
    code = normal_code(code) or code
    locked = fid = error = remembered = None
    try:
        dest = os.path.splitext(received_zip)[0] + EXT
        if hidden or os.path.lexists(dest):   # hidden: name it like a received backup
            dest = backup.received_path(folder, now, ext=EXT)
        fid = lock(received_zip, dest, code, progress)
        locked = dest
        if remember is not None:
            remembered = False
            remembered = bool(remember(fid, os.path.basename(dest), os.path.getsize(dest),
                                       code))
            if not remembered:
                log.warning("The code of the locked copy couldn't be saved")
    except LockError as e:
        error = e
        log.warning("Could not keep a locked copy (%s %s)", e.kind, e.detail)
    except Exception as e:   # noqa: BLE001 (never lose the received file over this)
        error = LockError("file", type(e).__name__)
        log.warning("Could not keep a locked copy (%s)", type(e).__name__)
    if not hidden:
        try:
            os.replace(received_zip, plain)
        except OSError:
            log.warning("Could not hide the received file")
            plain = received_zip
    return Kept(locked, plain, code, fid, error, remembered)


# ---------- Save zip ----------
def _fat32(folder):
    """True when folder is on a FAT32 drive (Windows only; elsewhere EFBIG tells)."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes
        root = ctypes.create_unicode_buffer(260)
        k32 = ctypes.windll.kernel32
        k32.GetVolumePathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        if not k32.GetVolumePathNameW(os.path.abspath(folder), root, 260):
            return False
        fs = ctypes.create_unicode_buffer(32)
        k32.GetVolumeInformationW.argtypes = [
            wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD, ctypes.c_void_p,
            ctypes.c_void_p, ctypes.c_void_p, wintypes.LPWSTR, wintypes.DWORD]
        if not k32.GetVolumeInformationW(root.value, None, 0, None, None, None, fs, 32):
            return False
        return fs.value.upper().startswith("FAT")
    except Exception:
        return False


def save_copy(plain, target, progress=None, cancel=None, disk_usage=None, fat32=None,
              game_check=None):
    """Copy the plain backup to target (via a hidden part next to it, then os.replace: an
    existing file is only replaced by a whole, checked copy). Raises LockError."""
    target = os.path.abspath(target)
    folder = os.path.dirname(target)
    try:
        if os.path.exists(target) and os.path.samefile(plain, target):
            raise LockError("same_file")
    except OSError:
        pass
    if game_check is not None and game_check(folder):
        raise LockError("inside_game")
    size = os.path.getsize(plain)
    if not os.path.isdir(folder):
        raise LockError("file", "no folder")
    free = _free(folder, disk_usage)
    if free is not None and free < size + SPARE_DISK:
        raise LockError("disk_full", "save", need=size + SPARE_DISK)
    if size > FAT32_MAX and (fat32 if fat32 is not None else _fat32(folder)):
        raise LockError("too_big_for_drive")
    part = _hidden_part(folder, SAVING_PREFIX)
    try:
        h_src, done = hashlib.sha256(), 0
        if progress:
            progress(0, size)
        with open(plain, "rb") as src, open(part, "xb") as out:
            while True:
                if cancel is not None and cancel.is_set():
                    raise LockError("cancelled")
                data = src.read(CHUNK)
                if not data:
                    break
                h_src.update(data)
                out.write(data)
                done += len(data)
                if progress:
                    progress(done, size)
            out.flush()
            os.fsync(out.fileno())
        # Checked: the copy has every byte (read back), before anything is replaced.
        h_out = hashlib.sha256()
        with open(part, "rb") as f:
            for block in iter(lambda: f.read(CHUNK), b""):
                h_out.update(block)
        if done != size or h_out.digest() != h_src.digest():
            raise LockError("file", "copy differs")
        os.replace(part, target)
        part = None
        return target
    except OSError as e:
        if getattr(e, "errno", None) == errno.EFBIG:
            raise LockError("too_big_for_drive") from e
        raise _os_error(e) from e
    finally:
        _remove(part)


# ---------- clean-up ----------
OWN_PREFIXES = (UNLOCKED_PREFIX, LOCKING_PREFIX)


def clean_parts(folders, prefixes=OWN_PREFIXES):
    """Remove leftover hidden plain/locking parts (any age): only at start, while this app
    holds the instance lock (no other window can be showing one). Own names, plain files,
    never links. prefixes=(SAVING_PREFIX,) for Save zip's last folder. Returns how many."""
    removed = 0
    for folder in dict.fromkeys(f for f in folders if f):
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for e in entries:
            if not (e.name.startswith(tuple(prefixes)) and e.name.endswith(PART_EXT)):
                continue
            try:
                if e.is_symlink() or not e.is_file(follow_symlinks=False):
                    continue
                os.remove(e.path)
                removed += 1
            except OSError:
                continue
    if removed:
        log.info("Removed %d leftover temporary backup file(s)", removed)
    return removed
