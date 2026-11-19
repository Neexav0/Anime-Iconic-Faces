"""Photos of unconfirmed drafts. Held in memory only, never written to disk.

A photo is dropped when its draft is confirmed or discarded, after TTL seconds, or (oldest first)
when the store would grow past MAX_BYTES. So an abandoned draft can no longer leak memory.
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict

TTL = 30 * 60
MAX_BYTES = 200 * 1024 * 1024


class PhotoStore:
    def __init__(self, ttl: float = TTL, max_bytes: int = MAX_BYTES, clock=time.monotonic):
        self.ttl, self.max_bytes, self._clock = ttl, max_bytes, clock
        self._items: OrderedDict[int, tuple[bytes, str, float]] = OrderedDict()  # insertion order = age
        self._bytes = 0
        self._lock = threading.Lock()

    def _drop(self, key: int) -> None:
        data, _, _ = self._items.pop(key)
        self._bytes -= len(data)

    def _prune(self) -> None:
        cutoff = self._clock() - self.ttl
        while self._items:
            key, (_, _, ts) = next(iter(self._items.items()))
            if ts >= cutoff:
                break
            self._drop(key)

    def put(self, key: int, data: bytes, mime: str) -> None:
        with self._lock:
            if key in self._items:
                self._drop(key)
            self._prune()
            while self._items and self._bytes + len(data) > self.max_bytes:
                self._drop(next(iter(self._items)))
            if len(data) <= self.max_bytes:
                self._items[key] = (data, mime, self._clock())
                self._bytes += len(data)

    def get(self, key: int) -> tuple[bytes, str] | None:
        with self._lock:
            self._prune()
            hit = self._items.get(key)
            return (hit[0], hit[1]) if hit else None

    def __contains__(self, key: int) -> bool:
        return self.get(key) is not None

    def pop(self, key: int, default=None) -> None:
        """Drop a photo if held (dict-style signature, so `pop(id, None)` works)."""
        with self._lock:
            if key in self._items:
                self._drop(key)

    @property
    def size(self) -> tuple[int, int]:
        """(photos, bytes) currently held."""
        with self._lock:
            self._prune()
            return len(self._items), self._bytes
