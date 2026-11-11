"""One open window gets the files: when the app is already open and a file is dropped on
the program's icon (or opened with "Open with"), the second start hands the file to the
open window and quits, instead of opening a second window.

How: the open app holds a lock on <home>\\instance.lock (msvcrt on Windows, flock on
Linux; the system frees it when the app ends, even after a crash). A second start that
can't get the lock writes the path into <home>\\open-requests\\ and exits; the open app
picks the requests up in its poll loop. Only paths are passed; the open window still
checks everything and asks before installing."""

import logging
import os
import threading
import time
import uuid

log = logging.getLogger(__name__)

LOCK_NAME = "instance.lock"
REQUEST_DIR = "open-requests"
MAX_AGE = 120          # seconds: older requests (the app was closing) are dropped
MAX_PATH_BYTES = 4096


class Lock:
    """The single-window lock. acquire() -> True if this run holds it."""

    def __init__(self, home):
        self.path = os.path.join(home, LOCK_NAME)
        self.handle = None

    @property
    def held(self):
        return self.handle is not None

    def acquire(self):
        if self.handle is not None:
            return True
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            f = open(self.path, "a+b")
        except OSError:
            log.info("No instance lock (can't open it)")
            return False
        try:
            if os.name == "nt":
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            return False
        self.handle = f
        return True

    def release(self):
        f, self.handle = self.handle, None
        if f is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        f.close()


def _request_dir(home):
    return os.path.join(home, REQUEST_DIR)


_stamp_lock = threading.Lock()
_last_stamp = 0


def _next_stamp():
    """Wall-clock time in ns, strictly rising within this process, so requests sent in
    the same instant (or with a coarse clock) still sort in the order they were sent."""
    global _last_stamp
    with _stamp_lock:
        _last_stamp = max(time.time_ns(), _last_stamp + 1)
        return _last_stamp


def _sort_key(entry):
    """Arrival order: the time in the name (new "<20-digit ns>-<hex>" or old
    "<seconds.mmm>-<hex>"), then the name. Unreadable names fall back to the file time."""
    head = entry.name.split("-", 1)[0]
    try:
        if head.isdigit():
            stamp = int(head)
        else:
            stamp = int(round(float(head) * 1_000_000_000))
    except (ValueError, OverflowError):
        try:
            stamp = entry.stat(follow_symlinks=False).st_mtime_ns
        except OSError:
            stamp = 0
    return (stamp, entry.name)


def send_request(home, path):
    """Hand a path to the open window (written atomically). True if written."""
    data = os.path.abspath(path).encode("utf-8")
    if len(data) > MAX_PATH_BYTES:
        return False
    folder = _request_dir(home)
    name = f"{_next_stamp():020d}-{uuid.uuid4().hex}"
    try:
        os.makedirs(folder, exist_ok=True)
        tmp = os.path.join(folder, name + ".part")
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, os.path.join(folder, name + ".txt"))
        return True
    except OSError:
        log.exception("Could not hand the file to the open window")
        return False


def take_requests(home, now=None):
    """Paths handed over since the last call, oldest first; every request file is removed.
    Requests older than MAX_AGE are dropped. Never raises."""
    folder = _request_dir(home)
    now = time.time() if now is None else now
    out = []
    try:
        entries = sorted(os.scandir(folder), key=_sort_key)
    except (OSError, ValueError):
        return out
    for e in entries:
        if not e.name.endswith((".txt", ".part")):
            continue
        try:
            st = e.stat(follow_symlinks=False)
            if e.name.endswith(".part"):
                if now - st.st_mtime > MAX_AGE:
                    os.remove(e.path)
                continue
            data = b""
            if e.is_file(follow_symlinks=False) and st.st_size <= MAX_PATH_BYTES:
                with open(e.path, "rb") as f:
                    data = f.read(MAX_PATH_BYTES)
            os.remove(e.path)
        except OSError:
            continue
        if now - st.st_mtime > MAX_AGE:
            continue
        try:
            path = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if path and "\0" not in path and os.path.isabs(path):
            out.append(path)
    return out
