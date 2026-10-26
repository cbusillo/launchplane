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

from control_plane.odoo_preview_resources import PREVIEW_RESOURCE_PROGRAM, RESOURCE_KEYS
from control_plane._odoo_snapshot_files import CLEAN_SNAPSHOT_STATE_DIRECTORY
from control_plane.contracts.disposable_lane import (
    ODOO_PREVIEW_RESOURCES_OK_MARKER,
    ODOO_PREVIEW_RESOURCES_BINDING_MARKER,
)


class PreviewResourceInspectionTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.volumes: dict[str, dict[str, Any]] = {}
        for name in ("preview-data", "preview-db", "preview-logs"):
            path = self.root / "volumes" / name / "_data"
            path.mkdir(parents=True)
            (path / "needed-data").write_text("preserve me")
            self.volumes[name] = {
                "Name": name,
                "Driver": "local",
                "Options": {},
                "Mountpoint": str(path),
            }
        self.spec: dict[str, Any] = {
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
        if args[0] == "ps":
            value = "\n".join(self.containers)
        elif args[0] == "info":
            value = str(self.root)
        elif args[:2] == ["volume", "ls"]:
            value = "\n".join(self.volumes)
        elif args[:2] == ["volume", "inspect"]:
            item = self.volumes[args[-1]]
            value = json.dumps(
                {
                    "Name": item["Name"],
                    "Driver": item["Driver"],
                    "Mountpoint": item["Mountpoint"],
                    "HasOptions": bool(item.get("Options")),
                    "Device": (item.get("Options") or {}).get("device"),
                    "Overlay": item.get("Options")
                    if (item.get("Options") or {}).get("type") == "overlay"
                    else None,
                    "Anonymous": item.get("Anonymous", False),
                }
            )
            if "--format" not in args:
                value = json.dumps([item])
        elif args[0] == "inspect":
            rows = []
            for identifier in args[3:]:
                item = self.containers[identifier]
                rows.append(json.dumps({"Id": identifier, **item}))
            value = "\n".join(rows)
        else:
            self.fail(f"Unexpected host mutation: {args}")
        return subprocess.CompletedProcess(command, 0, value, "")

    def run_program(self, spec: dict[str, Any] | None = None) -> tuple[int, dict[str, str]]:
        encoded = base64.b64encode(
            json.dumps(spec if spec is not None else self.spec).encode()
        ).decode()
        output = io.StringIO()
        status = 0
        with (
            patch.object(sys, "argv", ["preview-inspector", encoded]),
            patch("subprocess.run", side_effect=self.docker),
            patch("pathlib.Path.home", return_value=self.root),
            redirect_stdout(output),
        ):
            try:
                exec(compile(PREVIEW_RESOURCE_PROGRAM, "<preview-inspector>", "exec"), {})
            except SystemExit as error:
                status = error.code if isinstance(error.code, int) else 1
        return status, dict(line.split("=", 1) for line in output.getvalue().splitlines())

    def retain_unregistered_fixture_backings(self) -> None:
        retained = self.root / "retained"
        retained.mkdir()
        for name, volume in self.volumes.items():
            original = Path(volume["Mountpoint"]).parent
            original.rename(retained / name)
            volume["Mountpoint"] = str(retained / name / "_data")

    def test_unstarted_requires_absent_containers_and_plain_storage(self) -> None:
        self.spec["unstarted"] = True
        self.retain_unregistered_fixture_backings()
        saved_volumes = dict(self.volumes)
        saved_containers = dict(self.containers)
        self.volumes.clear()
        self.containers.clear()
        self.assertEqual(self.run_program()[0], 0)
        self.containers.update(saved_containers)
        self.assertNotEqual(self.run_program()[0], 0)
        self.containers.clear()
        self.volumes.update(saved_volumes)
        self.assertNotEqual(self.run_program()[0], 0)
        self.assertTrue(
            all((Path(v["Mountpoint"]) / "needed-data").exists() for v in self.volumes.values())
        )

    def test_unstarted_refuses_unregistered_needed_backing(self) -> None:
        self.spec["unstarted"] = True
        self.containers.clear()
        needed = Path(next(iter(self.volumes.values()))["Mountpoint"]) / "needed-data"
        self.volumes.clear()
        for pending in ("", "preview-data-filestore"):
            with self.subTest(pending=pending):
                spec = {**self.spec, "pending_filestore": pending}
                if pending:
                    spec["volumes"] = [*self.spec["volumes"], pending]
                self.assertNotEqual(self.run_program(spec)[0], 0)
                self.assertTrue(needed.is_file())

    def test_unstarted_refuses_storage_appearing_during_inventory(self) -> None:
        self.spec["unstarted"] = True
        self.retain_unregistered_fixture_backings()
        volume = next(iter(self.volumes.items()))
        self.volumes.clear()
        self.containers.clear()
        inspect = self.docker
        inventories = 0

        def changing(command: list[str], **kwargs: Any) -> Any:
            nonlocal inventories
            if command[1:3] == ["volume", "ls"]:
                inventories += 1
                if inventories == 2:
                    self.volumes[volume[0]] = volume[1]
            return inspect(command, **kwargs)

        with patch.object(self, "docker", side_effect=changing):
            self.assertNotEqual(self.run_program()[0], 0)

    def test_destroy_allows_missing_volumes_and_own_host_bindings(self) -> None:
        self.spec["operation"] = "destroy"
        self.containers["preview"]["HostConfig"] = {
            "PortBindings": {"8069/tcp": [{}]},
            "NetworkMode": "host",
        }
        self.containers["preview"]["Mounts"].append(
            {"Type": "bind", "Source": "/own/config", "RW": True}
        )
        self.assertEqual(self.run_program()[0], 0)
        self.containers.clear()
        self.volumes.clear()
        self.assertEqual(self.run_program()[0], 0)
        self.spec["operation"] = "refresh"
        self.assertEqual(self.run_program()[0], 1)

    def test_destroy_still_refuses_foreign_consumers(self) -> None:
        self.spec["operation"] = "destroy"
        mount = self.containers["preview"]["Mounts"][0]
        self.containers["production"] = {"Config": {"Labels": {}}, "Mounts": [mount]}
        self.assertEqual(self.run_program()[0], 1)

    def test_pending_allocation_allows_only_its_missing_overlay(self) -> None:
        self.spec["volumes"].append("preview-data-filestore")
        self.spec["pending_filestore"] = "preview-data-filestore"
        self.assertEqual(self.run_program()[0], 0)
        self.volumes.pop("preview-db")
        self.assertEqual(self.run_program()[0], 1)
        self.spec["pending_filestore"] = "preview-db"
        self.assertEqual(self.run_program()[0], 1)

    def test_collector_qualifies_pending_overlay_before_compose_declares_it(self) -> None:
        import click
        from control_plane.dokploy import api, compose, post_deploy
        from control_plane.odoo_preview_resources import require_existing_preview_resources

        environment = dict(zip(RESOURCE_KEYS, self.volumes))
        payload = {
            "composeId": self.spec["compose_id"],
            "appName": self.spec["project"],
            "serverId": "fixture-server",
            "composeFile": compose.render_odoo_raw_compose_file(
                image_reference="fixture", hold_web_until_integration_readback=True
            ),
            "env": "\n".join(f"{key}={value}" for key, value in environment.items()),
        }

        def schedule(**kwargs: Any) -> dict[str, str]:
            script = kwargs["schedule_payload"]["script"]
            encoded = script.split("python3 - ")[1].split(" ")[0]
            _, evidence = self.run_program(json.loads(base64.b64decode(encoded)))
            return evidence

        with (
            patch.object(api, "fetch_dokploy_target_payload", return_value=payload),
            patch.object(api, "find_matching_dokploy_schedule", return_value=None),
            patch.object(post_deploy, "run_manual_data_schedule", side_effect=schedule),
        ):
            kwargs: dict[str, Any] = {
                "host": "fixture",
                "token": "fixture",
                "compose_id": payload["composeId"],
                "compose_name": "preview",
                "context": "example",
                "instance": "pr-42",
                "environment": environment,
                "checkpoint": lambda: None,
                "pending_filestore": True,
            }
            require_existing_preview_resources(**kwargs)
            self.volumes.pop("preview-db")
            with self.assertRaises(click.ClickException):
                require_existing_preview_resources(**kwargs)

    def test_overlay_ownership_includes_hidden_writable_backing(self) -> None:
        project = self.spec["project"]
        data = self.volumes["preview-data"]
        data["Labels"] = {"com.docker.compose.project": project}
        private = Path(data["Mountpoint"]).parent / "launchplane-preview-filestore"
        for directory in (private / "upper", private / "work"):
            directory.mkdir(parents=True)
        snapshot_id = "a" * 64
        lower = self.root / CLEAN_SNAPSHOT_STATE_DIRECTORY / snapshot_id / "filestore"
        lower.mkdir(parents=True)
        name = "preview-data-filestore"
        mountpoint = self.root / "volumes" / name / "_data"
        mountpoint.mkdir(parents=True)
        self.volumes[name] = {
            "Name": name,
            "Driver": "local",
            "Mountpoint": str(mountpoint),
            "Options": {
                "type": "overlay",
                "device": "overlay",
                "o": f"lowerdir={lower},upperdir={private}/upper,workdir={private}/work,userxattr",
            },
            "Labels": {
                "com.docker.compose.project": project,
                "io.launchplane.preview-snapshot": snapshot_id,
            },
        }
        self.spec["volumes"].append(name)
        self.containers["preview"]["Mounts"].append(
            {"Type": "volume", "Name": name, "Source": str(mountpoint), "RW": True}
        )
        self.assertEqual(self.run_program()[0], 0)
        self.containers["foreign"] = {
            "Config": {"Labels": {}},
            "Mounts": [{"Type": "bind", "Source": str(private / "upper"), "RW": True}],
        }
        self.assertEqual(self.run_program()[0], 1)
        self.spec["operation"] = "destroy"
        self.assertEqual(self.run_program()[0], 1)
        self.containers.pop("foreign")
        self.volumes.pop("preview-data")
        self.containers["preview"]["Mounts"] = [
            mount
            for mount in self.containers["preview"]["Mounts"]
            if mount["Name"] != "preview-data"
        ]
        self.assertEqual(self.run_program()[0], 0)

    def test_existing_local_data_is_preserved_by_read_only_inspection(self) -> None:
        status, evidence = self.run_program()
        self.assertEqual(status, 0, evidence)
        self.assertEqual(evidence[ODOO_PREVIEW_RESOURCES_OK_MARKER], "true")
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

    def test_absent_containers_qualify_but_incomplete_metadata_refuses(self) -> None:
        self.containers.clear()
        self.assertEqual(self.run_program()[0], 0)
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
            "Anonymous": True,
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
        self.volumes[anonymous]["Anonymous"] = False
        self.assertNotEqual(self.run_program()[0], 0)
        self.volumes[anonymous]["Anonymous"] = True
        self.containers["unrelated-stopped"]["Mounts"] = [
            {
                "Type": "volume",
                "Name": anonymous,
                "Source": self.volumes[anonymous]["Mountpoint"],
                "RW": True,
            }
        ]
        self.assertNotEqual(self.run_program()[0], 0)

    def test_foreign_bind_backed_named_volume_protects_preview_data(self) -> None:
        name, backing = next(iter(self.volumes.items()))
        alias = "foreign-alias-volume"
        self.volumes[alias] = {
            "Name": alias,
            "Driver": "local",
            "Mountpoint": f"/var/lib/docker/volumes/{alias}/_data",
            "Options": {"type": "none", "o": "bind", "device": backing["Mountpoint"]},
        }
        self.containers["foreign"] = {
            "Config": {"Labels": {}},
            "Mounts": [
                {
                    "Type": "volume",
                    "Name": alias,
                    "Source": self.volumes[alias]["Mountpoint"],
                    "RW": True,
                }
            ],
        }
        self.assertNotEqual(self.run_program()[0], 0)
        self.volumes[alias]["Options"]["device"] = str(self.root)
        self.assertNotEqual(self.run_program()[0], 0)

    def test_indirect_bind_alias_uses_unmounted_daemon_records(self) -> None:
        _, backing = next(iter(self.volumes.items()))
        first = "intermediate-alias"
        second = "mounted-alias"
        root = Path(backing["Mountpoint"]).parent.parent
        self.volumes[first] = {
            "Name": first,
            "Driver": "local",
            "Mountpoint": str(root / first / "_data"),
            "Options": {"o": "bind", "device": backing["Mountpoint"]},
        }
        self.volumes[second] = {
            "Name": second,
            "Driver": "local",
            "Mountpoint": str(root / second / "_data"),
            "Options": {"o": "bind", "device": self.volumes[first]["Mountpoint"]},
        }
        self.containers["foreign"] = {
            "Config": {"Labels": {}},
            "Mounts": [
                {
                    "Type": "volume",
                    "Name": second,
                    "Source": self.volumes[second]["Mountpoint"],
                    "RW": True,
                }
            ],
        }
        self.assertNotEqual(self.run_program()[0], 0)
        # A direct bind can retain the same intermediate backing too.
        self.containers["foreign"]["Mounts"][0].update(
            Type="bind", Source=self.volumes[first]["Mountpoint"]
        )
        self.assertNotEqual(self.run_program()[0], 0)
        self.volumes[first]["Options"]["device"] = self.volumes[second]["Mountpoint"]
        self.assertNotEqual(self.run_program()[0], 0)
        del self.volumes[first]
        self.assertNotEqual(self.run_program()[0], 0)

    def test_disappeared_foreign_container_is_not_an_unknown_consumer(self) -> None:
        self.containers["gone"] = {}
        original = self.docker

        def disappearing(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            if command[1] == "inspect":
                forwarded = [part for part in command if part != "gone"]
                result = original(forwarded, **kwargs)
                return subprocess.CompletedProcess(
                    command, 1, result.stdout, "Error: No such container: gone"
                )
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

        fixture = PreviewResourceInspectionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)

        def schedule(**kwargs: Any) -> dict[str, str]:
            executed.append(kwargs)
            script = kwargs["schedule_payload"]["script"]
            encoded = script.split("python3 - ")[1].split(" ")[0]
            spec = json.loads(base64.b64decode(encoded))
            status, evidence = fixture.run_program(spec)
            self.assertEqual(status, 0, evidence)
            return extract_odoo_post_deploy_readback_markers(
                {"logs": [f"{key}={value}" for key, value in evidence.items()]}
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
            fetch.side_effect = None
            original_app_name = executed[0]["schedule_app_name"]
            stale = {"deploymentId": "stale-inspection", "status": "running"}
            with patch.object(
                api,
                "find_matching_dokploy_schedule",
                return_value={"deployments": [stale]},
            ):
                require_existing_preview_resources(**kwargs)
                alternate = executed[-1]["schedule_app_name"]
                stale["updatedAt"] = "later-provider-read"
                require_existing_preview_resources(**kwargs)
                self.assertEqual(executed[-1]["schedule_app_name"], alternate)
            self.assertNotEqual(executed[-1]["schedule_app_name"], original_app_name)
            self.assertFalse(executed[-1]["schedule_payload"]["enabled"])

    def test_destroy_source_and_later_binding_cover_actual_deletion_surface(self) -> None:
        import copy
        import click
        import yaml
        from control_plane.dokploy import api, compose
        from control_plane.odoo_preview_resources import (
            _preview_resource_binding,
            _require_preview_destroy_source,
            _preview_destroy_volumes,
            require_preview_resource_binding,
        )

        environment = {
            "ODOO_DATA_VOLUME": "preview-data",
            "ODOO_DB_VOLUME": "preview-db",
            "ODOO_LOG_VOLUME": "preview-logs",
        }
        raw = compose.render_odoo_raw_compose_file(
            image_reference="fixture", hold_web_until_integration_readback=True
        )
        payload = {
            "composeId": "preview",
            "serverId": "server",
            "appName": "app",
            "sourceType": "raw",
            "composePath": "docker-compose.yml",
            "composeFile": raw,
            "env": "\n".join(f"{key}={value}" for key, value in environment.items()),
        }
        _require_preview_destroy_source(payload)
        with self.assertRaises(click.ClickException):
            _require_preview_destroy_source({**payload, "randomize": True})
        declared_names = _preview_destroy_volumes(payload, project="app", environment=environment)
        self.assertTrue(set(environment.values()).issubset(declared_names))
        implicit_names = {
            f"app_{key}" for key, value in yaml.safe_load(raw)["volumes"].items() if not value
        }
        self.assertEqual(set(declared_names), set(environment.values()) | implicit_names)
        binding = _preview_resource_binding(payload, compose_id="preview", environment=environment)
        parsed = yaml.safe_load(raw)
        legacy = {
            **parsed,
            "volumes": {key: value for key, value in parsed["volumes"].items() if value},
        }
        _require_preview_destroy_source({**payload, "composeFile": yaml.safe_dump(legacy)})
        with self.assertRaises(click.ClickException):
            _require_preview_destroy_source(
                {**payload, "env": payload["env"] + "\nCOMPOSE_FILE=other.yml"}
            )
        mutations: list[dict[str, object]] = [
            {"serverId": "other"},
            {"appName": "other"},
            {"sourceType": "git"},
            {"composeType": "stack"},
            {"randomize": True},
            {"isolatedDeployment": True},
            {"env": payload["env"] + "\nCOMPOSE_FILE=other.yml"},
            {"composePath": "other.yml"},
            {"composeFile": raw + "\n"},
            {"env": ""},
        ]
        for mutation in mutations:
            with (
                self.subTest(mutation=tuple(mutation)),
                patch.object(
                    api, "fetch_dokploy_target_payload", return_value={**payload, **mutation}
                ),
            ):
                with self.assertRaises(click.ClickException):
                    require_preview_resource_binding(
                        host="fixture",
                        token="fixture",
                        compose_id="preview",
                        environment=environment,
                        binding=binding,
                    )
        for mode in ("foreign-volume", "include", "extends"):
            changed = copy.deepcopy(parsed)
            if mode == "foreign-volume":
                changed["volumes"]["odoo_data"]["name"] = "production-data"
            elif mode == "include":
                changed["include"] = ["other.yml"]
            else:
                changed["services"]["web"]["extends"] = {"file": "other.yml", "service": "web"}
            with self.subTest(mode=mode), self.assertRaises(click.ClickException):
                _require_preview_destroy_source({**payload, "composeFile": yaml.safe_dump(changed)})

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
                {ODOO_PREVIEW_RESOURCES_OK_MARKER: "false"},
                {
                    ODOO_PREVIEW_RESOURCES_OK_MARKER: "true",
                    ODOO_PREVIEW_RESOURCES_BINDING_MARKER: "previous-target-receipt",
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

        for late, creating in ((False, False), (True, False), (False, True), (True, True)):
            with self.subTest(late=late, creating=creating), ExitStack() as stack:
                apply = OdooPreviewApplyEnvelope.model_validate(_refresh_payload()).apply
                apply = apply.model_copy(
                    update={
                        "dry_run_plan": apply.dry_run_plan.model_copy(
                            update={} if creating else {"compose_ref": "preview-target"}
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
                inspection = stack.enter_context(
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
                    qualify_existing_preview=True,
                )
                self.assertEqual(result.status, "fail")
                inspection.assert_called()
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
