"""Per-path locks whose registry does not retain idle user/file entries."""
import threading
import weakref
from contextlib import contextmanager

_guard = threading.Lock()
_locks = weakref.WeakValueDictionary()


@contextmanager
def path_lock(path):
    key = str(path.resolve())
    with _guard:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _locks[key] = lock
    with lock:
        yield
