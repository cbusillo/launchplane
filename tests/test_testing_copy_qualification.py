import unittest
import copy
from typing import Any

from pydantic import ValidationError

from control_plane.contracts.lane_service_restart import LaneServiceRestartRequest
from control_plane._testing_copy_qualification_program import (
    offline_container_isolated,
    original_container_stays_stopped,
)


class TestingCopyQualificationContractTests(unittest.TestCase):
    def test_qualification_is_explicit_and_bound_to_the_testing_database(self) -> None:
        payload = {
            "product": "example",
            "context": "example",
            "instance": "testing",
            "service": "database",
            "reason": "Qualify the retained copy",
            "qualification_mode": "sanitize",
        }
        request = LaneServiceRestartRequest.model_validate(payload)
        self.assertEqual(request.qualification_mode, "sanitize")
        for changes in ({"instance": "prod"}, {"service": "web"}, {"qualification_mode": "start"}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                LaneServiceRestartRequest.model_validate({**payload, **changes})
        plain = LaneServiceRestartRequest.model_validate(
            {key: value for key, value in payload.items() if key != "qualification_mode"}
        )
        self.assertNotIn("qualification_mode", plain.model_dump())

    def test_offline_inspection_refuses_every_network_logging_credential_and_mount_escape(
        self,
    ) -> None:
        expected: dict[str, Any] = {
            "image": "sha256:" + "a" * 64,
            "user": "1000:1000",
            "network": "none",
            "operation": "b" * 64,
            "environment": {"PATH": "/usr/bin:/bin"},
            "entrypoint": ["python3"],
            "command": ["-c", "pass"],
            "mounts": [
                {"Type": "volume", "Name": "copy-data", "Destination": "/volumes/data", "RW": True}
            ],
        }
        valid: dict[str, Any] = {
            "Image": expected["image"],
            "Mounts": copy.deepcopy(expected["mounts"]),
            "Config": {
                "Image": expected["image"],
                "User": expected["user"],
                "Entrypoint": expected["entrypoint"],
                "Cmd": expected["command"],
                "Labels": {"io.launchplane.copy-qualification": expected["operation"]},
                "Env": [f"{key}={value}" for key, value in expected["environment"].items()],
            },
            "HostConfig": {
                "NetworkMode": "none",
                "Privileged": False,
                "ReadonlyRootfs": True,
                "RestartPolicy": {"Name": "no"},
                "LogConfig": {"Type": "none", "Config": {}},
                "CapDrop": ["ALL"],
                "IpcMode": "private",
                "SecurityOpt": ["no-new-privileges"],
                "Tmpfs": {"/tmp": "rw,noexec,nosuid,nodev"},
            },
        }
        self.assertTrue(offline_container_isolated(valid, expected))
        for change in (
            "network",
            "logging",
            "credentials",
            "mount",
            "privileged",
            "restart",
            "image",
        ):
            with self.subTest(change=change):
                container = copy.deepcopy(valid)
                if change == "network":
                    container["HostConfig"]["NetworkMode"] = "bridge"
                elif change == "logging":
                    container["HostConfig"]["LogConfig"]["Type"] = "syslog"
                elif change == "credentials":
                    container["Config"]["Env"].append("SHOPIFY_TOKEN=sentinel")
                elif change == "mount":
                    container["Mounts"].append(
                        {
                            "Type": "bind",
                            "Source": "/run/docker.sock",
                            "Destination": "/run/docker.sock",
                        }
                    )
                elif change == "privileged":
                    container["HostConfig"]["Privileged"] = True
                elif change == "restart":
                    container["HostConfig"]["RestartPolicy"]["Name"] = "always"
                else:
                    container["Image"] = "sha256:" + "c" * 64
                self.assertFalse(offline_container_isolated(container, expected))

    def test_original_stop_needs_daemon_persistence_when_restart_policy_can_autostart(self) -> None:
        container: dict[str, Any] = {
            "Id": "a" * 64,
            "State": {"Running": False, "Pid": 0},
            "HostConfig": {"RestartPolicy": {"Name": "unless-stopped"}},
        }
        saved = {"ID": container["Id"], "HasBeenManuallyStopped": True}
        self.assertTrue(original_container_stays_stopped(container, saved))
        for changed in (
            None,
            {**saved, "HasBeenManuallyStopped": False},
            {**saved, "ID": "another-container"},
        ):
            self.assertFalse(original_container_stays_stopped(container, changed))
        container["State"]["Running"] = True
        self.assertFalse(original_container_stays_stopped(container, saved))
