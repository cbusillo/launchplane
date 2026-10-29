from __future__ import annotations

import unittest
import base64
import io
import json
import subprocess
import sys
from collections.abc import Callable
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
from unittest.mock import patch

from pydantic import ValidationError

from control_plane.contracts.product_retirement import (
    ProductRetirementProviderObservation,
    canonical_sha256,
)
from control_plane.contracts.retained_volume import RetainedVolumeBinding
from control_plane.product_retirement import (
    _same_planned_observation,
    build_provider_observation,
    redacted_product_retirement_response,
)
from control_plane.retained_volume_capture import (
    _RETAINED_VOLUME_CAPTURE_PROGRAM,
    capture_retained_volumes,
    retained_volume_engine_identity,
    retained_volume_identity,
)


class RetainedVolumeCaptureTests(unittest.TestCase):
    def test_non_string_provider_identity_never_dispatches_capture(self) -> None:
        for field in ("applicationId", "appName"):
            with (
                self.subTest(field=field),
                patch(
                    "control_plane.retained_volume_capture.native_copy_stream.run_managed_server_program"
                ) as dispatch,
            ):
                payload: dict[str, object] = {
                    "applicationId": "registered-app",
                    "appName": "registered-app",
                    "serverId": "registered-server",
                    "mounts": [{"type": "volume", "volumeName": "site-data"}],
                    field: 123,
                }
                self.assertEqual(
                    capture_retained_volumes(
                        host="https://provider.invalid", token="test-token", payload=payload
                    ),
                    ((), False),
                )
                dispatch.assert_not_called()

    def test_identity_binds_full_metadata_engine_and_backing_inode(self) -> None:
        volume = {
            "Name": "site-data",
            "Driver": "local",
            "Options": {"type": "none", "o": "bind", "device": "/srv/site"},
            "CreatedAt": "2026-10-29T12:00:00Z",
            "Mountpoint": "/var/lib/docker/volumes/site-data/_data",
        }
        first = retained_volume_identity(volume, "engine-a", 2049, 88)
        reordered = retained_volume_identity(
            dict(reversed(tuple(volume.items()))), "engine-a", 2049, 88
        )

        self.assertEqual(first, reordered)
        self.assertNotEqual(first, retained_volume_identity(volume, "engine-a", 2049, 89))
        self.assertNotEqual(first, retained_volume_identity(volume, "engine-b", 2049, 88))

    def test_capture_keeps_only_real_inventory_identity(self) -> None:
        volume = {
            "Name": "site-data",
            "Driver": "local",
            "Options": None,
            "CreatedAt": "2026-10-29T12:00:00Z",
            "Mountpoint": "/var/lib/docker/volumes/site-data/_data",
        }

        def run(**kwargs: object) -> None:
            receive_callback = kwargs["receive"]
            assert callable(receive_callback)
            receive = cast(Callable[[dict[str, object]], None], receive_callback)
            receive(
                {
                    "type": "complete",
                    "result": {
                        "complete": True,
                        "volumes": [
                            {
                                "volume": volume,
                                "volume_identity_sha256": retained_volume_identity(
                                    volume, "engine-a", 2049, 88
                                ),
                                "engine_identity_sha256": retained_volume_engine_identity(
                                    "engine-a"
                                ),
                                "backing_device": 2049,
                                "backing_inode": 88,
                            }
                        ],
                    },
                }
            )

        with (
            patch(
                "control_plane.retained_volume_capture.native_copy_stream.require_registered_remote_server",
                return_value="server-a",
            ),
            patch(
                "control_plane.retained_volume_capture.native_copy_stream.run_managed_server_program",
                side_effect=run,
            ) as remote,
        ):
            bindings, complete = capture_retained_volumes(
                host="https://dokploy.example",
                token="secret",
                payload={
                    "applicationId": "application-a",
                    "appName": "site-service",
                    "serverId": "server-a",
                    "mounts": [{"type": "volume", "volumeName": "site-data"}],
                },
            )

        self.assertTrue(complete)
        self.assertEqual(
            bindings,
            (
                RetainedVolumeBinding(
                    volume_name="site-data",
                    server_id="server-a",
                    application_id="application-a",
                    application_name="site-service",
                    volume_identity_sha256=retained_volume_identity(volume, "engine-a", 2049, 88),
                    engine_identity_sha256=retained_volume_engine_identity("engine-a"),
                ),
            ),
        )
        self.assertEqual(remote.call_args.kwargs["payload"]["configured"], ["site-data"])

    def test_primary_server_keeps_configured_names_without_claiming_inventory(self) -> None:
        with patch(
            "control_plane.retained_volume_capture.native_copy_stream.run_managed_server_program"
        ) as remote:
            bindings, complete = capture_retained_volumes(
                host="https://provider.invalid",
                token="test-token",
                payload={
                    "applicationId": "application-a",
                    "appName": "service-a",
                    "serverId": None,
                    "mounts": [{"type": "volume", "volumeName": "kept-data"}],
                },
            )
        self.assertFalse(complete)
        self.assertEqual([binding.volume_name for binding in bindings], ["kept-data"])
        self.assertEqual(bindings[0].server_id, "")
        self.assertEqual(bindings[0].volume_identity_sha256, "")
        remote.assert_not_called()

    def test_contract_rejects_unsafe_names_and_inexact_complete_inventory(self) -> None:
        with self.assertRaises(ValidationError):
            RetainedVolumeBinding(
                volume_name="../data",
                server_id="server-a",
                application_id="application-a",
                application_name="site-service",
            )
        with self.assertRaises(ValidationError):
            RetainedVolumeBinding(
                volume_name="site-data",
                server_id="server-a",
                application_id="application-a",
                application_name="site-service",
                engine_identity_sha256="not-a-digest",
            )
        binding = RetainedVolumeBinding(
            volume_name="site-data",
            server_id="server-a",
            application_id="application-a",
            application_name="site-service",
        )
        with self.assertRaises(ValidationError):
            ProductRetirementProviderObservation(
                observed_at="2026-10-29T12:00:00Z",
                target_id="application-a",
                target_id_sha256="5c44c50f3b79f26526bc32b2b6583e8b0ba6d3405257b31f51ca7a80a3975d32",
                state="absent",
                retirable=False,
                retained_volumes=(binding,),
                retained_volume_inventory_complete=True,
            )

    def test_complete_mount_change_invalidates_modern_plan_but_legacy_remains_compatible(
        self,
    ) -> None:
        binding = RetainedVolumeBinding(
            volume_name="site-data",
            server_id="server-a",
            application_id="application-a",
            application_name="site-service",
            volume_identity_sha256="a" * 64,
        )
        planned = build_provider_observation(
            target_id="application-a",
            payload={
                "applicationId": "application-a",
                "status": "idle",
                "mounts": [{"type": "volume", "volumeName": "site-data"}],
            },
            domains=(),
            latest_deployment={"status": "done"},
            observed_at="2026-10-29T12:00:00Z",
            retained_volumes=(binding,),
            retained_volume_inventory_complete=True,
        )
        changed = planned.model_copy(
            update={
                "mounts_sha256": canonical_sha256([{"type": "volume", "volumeName": "other-data"}])
            }
        )
        legacy = planned.model_copy(
            update={
                "retained_volumes": (),
                "retained_volume_inventory_complete": False,
                "mounts_sha256": "",
            }
        )

        self.assertFalse(_same_planned_observation(planned=planned, current=changed))
        self.assertTrue(_same_planned_observation(planned=legacy, current=changed))

    def test_public_retirement_response_redacts_raw_custody_fields(self) -> None:
        from tests.test_product_retirement import _Store, _observation, _plan

        record = _plan(_Store(), _observation())
        binding = RetainedVolumeBinding(
            volume_name="private-volume-marker",
            server_id="private-server-marker",
            application_id="private-application-marker",
            application_name="private-service-marker",
            volume_identity_sha256="a" * 64,
            engine_identity_sha256="b" * 64,
        )
        record = record.model_copy(
            update={
                "provider_observation": record.provider_observation.model_copy(
                    update={
                        "retained_volumes": (binding,),
                        "retained_volume_inventory_complete": True,
                    }
                )
            }
        )
        response = redacted_product_retirement_response(record)
        serialized = str(response)

        self.assertNotIn("retained_volumes", serialized)
        self.assertNotIn("volume_name", serialized)
        self.assertNotIn("server_id", serialized)
        self.assertNotIn(binding.volume_name, serialized)
        self.assertNotIn(binding.server_id, serialized)
        self.assertNotIn(binding.engine_identity_sha256, serialized)

    def test_modern_plan_binds_non_volume_mount_changes(self) -> None:
        payload = {"applicationId": "application-a", "status": "idle", "mounts": []}
        planned = build_provider_observation(
            target_id="application-a",
            payload=payload,
            domains=(),
            latest_deployment={"status": "done"},
            observed_at="2026-10-29T12:00:00Z",
        )
        changed = build_provider_observation(
            target_id="application-a",
            payload={**payload, "mounts": [{"type": "bind", "hostPath": "/private/state"}]},
            domains=(),
            latest_deployment={"status": "done"},
            observed_at=planned.observed_at,
        )
        self.assertFalse(_same_planned_observation(planned=planned, current=changed))

    def test_capture_availability_does_not_change_retirement_authority(self) -> None:
        from tests.test_product_retirement import _observation

        binding = RetainedVolumeBinding(
            volume_name="kept-data", server_id="", application_id="app", application_name="app"
        )
        incomplete = _observation().model_copy(update={"retained_volumes": (binding,)})
        complete = incomplete.model_copy(
            update={
                "retained_volumes": (
                    binding.model_copy(
                        update={"server_id": "server", "volume_identity_sha256": "a" * 64}
                    ),
                ),
                "retained_volume_inventory_complete": True,
            }
        )
        for planned, current in (
            (incomplete, incomplete),
            (incomplete, complete),
            (complete, incomplete),
        ):
            self.assertTrue(_same_planned_observation(planned=planned, current=current))

    def test_legacy_observation_dump_keeps_its_original_digest_shape(self) -> None:
        dumped = _observation_for_capture().model_dump(mode="json")
        self.assertNotIn("retained_volumes", dumped)
        self.assertNotIn("retained_volume_inventory_complete", dumped)
        self.assertNotIn("mounts_sha256", dumped)

    def test_actual_capture_program_scopes_owned_containers_and_detects_storage_drift(
        self,
    ) -> None:
        for drift in (False, True):
            with self.subTest(drift=drift), TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                data = root / "volumes" / "retained-volume" / "_data"
                data.mkdir(parents=True)
                service = {
                    "ID": "service1",
                    "Spec": {
                        "Name": "application1",
                        "TaskTemplate": {"ContainerSpec": {"Mounts": []}},
                    },
                }
                volume = {
                    "Name": "retained-volume",
                    "Driver": "local",
                    "Options": None,
                    "CreatedAt": "2026-10-29T12:00:00Z",
                    "Mountpoint": str(data),
                }
                container = {
                    "Id": "container1",
                    "Config": {"Labels": {"com.docker.swarm.service.id": "service1"}},
                    "Mounts": [{"Type": "volume", "Name": "retained-volume"}],
                }
                container_reads = 0

                def docker(
                    command: list[str], **_kwargs: object
                ) -> subprocess.CompletedProcess[bytes]:
                    nonlocal container_reads
                    if command[1:3] != ["--host", "unix:///var/run/docker.sock"]:
                        raise OSError("The Docker fixture routed away from the local daemon")
                    arguments = command[3:]
                    if arguments[:2] == ["node", "ls"]:
                        value = json.dumps({"ID": "node1", "Self": True})
                    elif arguments[:2] == ["service", "ps"]:
                        value = ""
                    elif arguments[0] == "ps":
                        container_reads += 1
                        value = "container1"
                        if "label=com.docker.swarm.service.id=service1" not in arguments:
                            value += f"\nunrelated-{container_reads}"
                    elif arguments[:2] == ["volume", "ls"]:
                        value = "retained-volume"
                    elif arguments[0] == "info":
                        value = json.dumps(
                            "engine1" if arguments[-1] == "{{json .ID}}" else str(root)
                        )
                    elif arguments[:3] == ["inspect", "--type", "service"]:
                        value = json.dumps([service])
                    elif arguments[:3] == ["inspect", "--type", "container"]:
                        if any(identifier.startswith("unrelated-") for identifier in arguments[3:]):
                            raise OSError("Unrelated container disappeared")
                        current = container
                        if drift and container_reads > 1:
                            current = {**container, "Mounts": []}
                        value = json.dumps(
                            [{**current, "State": {"Health": {"Log": [container_reads]}}}]
                        )
                    elif arguments[:3] == ["inspect", "--type", "volume"]:
                        value = json.dumps([volume])
                    else:
                        raise AssertionError(arguments)
                    return subprocess.CompletedProcess(command, 0, value.encode(), b"")

                output = io.StringIO()
                payload = base64.b64encode(
                    json.dumps({"application_name": "application1", "configured": []}).encode()
                ).decode()
                with (
                    patch.object(sys, "argv", ["capture", payload, "frame:"]),
                    patch.object(subprocess, "run", side_effect=docker),
                    patch.dict(
                        "os.environ",
                        {
                            "DOCKER_CONTEXT": "inherited-remote-context",
                            "DOCKER_HOST": "tcp://inherited-remote.invalid:2376",
                        },
                    ),
                    redirect_stdout(output),
                ):
                    exec(
                        compile(
                            _RETAINED_VOLUME_CAPTURE_PROGRAM, "<retained-volume-capture>", "exec"
                        ),
                        {},
                    )
                frame = json.loads(output.getvalue().removeprefix("frame:"))
                self.assertEqual(frame["result"]["complete"], not drift)
                if not drift:
                    self.assertEqual(frame["result"]["volumes"][0]["volume"], volume)
                    self.assertEqual(
                        frame["result"]["volumes"][0]["engine_identity_sha256"],
                        retained_volume_engine_identity("engine1"),
                    )


def _observation_for_capture() -> ProductRetirementProviderObservation:
    from tests.test_product_retirement import _observation

    return _observation()


if __name__ == "__main__":
    unittest.main()
