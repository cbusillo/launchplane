"""Fictional source installation through the real durable-operation runner."""

import asyncio
from copy import deepcopy
import json
import os
from typing import Any
import unittest
from unittest.mock import patch
from click.testing import CliRunner
from control_plane.cli import main

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.private_testing_activation import (
    ROUTE,
    PrivateTestingActivationAdapter,
    PrivateTestingActivationRequest,
)
from control_plane.provider_operations import run_durable_provider_operation
from control_plane.service_auth import BearerIdentityConfig, LaunchplaneAuthzPolicy
from control_plane.workflows.dokploy_deploy import update_dokploy_target_artifact
from tests.http_app_test_support import _RejectingVerifier
from tests.support.http import request as http_request
from tests.support.auth import scoped_agent_transport
from tests import test_private_testing_transport as transport_fixture


class PrivateTestingActivationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = transport_fixture.PrivateTestingTransportTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store = self.fixture.store
        self.root = self.fixture.root
        self.commit = "e" * 40
        self.template = deepcopy(self.fixture.compose)
        self.template["services"]["bridge"]["image"] = (
            "${DOCKER_IMAGE_REFERENCE:?immutable artifact required}"
        )
        self.template["services"]["bridge"]["environment"] = {
            "AGENT_SESSION_TOKEN": "${AGENT_SESSION_TOKEN:-}"
        }
        target = self.store.read_dokploy_target_record(
            context_name="example-site", instance_name="testing"
        )
        url = "https://github.com/" + self.fixture.profile.repository + ".git"
        self.store.write_dokploy_target_record(
            target.model_copy(
                update={
                    "source_type": "git",
                    "custom_git_branch": self.commit,
                    "custom_git_url": url,
                    "compose_path": "compose.runtime.yml",
                }
            )
        )
        self.fixture.placement.update(
            {
                "sourceType": "git",
                "customGitUrl": url,
                "customGitBranch": self.commit,
                "composePath": "compose.runtime.yml",
                "composeFile": "",
                "autoDeploy": False,
                "environmentId": "fictional-testing-environment",
                "name": "testing",
                "env": "SETTING=unchanged",
                "composeStatus": "idle",
            }
        )
        self.before = deepcopy(self.fixture.placement)
        self.payload = {
            **self.fixture.payload,
            "source_commit": self.commit,
            "source_path": "compose.runtime.yml",
            "certificate_root": "/srv/testing-certificates",
            "host_review": "fixture exact private host and port exclusion review",
            "custody_review": "fixture Owner-only certificate and key custody review",
            "reason": "fixture install",
        }
        self.reader = self.enterContext(
            patch(
                "control_plane.private_testing_activation.read_product_compose",
                side_effect=lambda *_args: json.dumps(self.template),
            )
        )
        self.image = self.enterContext(
            patch(
                "control_plane.private_testing_activation.verified_product_image",
                return_value="fixture@sha256:" + "a" * 64,
            )
        )
        self.calls: list[dict[str, Any]] = []
        self.lose_response = False
        self.active_work = False
        self.enterContext(
            patch("control_plane.dokploy.api.dokploy_request", side_effect=self.provider)
        )

    def provider(self, **kwargs: Any) -> Any:
        if kwargs.get("method", "GET") == "GET":
            return (
                [{"state": "waiting", "data": {"composeId": "fixture-testing"}}]
                if self.active_work and kwargs["path"] == "/api/deployment.queueList"
                else []
            )
        self.assertEqual(kwargs["path"], "/api/compose.update")
        self.calls.append(deepcopy(kwargs["payload"]))
        self.fixture.placement.update(kwargs["payload"])
        if self.lose_response:
            raise OSError("fictional response lost after provider accepted")
        return {}

    def prepare(self, key: str = "", **changes: Any) -> PrivateTestingActivationAdapter:
        return PrivateTestingActivationAdapter.prepare(
            self.root,
            self.store,
            "example-site",
            PrivateTestingActivationRequest.model_validate({**self.payload, **changes}),
            "fixture-actor",
            "fixture-trace",
            key,
        )

    def execute(self, adapter: PrivateTestingActivationAdapter, key: str = "install") -> Any:
        return run_durable_provider_operation(
            store=self.store,
            scope="fixture-actor",
            route_path=ROUTE.format(product="example-site"),
            idempotency_key=key,
            request_fingerprint=canonical_json_sha256(
                {"request": adapter.intent["request"], "plan": adapter.intent["plan_sha256"]}
            ),
            lease_owner="fixture-lease",
            response_trace_id="fixture-trace",
            adapter=adapter,
            original_intent=adapter.intent,
        )

    def test_inert_plan_binds_real_source_reader_and_preserves_templates(self) -> None:
        plan = self.prepare()
        self.reader.assert_called_once_with(
            self.store, "example-site", self.commit, self.payload["source_path"]
        )
        self.image.assert_called_once_with(self.store, "example-site", self.commit)
        self.assertFalse(plan.result(matches=False)["deployment_performed"])
        self.assertEqual(self.fixture.placement, self.before)
        self.assertFalse(self.calls)
        desired = json.loads(plan.intent["after"]["composeFile"])
        self.assertEqual(desired["services"]["bridge"], self.template["services"]["bridge"])
        self.assertEqual(self.prepare().reconciliation_key(), plan.reconciliation_key())
        for field, value in (
            ("host_review", "changed"),
            ("custody_review", "changed"),
            ("certificate_sha256", "f" * 64),
        ):
            with self.subTest(field=field):
                self.assertNotEqual(
                    self.prepare(**{field: value}).reconciliation_key(), plan.reconciliation_key()
                )

    def test_install_replay_independent_readback_and_existing_deploy_image_update(self) -> None:
        plan = self.prepare()
        result = self.execute(plan)
        self.assertEqual(result.status, "completed")
        self.assertEqual(self.fixture.placement["env"], self.before["env"])
        self.assertEqual(self.execute(plan).status, "replayed")
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(plan.readback()["configuration_matches"])
        self.assertFalse(plan.readback()["certificate_verified"])
        composed = self.fixture.placement["composeFile"]
        update_dokploy_target_artifact(
            host="https://provider.test",
            token="fixture",
            target_type="compose",
            target_id="fixture-testing",
            artifact_id=plan.intent["image"],
        )
        self.assertEqual(self.fixture.placement["composeFile"], composed)
        self.assertIn(
            "DOCKER_IMAGE_REFERENCE=" + plan.intent["image"], str(self.fixture.placement["env"])
        )
        self.assertTrue(plan.readback()["configuration_matches"])
        self.assertFalse(plan.readback()["runtime_inputs_match_review"])

    def test_reviewed_refresh_preserves_owned_source_and_can_restore_it(self) -> None:
        first = self.prepare()
        self.execute(first)
        original_source = self.fixture.placement["composeFile"]
        self.template["services"]["bridge"]["init"] = True
        refresh = self.prepare(
            operation="refresh", original_activation_key="install", source_commit="f" * 40
        )
        self.assertEqual(self.execute(refresh, "refresh").status, "completed")
        self.assertNotEqual(self.fixture.placement["composeFile"], original_source)
        rollback = self.prepare(
            operation="remove", original_activation_key="refresh", source_commit="f" * 40
        )
        self.assertEqual(self.execute(rollback, "rollback").status, "completed")
        self.assertEqual(self.fixture.placement["composeFile"], original_source)

    def test_response_loss_recovers_original_key_without_repeating_update(self) -> None:
        plan = self.prepare()
        self.lose_response = True
        self.assertEqual(self.execute(plan).status, "reconcile_required")
        recovered = self.prepare(
            "install", mode="apply", expected_plan_sha256=plan.reconciliation_key()
        )
        self.assertEqual(self.execute(recovered).status, "adopted")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.execute(recovered).status, "replayed")
        with self.assertRaises(ValueError):
            self.prepare(
                "install",
                mode="apply",
                expected_plan_sha256=plan.reconciliation_key(),
                listener_port=9553,
            )

    def test_unknown_or_foreign_source_stays_uncertain_and_blocks_a_new_key(self) -> None:
        plan = self.prepare()
        self.lose_response = True
        self.execute(plan)
        self.fixture.placement["composeFile"] = "foreign"
        self.assertEqual(self.execute(plan).status, "reconcile_required")
        self.assertEqual(self.execute(plan, "another-key").status, "target_busy")
        self.assertEqual(len(self.calls), 1)
        with self.assertRaises(ValueError):
            plan.readback()

    def test_reviewed_remove_restores_original_source_and_refuses_drift(self) -> None:
        plan = self.prepare()
        self.execute(plan)
        removal = self.prepare(operation="remove", original_activation_key="install")
        self.assertEqual(self.execute(removal, "remove").status, "completed")
        self.assertEqual(self.fixture.placement, self.before)
        self.assertTrue(removal.readback()["configuration_matches"])
        self.assertEqual(self.execute(removal, "remove").status, "replayed")
        self.fixture.placement["composeFile"] = "foreign"
        with self.assertRaises(ValueError):
            self.prepare(operation="remove", original_activation_key="install")

    def test_stale_source_provider_modes_and_active_queue_refuse_before_writes(self) -> None:
        for changes in (
            {"customGitBranch": "main"},
            {"sourceType": "raw"},
            {"autoDeploy": True},
            {"composeType": "stack"},
            {"serviceNetworks": ["production"]},
            {"mounts": ["production"]},
        ):
            self.fixture.placement = {**self.before, **changes}
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.prepare()
        self.fixture.placement = deepcopy(self.before)
        plan = self.prepare()
        self.active_work = True
        with self.assertRaises(ValueError):
            self.execute(plan)
        self.assertFalse(self.calls)
        self.assertEqual(self.fixture.placement, self.before)

    def test_unqualified_template_refusals_do_not_write_provider_or_original_values(self) -> None:
        original = deepcopy(self.template)
        for service_change in (
            {"volumes": ["/production:/state"]},
            {"network_mode": "host"},
            {"environment": {"AGENT_SESSION_TOKEN": "fictional-literal"}},
            {"environment": {"AGENT_SESSION_TOKEN": "${AGENT_SESSION_TOKEN:-fictional-literal}"}},
            {"image": "fixture:latest"},
            {"volumes": ["${HOST_VOLUME}:/state"]},
        ):
            self.template = deepcopy(original)
            self.template["services"]["bridge"].update(service_change)
            with self.subTest(service_change=service_change), self.assertRaises(ValueError):
                self.prepare()
        self.assertFalse(self.calls)

    def test_http_auth_review_and_database_execution_guards(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=LaunchplaneAuthzPolicy(schema_version=2),
            record_store_factory=lambda: self.store,
            control_plane_root_path=self.root,
            bearer_identity_config=BearerIdentityConfig(
                local_operator_token="fictional-test-operator",
                local_operator_subject="local-owner-agent",
                local_operator_token_label="local-owner-write",
            ),
        )
        headers = {"Authorization": "Bearer fictional-test-operator"}

        async def exercise() -> None:
            route = ROUTE.format(product="example-site")
            with scoped_agent_transport():
                for mode in ("dry-run", "apply"):
                    response = await http_request(
                        app, "POST", route, headers=headers, payload={**self.payload, "mode": mode}
                    )
                    self.assertEqual(response.status_code, 403, response.text)
            denied = await http_request(app, "POST", route, payload=self.payload)
            self.assertEqual(denied.status_code, 401)
            plan = await http_request(app, "POST", route, headers=headers, payload=self.payload)
            self.assertEqual(plan.status_code, 202, plan.text)
            missing = await http_request(
                app, "POST", route, headers=headers, payload={**self.payload, "mode": "apply"}
            )
            self.assertEqual(missing.status_code, 400)
            guarded = await http_request(
                app,
                "POST",
                route,
                headers={**headers, "Idempotency-Key": "install"},
                payload={
                    **self.payload,
                    "mode": "apply",
                    "expected_plan_sha256": plan.json()["result"]["plan_sha256"],
                },
            )
            self.assertEqual(guarded.status_code, 503, guarded.text)
            production = await http_request(
                app, "POST", route, headers=headers, payload={**self.payload, "instance": "prod"}
            )
            self.assertEqual(production.status_code, 400)

        asyncio.run(exercise())
        self.assertFalse(self.calls)

    def test_cli_review_validation_and_original_key_forwarding(self) -> None:
        payload = self.root / "metadata.json"
        payload.write_text(json.dumps(self.payload))
        args = [
            "ingress",
            "private-testing-activate",
            "--service-url",
            "https://launchplane.test",
            "--product",
            "example-site",
            "--payload-file",
            str(payload),
        ]
        with (
            patch.dict(os.environ, {"LAUNCHPLANE_SERVICE_TOKEN": "fictional-transport-only"}),
            patch(
                "control_plane.cli_ingress._post_launchplane_service_json",
                return_value={"status": "accepted"},
            ) as post,
        ):
            runner = CliRunner()
            self.assertEqual(runner.invoke(main, args).exit_code, 0)
            payload.write_text(json.dumps({**self.payload, "mode": "apply"}))
            self.assertNotEqual(runner.invoke(main, args).exit_code, 0)
            self.assertEqual(post.call_count, 1)
            digest = self.prepare().reconciliation_key()
            payload.write_text(
                json.dumps({**self.payload, "mode": "apply", "expected_plan_sha256": digest})
            )
            result = runner.invoke(main, [*args, "--idempotency-key", "original-key"])
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertEqual(post.call_args.kwargs["idempotency_key"], "original-key")
            self.assertEqual(post.call_args.kwargs["payload"]["expected_plan_sha256"], digest)

    def test_shared_physical_target_and_changed_review_refuse_before_mutation(self) -> None:
        original = self.prepare()
        self.fixture.placement["env"] = "SETTING=changed"
        with self.assertRaises(ValueError):
            self.prepare(expected_plan_sha256=original.reconciliation_key())
        production = self.store.read_dokploy_target_id_record(
            context_name="example-site", instance_name="prod"
        )
        self.store.write_dokploy_target_id_record(
            production.model_copy(update={"target_id": "fixture-testing"})
        )
        with self.assertRaises(ValueError):
            self.prepare()
        self.assertFalse(self.calls)
