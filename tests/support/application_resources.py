import base64
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
from typing import Any, cast
from unittest.mock import patch

from control_plane.dokploy import native_copy_stream


class ApplicationResourceFixture:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.data = self.root / "volumes" / "owned-state" / "_data"
        self.data.mkdir(parents=True)
        self.application = "owned-application"
        self.image = "registry.invalid/example@sha256:" + "a" * 64
        self.nodes = [{"ID": "node1", "Self": True}]
        self.service: dict[str, Any] = {
            "ID": "service1",
            "Version": 7,
            "Name": self.application,
            "Mode": {"Replicated": {"Replicas": 1}},
            "Mounts": [{"Type": "volume", "Source": "owned-state"}],
            "Image": self.image,
            "Update": {"State": "rollback_completed"},
        }
        self.tasks: dict[str, dict[str, Any]] = {
            "task1": {
                "ID": "task1",
                "ServiceID": self.service["ID"],
                "NodeID": "node1",
                "DesiredState": "running",
                "Status": {"State": "pending"},
                "Mounts": self.service["Mounts"],
            }
        }
        self.containers: dict[str, dict[str, Any]] = {}
        self.volumes: list[str] = ["owned-state"]
        self.volume: dict[str, Any] = {
            "Name": "owned-state",
            "Driver": "local",
            "Options": None,
            "Mountpoint": str(self.data),
        }
        self.image_row: dict[str, Any] = {
            "Id": "sha256:" + "b" * 64,
            "RepoDigests": [self.image],
            "Volumes": {"/state": {}},
        }
        self.commands: list[list[str]] = []
        self.container_list_calls = 0
        self.change_containers = False
        self.denied = False
        self.image_error: bytes | None = None

    def docker(self, command: list[str], **_arguments: Any) -> subprocess.CompletedProcess[bytes]:
        arguments = command[1:]
        self.commands.append(arguments)
        if self.denied:
            return subprocess.CompletedProcess(
                command, 1, b"", b"authorization denied by plugin: synthetic-private-detail"
            )
        if arguments[:2] == ["node", "ls"]:
            output = "\n".join(json.dumps(row) for row in self.nodes)
        elif arguments[:2] == ["service", "inspect"]:
            output = json.dumps(self.service)
        elif arguments[:2] == ["service", "ps"]:
            output = "\n".join(self.tasks)
        elif arguments[0] == "ps":
            self.container_list_calls += 1
            output = "\n".join(self.containers)
            if self.change_containers and self.container_list_calls > 1:
                output += "\nnew-container"
        elif arguments[:2] == ["volume", "ls"]:
            output = "\n".join(self.volumes)
        elif arguments[:2] == ["volume", "inspect"]:
            output = json.dumps(self.volume)
        elif arguments[:2] == ["image", "inspect"]:
            if self.image_error is not None:
                return subprocess.CompletedProcess(command, 1, b"", self.image_error)
            output = json.dumps(self.image_row)
        elif arguments[0] == "info":
            output = json.dumps(str(self.root))
        elif arguments[0] == "inspect":
            identifier = arguments[-1]
            output = json.dumps(
                self.tasks[identifier] if identifier in self.tasks else self.containers[identifier]
            )
        else:
            raise AssertionError(f"Unexpected Docker operation: {arguments}")
        return subprocess.CompletedProcess(command, 0, output.encode(), b"")

    def run(self, program: str, payload: dict[str, object]) -> dict[str, Any]:
        encoded = base64.b64encode(json.dumps(payload).encode()).decode()
        output = io.StringIO()
        with (
            patch.object(sys, "argv", ["resource-observation", encoded, "frame:"]),
            patch.object(subprocess, "run", side_effect=self.docker),
            redirect_stdout(output),
        ):
            try:
                exec(compile(program, "<resource-observation>", "exec"), {})
            except SystemExit as error:
                if error.code != 0:
                    raise
        for command in self.commands:
            if command[0] in {"inspect", "ps", "info"}:
                continue
            if tuple(command[:2]) not in {
                ("node", "ls"),
                ("service", "inspect"),
                ("service", "ps"),
                ("volume", "ls"),
                ("volume", "inspect"),
                ("image", "inspect"),
            }:
                raise AssertionError(f"Provider write attempted: {command}")
        lines = output.getvalue().splitlines()
        if len(lines) != 1 or not lines[0].startswith("frame:"):
            raise AssertionError("The fixed program did not emit one bounded frame.")
        return cast(dict[str, Any], json.loads(lines[0].removeprefix("frame:")))

    def native_read(self, **arguments: Any) -> None:
        frame = self.run(arguments["program"], arguments["payload"])
        if frame["type"] == "denied":
            raise native_copy_stream.NativeSourceAuthorizationDenied(
                "The provider refused the fixture read."
            )
        arguments["receive"](frame)
