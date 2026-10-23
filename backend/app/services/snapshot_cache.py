"""Bounded snapshot cache: one producer per key, computation outside the lock."""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Hashable
from concurrent.futures import Future


class SnapshotCache:
    def __init__(self, ttl: float, maxsize: int) -> None:
        self.ttl, self.maxsize = ttl, maxsize
        self._lock = threading.Lock()
        self._epoch = 0
        self._values: OrderedDict = OrderedDict()
        self._flights: dict[tuple, Future] = {}

    def clear(self) -> None:
        with self._lock:
            self._epoch += 1
            self._values.clear()

    def get_or_compute(self, key: Hashable, build: Callable, *, cache_if: Callable | None = None):
        with self._lock:
            entry = self._values.get(key)
            if entry is not None and time.monotonic() < entry[0]:
                self._values.move_to_end(key)
                return entry[1]
            self._values.pop(key, None)
            flight_key = (self._epoch, key)
            future = self._flights.get(flight_key)
            leader = future is None
            if leader:
                future = self._flights[flight_key] = Future()
        if not leader:
            return future.result()
        try:
            result = build()
            cacheable = cache_if is None or cache_if(result)
            with self._lock:
                # A clear during computation must not republish the old snapshot.
                if cacheable and self._epoch == flight_key[0]:
                    self._values[key] = (time.monotonic() + self.ttl, result)
                    self._values.move_to_end(key)
                    while len(self._values) > self.maxsize:
                        self._values.popitem(last=False)
            future.set_result(result)
            return result
        except BaseException as exc:
            future.set_exception(exc)
            raise
        finally:
            with self._lock:
                self._flights.pop(flight_key, None)
