"""This PC's codes ("Your codes" in the Backup window), kept in one ENCRYPTED file
`codes.amkeys` in the app's home folder: never settings.json, never the log.

- sent: send-later codes this PC sent that may still work (code, channel, the sender's relay
  token, the host, the Send servers' file ids + owner tokens to delete them, end times, the
  last known state). Removed once they stop working, after Stop, or after a pickup's end.
- locked: the codes of the locked kept copies of received backups (am\\backup_lock.py), by
  the file's random id (+ its name and size to mark it in lists). Kept until Forget, or until
  that locked copy is removed (Save zip, Old backups...).

The file: MAGIC, header length, header JSON (how the key is made), a 12-byte nonce and the
AES-GCM sealed JSON (the header is the extra data). Where the key comes from:
- Windows: a random key protected with DPAPI (CryptProtectData, tied to the Windows user's
  login), kept in the header.
- Linux / Steam Deck: HKDF-SHA256 of /etc/machine-id + the user id + user name (salt in the
  header): a copied file is useless on another PC or for another user. Without a machine id: a
  random key file in $XDG_DATA_HOME/AddonManager (mode 0600, another folder than this file).
The file is 0600 on Linux. This keeps the codes from casual reading and from copies of the file
(cloud sync, backup tools, a copied folder); it does NOT protect against software running as
this user on this PC (it can do what the app does).

A file that can't be opened (machine id changed by a reinstall, damaged) is moved aside as
codes.amkeys.old and a new, empty store starts. Never raises into the window; logs no code."""

import base64
import json
import logging
import os
import secrets
import struct
import threading
import time

from am import config

log = logging.getLogger(__name__)

STORE_NAME = "codes.amkeys"
MAGIC = b"AMKEYS1\n"
VERSION = 1
MAX_HEADER = 2048
MAX_FILE = 1024 * 1024
MAX_SENT = 50
MAX_LOCKED = 200
MACHINE_ID_PATHS = ("/etc/machine-id", "/var/lib/dbus/machine-id")
KEY_FILE_NAME = "codes.key"
SENT_STATES = ("uploading", "ready", "taken", "stopped", "burned", "ended", "unknown")
_LOCK = threading.RLock()


def store_path(home=None):
    return os.path.join(home or config.home_dir(), STORE_NAME)


# ---------- keys ----------
def _dpapi(data, protect):
    """CryptProtectData / CryptUnprotectData (current user, no UI). Raises OSError."""
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]
    buf = ctypes.create_string_buffer(data, len(data))
    src = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = Blob()
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                   ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    fn.restype = wintypes.BOOL
    entropy = b"AddonManager codes v1"
    ebuf = ctypes.create_string_buffer(entropy, len(entropy))
    eblob = Blob(len(entropy), ctypes.cast(ebuf, ctypes.POINTER(ctypes.c_char)))
    CRYPTPROTECT_UI_FORBIDDEN = 0x1
    if not fn(ctypes.byref(src), None, ctypes.byref(eblob), None, None,
              CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out)):
        raise OSError("DPAPI failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


def _machine_id(paths=None):
    for p in paths or MACHINE_ID_PATHS:
        try:
            with open(p, "r", encoding="ascii") as f:
                text = f.read(100).strip()
            if len(text) >= 16:
                return text
        except (OSError, UnicodeDecodeError):
            continue
    return None


def _user():
    uid = os.getuid() if hasattr(os, "getuid") else -1
    try:
        import getpass
        name = getpass.getuser()
    except Exception:
        name = ""
    return f"{uid}|{name}"


def _hkdf(ikm, salt, info):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=info).derive(ikm)


def _data_home():
    base = os.environ.get("XDG_DATA_HOME", "")
    if not os.path.isabs(base):
        base = os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "AddonManager")


def _key_file(folder, create):
    """The random key of a PC without a machine id (0600, outside the codes folder)."""
    path = os.path.join(folder, KEY_FILE_NAME)
    try:
        with open(path, "rb") as f:
            key = f.read(64)
        if len(key) == 32:
            return key
    except FileNotFoundError:
        pass
    except OSError as e:   # there but not readable now: never treated as "no key"
        raise Unreadable("key file") from e
    if not create:
        return None
    os.makedirs(folder, exist_ok=True)
    key = secrets.token_bytes(32)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key)
    os.replace(tmp, path)
    return key


class Keys:
    """Where the key comes from. Tests pass their own (kind, machine id paths, key folder)."""

    def __init__(self, kind=None, machine_paths=None, key_folder=None):
        self.kind = kind or ("dpapi" if os.name == "nt" else "machine")
        self.machine_paths = machine_paths
        self.key_folder = key_folder

    def new(self):
        """(header dict, key) for a new write."""
        if self.kind == "dpapi":
            key = secrets.token_bytes(32)
            return {"v": VERSION, "kind": "dpapi",
                    "wrapped": base64.b64encode(_dpapi(key, True)).decode("ascii")}, key
        salt = secrets.token_bytes(16)
        if self.kind == "machine":
            mid = _machine_id(self.machine_paths)
            if mid:
                return {"v": VERSION, "kind": "machine", "salt": salt.hex()}, self._machine(
                    mid, salt)
        key = _key_file(self.key_folder or _data_home(), create=True)
        return {"v": VERSION, "kind": "keyfile", "salt": salt.hex()}, _hkdf(
            key, salt, b"AM-codes-v1|keyfile")

    @staticmethod
    def _machine(mid, salt):
        return _hkdf(f"{mid}|{_user()}".encode("utf-8"), salt, b"AM-codes-v1|machine")

    def of(self, h):
        """The key for a stored header. Raises ValueError/OSError when it can't be made."""
        kind = h.get("kind")
        if kind == "dpapi":
            return _dpapi(base64.b64decode(h["wrapped"]), False)
        salt = bytes.fromhex(h["salt"])
        if kind == "machine":
            mid = _machine_id(self.machine_paths)
            if not mid:
                raise ValueError("no machine id")
            return self._machine(mid, salt)
        if kind == "keyfile":
            key = _key_file(self.key_folder or _data_home(), create=False)
            if key is None:
                raise ValueError("no key file")
            return _hkdf(key, salt, b"AM-codes-v1|keyfile")
        raise ValueError("kind")


KEYS = Keys()


# ---------- the file ----------
class Unreadable(Exception):
    """The codes file (or its key file) is there but can't be read right now (in use,
    permissions): nothing may be saved over it."""


READ_TRIES = 3


def _read_raw(path):
    with open(path, "rb") as f:
        return f.read(MAX_FILE + 1)


def _empty():
    return {"v": VERSION, "sent": [], "locked": {}}


def _clean(data):
    """Only well-formed entries (a damaged or hand-edited store never breaks the window)."""
    out = _empty()
    if not isinstance(data, dict):
        return out
    for e in data.get("sent") or []:
        if (isinstance(e, dict) and isinstance(e.get("id"), str) and isinstance(e.get("code"), str)
                and type(e.get("ch")) is int and isinstance(e.get("token"), str)
                and isinstance(e.get("end"), (int, float))):
            out["sent"].append(e)
    locked = data.get("locked")
    if isinstance(locked, dict):
        for fid, e in locked.items():
            if isinstance(fid, str) and isinstance(e, dict) and isinstance(e.get("code"), str):
                out["locked"][fid] = e
    out["sent"] = out["sent"][-MAX_SENT:]
    if len(out["locked"]) > MAX_LOCKED:
        keep = sorted(out["locked"].items(), key=lambda kv: kv[1].get("saved", 0))[-MAX_LOCKED:]
        out["locked"] = dict(keep)
    return out


def _set_aside(path):
    try:
        os.replace(path, path + ".old")
        log.warning("The codes file couldn't be opened here; it was set aside")
    except OSError:
        log.warning("The codes file couldn't be opened here")


def load(home=None, keys=None, strict=False):
    """The store (dict). A missing file = empty. A file that truly can't be decrypted
    (another PC/user, damaged) is set aside as .old. A file that can't be READ now (in use,
    permissions) is never touched: strict=True raises Unreadable (callers that save
    afterwards), else an empty store is returned for showing only."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    path = store_path(home)
    with _LOCK:
        raw = None
        for attempt in range(READ_TRIES):
            try:
                raw = _read_raw(path)
                break
            except FileNotFoundError:
                return _empty()
            except OSError as e:
                if attempt + 1 < READ_TRIES:
                    time.sleep(0.05 * (attempt + 1))
                    continue
                log.warning("Could not read the codes file (%s)", type(e).__name__)
                if strict:
                    raise Unreadable("codes file") from e
                return _empty()
        try:
            if len(raw) > MAX_FILE or raw[:len(MAGIC)] != MAGIC:
                raise ValueError("not ours")
            n = struct.unpack(">I", raw[len(MAGIC):len(MAGIC) + 4])[0]
            if not 10 < n <= MAX_HEADER:
                raise ValueError("header")
            head = raw[len(MAGIC) + 4:len(MAGIC) + 4 + n]
            h = json.loads(head.decode("ascii"))
            if not isinstance(h, dict) or h.get("v") != VERSION:
                raise ValueError("header")
            rest = raw[len(MAGIC) + 4 + n:]
            nonce, sealed = rest[:12], rest[12:]
            key = (keys or KEYS).of(h)
            plain = AESGCM(key).decrypt(nonce, sealed, raw[:len(MAGIC) + 4 + n])
            return _clean(json.loads(plain.decode("utf-8")))
        except Unreadable:
            log.warning("Could not read the codes key file")
            if strict:
                raise
            return _empty()
        except Exception as e:   # noqa: BLE001 (wrong PC/user, damaged: start again)
            log.info("Codes file not usable (%s)", type(e).__name__)
            _set_aside(path)
            return _empty()


def save(data, home=None, keys=None):
    """Write the store atomically (0600 on Linux). False (logged) if it can't."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    path = store_path(home)
    with _LOCK:
        try:
            h, key = (keys or KEYS).new()
            head = json.dumps(h, separators=(",", ":")).encode("ascii")
            prefix = MAGIC + struct.pack(">I", len(head)) + head
            nonce = secrets.token_bytes(12)
            body = json.dumps(_clean(data), separators=(",", ":")).encode("utf-8")
            blob = prefix + nonce + AESGCM(key).encrypt(nonce, body, prefix)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(blob)
                f.flush()
                os.fsync(f.fileno())
            if os.name != "nt":
                os.chmod(tmp, 0o600)
            os.replace(tmp, path)
            return True
        except Exception as e:   # noqa: BLE001
            log.warning("Could not save the codes file (%s)", type(e).__name__)
            return False


def _change(fn, home=None, keys=None):
    """Load, change, save. If the file can't be read right now nothing is saved (it would
    overwrite every remembered code with an empty list): returns None, logged. None too
    when the save itself failed (nothing was kept)."""
    with _LOCK:
        try:
            data = load(home, keys, strict=True)
        except Unreadable:
            log.warning("Codes file not changed: it can't be read right now")
            return None
        out = fn(data)
        if not save(data, home, keys):
            return None
        return out


# ---------- sent codes ----------
def prune(data, now=None):
    """Drop sent codes past their end. True if something went."""
    now = time.time() if now is None else now
    before = len(data["sent"])
    data["sent"] = [e for e in data["sent"] if e.get("end", 0) > now]
    return len(data["sent"]) != before


def sent(home=None, keys=None, now=None, strict=False):
    """The sent codes that may still work (pruned; saved if something went). A file that
    can't be read right now gives [], or raises Unreadable with strict=True."""
    with _LOCK:
        try:
            data = load(home, keys, strict=True)
        except Unreadable:
            if strict:
                raise
            return []
        if prune(data, now):
            save(data, home, keys)
        return list(data["sent"])


def add_sent(code, channel, token, host, end, hours, parts=(), home=None, keys=None,
             now=None):
    """Remember a send-later code this PC sent (until `end`, epoch seconds). parts: the Send
    servers' [{"id", "owner"}] to delete them on Stop. Returns its id."""
    entry = {"id": secrets.token_hex(4), "code": code, "ch": int(channel), "token": token,
             "host": host, "end": float(end), "hours": int(hours),
             "made": time.time() if now is None else now,
             "parts": [{"id": p["id"], "owner": p["owner"]} for p in parts],
             "state": "ready", "state_at": None}

    def add(data):
        prune(data, now)
        data["sent"].append(entry)
        return entry["id"]
    return _change(add, home, keys)


def update_sent(entry_id, home=None, keys=None, **fields):
    def upd(data):
        for e in data["sent"]:
            if e["id"] == entry_id:
                e.update(fields)
                return True
        return False
    return _change(upd, home, keys)


def remove_sent(entry_id, home=None, keys=None):
    def rm(data):
        data["sent"] = [e for e in data["sent"] if e["id"] != entry_id]
    _change(rm, home, keys)


# ---------- locked kept copies ----------
def remember_lock(file_id, name, size, code, home=None, keys=None, now=None):
    """True once the code is saved; None if the codes file couldn't be read or saved."""
    def add(data):
        data["locked"][file_id] = {"code": code, "name": name, "size": int(size),
                                   "saved": time.time() if now is None else now}
        return True
    return _change(add, home, keys)


def lock_code(file_id, home=None, keys=None):
    """The remembered code of a locked file, or None."""
    if not file_id:
        return None
    e = load(home, keys)["locked"].get(file_id)
    return e["code"] if e else None


def forget_lock(file_id, home=None, keys=None):
    def rm(data):
        data["locked"].pop(file_id, None)
    if file_id:
        _change(rm, home, keys)


def forget_named(name, size, home=None, keys=None):
    """Forget the code of a locked file that was deleted (by name + size: the file is gone,
    or must not be opened just for this)."""
    def rm(data):
        for fid in [k for k, e in data["locked"].items()
                    if e.get("name") == name and e.get("size") == size]:
            data["locked"].pop(fid, None)
    _change(rm, home, keys)


def locked(home=None, keys=None):
    """{file id: {"code", "name", "size", "saved"}}."""
    return dict(load(home, keys)["locked"])


def known_lock(name, size, home=None, keys=None):
    """True when this PC remembers a code for a locked file of this name and size (marks
    rows in lists without opening the file)."""
    for e in load(home, keys)["locked"].values():
        if e.get("name") == name and e.get("size") == size:
            return True
    return False
