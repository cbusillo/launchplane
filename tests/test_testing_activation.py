"""Qualified testing activation preserves production and the original unknown outcome."""

import base64
from copy import deepcopy
import json
import unittest
from typing import Any
from collections.abc import Callable
from unittest.mock import patch

from control_plane.contracts.testing_activation import TestingActivationRequest
from control_plane.integration_allowances import target_record_sha256
from control_plane.provider_operations import run_durable_provider_operation
from control_plane.testing_activation import (
    ROUTE,
    TestingActivationAdapter,
    activation_record,
    retained_fence_is_separated,
    runtime_values as resolve_activation_runtime,
)
from control_plane.testing_activation_compose import (
    interpolate,
    qualify_compose,
    read_compose_source,
)
from tests import test_testing_separation_resources as preparation


COMPOSE = {
    "services": {
        "web": {
            "image": "${DOCKER_IMAGE_REFERENCE:?required}",
            "environment": {"DB_HOST": "database", "DB_PASSWORD": "${DB_PASSWORD}"},
            "ports": ["${HTTP_PORT}:8000"],
            "volumes": ["config:/config"],
        },
        "database": {
            "image": "mariadb:fixture",
            "environment": {"MYSQL_PASSWORD": "${DB_PASSWORD}"},
            "ports": ["${DB_PORT}:3306"],
            "volumes": ["database:/var/lib/mysql"],
        },
    },
    "volumes": {"database": {"name": "${DB_VOLUME}"}, "config": {"name": "${CONFIG_VOLUME}"}},
}


class ComposeQualificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.values = {
            "DOCKER_IMAGE_REFERENCE": "example@sha256:" + "a" * 64,
            "DB_PASSWORD": "safe-test-password",
            "HTTP_PORT": "19212",
            "DB_PORT": "19213",
            "DB_VOLUME": "new-db",
            "CONFIG_VOLUME": "new-config",
        }
        self.document = interpolate(COMPOSE, self.values)

    def qualify(self) -> None:
        qualify_compose(
            self.document,
            volumes={"new-db", "new-config"},
            ports={19212, 19213},
            credential_values={"safe-test-password"},
        )

    def test_valid_render_and_product_local_include(self) -> None:
        files = {"deploy/compose.yml": {"include": ["../service.yml"]}, "service.yml": COMPOSE}

        def read(path: str) -> Any:
            name = path.split("/contents/", 1)[1].split("?", 1)[0]
            return {
                "type": "file",
                "encoding": "base64",
                "content": base64.b64encode(json.dumps(files[name]).encode()).decode(),
            }

        source = read_compose_source(
            repository="example/site", commit="a" * 40, path="deploy/compose.yml", read=read
        )
        self.assertEqual(json.loads(source), COMPOSE)
        self.qualify()

    def test_production_storage_ports_credentials_and_host_capabilities_refuse(self) -> None:
        mutations: list[Callable[[Any], None]] = [
            lambda d: d["volumes"]["database"].update(name="production-db"),
            lambda d: d["volumes"]["database"].update(driver_opts={"device": "/production"}),
            lambda d: d["services"]["database"].update(volumes=["/production:/var/lib/mysql"]),
            lambda d: d["services"]["web"].update(ports=["3307:8000"]),
            lambda d: d["services"]["web"].update(network_mode="host"),
            lambda d: d["services"]["web"].update(privileged=True),
            lambda d: d["services"]["web"]["environment"].update(DB_PASSWORD="production-password"),
            lambda d: d["services"]["web"]["environment"].update(DB_HOST="production.internal"),
            lambda d: d.update(networks={"default": {"external": True}}),
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.document = interpolate(COMPOSE, self.values)
                mutation(self.document)
                with self.assertRaises(ValueError):
                    self.qualify()


class ActivationTests(unittest.TestCase):
    database_url = ""

    def setUp(self) -> None:
        self.preparation = preparation.SeparationResourceTests()
        self.preparation.database_url = self.database_url
        self.preparation.setUp()
        self.addCleanup(self.preparation.doCleanups)
        fixture = self.preparation
        self.store, self.root, self.actor = fixture.store, fixture.root, fixture.actor
        self.product, self.context, self.scope = (
            fixture.product,
            fixture.context,
            "local-operator|local-owner-agent|local-owner-write",
        )
        with patch(
            "control_plane.testing_separation_resources.run_preparation_schedule",
            side_effect=fixture.prepare,
        ):
            from control_plane.testing_separation_resources import ROUTE as preparation_route

            prepared = run_durable_provider_operation(
                store=self.store,
                scope=self.scope,
                route_path=preparation_route.format(product=self.product),
                idempotency_key="prepare",
                request_fingerprint="a" * 64,
                lease_owner="preparation-worker",
                response_trace_id="preparation-trace",
                adapter=fixture.adapter,
            )
            self.assertEqual(prepared.response_status_code, 202)
        self.request = TestingActivationRequest(
            context=self.context,
            preparation_key="prepare",
            interlock_record_sha256=target_record_sha256(fixture.retained),
            source_commit="a" * 40,
            volume_keys={"database": "DB_VOLUME", "config": "CONFIG_VOLUME"},
            port_keys=("DB_PORT", "HTTP_PORT"),
            reason="Explicitly continue the retained staging interlock onto isolated testing.",
        )
        self.payloads = {
            fixture.fixture.target.resolved_target.target_id: deepcopy(fixture.fixture.payload)
        }
        self.writes: list[tuple[str, Any]] = []
        self.inspections = 0
        self.enterContext(
            patch(
                "control_plane.testing_activation.read_product_compose",
                return_value=json.dumps(COMPOSE),
            )
        )
        self.enterContext(
            patch(
                "control_plane.testing_activation.verified_product_image",
                return_value="ghcr.io/example/site@sha256:" + "a" * 64,
            )
        )
        self.enterContext(
            patch("control_plane.testing_activation.runtime_values", side_effect=self.values)
        )
        self.enterContext(
            patch(
                "control_plane.testing_activation.api.fetch_dokploy_target_payload",
                side_effect=lambda **kw: deepcopy(self.payloads[kw["target_id"]]),
            )
        )
        self.enterContext(
            patch("control_plane.testing_activation.api.fetch_dokploy_projects", return_value=[])
        )
        self.enterContext(
            patch("control_plane.testing_activation.api.dokploy_request", side_effect=self.provider)
        )
        self.enterContext(
            patch(
                "control_plane.testing_activation.inspect_new_resources", side_effect=self.inspect
            )
        )
        self.enterContext(
            patch(
                "control_plane.testing_activation.compose.fetch_dokploy_converted_compose_file",
                side_effect=self.converted,
            )
        )
        self.enterContext(
            patch(
                "control_plane.testing_activation.inspect_generic_web_testing_exclusion",
                return_value=type("Exclusion", (), {"status": "excluded"})(),
            )
        )
        self.adapter = TestingActivationAdapter(
            self.root,
            self.store,
            self.product,
            self.request,
            self.actor,
            self.scope,
            "launchplane_req_fixture",
        )
        record = self.adapter.current()
        self.planned_record = record
        self.request = self.request.model_copy(
            update={"mode": "apply", "reviewed_plan_sha256": record.plan_sha256}
        )
        self.adapter.request = self.request

    def values(self, store: Any, root: Any, record: Any, resources: Any) -> dict[str, str]:
        return {
            "DOCKER_IMAGE_REFERENCE": "ghcr.io/example/site@sha256:" + "a" * 64,
            "DB_PASSWORD": "safe-test-password",
            "DB_VOLUME": resources.volumes["database"],
            "CONFIG_VOLUME": resources.volumes["config"],
            "DB_PORT": "19213",
            "HTTP_PORT": "19212",
        }

    def inspect(self, root: Any, resources: Any, ports: Any, before_effect: Any) -> None:
        before_effect("inspect_new_resources")
        self.inspections += 1

    def converted(self, **kwargs: Any) -> str:
        payload = self.payloads[kwargs["compose_id"]]
        from control_plane.dokploy.api import parse_dokploy_env_text

        return json.dumps(
            interpolate(json.loads(payload["composeFile"]), parse_dokploy_env_text(payload["env"]))
        )

    def provider(self, **kwargs: Any) -> Any:
        path, body = kwargs["path"], kwargs.get("payload")
        assert isinstance(path, str)
        if kwargs.get("method", "GET") != "GET":
            assert isinstance(body, dict)
        if kwargs.get("method", "GET") == "GET":
            return self.preparation.fixture.responses.get(path)
        assert isinstance(body, dict)
        self.writes.append((path, deepcopy(body)))
        if path == "/api/project.create":
            return {"projectId": "new-project"}
        if path == "/api/environment.create":
            return {"environmentId": "new-environment"}
        if path == "/api/compose.create":
            self.payloads["new-compose"] = {
                **body,
                "composeId": "new-compose",
                "env": "",
                "serverId": "server",
            }
            return {"composeId": "new-compose"}
        if path == "/api/compose.update":
            self.payloads[body["composeId"]].update(body)
            return {}
        self.fail(f"Unexpected provider effect: {path}")

    def run_activation(self, key: str = "activate") -> Any:
        return run_durable_provider_operation(
            store=self.store,
            scope=self.scope,
            route_path=ROUTE.format(product=self.product),
            idempotency_key=key,
            request_fingerprint="b" * 64,
            lease_owner="fixture-worker",
            response_trace_id="launchplane_req_fixture",
            adapter=self.adapter,
        )

    def test_activation_and_original_key_replay_preserve_production_old_target_hold_and_fence(
        self,
    ) -> None:
        production = self.store.read_dokploy_target_record(
            context_name=self.context, instance_name="prod"
        )
        old_provider = deepcopy(self.payloads)
        result = self.run_activation()
        self.assertEqual(result.response_status_code, 202)
        writes = deepcopy(self.writes)
        self.assertEqual(self.run_activation().response_status_code, 202)
        self.assertEqual(self.writes, writes)
        current = self.store.read_dokploy_target_record(
            context_name=self.context, instance_name="testing"
        )
        self.assertEqual(
            current.policies.staff_testing_hold,
            self.preparation.retained.policies.staff_testing_hold,
        )
        self.assertEqual(
            self.store.read_dokploy_target_record(context_name=self.context, instance_name="prod"),
            production,
        )
        self.assertEqual(
            self.store.list_held_provider_target_reservations(), (self.preparation.original,)
        )
        self.assertTrue(retained_fence_is_separated(self.store, self.preparation.original))
        self.assertEqual(
            self.payloads[self.preparation.fixture.target.resolved_target.target_id],
            old_provider[self.preparation.fixture.target.resolved_target.target_id],
        )
        self.assertEqual(self.store.list_product_reconcile_requests(), ())
        self.assertEqual(self.inspections, 2)
        record = activation_record(self.store, self.product, self.context)
        assert record is not None
        self.assertEqual(record.retained_target, self.preparation.retained)
        self.assertTrue(record.published)

    def test_lost_creation_reply_and_new_key_never_duplicate_creation(self) -> None:
        original = self.provider

        def lost(**kwargs: Any) -> Any:
            value = original(**kwargs)
            if kwargs["path"] == "/api/compose.create":
                raise OSError("Lost creation response")
            return value

        with patch("control_plane.testing_activation.api.dokploy_request", side_effect=lost):
            for key in ("activate", "activate", "different-key"):
                self.assertEqual(self.run_activation(key).response_status_code, 409)
        self.assertEqual(sum(path == "/api/compose.create" for path, _ in self.writes), 1)
        self.assertFalse(retained_fence_is_separated(self.store, self.preparation.original))
        self.preparation.assert_retained()

    def test_unknown_port_or_storage_readback_keeps_old_recipient(self) -> None:
        with patch(
            "control_plane.testing_activation.inspect_new_resources",
            side_effect=ValueError("Storage/port overlap"),
        ):
            with self.assertRaises(ValueError):
                self.run_activation()
        self.assertEqual(self.writes, [])
        self.preparation.assert_retained()

    def test_configuration_drift_after_effect_keeps_fences_and_never_publishes(self) -> None:
        original = self.inspect

        def drift(*args: Any) -> None:
            original(*args)
            target = self.store.read_dokploy_target_record(
                context_name=self.context, instance_name="testing"
            )
            target.env["CHANGED"] = "new authority"
            self.store.write_dokploy_target_record(target)

        with patch("control_plane.testing_activation.inspect_new_resources", side_effect=drift):
            self.assertEqual(self.run_activation().response_status_code, 409)
        self.assertEqual(self.writes, [])
        self.assertIn(
            self.preparation.original, self.store.list_held_provider_target_reservations()
        )

    def test_plain_repoint_cannot_hide_an_old_incident_or_release_its_lane_fence(self) -> None:
        target_id = self.store.read_dokploy_target_id_record(
            context_name=self.context, instance_name="testing"
        )
        self.store.write_dokploy_target_id_record(
            target_id.model_copy(update={"target_id": "unproved-replacement"})
        )
        self.assertFalse(retained_fence_is_separated(self.store, self.preparation.original))

    def test_lost_configuration_reply_adopts_only_the_original_complete_render(self) -> None:
        original = self.provider

        def lost(**kwargs: Any) -> Any:
            value = original(**kwargs)
            if kwargs["path"] == "/api/compose.update":
                raise OSError("Lost response after configuration persisted")
            return value

        with patch("control_plane.testing_activation.api.dokploy_request", side_effect=lost):
            self.assertEqual(self.run_activation().response_status_code, 409)
            self.assertEqual(self.run_activation().response_status_code, 202)
        self.assertEqual(sum(path == "/api/compose.create" for path, _ in self.writes), 1)
        self.assertEqual(sum(path == "/api/compose.update" for path, _ in self.writes), 1)
        self.assertIn(
            self.preparation.original, self.store.list_held_provider_target_reservations()
        )

    def test_unsafe_or_production_equal_carried_key_refuses_without_provider_writes(self) -> None:
        from control_plane.contracts.secret_record import SecretBinding, SecretRecord
        from control_plane.contracts.runtime_key_safety_policy import (
            RuntimeKeySafetyPolicyRecord,
            RuntimeSecretSafetyRule,
        )
        from control_plane.runtime_environments import SiteRuntimeEnvironment
        from control_plane.contracts.deploy_target import ProviderTargetRecord
        from control_plane.testing_activation import preparation_resources

        profile = self.store.read_product_profile_record(self.product)
        self.store.write_product_profile_record(
            profile.model_copy(
                update={
                    "lanes": (
                        *profile.lanes,
                        profile.lanes[0].model_copy(
                            update={"instance": "prod", "context": self.context}
                        ),
                    )
                }
            )
        )

        now = self.preparation.fixture.now()
        self.store.write_secret_record(
            SecretRecord(
                secret_id="testing-password",
                scope="context_instance",
                integration="runtime_environment",
                name="DB_PASSWORD",
                context=self.context,
                instance="testing",
                current_version_id="testing-version",
                created_at=now,
                updated_at=now,
            )
        )
        binding = SecretBinding(
            binding_id="testing-password",
            secret_id="testing-password",
            integration="runtime_environment",
            binding_key="DB_PASSWORD",
            context=self.context,
            instance="testing",
            declared_secret_class="testing",
            created_at=now,
            updated_at=now,
        )
        self.store.write_secret_binding(binding)
        self.store.write_runtime_key_safety_policy_record(
            RuntimeKeySafetyPolicyRecord(
                record_id="testing-policy",
                source="fixture",
                updated_at=now,
                rules=(RuntimeSecretSafetyRule(binding_key="DB_PASSWORD", secret_class="testing"),),
            )
        )
        self.store.write_provider_target_record(
            ProviderTargetRecord(
                context=self.context,
                instance="prod",
                provider_id="dokploy",
                target_category="compose",
                target_id="production-compose",
                display_name="production",
                updated_at=now,
            )
        )
        self.payloads["production-compose"] = {
            "composeId": "production-compose",
            "env": "DB_PASSWORD=production-password",
        }
        resources, _ = preparation_resources(self.store, self.product, self.request, self.scope)
        for value, declared, passes in (
            ("independent-password", "testing", True),
            ("production-password", "testing", False),
            ("independent-password", "prod_only", False),
        ):
            self.store.write_secret_binding(
                binding.model_copy(update={"declared_secret_class": declared})
            )
            resolved = SiteRuntimeEnvironment(
                values={"DB_PASSWORD": value}, secret_keys=frozenset({"DB_PASSWORD"})
            )
            with patch(
                "control_plane.testing_activation.runtime_environments.resolve_site_runtime_environment",
                return_value=resolved,
            ):
                if passes:
                    result = resolve_activation_runtime(
                        self.store, self.root, self.planned_record, resources
                    )
                    self.assertEqual(result["DB_PASSWORD"], value)
                else:
                    with self.assertRaises(ValueError):
                        resolve_activation_runtime(
                            self.store, self.root, self.planned_record, resources
                        )
        self.assertEqual(self.writes, [])

    def test_hold_continuation_requires_exact_caller_and_preserves_later_staff_hold(self) -> None:
        from control_plane.testing_activation import require_activation_hold_lift
        from control_plane.testing_lane_hold import TestingHoldApplyRequest
        from control_plane.contracts.dokploy_target_record import DokployTargetStaffTestingHold

        self.assertEqual(self.run_activation().response_status_code, 202)
        request = TestingHoldApplyRequest(
            product=self.product,
            context=self.context,
            instance="testing",
            hold=False,
            reason="Lift only the now-satisfied interlock.",
            activation_plan_sha256=self.request.reviewed_plan_sha256,
        )
        self.assertTrue(require_activation_hold_lift(self.store, request, self.actor, self.root))
        with self.assertRaises(ValueError):
            require_activation_hold_lift(self.store, request, "foreign-agent", self.root)
        current = self.store.read_dokploy_target_record(
            context_name=self.context, instance_name="testing"
        )
        current.policies.staff_testing_hold = DokployTargetStaffTestingHold(
            reason="Staff began a real test.", recorded_by=self.actor
        )
        self.store.write_dokploy_target_record(current)
        with self.assertRaises(ValueError):
            require_activation_hold_lift(self.store, request, self.actor, self.root)
        self.assertEqual(
            self.store.read_dokploy_target_record(
                context_name=self.context, instance_name="testing"
            ).policies.staff_testing_hold,
            current.policies.staff_testing_hold,
        )

    def test_supported_route_rejects_production_and_preserves_original_key_receipt(self) -> None:
        import asyncio
        from control_plane.http_app import create_launchplane_fastapi_app
        from control_plane.service_auth import LaunchplaneAuthzPolicy
        from tests.http_app_test_support import _local_operator_bearer_config, _RejectingVerifier
        from tests.support.http import request

        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=LaunchplaneAuthzPolicy(),
            bearer_identity_config=_local_operator_bearer_config(token_label="local-owner-write"),
            record_store_factory=lambda: self.store,
            control_plane_root_path=self.root,
        )
        draft = self.request.model_copy(
            update={"mode": "dry-run", "reviewed_plan_sha256": ""}
        ).model_dump(mode="json")

        async def post(body: Any, key: str = "") -> Any:
            return await request(
                app,
                "POST",
                ROUTE.format(product=self.product),
                headers={
                    "Authorization": "Bearer local-operator-token",
                    **({"Idempotency-Key": key} if key else {}),
                },
                payload=body,
            )

        for instance in ("prod", "preview-1"):
            response = asyncio.run(post({**draft, "instance": instance}))
            self.assertIn(response.status_code, (400, 422), response.text)
        self.assertEqual(self.writes, [])
        planned = asyncio.run(post(draft))
        self.assertEqual(planned.status_code, 202, planned.text)
        self.assertEqual(planned.json()["result"]["plan_sha256"], self.request.reviewed_plan_sha256)
        key = "activate-http"
        applied = asyncio.run(post(self.request.model_dump(mode="json"), key))
        if self.store.database_dialect_name == "postgresql":
            self.assertEqual(applied.status_code, 202, applied.text)
        else:
            self.assertEqual(applied.status_code, 503, applied.text)
            key = "activate"
            self.assertEqual(self.run_activation().response_status_code, 202)
        receipt = asyncio.run(post({**draft, "mode": "receipt"}, key))
        self.assertEqual(receipt.status_code, 202, receipt.text)
        self.assertTrue(receipt.json()["result"]["separation_verified"])
        # Receipt retrieval remains available after configuration drift.
        target = self.store.read_dokploy_target_record(
            context_name=self.context, instance_name="testing"
        )
        target.env["CHANGED_AFTER_ACTIVATION"] = "fixture"
        self.store.write_dokploy_target_record(target)
        repeated = asyncio.run(post({**draft, "mode": "receipt"}, key))
        self.assertEqual(repeated.status_code, 202, repeated.text)
        readback = asyncio.run(post({**draft, "mode": "readback"}))
        self.assertEqual(readback.status_code, 409, readback.text)


class ActivationPortProgramTests(unittest.TestCase):
    def run_program(self, ports: list[int], containers: list[dict[str, Any]]) -> tuple[int, str]:
        from contextlib import redirect_stdout
        import io
        import subprocess
        import sys
        from control_plane.testing_activation import PORT_INSPECTION_PROGRAM

        def docker(command: list[str], **kwargs: Any) -> Any:
            if command == ["docker", "ps", "-aq"]:
                output = "\n".join(str(index) for index in range(len(containers)))
            elif command[:2] == ["docker", "inspect"]:
                output = json.dumps([containers[int(command[2])]])
            else:
                self.fail(f"Unexpected host mutation: {command}")
            return subprocess.CompletedProcess(command, 0, stdout=output)

        output = io.StringIO()
        encoded = base64.b64encode(json.dumps(ports).encode()).decode()
        status = 0
        with (
            patch.object(sys, "argv", ["port-inspection", encoded]),
            patch("subprocess.run", side_effect=docker),
            redirect_stdout(output),
        ):
            try:
                exec(compile(PORT_INSPECTION_PROGRAM, "<testing-port-inspection>", "exec"), {})
            except SystemExit as error:
                status = error.code if isinstance(error.code, int) else 1
        return status, output.getvalue()

    def test_actual_listener_and_stopped_container_port_are_protected(self) -> None:
        import socket

        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            self.assertNotEqual(self.run_program([port], [])[0], 0)
            self.assertEqual(listener.getsockname()[1], port)
        stopped: dict[str, Any] = {
            "State": {"Running": False},
            "HostConfig": {"PortBindings": {"3306/tcp": [{"HostPort": str(port)}]}},
        }
        self.assertNotEqual(self.run_program([port], [stopped])[0], 0)
        self.assertEqual(
            stopped["HostConfig"]["PortBindings"]["3306/tcp"][0]["HostPort"], str(port)
        )
        status, output = self.run_program([port], [])
        self.assertEqual(status, 0, output)
