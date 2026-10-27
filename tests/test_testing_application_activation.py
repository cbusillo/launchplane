"""Reviewed fresh application inputs and real managed runtime delivery."""

import base64
from contextlib import nullcontext
from copy import deepcopy
from functools import partial
from io import StringIO
import json
import os
import subprocess
import sys
from typing import Any
import time
from uuid import uuid4
import unittest
from unittest.mock import Mock, patch

from control_plane import secrets
from control_plane.contracts.product_profile_record import (
    ProductSecretConfigRequirement,
    ProductOwnerSecretInput,
)
from control_plane.contracts.testing_application import (
    TestingApplicationSpecification,
    TestingApplicationState,
    TestingApplicationCredential,
)
from control_plane.contracts.testing_activation import TestingActivationRecord
from control_plane.odoo_testing_resources import ApplicationTestingResources
from control_plane.testing_application_credentials import (
    generation_bundle,
)
from tests import test_testing_activation as activation_fixture


class RetainedCapabilityProgramTests(unittest.TestCase):
    spec: dict[str, Any]
    configuration: dict[str, Any]
    task: dict[str, Any]
    containers: dict[str, Any]

    def test_fixed_program_is_identical_across_process_hash_seeds(self) -> None:
        code = (
            "import hashlib,json,sys; "
            "from control_plane.testing_application_capabilities import retained_application_capability_program; "
            "print(hashlib.sha256(retained_application_capability_program(json.load(sys.stdin)).encode()).hexdigest())"
        )
        programs = [
            subprocess.run(
                [sys.executable, "-c", code],
                input=json.dumps(self.spec),
                capture_output=True,
                text=True,
                check=True,
                env={**os.environ, "PYTHONHASHSEED": seed},
            ).stdout
            for seed in ("1", "2")
        ]
        self.assertEqual(*programs)

    @patch(
        "control_plane.runtime_platform_credentials.PLATFORM_CREDENTIAL_KEYS",
        frozenset({"FIXTURE_A", "FIXTURE_B", "FIXTURE_C"}),
    )
    def test_program_provenance_accepts_only_key_order_permutations(self) -> None:
        from control_plane.runtime_platform_credentials import PLATFORM_CREDENTIAL_KEYS
        from control_plane.testing_application_capabilities import (
            capability_program_matches,
            retained_application_capability_program,
        )

        current = retained_application_capability_program(self.spec)
        line = next(
            row for row in current.splitlines() if row.startswith("PLATFORM_CREDENTIAL_KEYS=")
        )
        keys = sorted(PLATFORM_CREDENTIAL_KEYS, reverse=True)
        historical_line = "PLATFORM_CREDENTIAL_KEYS=frozenset({" + ", ".join(map(repr, keys)) + "})"
        historical = current.replace(line, historical_line, 1)
        self.assertTrue(capability_program_matches(historical, current))
        self.assertTrue(capability_program_matches(current, current))
        replaced = ["X" * len(keys[0]), *keys[1:]]
        duplicated = [keys[1], *keys[1:]]
        foreign = [
            None,
            historical + "\n",
            historical.replace("set -euo pipefail", "set -euO pipefail", 1),
            historical.replace("python3 - ", "python3 + ", 1),
            historical.replace(historical_line, historical_line.replace("'", '"')),
            historical.replace(historical_line, historical_line + "\n" + historical_line),
            historical.replace(
                historical_line, "PLATFORM_CREDENTIAL_KEYS=frozenset(" + repr(keys) + ")"
            ),
        ]
        for altered in (replaced, duplicated):
            changed = "PLATFORM_CREDENTIAL_KEYS=frozenset({" + ", ".join(map(repr, altered)) + "})"
            foreign.append(historical.replace(historical_line, changed))
        for observed in foreign:
            with self.subTest(observed=type(observed).__name__):
                self.assertFalse(capability_program_matches(observed, current))

    def test_previous_stricter_program_is_recognized_without_partial_proof_edits(self) -> None:
        from control_plane.testing_application_capabilities import (
            capability_program_matches,
            retained_application_capability_program,
            PREPARED_CONTAINER_CAPABILITY_PROGRAM,
            PREPARED_CONTAINER_LOOKUP,
            PREPARED_CONTAINER_READBACK,
            CAPABILITY_FAILURE_REPORT,
            LEGACY_CAPABILITY_FAILURE_REPORT,
        )

        current = retained_application_capability_program(self.spec)
        historical = current.replace(CAPABILITY_FAILURE_REPORT, LEGACY_CAPABILITY_FAILURE_REPORT, 1)
        self.assertTrue(capability_program_matches(historical, current))
        for fragment in (
            PREPARED_CONTAINER_CAPABILITY_PROGRAM,
            PREPARED_CONTAINER_LOOKUP,
            PREPARED_CONTAINER_READBACK,
        ):
            historical = historical.replace(fragment, "", 1)
            self.assertEqual(
                capability_program_matches(historical, current),
                fragment == PREPARED_CONTAINER_READBACK,
            )
        changed = deepcopy(self.spec)
        changed["application_id"] = "foreign-target"
        self.assertFalse(
            capability_program_matches(historical, retained_application_capability_program(changed))
        )
        self.assertFalse(
            capability_program_matches(
                historical.replace("retained image digest unavailable", "removed image check"),
                current,
            )
        )

    def test_historical_host_program_keeps_terminal_and_provenance_guards(self) -> None:
        from pathlib import Path

        from control_plane.runtime_platform_credentials import PLATFORM_CREDENTIAL_KEYS
        from control_plane.testing_application_capabilities import (
            capability_program_matches,
            retained_application_capability_program,
        )
        from control_plane.testing_application_host import run_application_host_program

        current = retained_application_capability_program(self.spec)
        line = next(
            row for row in current.splitlines() if row.startswith("PLATFORM_CREDENTIAL_KEYS=")
        )
        historical = current.replace(
            line,
            "PLATFORM_CREDENTIAL_KEYS=frozenset({"
            + ", ".join(map(repr, sorted(PLATFORM_CREDENTIAL_KEYS, reverse=True)))
            + "})",
        )
        original = {"scheduleId": "original", "script": historical, "enabled": False}
        readback = {**original, "scheduleId": "readback"}
        changed = historical.replace("set -euo pipefail", "set -euO pipefail", 1)
        for program, previous in (
            (original, readback),
            ({**original, "enabled": True}, readback),
            ({**original, "script": changed}, readback),
            (original, {**readback, "enabled": True}),
            (original, {**readback, "script": changed}),
        ):
            with (
                self.subTest(program=program["enabled"]),
                patch(
                    "control_plane.dokploy.source.read_dokploy_config",
                    return_value=("fixture", "fixture"),
                ),
                patch(
                    "control_plane.dokploy.api.fetch_dokploy_target_payload",
                    return_value={"applicationId": "fixture"},
                ),
                patch(
                    "control_plane.dokploy.post_deploy._resolve_dokploy_schedule_runtime",
                    return_value=("application", "fixture", "fixture", "fixture"),
                ),
                patch(
                    "control_plane.testing_application_host._find_storage_schedule",
                    side_effect=[program, previous],
                ),
                patch(
                    "control_plane.testing_application_host._require_terminal_storage_history"
                ) as terminal,
                patch(
                    "control_plane.dokploy.post_deploy.run_manual_data_schedule", return_value={}
                ) as dispatch,
            ):
                run = partial(
                    run_application_host_program,
                    root=Path("."),
                    retained_target_id="fixture",
                    context="fixture",
                    namespace="fixture",
                    name="fixture",
                    prepare_script=current,
                    readback_script=current,
                    before_effect=Mock(),
                    script_matches=capability_program_matches,
                )
                if program == original and previous == readback:
                    run()
                    self.assertEqual(terminal.call_count, 2)
                    self.assertEqual(
                        dispatch.call_args.kwargs["schedule_payload"]["script"], current
                    )
                else:
                    with self.assertRaises(ValueError):
                        run()
                    dispatch.assert_not_called()

    def setUp(self) -> None:
        from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
        from control_plane.event_testing_deploy import event_testing_deploy_request
        from control_plane.workflows.generic_web_deploy import _build_runtime_identity
        from tests.support.profiles import product_profile_payload
        from tests.test_generic_web_deploy_recovery import _generic_web_recovery_target

        profile = LaunchplaneProductProfileRecord.model_validate(product_profile_payload())
        lane = next(item for item in profile.lanes if item.instance == "testing")
        artifact = "example/app@sha256:" + "a" * 64
        event = event_testing_deploy_request(
            product=profile.product,
            image_reference=artifact,
            source_commit="a" * 40,
            deploy_reference="example/app:sha-" + "a" * 40,
        )
        request = _generic_web_recovery_target().ship_request.model_copy(
            update={
                "artifact_id": event.deploy.artifact_id,
                "deploy_reference": event.deploy.deploy_reference,
                "source_git_ref": event.deploy.source_git_ref,
            }
        )
        self.identity = _build_runtime_identity(
            profile=profile,
            lane=lane,
            ship_request=request,
            deployment_record_id="original-deployment",
        ).model_dump(mode="json")
        self.spec = {
            "product": profile.product,
            "context": lane.context,
            "application": "retained-app",
            "application_id": "retained-id",
            "deployments": {
                "original-deployment": {
                    "artifact": artifact,
                    "provider_reference": event.deploy.deploy_reference,
                    "source": event.deploy.source_git_ref,
                }
            },
        }
        self.configuration = {
            "Env": ["LAUNCHPLANE_RUNTIME_IDENTITY_JSON=" + json.dumps(self.identity)],
            "Image": event.deploy.deploy_reference + "@" + artifact.split("@", 1)[1],
            "Mounts": [
                {"Type": "volume", "Source": "retained-state", "Target": "/var/lib/example"}
            ],
        }
        self.nodes = [{"ID": "retained-node", "Self": True}]
        self.service = {
            "ID": "retained-service",
            "Spec": {
                "Name": "retained-app",
                "TaskTemplate": {"ContainerSpec": deepcopy(self.configuration)},
            },
        }
        self.task = {
            "ID": "original-task",
            "ServiceID": "retained-service",
            "NodeID": self.nodes[0]["ID"],
            "Spec": {"ContainerSpec": deepcopy(self.configuration)},
            "Status": {"ContainerStatus": {"ContainerID": "original-container"}},
        }
        self.tasks = {self.task["ID"]: self.task}
        self.containers = {
            "original-container": {
                "Id": "original-container",
                "Image": "sha256:" + "b" * 64,
                "Config": {
                    "Image": event.deploy.deploy_reference,
                    "Labels": {"com.docker.swarm.service.name": "retained-app"},
                    "Env": list(self.configuration["Env"]),
                },
                "Mounts": [
                    {
                        "Type": "volume",
                        "Name": "retained-state",
                        "Source": "/var/lib/docker/volumes/retained-state/_data",
                    }
                ],
            },
            "provider-manager": {
                "Id": "provider-manager",
                "Config": {"Labels": {}, "Env": ["DOKPLOY_TOKEN=provider-private-token"]},
                "Mounts": [{"Type": "bind", "Source": "/var/run/docker.sock"}],
            },
        }

    def docker(self, arguments: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        values = arguments[1:]
        if values[:2] == ["node", "ls"]:
            result = "\n".join(json.dumps(node) for node in self.nodes)
        elif values == ["service", "inspect", "retained-app"]:
            result = json.dumps([self.service])
        elif values == ["service", "ps", "--no-trunc", "-q", "retained-app"]:
            result = "\n".join(self.tasks)
        elif values == [
            "ps",
            "-aq",
            "--no-trunc",
            "--filter",
            "label=com.docker.swarm.service.name=retained-app",
        ]:
            result = "\n".join(
                key
                for key, container in self.containers.items()
                if (container["Config"].get("Labels") or {}).get("com.docker.swarm.service.name")
                == "retained-app"
            )
        elif values[:2] == ["image", "inspect"]:
            result = json.dumps(
                [
                    {
                        "Id": values[2],
                        "RepoDigests": [self.identity["artifact_id"]],
                        "Config": {"Env": getattr(self, "image_environment", [])},
                    }
                ]
            )
        elif values[:3] == ["inspect", "--type", "task"]:
            result = json.dumps([self.tasks[values[3]]])
        elif values[:3] == ["inspect", "--type", "container"]:
            result = json.dumps(
                [
                    container
                    for container in self.containers.values()
                    if container.get("Name", "").removeprefix("/") == values[3]
                    and not values[3].startswith("/")
                ]
            )
        elif len(values) == 2 and values[0] == "inspect" and values[1] in self.containers:
            result = json.dumps([self.containers[values[1]]])
        else:
            raise AssertionError(arguments)
        return subprocess.CompletedProcess(arguments, 0, result, "")

    def observe(self) -> str:
        from control_plane.testing_application_capabilities import (
            retained_application_capability_program,
        )

        script = retained_application_capability_program(self.spec)
        delimiter = "LP_RETAINED_CAPABILITY"
        header = script.split("<<'" + delimiter + "'\n", 1)[1].rsplit("\n" + delimiter, 1)[0]
        output = StringIO()
        with (
            patch.object(
                sys, "argv", ["program", base64.b64encode(json.dumps(self.spec).encode()).decode()]
            ),
            patch.object(subprocess, "run", side_effect=self.docker),
            patch("sys.stdout", output),
        ):
            try:
                exec(compile(header, "<stdin>", "exec"), {})
            finally:
                self.last_program_output = output.getvalue()
                self.last_program_body = header
        return output.getvalue()

    def test_native_refusals_preserve_distinct_bounded_locations_without_values(self) -> None:
        from control_plane.dokploy.post_deploy import extract_odoo_post_deploy_readback_markers
        from control_plane.dokploy.post_deploy import _safe_odoo_post_deploy_marker
        from control_plane.dokploy.native_copy_stream import NativeProgramFailed

        original = deepcopy(self.task)
        locations = []
        private = "private-retained-capability-value"
        for kind in ("mount", "credential"):
            self.task = deepcopy(original)
            self.tasks = {self.task["ID"]: self.task}
            configuration = self.task["Spec"]["ContainerSpec"]
            if kind == "mount":
                configuration["Mounts"] = [
                    {
                        "Type": "bind",
                        "Source": "/var/run/docker.sock",
                        "Target": "/var/run/docker.sock",
                    }
                ]
            else:
                configuration["Env"].append("DOKPLOY_TOKEN=" + private)
            with self.subTest(kind=kind), self.assertRaises(SystemExit):
                self.observe()
            markers = extract_odoo_post_deploy_readback_markers({"logs": self.last_program_output})
            from control_plane.dokploy.native_copy_stream import CAPABILITY_FAILURE_SITES_MARKER

            failure = NativeProgramFailed(
                "Retained capability refused.",
                program=self.last_program_body,
                program_sites=[
                    int(value) for value in markers[CAPABILITY_FAILURE_SITES_MARKER].split(",")
                ],
            )
            self.assertTrue(failure.program_sites)
            import ast
            from control_plane.testing_application_capabilities import (
                capability_program_body,
                retained_application_capability_program,
            )

            body = capability_program_body(retained_application_capability_program(self.spec))
            self.assertIn(
                failure.program_sites[-1],
                {node.lineno for node in ast.walk(ast.parse(body)) if isinstance(node, ast.Raise)},
            )
            self.assertNotIn(private, self.last_program_output)
            for line in self.last_program_output.splitlines():
                key, separator, value = line.partition("=")
                self.assertTrue(separator and _safe_odoo_post_deploy_marker(key, value))
            locations.append(failure.program_sites)
        self.assertNotEqual(*locations)

    def test_attributable_named_volume_original_ignores_provider_manager_authority(self) -> None:
        self.assertIn("testing_resources_ok=true", self.observe())

    def test_retained_socket_mount_refuses_even_without_resource_backing_overlap(self) -> None:
        self.task["Spec"]["ContainerSpec"]["Mounts"] = [
            {"Type": "bind", "Source": "/var/run/docker.sock", "Target": "/var/run/docker.sock"}
        ]
        with self.assertRaises(SystemExit):
            self.observe()

    def test_inherited_platform_key_in_captured_original_task_refuses(self) -> None:
        self.task["Spec"]["ContainerSpec"]["Env"].append("DOKPLOY_TOKEN=retained-admin-capability")
        with self.assertRaises(SystemExit):
            self.observe()

    def test_current_clean_task_cannot_substitute_an_unrelated_deployment(self) -> None:
        self.spec["deployments"] = {
            "different-original-deployment": self.spec["deployments"]["original-deployment"]
        }
        with self.assertRaises(SystemExit):
            self.observe()

    def prepare_container_without_status_id(self) -> None:
        self.task["Slot"] = 1
        self.task["Status"] = {"State": "ready"}
        self.task["Spec"]["ContainerSpec"]["Image"] = self.spec["deployments"][
            "original-deployment"
        ]["provider_reference"]
        container = self.containers["original-container"]
        container["Name"] = "/retained-app.1.original-task"
        container["State"] = {"Status": "created", "Running": False}
        container["Config"]["Labels"].update(
            {
                "com.docker.swarm.service.id": self.service["ID"],
                "com.docker.swarm.task.id": self.task["ID"],
                "com.docker.swarm.node.id": self.nodes[0]["ID"],
            }
        )

    def test_prepared_container_proves_image_before_task_status_publishes_id(self) -> None:
        self.prepare_container_without_status_id()
        self.assertIn("testing_resources_ok=true", self.observe())

    def test_prepared_digest_pinned_task_keeps_existing_image_proof(self) -> None:
        self.prepare_container_without_status_id()
        self.task["Spec"]["ContainerSpec"]["Image"] = self.configuration["Image"]
        for state in ("preparing", "ready", "shutdown"):
            with self.subTest(state=state):
                self.task["Status"] = {"State": state}
                self.assertIn("testing_resources_ok=true", self.observe())

    def test_prepared_container_requires_actual_digest_and_merged_image_inputs(self) -> None:
        self.prepare_container_without_status_id()
        self.image_environment = ["IMAGE_DEFAULT=present"]
        with self.assertRaises(SystemExit):
            self.observe()
        self.containers["original-container"]["Config"]["Env"].extend(self.image_environment)
        self.assertIn("testing_resources_ok=true", self.observe())
        self.identity["artifact_id"] = "example/app@sha256:" + "c" * 64
        with self.assertRaises(SystemExit):
            self.observe()

    def test_prepared_container_or_task_change_during_proof_refuses(self) -> None:
        for changed_record in ("container", "task"):
            self.setUp()
            self.prepare_container_without_status_id()
            docker = self.docker

            def changed(arguments: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                result = docker(arguments, **kwargs)
                if arguments[1:3] == ["image", "inspect"]:
                    if changed_record == "container":
                        self.containers["original-container"]["State"] = {
                            "Status": "running",
                            "Running": True,
                        }
                    else:
                        self.task["Status"] = {"State": "starting"}
                return result

            with (
                self.subTest(changed_record=changed_record),
                patch.object(self, "docker", side_effect=changed),
                self.assertRaises(SystemExit),
            ):
                self.observe()

    def test_unproven_prepared_container_never_supplies_tag_image_proof(self) -> None:
        self.prepare_container_without_status_id()
        original = deepcopy(self.containers)
        for mutation in (
            {"Name": "/foreign.1.original-task"},
            {"State": {"Status": "running", "Running": True}},
            {"Image": "not-an-image-id"},
        ):
            with self.subTest(mutation=mutation):
                self.containers = deepcopy(original)
                self.containers["original-container"].update(mutation)
                with self.assertRaises(SystemExit):
                    self.observe()
        for key in ("service.id", "service.name", "task.id", "node.id"):
            with self.subTest(label=key):
                self.containers = deepcopy(original)
                self.containers["original-container"]["Config"]["Labels"][
                    "com.docker.swarm." + key
                ] = "foreign"
                with self.assertRaises(SystemExit):
                    self.observe()
        self.containers = deepcopy(original)
        duplicate = deepcopy(self.containers["original-container"])
        duplicate["Id"] = "duplicate"
        self.containers["duplicate"] = duplicate
        with self.assertRaises(SystemExit):
            self.observe()
        self.containers = deepcopy(original)
        self.containers["original-container"]["Config"]["Env"].append("FOREIGN_INPUT=changed")
        with self.assertRaises(SystemExit):
            self.observe()
        self.containers = deepcopy(original)
        self.containers["original-container"]["Config"]["Image"] = "example/foreign:changed"
        with self.assertRaises(SystemExit):
            self.observe()
        self.containers = deepcopy(original)
        self.task["Status"]["ContainerStatus"] = {"ContainerID": "different-container"}
        with self.assertRaises(SystemExit):
            self.observe()

    def test_prospective_and_removed_immutable_tasks_keep_capability_custody(self) -> None:
        original = deepcopy(self.task)
        self.containers.pop("original-container")
        for status in (
            {"State": "preparing", "ContainerStatus": {}},
            {"State": "rejected", "ContainerStatus": {}},
            original["Status"],
        ):
            with self.subTest(status=status):
                self.task["Status"] = status
                self.assertIn("testing_resources_ok=true", self.observe())
        self.task["Spec"]["ContainerSpec"]["Image"] = self.spec["deployments"][
            "original-deployment"
        ]["provider_reference"]
        with self.assertRaises(SystemExit):
            self.observe()
        self.task.update(deepcopy(original))
        self.task["Status"] = {"ContainerStatus": {}}
        self.task["Spec"]["ContainerSpec"]["Env"].append("DOKPLOY_TOKEN=unsafe")
        with self.assertRaises(SystemExit):
            self.observe()

    def test_complete_local_inventory_and_extant_capability_scan_remain_required(self) -> None:
        for nodes in (
            [],
            [{"ID": "remote", "Self": False}],
            [*self.nodes, {"ID": "remote", "Self": False}],
        ):
            with self.subTest(nodes=nodes):
                with patch.object(self, "nodes", nodes), self.assertRaises(SystemExit):
                    self.observe()
        self.task["NodeID"] = "remote"
        with self.assertRaises(SystemExit):
            self.observe()
        self.task["NodeID"] = self.nodes[0]["ID"]
        self.containers["extra-retained"] = {
            "Id": "extra-retained",
            "Config": {"Env": [], "Labels": {"com.docker.swarm.service.name": "retained-app"}},
            "Mounts": [{"Type": "bind", "Source": "/var/run/docker.sock"}],
        }
        with self.assertRaises(SystemExit):
            self.observe()


class ApplicationInputTests(unittest.TestCase):
    def test_data_mounts_reject_protected_descendants_and_unnormalized_paths(self) -> None:
        for path in (
            "/etc/ssh",
            "/proc/sys",
            "/dev/shm",
            "/run/docker",
            "/sys/fs",
            "/var/../etc",
            "/var//data",
        ):
            with self.subTest(path=path), self.assertRaises(ValueError):
                TestingApplicationState(kind="volume", container_path=path)
        self.assertEqual(
            TestingApplicationState(
                kind="volume", container_path="/var/lib/example"
            ).container_path,
            "/var/lib/example",
        )

    def test_deferred_state_kind_and_options_refuse_at_contract_boundary(self) -> None:
        for payload in (
            {"kind": "postgresql"},
            {"kind": "none", "connection_binding": "DATABASE_URL"},
            {
                "kind": "volume",
                "container_path": "/var/lib/example",
                "image_reference": "example/db@sha256:" + "a" * 64,
            },
        ):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                TestingApplicationState.model_validate(payload)

    def test_actual_state_kind_controls_resource_count_and_binding(self) -> None:
        common = dict(
            product="fixture",
            context="fixture",
            operation_id="allocation",
            target_id="old",
            namespace="lp-testing-" + "a" * 32,
        )
        none = ApplicationTestingResources.model_validate({**common, "state_kind": "none"})
        local = ApplicationTestingResources.model_validate({**common, "state_kind": "volume"})
        self.assertEqual(none.volumes, {})
        self.assertEqual(set(local.volumes), {"state"})
        self.assertNotEqual(none.binding_sha256, local.binding_sha256)


class ApplicationCredentialTests(unittest.TestCase):
    record: TestingActivationRecord

    def discord_intent(self) -> str:
        profile = self.store.read_product_profile_record(self.record.product)
        profile.expected_config.managed_secret_bindings = (
            ProductSecretConfigRequirement(
                binding_key="DB_PASSWORD",
                context=self.record.context,
                instance="testing",
                external_provider="discord_bot",
            ),
        )
        self.store.write_product_profile_record(profile)
        self.record.request.application = TestingApplicationSpecification.model_validate(
            {
                **self.specification.model_dump(),
                "local_credentials": [],
                "external_credentials": [{"binding_key": "DB_PASSWORD", "guild_ids": ["10"]}],
            }
        )
        token = base64.urlsafe_b64encode(b"123").decode().rstrip("=") + ".fixture.signature"
        version = self.store.read_secret_version("testing-version")
        ciphertext, key_id = secrets._encrypt_secret_value(token)
        self.store.write_secret_version(
            version.model_copy(
                update={
                    "ciphertext": ciphertext,
                    "key_id": key_id,
                    "value_origin": None,
                }
            )
        )
        return token

    def discord_response(self, url: str, **kwargs: Any) -> Any:
        from control_plane.outbound_http import PublicHttpResponse

        payload: Any = []
        if url.endswith("/users/@me"):
            payload = {"id": "123", "bot": True}
        elif url.endswith("/applications/@me"):
            payload = {"id": "456", "bot": {"id": "123"}}
        elif url.endswith("/gateway/bot"):
            payload = {"session_start_limit": {"max_concurrency": 1}}
        elif "/guilds?" in url and "after=" not in url:
            payload = [{"id": "10"}]
        return PublicHttpResponse(200, "", 0, (), json.dumps(payload).encode())

    def test_native_discord_proof_allows_exact_carried_version_and_refuses_drift(self) -> None:
        token = self.discord_intent()
        from control_plane.testing_external_capabilities import observe_activation_discord
        from control_plane.testing_application_credentials import runtime_activation_bundle

        with patch(
            "control_plane.discord_external_resources.request_public_http",
            side_effect=self.discord_response,
        ):
            proof = observe_activation_discord(
                self.store,
                self.record,
                authorize=self.store.list_product_profile_records,
                check_current=lambda: None,
                actor="fixture",
                trace_id="launchplane_req_fixture",
            )
        self.record.external_capabilities = (proof,)
        self.assertEqual(proof.guild_ids, ("10",))
        self.assertNotIn(token, proof.model_dump_json())
        self.assertFalse(generation_bundle(self.store, self.record).secret_records)
        self.assertFalse(runtime_activation_bundle(self.store, self.record).secret_records)
        from control_plane.testing_external_capabilities import require_discord_capability
        from control_plane.discord_identity import capture_identity_inputs
        from control_plane.contracts.testing_activation import (
            TestingActivationRefusal,
            TestingActivationRefusalCode,
        )

        profile = self.store.read_product_profile_record(self.record.product)
        self.store.write_product_profile_record(
            profile.model_copy(update={"display_name": "Renamed fixture"})
        )
        require_discord_capability(self.store, self.record, proof)
        with self.assertRaises(TestingActivationRefusal) as changed:
            require_discord_capability(
                self.store, self.record.model_copy(update={"plan_sha256": "f" * 64}), proof
            )
        self.assertEqual(changed.exception.code, TestingActivationRefusalCode.AUTHORITY_CHANGED)
        with self.assertRaises(TestingActivationRefusal) as changed:
            require_discord_capability(
                self.store, self.record, proof.model_copy(update={"guild_ids": ("20",)})
            )
        self.assertEqual(changed.exception.code, TestingActivationRefusalCode.AUTHORITY_CHANGED)
        from dataclasses import replace

        captured = capture_identity_inputs(self.store, self.record.product)
        compared = replace(
            captured, production=((captured.binding, captured.record, captured.version),)
        )
        with patch(
            "control_plane.testing_external_capabilities.capture_identity_inputs",
            return_value=compared,
        ):
            require_discord_capability(self.store, self.record, proof)
            require_discord_capability(
                self.store, self.record.model_copy(update={"published": True}), proof
            )
        rotated = captured.version.model_copy(update={"version_id": "rotated-testing-fixture"})
        self.store.write_secret_version(rotated)
        self.store.write_secret_record(
            captured.record.model_copy(update={"current_version_id": rotated.version_id})
        )
        with self.assertRaises(TestingActivationRefusal) as changed:
            require_discord_capability(self.store, self.record, proof)
        self.assertEqual(changed.exception.code, TestingActivationRefusalCode.AUTHORITY_CHANGED)
        self.store.write_secret_record(captured.record)
        version = self.store.read_secret_version("testing-version")
        self.store.write_secret_version(version.model_copy(update={"value_origin": "generated"}))
        for consume in (generation_bundle, runtime_activation_bundle):
            with self.assertRaises(ValueError):
                consume(self.store, self.record)

    def test_discord_admission_refuses_incomplete_or_foreign_guild_without_writes(self) -> None:
        self.discord_intent()
        from control_plane.testing_external_capabilities import (
            observe_activation_discord,
            ExternalCapabilityReadRefusal,
        )
        from control_plane.outbound_http import PublicHttpResponse

        for payload in (b'{"retry_after":120,"global":false}', b'[{"id":"20"}]'):

            def respond(url: str, **kwargs: Any) -> Any:
                if "/guilds?" in url:
                    return PublicHttpResponse(
                        429 if payload.startswith(b"{") else 200, "", 0, (), payload
                    )
                return self.discord_response(url, **kwargs)

            records = self.store.list_secret_records()
            with patch(
                "control_plane.discord_external_resources.request_public_http", side_effect=respond
            ):
                with self.assertRaises(ExternalCapabilityReadRefusal) as refused:
                    observe_activation_discord(
                        self.store,
                        self.record,
                        authorize=self.store.list_product_profile_records,
                        check_current=lambda: None,
                        actor="fixture",
                        trace_id="launchplane_req_fixture",
                    )
                assert refused.exception.result.observation is not None
                if payload.startswith(b"{"):
                    self.assertEqual(
                        refused.exception.result.observation.rate_limits[0].retry_after, 120
                    )
            self.assertEqual(self.store.list_secret_records(), records)

    def production_value(self, value: str, version_id: str) -> None:
        from control_plane.discord_identity import capture_identity_inputs

        captured = capture_identity_inputs(
            self.store, self.record.product, compare_production=False
        )
        profile = self.store.read_product_profile_record(self.record.product)
        if not any(lane.instance == "prod" for lane in profile.lanes):
            profile.lanes = (
                *profile.lanes,
                profile.lanes[0].model_copy(update={"instance": "prod"}),
            )
            self.store.write_product_profile_record(profile)
        ciphertext, key_id = secrets._encrypt_secret_value(value)
        self.store.write_secret_record(
            captured.record.model_copy(
                update={
                    "secret_id": "production-comparison",
                    "name": "fixture-production",
                    "instance": "prod",
                    "current_version_id": version_id,
                }
            )
        )
        self.store.write_secret_version(
            captured.version.model_copy(
                update={
                    "version_id": version_id,
                    "secret_id": "production-comparison",
                    "ciphertext": ciphertext,
                    "key_id": key_id,
                }
            )
        )
        self.store.write_secret_binding(
            captured.binding.model_copy(
                update={
                    "binding_id": "production-comparison",
                    "secret_id": "production-comparison",
                    "instance": "prod",
                    "binding_key": "API_TOKEN",
                    "declared_secret_class": "prod_only",
                }
            )
        )

    def test_forward_comparison_accepts_rotation_and_refuses_same_bot_without_discord(self) -> None:
        from control_plane.testing_application_activation import ApplicationActivationAdapter
        from control_plane.testing_external_capabilities import (
            observe_activation_discord,
            ExternalCapabilityConflict,
        )

        token = self.discord_intent()
        self.production_value("production-unrelated", "production-initial")
        with patch(
            "control_plane.discord_external_resources.request_public_http",
            side_effect=self.discord_response,
        ):
            proof = observe_activation_discord(
                self.store,
                self.record,
                authorize=self.store.list_product_profile_records,
                check_current=lambda: None,
                actor="fixture",
                trace_id="launchplane_req_fixture",
            )
        self.record.external_capabilities = (proof,)
        adapter = ApplicationActivationAdapter(
            self.fixture.adapter.root,
            self.store,
            self.record.product,
            self.record.request,
            "fixture",
            self.record.scope,
            "launchplane_req_fixture",
            authorize_external=self.store.list_product_profile_records,
        )
        self.production_value("another-unrelated-value", "production-rotated")
        with patch("control_plane.discord_external_resources.request_public_http") as provider:
            adapter.require_production_separation(self.record, lambda: None)
            self.assertFalse(generation_bundle(self.store, self.record).secret_records)
            for index, production in enumerate(
                (token, token.split(".")[0] + ".different.signature")
            ):
                self.production_value(production, f"production-match-{index}")
                with self.assertRaises(ExternalCapabilityConflict):
                    adapter.require_production_separation(self.record, lambda: None)
            adapter.authorize_external = Mock(side_effect=PermissionError("Denied"))
            with patch("control_plane.secrets._decrypt_secret_value") as decrypt:
                with self.assertRaises(PermissionError):
                    adapter.require_production_separation(self.record, lambda: None)
                decrypt.assert_not_called()
            provider.assert_not_called()

    def test_structural_discord_plan_cannot_install_without_native_proof(self) -> None:
        self.discord_intent()
        from control_plane.testing_application_credentials import credential_binding_names

        assert self.record.request.application is not None

        self.assertFalse(
            credential_binding_names(
                self.store,
                self.record.product,
                self.record.context,
                self.record.request.application,
            )
        )
        with self.assertRaises(ValueError):
            generation_bundle(self.store, self.record)

    def test_runtime_resolution_rechecks_the_proved_external_version(self) -> None:
        from types import SimpleNamespace
        from control_plane.testing_application_activation import ApplicationActivationAdapter
        from control_plane.testing_external_capabilities import observe_activation_discord
        from control_plane.discord_identity import capture_identity_inputs
        from control_plane.contracts.testing_activation import (
            TestingActivationRefusal,
            TestingActivationRefusalCode,
        )

        self.discord_intent()
        with patch(
            "control_plane.discord_external_resources.request_public_http",
            side_effect=self.discord_response,
        ):
            proof = observe_activation_discord(
                self.store,
                self.record,
                authorize=self.store.list_product_profile_records,
                check_current=lambda: None,
                actor="fixture",
                trace_id="launchplane_req_fixture",
            )
        self.record.external_capabilities = (proof,)
        self.record = self.record.model_copy(
            update={"application_readiness": SimpleNamespace(runtime_identity=object())}
        )
        captured = capture_identity_inputs(self.store, self.record.product)
        adapter = ApplicationActivationAdapter(
            self.fixture.adapter.root,
            self.store,
            self.record.product,
            self.record.request,
            "fixture",
            self.record.scope,
            "launchplane_req_fixture",
        )

        def rotate(**kwargs: Any) -> Any:
            version = captured.version.model_copy(
                update={"version_id": "rotated-during-resolution"}
            )
            self.store.write_secret_version(version)
            self.store.write_secret_record(
                captured.record.model_copy(update={"current_version_id": version.version_id})
            )
            return SimpleNamespace(
                values={"DB_PASSWORD": "unproved-fixture"}, secret_keys={"DB_PASSWORD"}
            )

        with (
            patch(
                "control_plane.testing_application_credentials.staged_runtime_values",
                return_value={},
            ),
            patch(
                "control_plane.runtime_environments.resolve_site_runtime_environment",
                side_effect=rotate,
            ),
            patch(
                "control_plane.testing_application_activation.runtime_identity_env", return_value={}
            ),
        ):
            with self.assertRaises(TestingActivationRefusal) as refused:
                adapter.application_values(self.record)
        self.assertEqual(refused.exception.code, TestingActivationRefusalCode.AUTHORITY_CHANGED)

    def test_discord_read_refusal_precedes_every_activation_effect(self) -> None:
        from control_plane.testing_application_activation import ApplicationActivationAdapter
        from control_plane.testing_external_capabilities import ExternalCapabilityReadRefusal
        from control_plane.provider_operations import ProviderMutationRejectedError
        from control_plane.outbound_http import PublicHttpResponse

        self.discord_intent()
        adapter = ApplicationActivationAdapter(
            self.fixture.adapter.root,
            self.store,
            self.record.product,
            self.record.request.model_copy(
                update={"reviewed_plan_sha256": self.record.plan_sha256}
            ),
            "fixture",
            self.record.scope,
            "launchplane_req_fixture",
            authorize_external=self.store.list_product_profile_records,
        )
        with (
            patch.object(ApplicationActivationAdapter, "evidence", return_value=nullcontext()),
            patch.object(ApplicationActivationAdapter, "current", return_value=self.record),
            patch.object(ApplicationActivationAdapter, "require_unchanged"),
            patch.object(ApplicationActivationAdapter, "refuse_after_failure", return_value=None),
            patch.object(ApplicationActivationAdapter, "preflight_application_route") as route,
            patch.object(ApplicationActivationAdapter, "create_placement") as create,
            patch.object(ApplicationActivationAdapter, "save") as save,
            patch.object(
                self.store,
                "testing_replacement_execution_lock",
                return_value=nullcontext(lambda: None),
            ),
            patch(
                "control_plane.testing_application_activation.require_retained_application_custody"
            ),
            patch(
                "control_plane.testing_application_activation.completed_application_forward_exclusion",
                return_value=False,
            ),
            patch(
                "control_plane.testing_application_capabilities.inspect_retained_application_capabilities"
            ) as inspect,
            patch(
                "control_plane.discord_external_resources.request_public_http",
                return_value=PublicHttpResponse(
                    429, "", 0, (), b'{"retry_after":120,"global":true}'
                ),
            ),
        ):
            lease = Mock(spec=["assert_current", "checkpoint_effect"])
            with self.assertRaises(ProviderMutationRejectedError) as refused:
                adapter.apply("original-operation", lease)
            self.assertIsInstance(
                refused.exception.cause,
                ExternalCapabilityReadRefusal,
                repr(refused.exception.__cause__),
            )
            cause = refused.exception.cause
            assert isinstance(cause, ExternalCapabilityReadRefusal)
            assert cause.result.observation is not None
            self.assertTrue(cause.result.observation.rate_limits[0].global_limit)
            self.assertEqual(cause.result.observation.rate_limits[0].retry_after, 120)
            for effect in (route, create, save, inspect, lease.checkpoint_effect):
                effect.assert_not_called()

            from fastapi import HTTPException

            denied = HTTPException(status_code=403, detail="Authorization denied")
            adapter.authorize_external = Mock(side_effect=denied)
            with self.assertRaises(ProviderMutationRejectedError) as unauthorized:
                adapter.apply("original-operation", lease)
            self.assertIs(unauthorized.exception.cause, denied)
            for effect in (route, create, save, inspect, lease.checkpoint_effect):
                effect.assert_not_called()

    def test_discord_authorization_precedes_decryption(self) -> None:
        from control_plane.testing_external_capabilities import observe_activation_discord

        self.discord_intent()
        with patch("control_plane.discord_identity.secrets._decrypt_secret_value") as decrypt:
            with self.assertRaises(PermissionError):
                observe_activation_discord(
                    self.store,
                    self.record,
                    authorize=Mock(side_effect=PermissionError("Denied")),
                    check_current=lambda: None,
                    actor="fixture",
                    trace_id="launchplane_req_fixture",
                )
            decrypt.assert_not_called()
            from control_plane.discord_identity import read_discord_identity
            from control_plane.contracts.testing_activation import (
                TestingActivationRefusal,
                TestingActivationRefusalCode,
            )

            refused = TestingActivationRefusal(
                "Changed fixture", code=TestingActivationRefusalCode.AUTHORITY_CHANGED
            )
            with self.assertRaises(TestingActivationRefusal) as failure:
                read_discord_identity(
                    self.store,
                    self.record.product,
                    profiles=self.store.list_product_profile_records(),
                    trace_id="launchplane_req_fixture",
                    actor="fixture",
                    purpose="activation_admission",
                    before_read=Mock(side_effect=refused),
                )
            self.assertIs(failure.exception, refused)
            decrypt.assert_not_called()

    def test_backoff_survives_input_drift_after_provider_response(self) -> None:
        from control_plane.testing_external_capabilities import (
            observe_activation_discord,
            ExternalCapabilityReadRefusal,
        )
        from control_plane.outbound_http import PublicHttpResponse

        self.discord_intent()
        changed = False

        def current() -> None:
            if changed:
                raise ValueError("Original lease changed")

        def respond(*args: Any, **kwargs: Any) -> PublicHttpResponse:
            nonlocal changed
            changed = True
            return PublicHttpResponse(429, "", 0, (), b'{"retry_after":120,"global":false}')

        with patch(
            "control_plane.discord_external_resources.request_public_http", side_effect=respond
        ):
            with self.assertRaises(ExternalCapabilityReadRefusal) as refused:
                observe_activation_discord(
                    self.store,
                    self.record,
                    authorize=self.store.list_product_profile_records,
                    check_current=current,
                    actor="fixture",
                    trace_id="launchplane_req_fixture",
                )
        assert refused.exception.result.observation is not None
        self.assertEqual(refused.exception.result.observation.rate_limits[0].retry_after, 120)

    def setUp(self) -> None:
        self.enterContext(
            patch.dict(
                os.environ,
                {
                    secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "application-activation-fixture-root"
                },
            )
        )
        self.fixture = activation_fixture.ActivationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store = self.fixture.store
        profile = self.store.read_product_profile_record(self.fixture.product)
        profile.expected_config.managed_secret_bindings = (
            ProductSecretConfigRequirement(
                binding_key="DB_PASSWORD", context=self.fixture.context, instance="testing"
            ),
        )
        self.store.write_product_profile_record(profile)
        self.record = self.fixture.planned_record.model_copy(deep=True)
        self.specification = TestingApplicationSpecification(
            runtime_port=8000,
            health_path="/health",
            network_name="fixture-provider-network",
            state=TestingApplicationState(kind="none"),
            local_credentials=(
                TestingApplicationCredential(binding_key="DB_PASSWORD", encoding="base64"),
            ),
        )
        self.record.request.application = self.specification
        version = self.store.read_secret_version("testing-version")
        ciphertext, key_id = secrets._encrypt_secret_value("retained-test-password")
        self.store.write_secret_version(
            version.model_copy(update={"ciphertext": ciphertext, "key_id": key_id})
        )

    def stage(self) -> Any:
        bundle = generation_bundle(self.store, self.record)
        self.store.write_product_authority_bundle(bundle)
        self.record.state_recipient.update(
            {
                secret.name.removeprefix(self.record.namespace + "-")
                + "_version": secret.current_version_id
                for secret in bundle.secret_records
            }
        )
        return bundle

    def test_reviewed_local_intent_generates_without_legacy_profile_flag(self) -> None:
        from control_plane.runtime_environments import resolve_site_runtime_environment_from_store

        profile = self.store.read_product_profile_record(self.record.product)
        self.assertFalse(
            profile.expected_config.managed_secret_bindings[0].testing_local_credential
        )
        from control_plane.testing_application_credentials import (
            staged_runtime_values,
            runtime_activation_bundle,
        )

        bundle = self.stage()
        environment = resolve_site_runtime_environment_from_store(
            record_store=self.store,
            context_name=self.record.context,
            instance_name="testing",
            target_environment={},
        )
        self.assertEqual(environment.values["DB_PASSWORD"], "retained-test-password")
        staged = staged_runtime_values(self.store, self.record)
        decoded = base64.b64decode(staged["DB_PASSWORD"], validate=True)
        self.assertTrue(decoded)
        self.assertEqual(base64.b64encode(decoded).decode(), staged["DB_PASSWORD"])
        self.store.write_product_authority_bundle(
            runtime_activation_bundle(self.store, self.record)
        )
        activated = resolve_site_runtime_environment_from_store(
            record_store=self.store,
            context_name=self.record.context,
            instance_name="testing",
            target_environment={},
        )
        self.assertEqual(activated.values["DB_PASSWORD"], staged["DB_PASSWORD"])
        binding = next(row for row in bundle.secret_bindings if row.binding_key == "DB_PASSWORD")
        self.assertEqual(binding.value_source, "generated")
        self.assertNotIn("GLOBAL_PLATFORM_PASSWORD", environment.values)

    def test_external_owner_input_cannot_be_reinterpreted_as_local_generation(self) -> None:
        profile = self.store.read_product_profile_record(self.record.product)
        profile.expected_config.managed_secret_bindings = (
            ProductSecretConfigRequirement(
                binding_key="DB_PASSWORD",
                context=self.record.context,
                instance="testing",
                owner_input=ProductOwnerSecretInput(
                    label="Externally issued application credential"
                ),
            ),
        )
        self.store.write_product_profile_record(profile)
        with patch(
            "control_plane.testing_application_credentials.random_secrets.token_bytes"
        ) as generate:
            with self.assertRaises(ValueError):
                generation_bundle(self.store, self.record)
        generate.assert_not_called()

    def test_declared_external_provider_cannot_be_reinterpreted_as_local_generation(self) -> None:
        profile = self.store.read_product_profile_record(self.record.product)
        profile.expected_config.managed_secret_bindings = (
            ProductSecretConfigRequirement(
                binding_key="DB_PASSWORD",
                context=self.record.context,
                instance="testing",
                external_provider="discord_bot",
            ),
        )
        self.store.write_product_profile_record(profile)
        with patch(
            "control_plane.testing_application_credentials.random_secrets.token_bytes",
            return_value=b"fixture-generated-material",
        ) as generate:
            with self.assertRaises(ValueError):
                generation_bundle(self.store, self.record)
        generate.assert_not_called()


class ApplicationRuntimeImageTests(unittest.TestCase):
    @unittest.skipUnless(
        os.environ.get("LAUNCHPLANE_TEST_TRAEFIK_IMAGE"),
        "Select an already-local immutable Traefik rehearsal image.",
    )
    def test_private_http_program_uses_selected_proxy_without_following_redirects(self) -> None:
        """Actual proxy binary/HTTP framing; Swarm ownership remains a finite fixture."""
        import shlex
        from control_plane.testing_application_runtime import (
            inspect_application_runtime,
            VerifiedApplicationRuntimeFailure,
        )
        from control_plane.testing_activation import preparation_resources
        from tests.test_testing_application_flow import ApplicationFlowTests

        image = os.environ["LAUNCHPLANE_TEST_TRAEFIK_IMAGE"]
        self.assertRegex(image, r"@sha256:[a-f0-9]{64}$")
        native_run = subprocess.run

        def command(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
            return native_run(arguments, capture_output=True, check=True, timeout=25, **kwargs)

        context = command(["docker", "context", "show"]).stdout.decode().strip()
        endpoint = json.loads(
            command(
                [
                    "docker",
                    "context",
                    "inspect",
                    context,
                    "--format",
                    "{{json .Endpoints.docker.Host}}",
                ]
            ).stdout
        )
        self.assertTrue(endpoint.startswith("unix://"), "Use the owned local Docker daemon.")
        self.assertEqual(
            command(["docker", "info", "--format", "{{.OSType}}"]).stdout.strip(), b"linux"
        )
        command(["docker", "image", "inspect", image])
        created = command(
            [
                "docker",
                "create",
                "--pull=never",
                "--name",
                "lp-application-http-" + uuid4().hex,
                "--label",
                "launchplane.fixture=application-http",
                "--network",
                "none",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--no-healthcheck",
                "--tmpfs",
                "/fixture:rw,nosuid,nodev,size=1m,mode=0700",
                "--entrypoint",
                "sleep",
                image,
                "300",
            ]
        )
        identifier = created.stdout.decode().strip()
        self.assertRegex(identifier, r"^[a-f0-9]{64}$")

        def cleanup() -> None:
            command(["docker", "rm", "--force", "--volumes", identifier])
            absent = command(
                ["docker", "ps", "-a", "-q", "--no-trunc", "--filter", "id=" + identifier]
            )
            self.assertEqual(absent.stdout.strip(), b"")

        self.addCleanup(cleanup)
        command(["docker", "start", identifier])
        flow = ApplicationFlowTests()
        flow.setUp()
        self.addCleanup(flow.doCleanups)
        flow.prepare()
        self.assertEqual(flow.activate().status, "completed")
        record = flow.adapter.current()
        assert record.application_readiness is not None
        readiness = record.application_readiness
        resources, _ = preparation_resources(flow.store, flow.product, flow.request, flow.scope)
        values = flow.adapter.application_values(record)
        specification = flow.specification
        private = flow.containers[readiness.container_id]["NetworkSettings"]["Networks"][
            specification.network_name
        ]["IPAddress"]
        identity = readiness.runtime_identity.model_dump(mode="json")
        cases = [
            ("healthy", 200, {"runtime_identity": identity}, None),
            ("redirect", 302, b"", ValueError),
            ("http_failure", 503, b"", VerifiedApplicationRuntimeFailure),
            ("malformed", 200, b"{", ValueError),
            ("truncated", 200, b"{}", ValueError),
            (
                "identity_mismatch",
                200,
                {"runtime_identity": {**identity, "artifact_id": "different-image"}},
                VerifiedApplicationRuntimeFailure,
            ),
        ]
        fixture_docker = flow.docker
        for index, (kind, status, payload, expected_error) in enumerate(cases):
            with self.subTest(kind=kind):
                body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                length = len(body) + (5 if kind == "truncated" else 0)
                response = (
                    "HTTP/1.1 "
                    + str(status)
                    + " Fixture\r\nContent-Length: "
                    + str(length)
                    + "\r\nConnection: close\r\n"
                    + (
                        "Location: http://127.0.0.1:19999/never-contacted\r\n"
                        if kind == "redirect"
                        else ""
                    )
                    + "\r\n"
                ).encode() + body
                script = (
                    "#!/bin/sh\nIFS= read -r request || exit 1\nprintf %s "
                    + shlex.quote(response.decode())
                    + "\n"
                )
                command(
                    ["docker", "exec", "-i", identifier, "tee", "/fixture/reply"],
                    input=script.encode(),
                )
                command(["docker", "exec", identifier, "chmod", "0700", "/fixture/reply"])
                port = str(18080 + index)
                server = subprocess.Popen(
                    [
                        "docker",
                        "exec",
                        identifier,
                        "nc",
                        "-l",
                        "-p",
                        port,
                        "-w",
                        "10",
                        "-e",
                        "/bin/sh",
                        "/fixture/reply",
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                try:
                    deadline = time.monotonic() + 8
                    while True:
                        tables = command(
                            ["docker", "exec", identifier, "cat", "/proc/net/tcp", "/proc/net/tcp6"]
                        ).stdout.decode()
                        if any(
                            ":" + format(int(port), "04X") in line and " 0A " in line
                            for line in tables.splitlines()
                        ):
                            break
                        self.assertIsNone(server.poll(), "The owned one-shot listener exited.")
                        self.assertLess(
                            time.monotonic(), deadline, "The owned listener did not become ready."
                        )
                        time.sleep(0.1)
                    calls = []

                    def actual_http(arguments: list[str], **kwargs: Any) -> Any:
                        if arguments[1:5] != ["exec", "-i", "proxy", "nc"]:
                            return fixture_docker(arguments, **kwargs)
                        self.assertEqual(arguments[-2:], [private, str(specification.runtime_port)])
                        calls.append(arguments)
                        actual = [*arguments]
                        actual[3], actual[-2], actual[-1] = identifier, "127.0.0.1", port
                        return native_run(actual, **kwargs)

                    def inspect() -> dict[str, Any]:
                        return inspect_application_runtime(
                            host="https://provider.test",
                            token="fixture-token",
                            server_id="server",
                            application=record.app_name,
                            runtime_port=specification.runtime_port,
                            network=specification.network_name,
                            image=record.image_reference,
                            identity=readiness.runtime_identity,
                            values=values,
                            volume=resources.volumes.get("state", ""),
                            volume_binding=resources.binding_sha256,
                            mount_path=specification.state.container_path,
                            health_path=specification.health_path,
                        )

                    with patch.object(flow, "docker", side_effect=actual_http):
                        if expected_error is None:
                            self.assertFalse(inspect()["verified_failure"])
                        else:
                            with self.assertRaises(expected_error) as raised:
                                inspect()
                            self.assertEqual(
                                isinstance(raised.exception, VerifiedApplicationRuntimeFailure),
                                expected_error is VerifiedApplicationRuntimeFailure,
                            )
                    self.assertEqual(len(calls), 1)
                    server.communicate(timeout=15)
                    self.assertEqual(server.returncode, 0)
                finally:
                    if server.poll() is None:
                        server.terminate()
                        server.communicate(timeout=15)


class ActivationDeploymentReceiptTests(unittest.TestCase):
    def test_only_raw_ids_or_exact_successful_wait_receipts_are_recognized(self) -> None:
        from control_plane.dokploy import api
        from control_plane.testing_application_activation import activation_deployment_key

        job: api.JsonObject = {"deploymentId": "original-job", "status": "done"}
        receipt = api._wait_for_deployment_status(
            fetch_latest_deployment=lambda: job,
            before_key="",
            timeout_seconds=1,
            failure_message_prefix="Fixture deployment",
        )
        self.assertEqual(activation_deployment_key(receipt), api.deployment_key(job))
        self.assertEqual(
            activation_deployment_key(api.deployment_key(job)), api.deployment_key(job)
        )
        for invalid in (
            None,
            "",
            " ",
            receipt + " ",
            " " + receipt,
            receipt + " status=done",
            "deployment= status=done",
            "deployment=id status=",
            "deployment=id status=running",
            "deployment=id status=error",
            "deployment=id status=unknown",
            "deployment=id=other status=done",
        ):
            with self.subTest(receipt=invalid):
                self.assertEqual(activation_deployment_key(invalid), "")
