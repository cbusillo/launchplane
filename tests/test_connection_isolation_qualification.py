"""Exercise the actual remote metadata qualifier using synthetic Docker records."""

import copy
import json
import subprocess
import sys
import unittest
from typing import Any, cast
import yaml

from control_plane.connection_isolation_qualification import (
    RESOURCE_QUALIFICATION_LIBRARY,
    RESOURCE_QUALIFICATION_PROGRAM,
)
from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.dokploy.compose import (
    ODOO_RAW_COMPOSE_REQUIRED_SERVICES,
    render_odoo_raw_compose_file,
)


def resource_fixture() -> tuple[dict[str, Any], str]:
    identity = RuntimeIdentity(
        product="example",
        context="example",
        instance="testing",
        deployment_record_id="deployment-fixture",
        artifact_id="artifact-fixture",
        source_git_ref="fixture-source",
    ).model_dump_json()
    compose = yaml.safe_load(
        render_odoo_raw_compose_file(
            image_reference="fixture-image",
            hold_web_until_integration_readback=True,
        )
    )
    environment = {
        "PLATFORM_INSTANCE": "testing",
        "PLATFORM_CONTEXT": "example",
        "ODOO_DB_NAME": "example_testing",
        "ODOO_DB_HOST": compose["x-odoo-env"]["ODOO_DB_HOST"],
        "ODOO_DB_PORT": compose["x-odoo-env"]["ODOO_DB_PORT"],
        "LAUNCHPLANE_RUNTIME_IDENTITY_JSON": identity,
    }

    def mount(name: str, destination: str) -> dict[str, Any]:
        return {
            "Type": "volume",
            "Name": name,
            "Source": f"/volumes/{name}",
            "Destination": destination,
            "RW": True,
        }

    containers: list[dict[str, Any]] = [
        {
            "id": f"{service}-id",
            "project": "example-testing-app",
            "service": service,
            "image": "postgres-image" if service == "database" else "odoo-image",
            "running": True,
            "ports": {},
            "environment": {} if service == "database" else dict(environment),
            "networks": {"private-network": f"172.24.0.{index}"},
            "mounts": [mount("db", compose["services"]["database"]["volumes"][0].split(":")[1])]
            if service == "database"
            else [mount("filestore", "/var/lib/odoo")],
        }
        for index, service in enumerate(ODOO_RAW_COMPOSE_REQUIRED_SERVICES, start=2)
    ]
    for item in containers:
        if item["service"] in {"web", "script-runner"}:
            item["mounts"].append(mount("logs", "/volumes/logs"))
        if item["service"] == "script-runner":
            item["mounts"].extend(
                {"Type": "bind", "Source": "/fixture/ssh", "RW": False, "Destination": path}
                for path in ("/root/.ssh", "/home/ubuntu/.ssh")
            )
    return {
        "containers": containers,
        "networks": [
            {
                "id": "private-network",
                "project": "example-testing-app",
                "members": [item["id"] for item in containers],
            }
        ],
        "volumes": [
            {
                "name": name,
                "driver": "local",
                "has_options": False,
                "mountpoint": f"/volumes/{name}",
            }
            for name in ("db", "filestore", "logs")
        ],
    }, identity


def inventory_rows(snapshot: dict[str, Any]) -> str:
    rows = []
    for item in snapshot["containers"]:
        container = copy.deepcopy(item)
        environment = container.pop("environment")
        container["networks"] = {
            key: {"NetworkID": key, "IPAddress": value}
            for key, value in container["networks"].items()
        }
        rows.append({"kind": "container", **container})
        rows.append(
            {
                "kind": "environment",
                "id": container["id"],
                "environment": [f"{key}={value}" for key, value in environment.items()],
            }
        )
    rows.extend({"kind": "volume", **item} for item in snapshot["volumes"])
    for item in snapshot["networks"]:
        rows.append({"kind": "network", **item, "members": {key: {} for key in item["members"]}})
    return "\n".join(json.dumps(row) for row in rows)


class ResourceQualificationTests(unittest.TestCase):
    def setUp(self) -> None:
        namespace: dict[str, Any] = {}
        exec(RESOURCE_QUALIFICATION_LIBRARY, namespace)
        self.qualify = namespace["qualify_resources"]
        self.snapshot, self.identity = resource_fixture()

    def run_qualifier(self, snapshot: dict[str, Any] | None = None) -> tuple[str, str]:
        return cast(
            tuple[str, str],
            self.qualify(
                snapshot or self.snapshot,
                "example-testing-app",
                "example_testing",
                "example",
                self.identity,
            ),
        )

    def test_exact_private_database_is_bound_to_reviewed_resources(self) -> None:
        digest, address = self.run_qualifier()
        self.assertEqual(address, self.snapshot["containers"][1]["networks"]["private-network"])
        self.snapshot["containers"][0]["image"] = "replacement"
        self.snapshot["containers"][2]["image"] = "replacement"
        self.assertNotEqual(self.run_qualifier()[0], digest)

    def test_docker_mount_and_inventory_order_does_not_change_the_review(self) -> None:
        digest, _ = self.run_qualifier()
        for item in self.snapshot["containers"]:
            item["mounts"].reverse()
        self.snapshot["containers"].reverse()
        self.snapshot["volumes"].reverse()
        self.snapshot["networks"][0]["members"].reverse()
        self.assertEqual(self.run_qualifier()[0], digest)

    def test_database_identity_and_runtime_must_match(self) -> None:
        for key, value in (
            ("PLATFORM_INSTANCE", "prod"),
            ("PLATFORM_CONTEXT", "another"),
            ("ODOO_DB_HOST", "production"),
            ("ODOO_DB_NAME", "another"),
            ("LAUNCHPLANE_RUNTIME_IDENTITY_JSON", "{}"),
        ):
            with self.subTest(key=key):
                snapshot = copy.deepcopy(self.snapshot)
                snapshot["containers"][2]["environment"][key] = value
                with self.assertRaises(ValueError):
                    self.run_qualifier(snapshot)

    def test_published_database_foreign_network_and_shared_mounts_are_rejected(self) -> None:
        for change in (
            "port",
            "network",
            "volume_options",
            "unknown_options",
            "bind",
            "exact",
            "ancestor",
            "descendant",
        ):
            with self.subTest(change=change):
                snapshot = copy.deepcopy(self.snapshot)
                if change == "port":
                    snapshot["containers"][1]["ports"] = {"5432/tcp": [{"HostPort": "5432"}]}
                elif change == "network":
                    snapshot["networks"][0]["members"].append("foreign-id")
                elif change in {"volume_options", "unknown_options"}:
                    snapshot["volumes"][0]["has_options"] = (
                        True if change == "volume_options" else None
                    )
                elif change == "bind":
                    snapshot["containers"][1]["mounts"][0]["Type"] = "bind"
                else:
                    source = {
                        "exact": "/volumes/db",
                        "ancestor": "/volumes",
                        "descendant": "/volumes/db/base",
                    }[change]
                    foreign = copy.deepcopy(snapshot["containers"][1])
                    foreign.update(id="foreign-id", project="production")
                    foreign["mounts"][0]["Source"] = source
                    snapshot["containers"].append(foreign)
                with self.assertRaises(ValueError):
                    self.run_qualifier(snapshot)

    def test_stopped_web_and_unrelated_host_activity_allow_the_same_resources(self) -> None:
        self.snapshot["containers"][0]["running"] = False
        self.snapshot["networks"][0]["members"].remove("web-id")
        digest, _ = self.run_qualifier()
        foreign = copy.deepcopy(self.snapshot["containers"][1])
        foreign.update(id="foreign-id", project="another", mounts=[])
        self.snapshot["containers"].append(foreign)
        self.snapshot["volumes"].append({"name": "foreign", "driver": "nfs"})
        self.assertEqual(self.run_qualifier()[0], digest)

    def test_real_program_filters_secrets_and_refuses_a_stale_review(self) -> None:
        digest, _ = self.run_qualifier()
        for item in self.snapshot["containers"]:
            item["environment"]["ODOO_DB_PASSWORD"] = "synthetic-secret-never-output"
        for reviewed, expected in ((digest, 0), ("wrong-review", 1)):
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    RESOURCE_QUALIFICATION_PROGRAM,
                    "example-testing-app",
                    "example_testing",
                    "example",
                    self.identity,
                    reviewed,
                ],
                input=inventory_rows(self.snapshot),
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
            self.assertNotIn("synthetic-secret", result.stdout + result.stderr)
