from pathlib import Path
import io
import shlex
import sys
from unittest.mock import patch

from control_plane.host_package import host_package, HOST_SCHEDULE_HEADER, HOST_INPUT_DELIMITER


def execute_host_schedule(script: str) -> None:
    from control_plane.host_package_runtime import main

    command, _, payload = script[len(HOST_SCHEDULE_HEADER) :].partition(
        " <<'" + HOST_INPUT_DELIMITER + "'\n"
    )
    arguments = shlex.split(command)
    path = Path(arguments[2].replace("$HOME", str(Path.home())))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(host_package().content)
    with (
        patch.object(sys, "argv", [str(path), *arguments[3:]]),
        patch.object(
            sys, "stdin", io.StringIO(payload.rsplit("\n" + HOST_INPUT_DELIMITER + "\n", 1)[0])
        ),
    ):
        main()
