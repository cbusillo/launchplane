"""Cross-process exclusion for a testing worker and its recovery inspector."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
import fcntl
import hashlib
from pathlib import Path


@contextmanager
def file_execution_lock(root: Path, operation_id: str) -> Iterator[Callable[[], None] | None]:
    root.mkdir(parents=True, exist_ok=True)
    path = root / (hashlib.sha256(operation_id.encode()).hexdigest() + ".lock")
    with path.open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield None
            return
        try:
            yield lambda: None
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
