import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile


def private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, exist_ok=True)
    metadata = path.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o022
    ):
        raise ValueError("Host package directory is not private")


def regular_bytes(path: Path, bound: int) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as handle:
        metadata = os.fstat(handle.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
            raise ValueError("Host package file is not private")
        value = handle.read(bound + 1)
    if len(value) > bound:
        raise ValueError("Host package file exceeds its bound")
    return value


def install(spec: dict[str, object]) -> dict[str, object]:
    version = spec["version"]
    if not isinstance(version, str) or not re.fullmatch(r"[a-f0-9]{64}", version):
        raise ValueError("Invalid host package version")
    home = Path.home().resolve(strict=True)
    for directory in (
        home / ".local",
        home / ".local/state",
        home / ".local/state/launchplane-host",
    ):
        private_directory(directory)
    root = home / ".local/state/launchplane-host"
    destination = root / (version + ".pyz")
    if destination.exists() or destination.is_symlink():
        if hashlib.sha256(regular_bytes(destination, 4 * 1024 * 1024)).hexdigest() != version:
            raise ValueError("Installed host package differs")
        return {"type": "complete", "installed": True, "version": version}
    if spec["mode"] == "inspect":
        return {"type": "complete", "installed": False, "version": version}
    staging = root / version
    private_directory(staging)
    descriptor = os.open(
        staging / "lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600
    )
    with os.fdopen(descriptor, "rb") as lock:
        if not stat.S_ISREG(os.fstat(lock.fileno()).st_mode):
            raise ValueError("Host package lock is not a regular file")
        fcntl.flock(lock, fcntl.LOCK_EX)
        if spec["mode"] == "chunk":
            index = spec["index"]
            if type(index) is not int or not 0 <= index < 128:
                raise ValueError("Invalid host package chunk")
            encoded = spec["content"]
            if not isinstance(encoded, str):
                raise ValueError("Invalid host package chunk")
            content = base64.b64decode(encoded, validate=True)
            if not 0 < len(content) <= 48 * 1024:
                raise ValueError("Invalid host package chunk")
            path = staging / str(index)
            if path.exists() or path.is_symlink():
                if regular_bytes(path, 48 * 1024) != content:
                    raise ValueError("Host package chunk differs")
            else:
                publish_bytes(path, content)
            return {"type": "complete", "installed": False, "version": version}
        if spec["mode"] != "commit":
            raise ValueError("Invalid host package installation mode")
        count = spec["count"]
        if type(count) is not int or not 0 < count <= 128:
            raise ValueError("Invalid host package chunk count")
        content = b"".join(regular_bytes(staging / str(index), 48 * 1024) for index in range(count))
        if len(content) > 4 * 1024 * 1024 or hashlib.sha256(content).hexdigest() != version:
            raise ValueError("Host package integrity failed")
        publish_bytes(destination, content)
    return {"type": "complete", "installed": True, "version": version}


def publish_bytes(destination: Path, content: bytes) -> None:
    with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)


def main() -> None:
    spec = json.loads(base64.b64decode(sys.argv[1], validate=True))
    prefix = sys.argv[2]
    try:
        result = install(spec)
    except (OSError, ValueError, KeyError, TypeError):
        print(prefix + json.dumps({"type": "failed"}), flush=True)
        raise SystemExit(1) from None
    print(prefix + json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
