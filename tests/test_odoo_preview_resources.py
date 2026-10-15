import base64
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from typing import Any
import unittest
from unittest.mock import patch

from control_plane.odoo_preview_resources import PREVIEW_RESOURCE_PROGRAM


class PreviewResourceInspectionTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.volumes: dict[str, dict[str, Any]] = {}
        for name in ("preview-data", "preview-db", "preview-logs"):
            path = self.root / name / "_data"
            path.mkdir(parents=True)
            (path / "needed-data").write_text("preserve me")
            self.volumes[name] = {
                "Name": name,
                "Driver": "local",
                "Options": {},
                "Mountpoint": str(path),
            }
        self.spec = {
            "compose_id": "preview-target",
            "project": "preview-project",
            "volumes": list(self.volumes),
        }
        self.containers: dict[str, dict[str, Any]] = {
            "preview": {
                "Config": {"Labels": {"com.docker.compose.project": self.spec["project"]}},
                "HostConfig": {"PortBindings": {}, "NetworkMode": "bridge"},
                "Mounts": [
                    {"Type": "volume", "RW": True, "Name": name, "Source": volume["Mountpoint"]}
                    for name, volume in self.volumes.items()
                ],
            },
        }
        self.calls: list[list[str]] = []

    def docker(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        args = command[1:]
        self.calls.append(args)
        if args == ["ps", "-aq"]:
            value = "\n".join(self.containers)
        elif args[:2] == ["volume", "inspect"]:
            value = json.dumps([self.volumes[args[-1]]])
        elif args[0] == "inspect":
            value = json.dumps([self.containers[args[-1]]])
        else:
            self.fail(f"Unexpected host mutation: {args}")
        return subprocess.CompletedProcess(command, 0, value, "")

    def run_program(self) -> tuple[int, dict[str, str]]:
        encoded = base64.b64encode(json.dumps(self.spec).encode()).decode()
        output = io.StringIO()
        status = 0
        with (
            patch.object(sys, "argv", ["preview-inspector", encoded]),
            patch("subprocess.run", side_effect=self.docker),
            redirect_stdout(output),
        ):
            try:
                exec(compile(PREVIEW_RESOURCE_PROGRAM, "<preview-inspector>", "exec"), {})
            except SystemExit as error:
                status = error.code if isinstance(error.code, int) else 1
        return status, dict(line.split("=", 1) for line in output.getvalue().splitlines())

    def test_existing_local_data_is_preserved_by_read_only_inspection(self) -> None:
        status, evidence = self.run_program()
        self.assertEqual(status, 0, evidence)
        self.assertEqual(evidence["preview_resources_ok"], "true")
        for volume in self.volumes.values():
            self.assertEqual(
                (Path(volume["Mountpoint"]) / "needed-data").read_text(), "preserve me"
            )

    def test_production_mounts_refuse_even_if_stopped_or_named_differently(self) -> None:
        name, volume = next(iter(self.volumes.items()))
        for mount in (
            {"Type": "volume", "Name": name, "Source": volume["Mountpoint"], "RW": True},
            {"Type": "bind", "Source": str(self.root), "RW": True},
            {"Type": "bind", "Source": volume["Mountpoint"], "RW": False},
        ):
            with self.subTest(mount=mount):
                self.containers["production"] = {
                    "Config": {"Labels": {}},
                    "State": {"Running": False},
                    "Mounts": [mount],
                }
                self.assertNotEqual(self.run_program()[0], 0)

    def test_unavailable_mounts_and_remote_backing_refuse(self) -> None:
        original = self.containers["preview"].pop("Mounts")
        self.assertNotEqual(self.run_program()[0], 0)
        self.containers["preview"]["Mounts"] = original
        name = next(iter(self.volumes))
        self.volumes[name]["Options"] = {"type": "nfs", "device": ":/business-data"}
        self.assertNotEqual(self.run_program()[0], 0)

    def test_host_ports_and_unexpected_writable_bind_refuse(self) -> None:
        self.containers["preview"]["HostConfig"]["PortBindings"] = {
            "8069/tcp": [{"HostPort": "8069"}]
        }
        self.assertNotEqual(self.run_program()[0], 0)
        self.containers["preview"]["HostConfig"]["PortBindings"] = {}
        self.containers["preview"]["Mounts"].append(
            {"Type": "bind", "Source": str(self.root), "RW": True}
        )
        self.assertNotEqual(self.run_program()[0], 0)

    def test_absent_target_and_unknown_container_owner_refuse(self) -> None:
        self.containers.clear()
        self.assertNotEqual(self.run_program()[0], 0)
        self.containers["unknown"] = {"Mounts": []}
        self.assertNotEqual(self.run_program()[0], 0)

    def test_remote_daemon_paths_anonymous_image_volumes_and_stale_foreign_bind(self) -> None:
        for name, volume in self.volumes.items():
            volume["Mountpoint"] = f"/var/lib/docker/volumes/{name}/_data"
        for mount in self.containers["preview"]["Mounts"]:
            mount["Source"] = self.volumes[mount["Name"]]["Mountpoint"]
        anonymous = "a" * 64
        self.volumes[anonymous] = {
            "Name": anonymous,
            "Driver": "local",
            "Options": {},
            "Mountpoint": f"/var/lib/docker/volumes/{anonymous}/_data",
        }
        self.containers["preview"]["Config"]["Volumes"] = {"/var/lib/odoo": {}}
        self.containers["preview"]["Mounts"].append(
            {
                "Type": "volume",
                "Name": anonymous,
                "Destination": "/var/lib/odoo",
                "Source": self.volumes[anonymous]["Mountpoint"],
                "RW": True,
            }
        )
        self.containers["unrelated-stopped"] = {
            "Config": {"Labels": None},
            "Mounts": [{"Type": "bind", "Source": "/unrelated/deleted/path", "RW": True}],
        }
        self.assertEqual(self.run_program()[0], 0)
        self.containers["unrelated-stopped"]["Mounts"] = [
            {
                "Type": "volume",
                "Name": anonymous,
                "Source": self.volumes[anonymous]["Mountpoint"],
                "RW": True,
            }
        ]
        self.assertNotEqual(self.run_program()[0], 0)

    def test_disappeared_foreign_container_is_not_an_unknown_consumer(self) -> None:
        self.containers["gone"] = {}
        original = self.docker

        def disappearing(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            if command[1:] == ["inspect", "gone"]:
                return subprocess.CompletedProcess(command, 1, "", "Error: No such container: gone")
            return original(command, **kwargs)

        with patch.object(self, "docker", side_effect=disappearing):
            self.assertEqual(self.run_program()[0], 0)


class PreviewResourceCollectorTests(unittest.TestCase):
    def test_collector_binds_receipt_to_target_resources_and_rechecks_drift(self) -> None:
        import click
        from control_plane.odoo_preview_resources import require_existing_preview_resources
        from control_plane.dokploy.post_deploy import extract_odoo_post_deploy_readback_markers

        environment = {
            "ODOO_DATA_VOLUME": "preview-data",
            "ODOO_DB_VOLUME": "preview-db",
            "ODOO_LOG_VOLUME": "preview-logs",
        }
        payload = {
            "composeId": "preview-target",
            "appName": "preview-project",
            "serverId": "fixture-server",
            "env": "\n".join(f"{key}={value}" for key, value in environment.items()),
        }
        executed: list[dict[str, Any]] = []

        def schedule(**kwargs: Any) -> dict[str, str]:
            executed.append(kwargs)
            script = kwargs["schedule_payload"]["script"]
            encoded = script.split("python3 - ")[1].split(" ")[0]
            spec = json.loads(base64.b64decode(encoded))
            import hashlib

            digest = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
            return extract_odoo_post_deploy_readback_markers(
                {
                    "logs": [
                        "preview_resources_ok=true",
                        "preview_resources_binding_sha256=" + digest,
                    ]
                }
            )

        from control_plane.dokploy import api, post_deploy

        with (
            patch.object(api, "fetch_dokploy_target_payload", return_value=payload) as fetch,
            patch.object(api, "find_matching_dokploy_schedule", return_value=None),
            patch.object(post_deploy, "run_manual_data_schedule", side_effect=schedule),
        ):
            kwargs: dict[str, Any] = dict(
                host="fixture",
                token="fixture",
                compose_id="preview-target",
                compose_name="preview-name",
                context="example",
                instance="pr-42",
                environment=environment,
                checkpoint=lambda: None,
            )
            require_existing_preview_resources(**kwargs)
            self.assertFalse(executed[0]["schedule_payload"]["enabled"])
            fetch.side_effect = [payload, {**payload, "serverId": "moved-server"}]
            with self.assertRaises(click.ClickException):
                require_existing_preview_resources(**kwargs)

    def test_wrong_missing_and_stale_receipts_refuse(self) -> None:
        import click
        from control_plane.odoo_preview_resources import require_existing_preview_resources
        from control_plane.dokploy import api, post_deploy

        environment = {
            "ODOO_DATA_VOLUME": "preview-data",
            "ODOO_DB_VOLUME": "preview-db",
            "ODOO_LOG_VOLUME": "preview-logs",
        }
        payload = {
            "composeId": "preview-target",
            "appName": "preview-project",
            "serverId": "fixture-server",
            "env": "\n".join(f"{key}={value}" for key, value in environment.items()),
        }
        with (
            patch.object(api, "fetch_dokploy_target_payload", return_value=payload),
            patch.object(api, "find_matching_dokploy_schedule", return_value=None),
            patch.object(post_deploy, "run_manual_data_schedule") as run,
        ):
            for receipt in (
                {},
                {"preview_resources_ok": "false"},
                {
                    "preview_resources_ok": "true",
                    "preview_resources_binding_sha256": "previous-target-receipt",
                },
            ):
                with self.subTest(receipt=receipt):
                    run.return_value = receipt
                    with self.assertRaises(click.ClickException):
                        require_existing_preview_resources(
                            host="fixture",
                            token="fixture",
                            compose_id="preview-target",
                            compose_name="preview",
                            context="example",
                            instance="pr-42",
                            environment=environment,
                            checkpoint=lambda: None,
                        )


class PreviewRefreshResourceFenceTests(unittest.TestCase):
    def test_failed_first_or_changed_final_inspection_prevents_deployment(self) -> None:
        import click
        from contextlib import ExitStack
        from control_plane.workflows.odoo_preview_runtime import _execute_refresh
        from control_plane.odoo_preview_apply_http import OdooPreviewApplyEnvelope
        from tests.test_odoo_preview_apply_execution import _refresh_payload
        from tests.test_odoo_preview_runtime import _environment_values

        for late in (False, True):
            with self.subTest(late=late), ExitStack() as stack:
                apply = OdooPreviewApplyEnvelope.model_validate(_refresh_payload()).apply
                apply = apply.model_copy(
                    update={
                        "dry_run_plan": apply.dry_run_plan.model_copy(
                            update={"compose_ref": "preview-target"}
                        ),
                        "environment_values": _environment_values(),
                    }
                )
                stack.enter_context(
                    patch(
                        "control_plane.workflows.odoo_preview_runtime.dokploy_api.fetch_dokploy_target_payload",
                        return_value={"composeId": "preview-target"},
                    )
                )
                stack.enter_context(
                    patch(
                        "control_plane.workflows.odoo_preview_runtime._resolve_or_create_compose",
                        return_value=("preview-target", False),
                    )
                )
                source_write = stack.enter_context(
                    patch(
                        "control_plane.workflows.odoo_preview_runtime.dokploy_compose.sync_dokploy_compose_raw_source"
                    )
                )
                env_write = stack.enter_context(
                    patch(
                        "control_plane.workflows.odoo_preview_runtime.dokploy_api.update_dokploy_target_env"
                    )
                )
                stack.enter_context(
                    patch(
                        "control_plane.workflows.odoo_preview_runtime.dokploy_compose.ensure_compose_web_domain_route",
                        return_value="preview-domain",
                    )
                )
                stack.enter_context(
                    patch(
                        "control_plane.workflows.odoo_preview_runtime.dokploy_api.latest_deployment_for_target",
                        return_value=None,
                    )
                )
                deploy = stack.enter_context(
                    patch(
                        "control_plane.workflows.odoo_preview_runtime.dokploy_api.trigger_deployment"
                    )
                )
                failure = click.ClickException("Foreign production mount appeared")
                stack.enter_context(
                    patch(
                        "control_plane.odoo_preview_resources.require_existing_preview_resources",
                        side_effect=[None, failure] if late else failure,
                    )
                )
                result = _execute_refresh(
                    host="fixture",
                    token="fixture",
                    request=apply,
                    provider_operation_title="fixture-operation",
                    provider_effect_checkpoint=None,
                    provider_lease_check=None,
                    expected_runtime_identity=None,
                    qualify_existing_refresh=True,
                )
                self.assertEqual(result.status, "fail")
                self.assertEqual(result.provider_effect_attempted, late)
                deploy.assert_not_called()
                if late:
                    source_write.assert_called_once()
                    env_write.assert_called_once()
                    # Check the effective mount behavior, not rendered wording.
                    import yaml

                    compose = yaml.safe_load(source_write.call_args.kwargs["compose_file"])
                    mounts = compose["services"]["script-runner"]["volumes"]
                    self.assertTrue(all(".ssh" not in str(mount) for mount in mounts))
                    self.assertFalse(compose["services"]["web"].get("ports"))
                else:
                    source_write.assert_not_called()
                    env_write.assert_not_called()
