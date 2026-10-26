"""The data mount path supported by fresh conventional application allocation."""

from pathlib import PurePosixPath
import re


def require_application_state_path(path: str) -> None:
    protected = ("/proc", "/sys", "/dev", "/etc", "/run")
    if (
        not re.fullmatch(r"/[A-Za-z0-9_./-]+", path)
        or str(PurePosixPath(path)) != path
        or ".." in PurePosixPath(path).parts
        or path == "/"
        or any(path == prefix or path.startswith(prefix + "/") for prefix in protected)
    ):
        raise ValueError("State storage requires a supported application data directory.")
