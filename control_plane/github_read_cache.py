"""Bounded, process-local validators for authenticated GitHub reads.

Mutable resources always contact GitHub. Credentials and API hosts have separate
namespaces; cache entries never contain credentials. Callers get independent
payloads so their edits cannot change a later provider observation.
"""

from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass
import json
from threading import Lock


@dataclass(frozen=True)
class GitHubCachedRead:
    etag: str
    payload: object
    bytes: int = 0


class GitHubReadCache:
    def __init__(self, *, capacity: int = 2048, max_bytes: int = 16 * 1024 * 1024) -> None:
        self.capacity = capacity
        self.max_bytes = max_bytes
        self._bytes = 0
        self._entries: OrderedDict[tuple[str, str], GitHubCachedRead] = OrderedDict()
        self._lock = Lock()

    def get(self, namespace: str, path: str) -> GitHubCachedRead | None:
        with self._lock:
            key = (namespace, path)
            entry = self._entries.get(key)
            if entry is None:
                return None
            self._entries.move_to_end(key)
            return deepcopy(entry)

    def put(self, namespace: str, path: str, *, etag: str, payload: object) -> None:
        size = len(json.dumps(payload).encode()) + len(path.encode()) + len(etag.encode())
        with self._lock:
            key = (namespace, path)
            previous = self._entries.pop(key, None)
            if previous is not None:
                self._bytes -= previous.bytes
            if size > self.max_bytes:
                return
            self._entries[key] = GitHubCachedRead(etag=etag, payload=deepcopy(payload), bytes=size)
            self._bytes += size
            self._entries.move_to_end(key)
            while len(self._entries) > self.capacity or self._bytes > self.max_bytes:
                _, evicted = self._entries.popitem(last=False)
                self._bytes -= evicted.bytes

    def discard(self, namespace: str, path: str) -> None:
        with self._lock:
            removed = self._entries.pop((namespace, path), None)
            if removed is not None:
                self._bytes -= removed.bytes


MERGE_TRAIN_READ_CACHE = GitHubReadCache()
