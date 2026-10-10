"""Bounded, process-local validators for authenticated GitHub reads.

Mutable resources always contact GitHub. Credentials and API hosts have separate
namespaces; cache entries never contain credentials. Callers get independent
payloads so their edits cannot change a later provider observation.
"""

from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
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
        self._token_scopes: OrderedDict[str, tuple[str, datetime]] = OrderedDict()
        self._lock = Lock()

    def register_token_scope(self, token: str, *, scope: str, expires_at: datetime) -> None:
        """Bind an already validated native App token without retaining the secret."""
        digest = sha256(token.encode()).hexdigest()
        with self._lock:
            self._token_scopes[digest] = (scope, expires_at)
            self._token_scopes.move_to_end(digest)
            while len(self._token_scopes) > self.capacity:
                self._token_scopes.popitem(last=False)

    def namespace(self, *, token: str, api_origin: str) -> str:
        digest = sha256(token.encode()).hexdigest()
        scope = "credential:" + digest
        with self._lock:
            registered = self._token_scopes.get(digest)
            if registered is not None:
                fingerprint, expires_at = registered
                if expires_at > datetime.now(timezone.utc):
                    scope = "validated-native-installation:" + fingerprint
                else:
                    del self._token_scopes[digest]
        return sha256(f"{api_origin}\0{scope}".encode()).hexdigest()

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
