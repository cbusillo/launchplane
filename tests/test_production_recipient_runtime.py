"""Actual Docker metadata must enforce the reviewed artifact, data and limits."""

import base64
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
import subprocess
import sys
from typing import Any
import unittest
from unittest.mock import patch

from control_plane.production_recipient_cutover import RUNTIME_PROGRAM


class RecipientRuntimeProgramTests(unittest.TestCase):
    def setUp(self) -> None:
        self.spec: dict[str, Any] = {
            "project": "private-recipient",
            "application": "web",
            "image": "web@sha256:" + "a" * 64,
            "services": {
                "web": {
                    "cpus": 1,
                    "memory": 2048,
                    "image": "web@sha256:" + "a" * 64,
                    "command": [],
                    "check_command": False,
                    "volumes": {"/config": "independent-config"},
                    "ports": {"8000/tcp": "19220"},
                },
                "database": {
                    "cpus": 1,
                    "memory": 2048,
                    "image": "mariadb@sha256:" + "b" * 64,
                    "command": ["--innodb-buffer-pool-size=512M"],
                    "check_command": True,
                    "volumes": {"/var/lib/mysql": "independent-data"},
                    "ports": {"3306/tcp": "19219"},
                },
            },
        }
        self.rows: dict[str, Any] = {
            name: {
                "Image": name,
                "State": {"Running": True},
                "Config": {"Image": row["image"], "Cmd": row["command"]},
                "HostConfig": {
                    "NanoCpus": 1000000000,
                    "Memory": 2048,
                    "PortBindings": {
                        key: [{"HostPort": value}] for key, value in row["ports"].items()
                    },
                },
                "Mounts": [
                    {"Type": "volume", "Destination": key, "Name": value}
                    for key, value in row["volumes"].items()
                ],
            }
            for name, row in self.spec["services"].items()
        }

    def docker(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if args[1] == "ps":
            output = args[-1].split("=")[-1]
        elif args[1] == "inspect":
            output = json.dumps([self.rows[args[-1]]])
        elif args[1:3] == ["image", "inspect"]:
            output = json.dumps([{"RepoDigests": [self.spec["services"][args[-1]]["image"]]}])
        else:
            self.fail("Only read-only Docker metadata is expected")
        return subprocess.CompletedProcess(args, 0, stdout=output, stderr="")

    def run_program(self) -> None:
        with (
            patch.object(
                sys, "argv", ["runtime", base64.b64encode(json.dumps(self.spec).encode()).decode()]
            ),
            patch("subprocess.run", side_effect=self.docker),
            redirect_stdout(io.StringIO()),
        ):
            exec(compile(RUNTIME_PROGRAM, "<runtime>", "exec"), {})

    def test_actual_runtime_accepts_only_reviewed_images_limits_storage_and_ports(self) -> None:
        self.run_program()
        original = deepcopy(self.rows)
        for service, section, key, value in (
            ("database", "Config", "Image", "mariadb:mutable"),
            ("database", "Config", "Cmd", ["--innodb-buffer-pool-size=8G"]),
            ("web", "HostConfig", "Memory", 0),
            ("web", "HostConfig", "NanoCpus", 2000000000),
            ("database", "HostConfig", "PortBindings", {"3306/tcp": [{"HostPort": "3307"}]}),
        ):
            with self.subTest(service=service, key=key):
                self.rows = deepcopy(original)
                self.rows[service][section][key] = value
                with self.assertRaises(ValueError):
                    self.run_program()
        self.rows = deepcopy(original)
        self.rows["database"]["Mounts"][0]["Name"] = "retained-production-data"
        with self.assertRaises(ValueError):
            self.run_program()
