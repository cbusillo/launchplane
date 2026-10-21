"""Reviewed fresh application inputs and real managed runtime delivery."""

import base64
from copy import deepcopy
import hashlib
from io import StringIO
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any
import time
from uuid import uuid4
import unittest
from unittest.mock import patch

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
        self.service = {"ID": "retained-service", "Spec": {"Name": "retained-app"}}
        self.task = {
            "ID": "original-task",
            "ServiceID": "retained-service",
            "Spec": {"ContainerSpec": deepcopy(self.configuration)},
            "Status": {"ContainerStatus": {"ContainerID": "original-container"}},
        }
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
        if values == ["service", "inspect", "retained-app"]:
            result = json.dumps([self.service])
        elif values == ["service", "ps", "--no-trunc", "-q", "retained-app"]:
            result = "original-task\n"
        elif values == [
            "ps",
            "-aq",
            "--no-trunc",
            "--filter",
            "label=com.docker.swarm.service.name=retained-app",
        ]:
            result = "original-container\n"
        elif values == ["image", "inspect", self.containers["original-container"]["Image"]]:
            result = json.dumps(
                [
                    {
                        "Id": self.containers["original-container"]["Image"],
                        "RepoDigests": [self.identity["artifact_id"]],
                    }
                ]
            )
        elif values == ["inspect", "--type", "task", "original-task"]:
            result = json.dumps([self.task])
        elif len(values) == 2 and values[0] == "inspect" and values[1] in self.containers:
            result = json.dumps([self.containers[values[1]]])
        else:
            raise AssertionError(arguments)
        return subprocess.CompletedProcess(arguments, 0, result, "")

    def observe(self) -> str:
        from control_plane.testing_application_capabilities import (
            RETAINED_APPLICATION_CAPABILITY_PROGRAM,
        )
        from control_plane.runtime_platform_credentials import find_platform_credentials
        from control_plane.contracts.deploy_reference import (
            docker_image_repository,
            docker_image_digest,
        )

        output = StringIO()
        values = {
            "base64": base64,
            "hashlib": hashlib,
            "json": json,
            "re": re,
            "subprocess": subprocess,
            "sys": sys,
            "Path": Path,
            "find_platform_credentials": find_platform_credentials,
            "docker_image_repository": docker_image_repository,
            "docker_image_digest": docker_image_digest,
        }
        with (
            patch.object(
                sys, "argv", ["program", base64.b64encode(json.dumps(self.spec).encode()).decode()]
            ),
            patch.object(subprocess, "run", side_effect=self.docker),
            patch("sys.stdout", output),
        ):
            exec(RETAINED_APPLICATION_CAPABILITY_PROGRAM, values)
        return output.getvalue()

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
