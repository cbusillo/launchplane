"""The administrator adapter admits the common native operation without provider writes."""

import asyncio
from threading import Event
from typing import Any
import unittest
from dataclasses import replace
from functools import partial
from email.message import Message
from urllib.error import HTTPError
from unittest.mock import patch

from httpx2 import Response

from control_plane.build_provenance import VerifiedBuildArtifact
from control_plane.contracts.artifact_identity import ArtifactSourceBuild
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.http_routes import testing_rebuild as routes
from control_plane import testing_rebuild_preflight as preflight
from control_plane import odoo_target_replacement_apply_http as admission
from control_plane.contracts.odoo_stable_target_replacement_operation import (
    OdooStableTargetReplacementOperationRecord,
)
from control_plane.service_human_auth import HumanSessionManager, InMemoryHumanSessionStore
from tests import test_testing_rebuild_worker as worker_tests
from tests import test_odoo_prod_promotion_operation as administrator_tests
from tests.http_app_test_support import (
    _browser_mutation_headers,
    _github_human_identity,
    _github_oauth_config,
    _RejectingVerifier,
)
from tests.support.http import LIFESPAN_HANG_DEADLINE_SECONDS, lifespan_client, request


class TestingRebuildHttpFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = worker_tests.NativeTestingWorkerTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store = self.fixture.store
        self.product = self.fixture.operation.product
        self.policy = administrator_tests._ADMINISTRATOR_POLICY
        self.store.seed_authz_policy_if_absent(administrator_tests._policy_record(self.policy))
        self.sessions = HumanSessionManager(
            config=_github_oauth_config(), session_store=InMemoryHumanSessionStore()
        )
        self.session = self.sessions.issue(_github_human_identity())
        self.app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=self.policy,
            record_store_factory=lambda: self.store,
            human_session_manager=self.sessions,
            control_plane_root_path=self.fixture.root,
        )
        artifact = self.fixture.fixture.fixture.artifact
        self.manifest = artifact.model_copy(
            update={
                "artifact_id": "ui-current-build",
                "source_commit": "c" * 40,
                "source_build": ArtifactSourceBuild(
                    repository=self.fixture.intent.profile.repository,
                    repository_id="12345",
                    workflow_path=".github/workflows/build.yml",
                    event="push",
                    purpose="release",
                    run_id=55,
                    run_attempt=1,
                    github_artifact_id=66,
                    manifest_artifact_id="producer-build",
                ),
            }
        )
        self.latest = self.enterContext(
            patch.object(
                routes,
                "latest_verified_testing_build",
                return_value=VerifiedBuildArtifact(manifest=self.manifest),
            )
        )
        self.enterContext(
            patch.object(preflight, "capture_rebuild_intent", return_value=self.fixture.intent)
        )
        self.url = f"/v1/products/{self.product}/testing-rebuild"
        self.payload: dict[str, object] = {
            "data_source_mode": "empty",
            "artifact_id": self.manifest.artifact_id,
            "source_git_ref": self.manifest.source_commit,
            "reason": "Rebuild disposable testing",
        }

    async def call(
        self,
        method: str,
        payload: dict[str, object] | None = None,
        *,
        key: str = "button-one",
        path: str = "",
        csrf: bool = True,
    ) -> Response:
        headers = _browser_mutation_headers(self.sessions, self.session)
        headers["Idempotency-Key"] = key
        if not csrf:
            headers.pop("X-CSRF-Token", None)
        return await request(self.app, method, path or self.url, headers=headers, payload=payload)


class TestingRebuildHttpTests(TestingRebuildHttpFixture, unittest.IsolatedAsyncioTestCase):
    async def test_native_preflight_exposes_fixed_checkpoint_without_private_exception(
        self,
    ) -> None:
        from control_plane.testing_rebuild_service import TestingRebuildRefused
        from control_plane.odoo_target_replacement_plan_http import (
            ODOO_TARGET_REPLACEMENT_PLAN_ROUTE,
        )
        from control_plane.service_auth import BearerIdentityConfig
        from control_plane.contracts.odoo_stable_target_replacement import (
            OdooStableTargetReplacementRequest,
        )

        before = self.store.list_odoo_stable_target_replacement_operation_records()
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=self.policy,
            record_store_factory=lambda: self.store,
            control_plane_root_path=self.fixture.root,
            bearer_identity_config=BearerIdentityConfig(
                local_operator_token="fixture-operator",
                local_operator_subject="fixture-agent",
                local_operator_token_label="fixture-agent-token",
            ),
        )
        failure = TestingRebuildRefused(
            "testing_route_compilation_failed",
            "The observed testing ingress cannot be safely compiled for its retained copy.",
        )
        with patch.object(preflight, "inspect_testing_rebuild", side_effect=failure):
            response = await request(
                app,
                "POST",
                ODOO_TARGET_REPLACEMENT_PLAN_ROUTE,
                headers={"Authorization": "Bearer fixture-operator"},
                payload={
                    "product": self.product,
                    "replacement": self.fixture.operation.request.model_dump(
                        mode="json", include=set(OdooStableTargetReplacementRequest.model_fields)
                    ),
                },
            )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["error"]["code"], failure.code)
        self.assertTrue(response.json()["trace_id"])
        self.assertEqual(self.store.list_odoo_stable_target_replacement_operation_records(), before)
        self.assertEqual(self.fixture.effects, [])

    async def test_read_only_preflight_does_not_admit_artifact_or_candidate(self) -> None:
        before = self.store.list_odoo_stable_target_replacement_operation_records()
        response = await self.call("GET", path=self.url + "/inputs")
        self.assertEqual(response.status_code, 200, response.json())
        self.assertEqual(response.json()["base_url"], "https://" + self.fixture.fixture.route.host)
        self.assertEqual(response.json()["artifact_id"], self.manifest.artifact_id)
        self.assertEqual(response.json()["candidate_verification"], "pending")
        self.assertEqual(self.store.list_odoo_stable_target_replacement_operation_records(), before)
        with self.assertRaises(FileNotFoundError):
            self.store.read_artifact_manifest(self.manifest.artifact_id)
        self.assertEqual(self.fixture.effects, [])

    async def test_provider_denial_and_unavailable_preflight_cannot_queue(self) -> None:
        import click
        from control_plane.dokploy import api

        before = self.store.list_odoo_stable_target_replacement_operation_records()
        for failure, status in (
            (
                api.DokployRequestFailed(
                    method="GET",
                    path="/api/compose.one",
                    detail="private-provider-message",
                    status_code=403,
                ),
                403,
            ),
            (
                api.DokployRequestFailed(
                    method="GET",
                    path="/api/compose.one",
                    detail="private-provider-message",
                    status_code=503,
                ),
                409,
            ),
            (click.ClickException("private-bootstrap-message"), 409),
            (ValueError("private-retained-identity-message"), 409),
            (FileNotFoundError("private-retained-record"), 409),
        ):
            for method in ("GET", "POST"):
                with (
                    self.subTest(failure=type(failure).__name__, method=method),
                    patch.object(preflight, "capture_rebuild_intent", side_effect=failure),
                ):
                    response = await self.call(
                        method,
                        self.payload if method == "POST" else None,
                        path=self.url if method == "POST" else self.url + "/inputs",
                    )
                    self.assertEqual(response.status_code, status, response.text)
                    self.assertTrue(response.json()["trace_id"])
                    self.assertEqual(
                        response.json()["error"]["code"],
                        "authorization_denied"
                        if status == 403
                        else "testing_rebuild_inputs_unverified",
                    )
                    self.assertNotIn("private-", response.text)
        self.assertEqual(self.store.list_odoo_stable_target_replacement_operation_records(), before)
        self.assertEqual(self.fixture.effects, [])

    async def test_pre_admission_enqueue_validation_has_a_fixed_refusal(self) -> None:
        before = self.store.list_odoo_stable_target_replacement_operation_records()
        with patch.object(
            admission,
            "_validate_target_replacement_apply_artifact_authority",
            side_effect=ValueError("private-revalidated-artifact"),
        ):
            response = await self.call("POST", self.payload)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["error"]["code"], "testing_rebuild_not_ready")
        self.assertTrue(response.json()["trace_id"])
        self.assertNotIn("private-", response.text)
        self.assertEqual(self.store.list_odoo_stable_target_replacement_operation_records(), before)

    async def test_lost_create_reply_keeps_the_admitted_operation_uncertain(self) -> None:
        create = self.store.create_odoo_stable_target_replacement_operation_record_if_no_active_lane

        def committed(
            record: OdooStableTargetReplacementOperationRecord,
        ) -> tuple[OdooStableTargetReplacementOperationRecord, bool]:
            create(record)
            raise ValueError("fixture lost database reply after commit")

        with (
            patch.object(
                self.store,
                "create_odoo_stable_target_replacement_operation_record_if_no_active_lane",
                side_effect=committed,
            ),
            self.assertRaises(ValueError),
        ):
            await self.call("POST", self.payload)
        saved = next(
            operation
            for operation in self.store.list_odoo_stable_target_replacement_operation_records()
            if operation.idempotency_key == "button-one"
        )
        self.assertTrue(saved.is_native_testing_rebuild)
        observed = await self.call("POST", self.payload, path=self.url + "/lookup")
        self.assertEqual(observed.status_code, 200, observed.text)
        self.assertEqual(observed.json()["operation"]["operation_id"], saved.operation_id)

    async def test_one_submit_replay_and_poll_keep_original_build_and_reason(self) -> None:
        accepted = await self.call("POST", self.payload)
        self.assertEqual(accepted.status_code, 200, accepted.json())
        operation = accepted.json()["operation"]
        saved = self.store.read_odoo_stable_target_replacement_operation_record(
            operation["operation_id"]
        )
        self.assertTrue(saved.is_native_testing_rebuild)
        self.assertEqual(saved.request.artifact_id, self.manifest.artifact_id)
        self.assertEqual(saved.request.reason, self.payload["reason"])
        self.assertIsNotNone(saved.authorization)
        self.latest.side_effect = AssertionError("A replay must not choose another build")
        replay = await self.call("POST", self.payload)
        self.assertEqual(replay.status_code, 200, replay.json())
        self.assertEqual(replay.json()["operation"], operation)
        polled = await self.call("GET", path=self.url + "/operations/" + operation["operation_id"])
        self.assertEqual(polled.status_code, 200, polled.json())
        self.assertEqual(polled.json()["operation"], operation)
        self.assertNotIn("request", polled.json()["operation"])
        self.assertEqual(self.fixture.effects, [])

    async def test_original_key_lookup_only_reads_and_never_admits_missing_or_changed_intent(
        self,
    ) -> None:
        before = self.store.list_odoo_stable_target_replacement_operation_records()
        missing_csrf = await self.call("POST", self.payload, path=self.url + "/lookup", csrf=False)
        self.assertEqual(missing_csrf.status_code, 403, missing_csrf.text)
        absent = await self.call("POST", self.payload, path=self.url + "/lookup")
        self.assertEqual(absent.status_code, 404, absent.text)
        self.assertEqual(self.store.list_odoo_stable_target_replacement_operation_records(), before)
        accepted = await self.call("POST", self.payload)
        self.assertEqual(accepted.status_code, 200, accepted.text)
        self.latest.side_effect = AssertionError("Lookup cannot reselect, prepare or enqueue")
        looked_up = await self.call("POST", self.payload, path=self.url + "/lookup")
        self.assertEqual(looked_up.status_code, 200, looked_up.text)
        self.assertEqual(looked_up.json()["operation"], accepted.json()["operation"])
        changed = await self.call(
            "POST", {**self.payload, "reason": "Changed intent"}, path=self.url + "/lookup"
        )
        self.assertEqual(changed.status_code, 409, changed.text)
        self.assertNotIn(accepted.json()["operation"]["operation_id"], changed.text)
        self.assertEqual(self.fixture.effects, [])

    async def test_absent_lookup_does_not_exclude_an_original_submission_in_flight(self) -> None:
        entered = asyncio.Event()
        release = Event()
        loop = asyncio.get_running_loop()
        inspect = preflight.inspect_testing_rebuild

        def held_inspection(**kwargs: Any) -> object:
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(LIFESPAN_HANG_DEADLINE_SECONDS):
                raise AssertionError("The test did not release its original inspection")
            return inspect(**kwargs)

        def headers() -> dict[str, str]:
            current = _browser_mutation_headers(self.sessions, self.session)
            current["Idempotency-Key"] = "original-in-flight"
            return current

        with patch.object(routes, "inspect_testing_rebuild", side_effect=held_inspection):
            async with lifespan_client(self.app) as client:
                original = asyncio.create_task(
                    request(client, "POST", self.url, headers=headers(), payload=self.payload)
                )
                try:
                    await asyncio.wait_for(entered.wait(), LIFESPAN_HANG_DEADLINE_SECONDS)
                    absent = await request(
                        client,
                        "POST",
                        self.url + "/lookup",
                        headers=headers(),
                        payload=self.payload,
                    )
                    self.assertEqual(absent.status_code, 404, absent.text)
                    self.assertFalse(original.done())
                finally:
                    release.set()
                    accepted = await original
                self.assertTrue(accepted.is_success, accepted.text)
                found = await request(
                    client, "POST", self.url + "/lookup", headers=headers(), payload=self.payload
                )
                self.assertEqual(
                    found.json()["operation"]["operation_id"],
                    accepted.json()["operation"]["operation_id"],
                )
        self.assertEqual(self.fixture.effects, [])

    async def test_missing_csrf_and_non_testing_input_cannot_queue(self) -> None:
        before = self.store.list_odoo_stable_target_replacement_operation_records()
        missing = await self.call("POST", self.payload, csrf=False)
        self.assertEqual(missing.status_code, 403, missing.json())
        for extra in ({"instance": "prod"}, {"target_id": "foreign"}, {"reason": "  "}):
            result = await self.call("POST", {**self.payload, **extra})
            self.assertEqual(result.status_code, 400, result.json())
        self.assertEqual(self.store.list_odoo_stable_target_replacement_operation_records(), before)
        self.assertEqual(self.fixture.effects, [])

    async def test_changed_build_refuses_without_admitting_old_or_new_manifest(self) -> None:
        self.latest.return_value = VerifiedBuildArtifact(
            manifest=self.manifest.model_copy(update={"source_commit": "d" * 40})
        )
        response = await self.call("POST", self.payload)
        self.assertEqual(response.status_code, 409, response.json())
        with self.assertRaises(FileNotFoundError):
            self.store.read_artifact_manifest(self.manifest.artifact_id)
        self.assertEqual(self.fixture.effects, [])

    async def test_another_signed_in_person_cannot_inspect_or_submit(self) -> None:
        self.session = self.sessions.issue(replace(_github_human_identity(), github_id=124))
        for method, path, payload in (
            ("GET", self.url + "/inputs", None),
            ("POST", self.url, self.payload),
            ("POST", self.url + "/lookup", self.payload),
        ):
            result = await self.call(method, payload, path=path)
            self.assertEqual(result.status_code, 403, result.json())
        self.latest.assert_not_called()
        self.assertEqual(self.fixture.effects, [])

    async def test_known_use_blocks_both_preflight_and_submission(self) -> None:
        from control_plane import testing_rebuild_service as service
        from control_plane.odoo_testing_usage import OdooTestingUsage

        with patch.object(
            service, "read_odoo_testing_usage", return_value=OdooTestingUsage("in_use")
        ):
            inspected = await self.call("GET", path=self.url + "/inputs")
            submitted = await self.call("POST", self.payload)
        self.assertEqual(inspected.status_code, 409, inspected.json())
        self.assertEqual(submitted.status_code, 409, submitted.json())
        with self.assertRaises(FileNotFoundError):
            self.store.read_artifact_manifest(self.manifest.artifact_id)
        self.assertEqual(self.fixture.effects, [])

    async def test_verified_down_historical_use_allows_preflight_and_the_same_queue(self) -> None:
        from datetime import UTC, datetime
        from control_plane import testing_rebuild_service as service
        from control_plane.odoo_testing_usage import OdooTestingUsage
        from tests.support.down_lanes import record_lane_health
        from tests.support.disposable_lanes import record_safety

        before = {
            item.operation_id
            for item in self.store.list_odoo_stable_target_replacement_operation_records(
                product=self.product
            )
        }
        record_lane_health(
            self.store, self.product, self.fixture.operation.context, datetime.now(UTC)
        )
        record_safety(
            self.store, self.product, self.fixture.operation.context, "testing", usage="in_use"
        )
        intent = self.fixture.intent.model_copy(
            update={"profile": self.store.read_product_profile_record(self.product)}
        )
        with (
            patch.object(preflight, "capture_rebuild_intent", return_value=intent),
            patch.object(
                service, "read_odoo_testing_usage", return_value=OdooTestingUsage("in_use")
            ),
        ):
            inspected = await self.call("GET", path=self.url + "/inputs")
            submitted = await self.call("POST", self.payload)
        self.assertEqual(inspected.status_code, 200, inspected.json())
        self.assertTrue(submitted.is_success, submitted.json())
        self.assertEqual(self.fixture.effects, [])
        operations = self.store.list_odoo_stable_target_replacement_operation_records(
            product=self.product
        )
        operation_id = submitted.json()["operation"]["operation_id"]
        self.assertEqual({item.operation_id for item in operations} - before, {operation_id})
        operation = next(item for item in operations if item.operation_id == operation_id)
        self.assertTrue(operation.is_native_testing_rebuild)

    async def test_provider_read_denial_keeps_trace_and_does_not_enqueue(self) -> None:
        from control_plane.dokploy.native_copy_stream import NativeSourceAuthorizationDenied

        self.latest.side_effect = NativeSourceAuthorizationDenied("Fixture provider refusal")
        response = await self.call("POST", self.payload)
        self.assertEqual(response.status_code, 403, response.json())
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")
        self.assertTrue(response.json()["trace_id"])
        with self.assertRaises(FileNotFoundError):
            self.store.read_artifact_manifest(self.manifest.artifact_id)
        self.assertEqual(self.fixture.effects, [])

    async def test_generated_credential_provenance_is_checked_without_rotating_values(self) -> None:
        binding = next(
            item
            for item in self.store.list_secret_bindings(
                context_name=self.fixture.operation.context, instance_name="testing"
            )
            if item.value_source == "generated"
        )
        before = self.store.read_secret_record(binding.secret_id)
        self.store.write_secret_binding(binding.model_copy(update={"value_source": "supplied"}))
        response = await self.call("GET", path=self.url + "/inputs")
        self.assertEqual(response.status_code, 409, response.json())
        self.assertEqual(self.store.read_secret_record(binding.secret_id), before)
        self.assertEqual(self.fixture.effects, [])

    async def test_upstream_preflight_refuses_missing_source_configuration(self) -> None:
        path = self.url + "/inputs?data_source_mode=upstream_restore"
        configured = await self.call("GET", path=path)
        self.assertEqual(configured.status_code, 200, configured.json())
        record = next(
            item
            for item in self.store.list_runtime_environment_records(
                context_name=self.fixture.operation.context, instance_name="testing"
            )
            if item.scope == "instance" and item.instance == "testing"
        )
        self.store.write_runtime_environment_record(
            record.model_copy(
                update={
                    "env": {
                        key: value
                        for key, value in record.env.items()
                        if key != "ODOO_UPSTREAM_HOST"
                    }
                }
            )
        )
        refused = await self.call("GET", path=path)
        self.assertEqual(refused.status_code, 409, refused.json())
        self.assertEqual(self.fixture.effects, [])

    def test_current_build_reader_stops_at_denial_without_selecting_another_build(self) -> None:
        from control_plane import product_reconcile
        from control_plane.build_provenance import BuildProvenanceError
        from control_plane.dokploy.native_copy_stream import NativeSourceAuthorizationDenied
        from unittest.mock import Mock

        calls: list[str] = []

        def denied(path: str) -> object:
            calls.append(path)
            raise BuildProvenanceError(
                "Fixture read refused", code="github_read_failed"
            ) from HTTPError("https://api.github.com/read", 403, "Forbidden", Message(), None)

        reader = product_reconcile._TestingRebuildBuildReads(Mock(get_json=denied))
        with self.assertRaises(NativeSourceAuthorizationDenied):
            # This is the selector's invalid-build fallback behavior. A genuine
            # denial must escape it before a read of another candidate.
            try:
                reader.get_json("current")
            except BuildProvenanceError:
                reader.get_json("older")
        self.assertEqual(calls, ["current"])

    def test_rate_limit_or_expired_storage_url_stops_selection_without_identity_denial(
        self,
    ) -> None:
        from control_plane import product_reconcile
        from control_plane.build_provenance import BuildProvenanceError
        from unittest.mock import Mock

        for url, headers in (
            ("https://api.github.com/repos/example/actions", {"X-RateLimit-Remaining": "0"}),
            ("https://api.github.com/repos/example/actions", {"Retry-After": "60"}),
            ("https://artifacts.example.invalid/archive", {}),
        ):
            calls: list[str] = []
            values = Message()
            for key, value in headers.items():
                values[key] = value

            def unavailable(path: str) -> object:
                calls.append(path)
                raise BuildProvenanceError(
                    "fixture temporary read failure", code="github_read_failed"
                ) from HTTPError(url, 403, "Forbidden", values, None)

            reader = product_reconcile._TestingRebuildBuildReads(Mock(get_json=unavailable))
            with (
                self.subTest(url=url, headers=headers),
                self.assertRaises(product_reconcile.ProductReconcileError),
            ):
                try:
                    reader.get_json("current")
                except BuildProvenanceError:
                    reader.get_json("older")
            self.assertEqual(calls, ["current"])

    async def test_new_machine_admission_requires_reason_but_original_receipt_still_replays(
        self,
    ) -> None:
        from control_plane.odoo_target_replacement_apply_http import (
            OdooTargetReplacementApplyEnvelope,
            enqueue_odoo_target_replacement_apply_operation,
        )

        accepted = await self.call("POST", self.payload)
        self.assertEqual(accepted.status_code, 200, accepted.json())
        saved = self.store.read_odoo_stable_target_replacement_operation_record(
            accepted.json()["operation"]["operation_id"]
        )
        assert saved.authorization is not None
        no_reason = OdooTargetReplacementApplyEnvelope(
            product=saved.product, replacement=saved.request.model_copy(update={"reason": ""})
        )
        enqueue = partial(
            enqueue_odoo_target_replacement_apply_operation,
            record_store=self.store,
            request=no_reason,
            context=saved.context,
            idempotency_scope=saved.idempotency_scope,
            request_fingerprint=saved.request_fingerprint,
            created_at=saved.created_at,
            authorization=saved.authorization,
        )
        # Read an already admitted receipt before checking new-operation prerequisites.
        _, replayed = enqueue(idempotency_key=saved.idempotency_key)
        self.assertEqual(replayed["operation_id"], saved.operation_id)
        with self.assertRaises(ValueError):
            enqueue(idempotency_key="new-machine-request")
        self.assertEqual(self.fixture.effects, [])

    async def test_native_admission_requires_a_caller_who_can_resume(self) -> None:
        from control_plane.contracts.durable_operation_authorization import (
            DurableOperationAuthorization,
        )
        from control_plane.durable_operation_authorization import (
            DurableOperationAuthorizationDeniedError,
        )

        accepted = await self.call("POST", self.payload)
        self.assertEqual(accepted.status_code, 200, accepted.json())
        saved = self.store.read_odoo_stable_target_replacement_operation_record(
            accepted.json()["operation"]["operation_id"]
        )
        assert saved.authorization is not None
        workflow = DurableOperationAuthorization.model_validate(
            {
                **saved.authorization.model_dump(mode="json"),
                "grant": "policy_rule",
                "managed_set_id": "fixture-workflow-set",
                "managed_rule_id": "fixture-workflow-rule",
                "caller": {
                    "identity_type": "github_actions",
                    "subject": "fixture-workflow",
                    "repository": "example/product",
                    "workflow_ref": "example/product/.github/workflows/testing.yml@main",
                },
            }
        )
        # Historical receipts remain readable, without admitting another effect.
        _, replayed = admission.enqueue_odoo_target_replacement_apply_operation(
            record_store=self.store,
            request=admission.OdooTargetReplacementApplyEnvelope(
                product=saved.product, replacement=saved.request
            ),
            context=saved.context,
            idempotency_key=saved.idempotency_key,
            idempotency_scope=saved.idempotency_scope,
            request_fingerprint=saved.request_fingerprint,
            created_at=saved.created_at,
            authorization=workflow,
        )
        self.assertEqual(replayed["operation_id"], saved.operation_id)
        with self.assertRaises(DurableOperationAuthorizationDeniedError):
            admission.build_odoo_target_replacement_apply_operation_record(
                replacement_request=saved.request,
                context=saved.context,
                idempotency_key="new-workflow-request",
                idempotency_scope=saved.idempotency_scope,
                request_fingerprint=saved.request_fingerprint,
                created_at=saved.created_at,
                authorization=workflow,
            )
        self.assertEqual(self.fixture.effects, [])

    async def test_queue_race_reports_the_same_key_conflict_as_the_initial_lookup(self) -> None:
        accepted = await self.call("POST", self.payload)
        self.assertEqual(accepted.status_code, 200, accepted.json())
        saved = self.store.read_odoo_stable_target_replacement_operation_record(
            accepted.json()["operation"]["operation_id"]
        )
        self.store.write_odoo_stable_target_replacement_operation_record(
            saved.model_copy(
                update={
                    "status": "fail",
                    "phase": "failed",
                    "finished_at": saved.created_at,
                    "error_message": "The fixture worker failed before provider effects.",
                }
            )
        )
        different = {**self.payload, "reason": "A different rebuild intent"}
        initial = await self.call("POST", different)
        self.assertEqual(initial.status_code, 409, initial.json())
        # Another request can commit between the handler's lookup and queue CAS.
        # Keep the real queue's second lookup and immutable request check.
        with patch.object(
            routes,
            "find_odoo_target_replacement_apply_operation_by_idempotency_key",
            return_value=None,
        ):
            raced = await self.call("POST", different)
        self.assertEqual(raced.status_code, initial.status_code, raced.json())
        self.assertEqual(raced.json()["error"]["code"], initial.json()["error"]["code"])
        self.assertEqual(self.fixture.effects, [])

    async def test_receipt_projects_the_saved_operation_without_extra_helper_fields(self) -> None:
        from control_plane.odoo_target_replacement_apply_http import (
            enqueue_odoo_target_replacement_apply_operation as enqueue,
        )

        def without_full_payload(**kwargs: object) -> tuple[dict[str, object], dict[str, object]]:
            from typing import Any, cast

            records, payload = enqueue(**cast(Any, kwargs))
            return records, {**payload, "provider_detail": "not a browser operation field"}

        with patch.object(
            routes,
            "enqueue_odoo_target_replacement_apply_operation",
            side_effect=without_full_payload,
        ):
            response = await self.call("POST", self.payload)
        self.assertEqual(response.status_code, 200, response.json())
        operation = response.json()["operation"]
        saved = self.store.read_odoo_stable_target_replacement_operation_record(
            operation["operation_id"]
        )
        self.assertEqual(operation["status"], saved.status)
        self.assertNotIn("provider_detail", operation)
        self.assertEqual(self.fixture.effects, [])
