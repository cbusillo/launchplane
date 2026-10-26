from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
import unittest

from control_plane.testing_compose_resources import (
    inspect_retained_compose,
    COMPOSE_RESOURCE_PROGRAM,
)


class RetainedComposeInspectionTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = self.enterContext(TemporaryDirectory())
        self.root = Path(temporary).resolve()
        self.volumes: dict[str, dict[str, Any]] = {}
        self.rows: dict[str, dict[str, Any]] = {}
        self.spec: dict[str, Any] = {
            "app_name": "lp-testing-fixture-recipient",
            "resources_sha256": "a" * 64,
            "volumes": {"testing-db": "database", "testing-config": "config"},
            "ports": [3308, 8001],
            "credentials": ["testing-password"],
            "render": {
                "services": {},
                "volumes": {
                    "database": {"name": "testing-db"},
                    "config": {"name": "testing-config"},
                },
            },
        }
        for name, role in self.spec["volumes"].items():
            path = self.root / "volumes" / name / "_data"
            path.mkdir(parents=True)
            (path / "retained-data").write_text("needed during qualification")
            self.volumes[name] = {
                "Name": name,
                "Driver": "local",
                "Options": {},
                "Mountpoint": str(path),
                "Labels": {
                    "io.launchplane.testing-resources": self.spec["resources_sha256"],
                    "io.launchplane.testing-resource-role": role,
                },
            }
        for index, (service, role, port, destination) in enumerate(
            (("web", "config", 8001, "/config"), ("database", "database", 3308, "/var/lib/mysql"))
        ):
            identifier = str(index + 1) * 64
            name = self.spec["render"]["volumes"][role]["name"]
            self.spec["render"]["services"][service] = {
                "image": "image-" + service,
                "environment": {"DB_PASSWORD": "testing-password"},
                "volumes": [role + ":" + destination],
                "ports": [f"{port}:8000"],
            }
            self.rows[identifier] = {
                "Id": identifier,
                "Image": "sha256:" + identifier,
                "Config": {
                    "Image": "image-" + service,
                    "Env": ["DB_PASSWORD=testing-password"],
                    "Labels": {
                        "com.docker.compose.project": self.spec["app_name"] + "-abc123",
                        "com.docker.compose.service": service,
                    },
                },
                "HostConfig": {
                    "PortBindings": {"8000/tcp": [{"HostPort": str(port), "HostIp": "0.0.0.0"}]}
                },
                "State": {"Running": True},
                "Mounts": [
                    {
                        "Type": "volume",
                        "Name": name,
                        "Source": self.volumes[name]["Mountpoint"],
                        "Destination": destination,
                        "RW": True,
                    }
                ],
            }
        self.images = {
            row["Image"]: {
                "Id": row["Image"],
                "RepoDigests": [row["Config"]["Image"]],
                "Config": {"Env": []},
            }
            for row in self.rows.values()
        }
        self.swarm_services: dict[str, Any] = {}
        for row in self.rows.values():
            row["NetworkSettings"] = {"Ports": deepcopy(row["HostConfig"]["PortBindings"])}
        self.calls: list[tuple[str, ...]] = []

    def docker(self, *arguments: str) -> str:
        if arguments == ("info", "--format", "{{json .DockerRootDir}}"):
            return json.dumps(str(self.root))
        if arguments == ("info", "--format", "{{json .Swarm.LocalNodeState}}"):
            return json.dumps("active" if self.swarm_services else "inactive")
        if arguments == ("service", "ls", "-q", "--no-trunc"):
            return "\n".join(self.swarm_services)
        if arguments[:2] == ("service", "inspect"):
            return json.dumps([self.swarm_services[arguments[2]]])
        self.calls.append(arguments)
        if arguments == ("ps", "-aq", "--no-trunc"):
            return "\n".join(self.rows)
        if arguments[0] == "inspect":
            return json.dumps([self.rows[arguments[1]]])
        if arguments[:2] == ("volume", "inspect"):
            return json.dumps([self.volumes[arguments[2]]])
        if arguments[:2] == ("image", "inspect"):
            return json.dumps([self.images[arguments[2]]])
        raise AssertionError(arguments)

    def test_populated_retained_storage_is_qualified_without_writes(self) -> None:
        digest = inspect_retained_compose(self.spec, self.docker)
        self.assertTrue(digest)
        for volume in self.volumes.values():
            self.assertEqual(
                (Path(volume["Mountpoint"]) / "retained-data").read_text(),
                "needed during qualification",
            )

    def test_empty_default_host_address_still_requires_effective_wildcard_listener(self) -> None:
        for row in self.rows.values():
            for entries in row["HostConfig"]["PortBindings"].values():
                entries[0]["HostIp"] = ""
        self.assertTrue(inspect_retained_compose(self.spec, self.docker))
        row = next(iter(self.rows.values()))
        row["NetworkSettings"]["Ports"] = {}
        with self.assertRaises(ValueError):
            inspect_retained_compose(self.spec, self.docker)

    def test_shared_storage_listener_and_credential_all_refuse(self) -> None:
        baseline = deepcopy(self.rows)
        for resource in ("storage", "listener", "credential"):
            with self.subTest(resource=resource):
                self.rows = deepcopy(baseline)
                foreign = deepcopy(next(iter(self.rows.values())))
                foreign["Id"] = "f" * 64
                foreign["Config"]["Labels"] = {"com.docker.compose.project": "production"}
                foreign["Config"]["Env"] = ["DB_PASSWORD=production-password"]
                foreign["Mounts"] = []
                foreign["HostConfig"]["PortBindings"] = {}
                if resource == "storage":
                    foreign["Mounts"] = deepcopy(next(iter(baseline.values()))["Mounts"])
                elif resource == "listener":
                    foreign["HostConfig"]["PortBindings"] = deepcopy(
                        next(iter(baseline.values()))["HostConfig"]["PortBindings"]
                    )
                else:
                    foreign["Config"]["Env"] = ["ANOTHER_KEY=testing-password"]
                self.rows[foreign["Id"]] = foreign
                with self.assertRaises(ValueError):
                    inspect_retained_compose(self.spec, self.docker)

    def test_custody_and_actual_mount_and_environment_drift_refuse(self) -> None:
        baseline = deepcopy(self.rows)
        for component in ("labels", "mount", "source", "environment", "missing", "stopped"):
            with self.subTest(component=component):
                self.rows = deepcopy(baseline)
                row = next(iter(self.rows.values()))
                if component == "labels":
                    row["Config"]["Labels"]["com.docker.compose.project"] = "other"
                elif component == "mount":
                    row["Mounts"][0]["Name"] = "unqualified-volume"
                elif component == "source":
                    row["Mounts"][0]["Source"] = str(self.root)
                elif component == "environment":
                    row["Config"]["Env"] = ["DB_PASSWORD=live-password"]
                elif component == "stopped":
                    row["State"]["Running"] = False
                else:
                    self.rows.pop(next(iter(self.rows)))
                with self.assertRaises(ValueError):
                    inspect_retained_compose(self.spec, self.docker)

    def test_unrelated_restart_between_qualifications_preserves_proof_but_conflicts_refuse(
        self,
    ) -> None:
        before = inspect_retained_compose(self.spec, self.docker)
        foreign = deepcopy(next(iter(self.rows.values())))
        foreign["Id"] = "f" * 64
        foreign["Config"]["Labels"] = {"com.docker.compose.project": "unrelated"}
        foreign["Config"]["Env"] = ["DB_PASSWORD=other-password"]
        foreign["Mounts"] = []
        foreign["HostConfig"]["PortBindings"] = {}
        foreign["NetworkSettings"]["Ports"] = {}
        self.rows[foreign["Id"]] = foreign
        self.assertEqual(inspect_retained_compose(self.spec, self.docker), before)
        foreign["State"]["StartedAt"] = "later-restart"
        self.assertEqual(inspect_retained_compose(self.spec, self.docker), before)
        foreign["Config"]["Env"] = ["DB_PASSWORD=testing-password"]
        with self.assertRaises(ValueError):
            inspect_retained_compose(self.spec, self.docker)

    def test_declared_tmpfs_is_supported_without_adopting_anonymous_storage(self) -> None:
        row = next(iter(self.rows.values()))
        service = self.spec["render"]["services"][
            row["Config"]["Labels"]["com.docker.compose.service"]
        ]
        service["tmpfs"] = ["/tmp:size=64m"]
        row["HostConfig"]["Tmpfs"] = {"/tmp": "size=64m"}
        self.assertTrue(inspect_retained_compose(self.spec, self.docker))
        row["Mounts"].append(
            {
                "Type": "volume",
                "Name": "anonymous",
                "Destination": "/unowned",
                "RW": True,
                "Source": "",
            }
        )
        with self.assertRaises(ValueError):
            inspect_retained_compose(self.spec, self.docker)

    def test_foreign_dynamic_and_range_ports_use_actual_publication(self) -> None:
        foreign = deepcopy(next(iter(self.rows.values())))
        foreign["Id"] = "f" * 64
        foreign["Config"]["Labels"] = {"com.docker.compose.project": "unrelated"}
        foreign["Config"]["Env"] = []
        foreign["Mounts"] = []
        foreign["HostConfig"]["PortBindings"] = {"80/tcp": [{"HostIp": "", "HostPort": ""}]}
        foreign["NetworkSettings"]["Ports"] = {
            "80/tcp": [{"HostIp": "0.0.0.0", "HostPort": "19000"}]
        }
        self.rows[foreign["Id"]] = foreign
        self.assertTrue(inspect_retained_compose(self.spec, self.docker))
        foreign["NetworkSettings"]["Ports"]["80/tcp"][0]["HostPort"] = str(self.spec["ports"][0])
        with self.assertRaises(ValueError):
            inspect_retained_compose(self.spec, self.docker)
        foreign["NetworkSettings"]["Ports"] = {}
        foreign["HostConfig"]["PortBindings"]["80/tcp"][0]["HostPort"] = "8000-8010"
        with self.assertRaises(ValueError):
            inspect_retained_compose(self.spec, self.docker)

    def test_scaled_down_swarm_current_and_previous_mounts_still_protect_testing(self) -> None:
        template: dict[str, Any] = {"TaskTemplate": {"ContainerSpec": {"Mounts": [], "Env": []}}}
        self.swarm_services["service-fixture"] = {
            "ID": "service-fixture",
            "Spec": deepcopy(template),
        }
        self.assertTrue(inspect_retained_compose(self.spec, self.docker))
        self.swarm_services["service-fixture"]["PreviousSpec"] = deepcopy(template)
        self.swarm_services["service-fixture"]["PreviousSpec"]["TaskTemplate"]["ContainerSpec"][
            "Mounts"
        ] = [{"Type": "volume", "Source": next(iter(self.volumes)), "Target": "/data"}]
        with self.assertRaises(ValueError):
            inspect_retained_compose(self.spec, self.docker)

    def test_image_digest_metadata_must_agree_with_the_actual_image(self) -> None:
        row = next(iter(self.rows.values()))
        service = self.spec["render"]["services"][
            row["Config"]["Labels"]["com.docker.compose.service"]
        ]
        image = "ghcr.io/example/site@sha256:" + "a" * 64
        service["image"] = row["Config"]["Image"] = image
        with self.assertRaises(ValueError):
            inspect_retained_compose(self.spec, self.docker)
        self.images[row["Image"]]["RepoDigests"] = [image]
        self.assertTrue(inspect_retained_compose(self.spec, self.docker))

    def test_read_only_administrative_parents_keep_exact_and_writable_mounts_protected(
        self,
    ) -> None:
        foreign = deepcopy(next(iter(self.rows.values())))
        foreign["Id"] = "f" * 64
        foreign["Config"]["Labels"] = {"com.docker.compose.project": "monitor"}
        foreign["Config"]["Env"] = []
        foreign["HostConfig"]["PortBindings"] = {}
        foreign["NetworkSettings"]["Ports"] = {}
        foreign["Mounts"] = [
            {"Type": "bind", "Source": str(self.root), "Destination": "/host", "RW": False}
        ]
        self.rows[foreign["Id"]] = foreign
        self.assertTrue(inspect_retained_compose(self.spec, self.docker))
        foreign["Mounts"][0]["RW"] = True
        with self.assertRaises(ValueError):
            inspect_retained_compose(self.spec, self.docker)
        foreign["Mounts"][0].update(
            RW=False, Source=next(iter(self.volumes.values()))["Mountpoint"]
        )
        with self.assertRaises(ValueError):
            inspect_retained_compose(self.spec, self.docker)
        foreign["Mounts"][0]["Source"] = str(
            Path(next(iter(self.volumes.values()))["Mountpoint"]).parent
        )
        with self.assertRaises(ValueError):
            inspect_retained_compose(self.spec, self.docker)
        foreign["Mounts"][0]["Source"] = str(self.root / "retired-bind")
        foreign["State"]["Running"] = False
        self.assertTrue(inspect_retained_compose(self.spec, self.docker))
        foreign["State"]["Running"] = True
        with self.assertRaises(FileNotFoundError):
            inspect_retained_compose(self.spec, self.docker)

    def test_image_retag_metadata_does_not_change_qualification(self) -> None:
        expected = inspect_retained_compose(self.spec, self.docker)
        for image in self.images.values():
            image["Metadata"] = {"LastTagTime": "new-tag-time"}
            image["RepoTags"] = ["unrelated-preview:tag"]
            image["RepoDigests"].append("unrelated@sha256:" + "f" * 64)
        self.assertEqual(inspect_retained_compose(self.spec, self.docker), expected)

        def retag(*arguments: str) -> str:
            result = self.docker(*arguments)
            if arguments[:2] == ("image", "inspect"):
                self.images[arguments[2]]["Metadata"]["LastTagTime"] += "-again"
            return result

        self.assertEqual(inspect_retained_compose(self.spec, retag), expected)

    def test_swarm_read_only_administrative_parents_keep_allocations_protected(self) -> None:
        template: dict[str, Any] = {"TaskTemplate": {"ContainerSpec": {"Mounts": []}}}
        allocation = Path(next(iter(self.volumes.values()))["Mountpoint"])
        for version in ("Spec", "PreviousSpec"):
            for source, read_only, allowed in (
                (self.root, True, True),
                (self.root, False, False),
                (allocation.parent, True, False),
                (allocation, True, False),
            ):
                with self.subTest(version=version, source=source, read_only=read_only):
                    row: dict[str, Any] = {"ID": "service-fixture", "Spec": deepcopy(template)}
                    row[version] = deepcopy(template)
                    row[version]["TaskTemplate"]["ContainerSpec"]["Mounts"] = [
                        {"Type": "bind", "Source": str(source), "ReadOnly": read_only}
                    ]
                    self.swarm_services = {"service-fixture": row}
                    if allowed:
                        self.assertTrue(inspect_retained_compose(self.spec, self.docker))
                    else:
                        with self.assertRaises(ValueError):
                            inspect_retained_compose(self.spec, self.docker)

    def test_concurrent_inventory_drift_refuses(self) -> None:
        reads = 0

        def drift(*arguments: str) -> str:
            nonlocal reads
            if arguments == ("ps", "-aq", "--no-trunc"):
                reads += 1
                if reads > 1:
                    return ""
            return self.docker(*arguments)

        with self.assertRaises(ValueError):
            inspect_retained_compose(self.spec, drift)

    def test_transported_program_executes_the_same_inspector(self) -> None:
        import base64
        import io
        from contextlib import redirect_stdout
        from types import SimpleNamespace
        from unittest.mock import patch
        import subprocess

        def run(arguments: list[str], **_kwargs: Any) -> Any:
            self.assertEqual(arguments[0], "docker")
            return SimpleNamespace(
                returncode=0, stdout=self.docker(*arguments[1:]).encode(), stderr=b""
            )

        output = io.StringIO()
        with (
            patch.object(subprocess, "run", side_effect=run),
            patch(
                "sys.argv",
                ["program", base64.b64encode(json.dumps(self.spec).encode()).decode(), "frame:"],
            ),
            redirect_stdout(output),
        ):
            exec(COMPOSE_RESOURCE_PROGRAM, {})
        result = json.loads(output.getvalue().removeprefix("frame:"))
        self.assertEqual(result["type"], "complete")
        self.assertEqual(result["sha256"], inspect_retained_compose(self.spec, self.docker))


class ComposeResourceFixture:
    def __init__(self, root: Path, spec: dict[str, Any]) -> None:
        self.root = root.resolve()
        self.spec = spec
        self.rows: dict[str, dict[str, Any]] = {}
        self.volumes: dict[str, dict[str, Any]] = {}
        for name, role in spec["volumes"].items():
            path = self.root / "volumes" / name / "_data"
            path.mkdir(parents=True, exist_ok=True)
            (path / "populated").write_text("retained")
            self.volumes[name] = {
                "Name": name,
                "Driver": "local",
                "Options": {},
                "Mountpoint": str(path),
                "Labels": {
                    "io.launchplane.testing-resources": spec["resources_sha256"],
                    "io.launchplane.testing-resource-role": role,
                },
            }
        names = {key: value["name"] for key, value in spec["render"]["volumes"].items()}
        for index, (name, service) in enumerate(spec["render"]["services"].items()):
            identifier = str(index + 1) * 64
            mounts = []
            for entry in service.get("volumes", []):
                key, destination = entry.split(":")[:2]
                volume = names[key]
                mounts.append(
                    {
                        "Type": "volume",
                        "Name": volume,
                        "Source": self.volumes[volume]["Mountpoint"],
                        "Destination": destination,
                        "RW": True,
                    }
                )
            bindings = {}
            for entry in service.get("ports", []):
                published, target = entry.split(":")
                bindings[target + "/tcp"] = [{"HostIp": "0.0.0.0", "HostPort": published}]
            self.rows[identifier] = {
                "Id": identifier,
                "Image": "sha256:" + identifier,
                "Config": {
                    "Image": service["image"],
                    "Env": [
                        f"{key}={value}" for key, value in service.get("environment", {}).items()
                    ],
                    "Labels": {
                        "com.docker.compose.project": spec["app_name"] + "-abc123",
                        "com.docker.compose.service": name,
                    },
                },
                "Mounts": mounts,
                "HostConfig": {"PortBindings": bindings},
                "State": {"Running": True},
            }

        self.images = {
            row["Image"]: {
                "Id": row["Image"],
                "RepoDigests": [row["Config"]["Image"]],
                "Config": {"Env": []},
            }
            for row in self.rows.values()
        }
        self.swarm_services: dict[str, Any] = {}
        for row in self.rows.values():
            row["NetworkSettings"] = {"Ports": deepcopy(row["HostConfig"]["PortBindings"])}

    def docker(self, *arguments: str) -> str:
        if arguments == ("info", "--format", "{{json .DockerRootDir}}"):
            return json.dumps(str(self.root))
        if arguments == ("info", "--format", "{{json .Swarm.LocalNodeState}}"):
            return json.dumps("active" if self.swarm_services else "inactive")
        if arguments == ("service", "ls", "-q", "--no-trunc"):
            return "\n".join(self.swarm_services)
        if arguments[:2] == ("service", "inspect"):
            return json.dumps([self.swarm_services[arguments[2]]])
        if arguments == ("ps", "-aq", "--no-trunc"):
            return "\n".join(self.rows)
        if arguments[0] == "inspect":
            return json.dumps([self.rows[arguments[1]]])
        if arguments[:2] == ("volume", "inspect"):
            return json.dumps([self.volumes[arguments[2]]])
        if arguments[:2] == ("image", "inspect"):
            return json.dumps([self.images[arguments[2]]])
        raise AssertionError(arguments)

    def read(self, **arguments: Any) -> None:
        digest = inspect_retained_compose(arguments["payload"], self.docker)
        arguments["receive"]({"type": "complete", "sha256": digest})
