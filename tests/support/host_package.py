from pathlib import Path
import shlex
import sys
from unittest.mock import patch

from control_plane.host_package import host_package


def execute_host_schedule(script: str) -> None:
    from control_plane.host_package_runtime import main

    arguments = shlex.split(script.splitlines()[-1])
    path = Path(arguments[2].replace("$HOME", str(Path.home())))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(host_package().content)
    with patch.object(sys, "argv", [str(path), *arguments[3:]]):
        main()
