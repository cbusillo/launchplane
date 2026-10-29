from tests.support.auth import scoped_agent_test
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import os
from typing import cast
from unittest.mock import patch
from urllib.parse import urlencode

from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import (
    BearerIdentityConfig,
    LaunchplaneAuthzPolicy,
    LocalOperatorPolicyRule,
)
from control_plane.service_human_auth import HumanSessionManager, InMemoryHumanSessionStore
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.contracts.verireel_prod_backup_gate import (
    VeriReelProdBackupGateRequest,
    VeriReelProdBackupGateResult,
)
from control_plane.contracts.verireel_prod_backup_gate_operation import (
    VeriReelProdBackupGateOperationRecord,
    VeriReelProdBackupGateOperationStatus,
)
from tests.http_app_test_support import (
    _browser_mutation_headers,
    _github_human_identity,
    _github_oauth_config,
    _local_operator_bearer_config,
    _RejectingVerifier,
)
from tests.support.http import get, request
from tests.test_production_backup_provider import _binding
from control_plane.contracts.authz_policy_record import (
    LaunchplaneAuthzPolicyRecord,
    authz_policy_sha256,
    build_authz_policy_record_id,
)
from tests.support.auth import _identity, _StubVerifier
from tests.test_http_app_production_backup_authority import (
    _authz_policy,
    _WORKFLOW_REF,
    _JOB_WORKFLOW_REF,
)
from tests.test_postgres_integration import _store_for_fresh_head_database
from tests import test_generic_web_client_release as release_fixtures
from control_plane.action_effects import BACKUP_CONTINUATION_ACTION
from control_plane.contracts.durable_operation_authorization import DurableOperationAuthorization
from tests.support.durable_operations import durable_operation_authorization_payload


class ProductionBackupGateHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_operations_rejects_blank_or_missing_scope_and_invalid_selectors(
        self,
    ) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            bearer_identity_config=_local_operator_bearer_config(),
            authz_policy=LaunchplaneAuthzPolicy(),
            record_store_factory=object,
        )
        scope = {"product": "verireel", "context": "allowed", "instance": "prod"}
        invalid_queries = [scope | {field: " "} for field in scope]
        invalid_queries.extend(
            {key: value for key, value in scope.items() if key != field} for field in scope
        )
        invalid_queries.extend(
            scope | selector
            for selector in ({"limit": "0"}, {"limit": "101"}, {"status": "unknown"})
        )
        for query in invalid_queries:
            with self.subTest(query=query):
                response = await get(
                    app,
                    f"/v1/production-backup-gates/operations?{urlencode(query)}",
                    headers={"Authorization": "Bearer local-operator-token"},
                )
                self.assertEqual(response.status_code, 400, response.text)

    async def test_list_operations_is_scoped_filtered_bounded_and_sanitized(self) -> None:
        policy = LaunchplaneAuthzPolicy(
            schema_version=2,
            local_operators=(
                LocalOperatorPolicyRule(
                    subjects=("local-owner-agent",),
                    token_labels=("local-owner-read",),
                    products=("verireel",),
                    contexts=("allowed",),
                    instances=("prod",),
                    actions=("production_backup_authority.read",),
                ),
            ),
        )
        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(
                database_url=f"sqlite+pysqlite:///{Path(directory) / 'state.db'}"
            )
            self.addCleanup(store.close)
            store.ensure_schema()
            for operation_id, context, instance, status, updated_at in (
                ("pending-new", "allowed", "prod", "pending", "2026-10-28T04:00:00Z"),
                ("pending-old", "allowed", "prod", "pending", "2026-10-28T03:00:00Z"),
                ("running", "allowed", "prod", "running", "2026-10-28T02:00:00Z"),
                ("other-context", "other", "prod", "pending", "2026-10-28T05:00:00Z"),
            ):
                request_payload = VeriReelProdBackupGateRequest(
                    context=context,
                    instance=instance,
                    backup_record_id=f"backup-{operation_id}",
                )
                store.write_verireel_prod_backup_gate_operation_record(
                    VeriReelProdBackupGateOperationRecord(
                        operation_id=operation_id,
                        context=context,
                        instance=instance,
                        backup_record_id=request_payload.backup_record_id,
                        request_fingerprint=request_payload.model_dump_json(),
                        request=request_payload,
                        status=cast(VeriReelProdBackupGateOperationStatus, status),
                        phase="running" if status == "running" else "created",
                        created_at="2026-10-28T01:00:00Z",
                        updated_at=updated_at,
                        progress_evidence={
                            "snapshot_name": f"snapshot-{operation_id}",
                            "provider_endpoint": "pve.internal",
                            "credential": "super-secret",
                            "error_message": "provider leaked topology",
                        },
                        error_message="provider said super-secret at 10.4.5.6",
                    )
                )
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                bearer_identity_config=_local_operator_bearer_config(),
                authz_policy=policy,
                record_store_factory=lambda: store,
            )
            headers = {"Authorization": "Bearer local-operator-token"}
            scope = {"product": "verireel", "context": "allowed", "instance": "prod"}
            pending = await get(
                app,
                f"/v1/production-backup-gates/operations?{urlencode(scope | {'status': 'pending', 'limit': 1})}",
                headers=headers,
            )
            running = await get(
                app,
                f"/v1/production-backup-gates/operations?{urlencode(scope | {'status': 'running'})}",
                headers=headers,
            )

        self.assertEqual(pending.status_code, 200, pending.text)
        self.assertTrue(pending.json()["truncated"])
        self.assertEqual(
            [operation["operation_id"] for operation in pending.json()["operations"]],
            ["pending-new"],
        )
        self.assertEqual(running.status_code, 200, running.text)
        self.assertEqual({key: running.json()[key] for key in scope}, scope)
        self.assertTrue(running.json()["trace_id"])
        self.assertFalse(running.json()["truncated"])
        self.assertEqual(
            [operation["operation_id"] for operation in running.json()["operations"]],
            ["running"],
        )
        self.assertEqual(
            running.json()["operations"][0]["evidence"], {"snapshot_name": "snapshot-running"}
        )
        self.assertFalse(running.json()["operations"][0]["typed_binding"])
        for withheld in ("pve.internal", "super-secret", "10.4.5.6", "other-context"):
            self.assertNotIn(withheld, running.text)

    async def test_list_successful_typed_capture_retains_proof_times_without_mutation(self) -> None:
        directory = self.enterContext(TemporaryDirectory())
        store = PostgresRecordStore(
            database_url=f"sqlite+pysqlite:///{Path(directory) / 'state.db'}"
        )
        self.addCleanup(store.close)
        store.ensure_schema()
        binding = _binding()
        evidence = {
            "policy_record_id": binding.policy.record_id,
            "snapshot_name": "verified-snapshot",
            "snapshot_finished_at": "2026-10-28T01:01:00Z",
            "independent_backup_id": "verified-archive",
            "independent_backup_finished_at": "2026-10-28T01:02:00Z",
            "capture_status": "verified",
        }
        operation = VeriReelProdBackupGateOperationRecord(
            schema_version=3,
            operation_id="typed-success",
            product=binding.request.product,
            context=binding.request.context,
            instance=binding.request.instance,
            backup_record_id=binding.request.backup_record_id,
            request_fingerprint=binding.request.model_dump_json(),
            request=binding.request,
            binding=binding,
            authorization=DurableOperationAuthorization.model_validate(
                durable_operation_authorization_payload(
                    action="production_backup_gate.execute",
                    managed_rule_id="example-prod-backup",
                    product=binding.request.product,
                    context=binding.request.context,
                    instances=(binding.request.instance,),
                )
            ),
            status="pass",
            phase="completed",
            created_at="2026-10-28T01:00:00Z",
            updated_at="2026-10-28T01:02:00Z",
            finished_at="2026-10-28T01:02:00Z",
            result=VeriReelProdBackupGateResult(
                backup_record_id=binding.request.backup_record_id,
                backup_status="pass",
                evidence=evidence,
            ),
        )
        store.write_verireel_prod_backup_gate_operation_record(operation)
        legacy_request = VeriReelProdBackupGateRequest(
            context=operation.context, backup_record_id="legacy-backup"
        )
        store.write_verireel_prod_backup_gate_operation_record(
            VeriReelProdBackupGateOperationRecord(
                operation_id="other-product-success",
                context=operation.context,
                instance=operation.instance,
                backup_record_id=legacy_request.backup_record_id,
                request_fingerprint=legacy_request.model_dump_json(),
                request=legacy_request,
                status="pass",
                phase="completed",
                created_at=operation.created_at,
                updated_at="2026-10-28T02:00:00Z",
                finished_at="2026-10-28T02:00:00Z",
                result=VeriReelProdBackupGateResult(
                    backup_record_id=legacy_request.backup_record_id,
                    backup_status="pass",
                    evidence={"snapshot_name": "legacy-snapshot"},
                ),
            )
        )
        before = store.list_verireel_prod_backup_gate_operation_records()
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            bearer_identity_config=_local_operator_bearer_config(),
            authz_policy=LaunchplaneAuthzPolicy(),
            record_store_factory=lambda: store,
        )
        query = urlencode(
            {
                "product": operation.product,
                "context": operation.context,
                "instance": operation.instance,
                "status": "pass",
            }
        )
        response = await get(
            app,
            f"/v1/production-backup-gates/operations?{query}",
            headers={"Authorization": "Bearer local-operator-token"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["truncated"])
        (summary,) = response.json()["operations"]
        self.assertTrue(summary["typed_binding"])
        self.assertEqual(summary["evidence"], evidence)
        self.assertEqual(summary["finished_at"], operation.finished_at)
        self.assertEqual(summary["created_at"], operation.created_at)
        self.assertEqual(summary["updated_at"], operation.updated_at)
        padded_query = urlencode(
            {
                "product": f" {operation.product.upper()} ",
                "context": f" {operation.context.upper()} ",
                "instance": f" {operation.instance.upper()} ",
                "status": "pass",
            }
        )
        padded = await get(
            app,
            f"/v1/production-backup-gates/operations?{padded_query}",
            headers={"Authorization": "Bearer local-operator-token"},
        )
        self.assertEqual(padded.status_code, 200, padded.text)
        self.assertEqual(padded.json()["operations"], response.json()["operations"])
        self.assertEqual(store.list_verireel_prod_backup_gate_operation_records(), before)
        self.assertEqual(store.list_backup_gate_records(), ())

    @scoped_agent_test
    async def test_list_operations_requires_exact_scope_read_authorization(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            bearer_identity_config=_local_operator_bearer_config(),
            authz_policy=LaunchplaneAuthzPolicy(),
            record_store_factory=object,
        )
        query = urlencode({"product": "verireel", "context": "allowed", "instance": "prod"})
        response = await get(
            app,
            f"/v1/production-backup-gates/operations?{query}",
            headers={"Authorization": "Bearer local-operator-token"},
        )
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

        read_policy = LaunchplaneAuthzPolicy(
            schema_version=2,
            local_operators=(
                LocalOperatorPolicyRule(
                    subjects=("local-owner-agent",),
                    token_labels=("local-owner-read",),
                    products=("verireel",),
                    contexts=("allowed",),
                    instances=("prod",),
                    actions=("production_backup_authority.read",),
                ),
            ),
        )
        directory = self.enterContext(TemporaryDirectory())
        store = PostgresRecordStore(
            database_url=f"sqlite+pysqlite:///{Path(directory) / 'state.db'}"
        )
        self.addCleanup(store.close)
        store.ensure_schema()
        scoped_app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            bearer_identity_config=_local_operator_bearer_config(),
            authz_policy=read_policy,
            record_store_factory=lambda: store,
        )
        scope = {"product": "verireel", "context": "allowed", "instance": "prod"}
        for query_scope in (scope, *(scope | {key: "other"} for key in scope)):
            with self.subTest(scope=query_scope):
                scoped_response = await get(
                    scoped_app,
                    f"/v1/production-backup-gates/operations?{urlencode(query_scope)}",
                    headers={"Authorization": "Bearer local-operator-token"},
                )
                self.assertEqual(scoped_response.status_code, 200 if query_scope == scope else 403)

    async def test_director_continuation_preserves_acceptance_without_capture_permission(
        self,
    ) -> None:
        fixture = release_fixtures.GenericWebClientReleaseTests("runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        accepted, operation_id = fixture.stopped_precapture_backup()
        policy = LaunchplaneAuthzPolicy(
            schema_version=2,
            local_operators=(
                LocalOperatorPolicyRule(
                    subjects=("local-owner-agent",),
                    token_labels=("local-owner-read",),
                    products=(fixture.profile.product,),
                    contexts=(fixture.context,),
                    instances=("prod",),
                    actions=("product_environment.read",),
                ),
            ),
        )
        sessions = HumanSessionManager(
            config=_github_oauth_config(), session_store=InMemoryHumanSessionStore()
        )
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            bearer_identity_config=_local_operator_bearer_config(),
            authz_policy=policy,
            record_store_factory=lambda: fixture.store,
            control_plane_root_path=fixture.root,
            human_session_manager=sessions,
        )
        path = f"/v1/products/{fixture.profile.product}/releases/{accepted.record_id}/backup-continuation"
        body: dict[str, object] = {"reason": "Capture enrollment is qualified"}
        with patch(
            "control_plane.client_release_backup_continuation.guest_capture_metadata",
            return_value=(),
        ):
            denied = await request(app, "POST", path, payload=body)
            self.assertIn(denied.status_code, (401, 403))
            headers = {"Authorization": "Bearer local-operator-token"}
            missing = await request(
                app,
                "POST",
                f"/v1/products/{fixture.profile.product}/releases/missing-decision/backup-continuation",
                headers=headers,
                payload=body,
            )
            self.assertEqual(missing.status_code, 404, missing.text)
            self.assertEqual(missing.json()["error"]["code"], "release_record_not_found")
            planned = await request(app, "POST", path, headers=headers, payload=body)
            self.assertEqual(planned.status_code, 200, planned.text)
            self.assertTrue(planned.json()["apply_authorized"])
            self.assertEqual(
                fixture.store.read_verireel_prod_backup_gate_operation_record(operation_id).status,
                "fail",
            )
            body.update(apply=True, expected_digest=planned.json()["review_digest"])
            direct_backup = fixture.store.read_verireel_prod_backup_gate_operation_record(
                operation_id
            ).request.model_dump(mode="json")
            direct_backup["backup_record_id"] = "unaccepted-capture"
            denied_capture = await request(
                app,
                "POST",
                "/v1/production-backup-gates",
                headers={**headers, "Idempotency-Key": "forbidden-direct-capture"},
                payload=direct_backup,
            )
            self.assertEqual(denied_capture.status_code, 403, denied_capture.text)
            fixture.switch("held")
            held = await request(app, "POST", path, headers=headers, payload=body)
            self.assertEqual(held.status_code, 409, held.text)
            self.assertEqual(held.json()["error"]["code"], "release_acceptance_no_longer_current")
            self.assertEqual(
                fixture.store.read_verireel_prod_backup_gate_operation_record(operation_id).status,
                "fail",
            )
            fixture.switch("promote_with_rollback_drill")
            refreshed = await request(
                app, "POST", path, headers=headers, payload={"reason": body["reason"]}
            )
            self.assertEqual(refreshed.status_code, 200, refreshed.text)
            body["expected_digest"] = refreshed.json()["review_digest"]
            human_session = sessions.issue(_github_human_identity(role="read_only"))
            for product in (fixture.profile.product, "missing-product"):
                human_headers = _browser_mutation_headers(sessions, human_session)
                human_path = (
                    f"/v1/products/{product}/releases/{accepted.record_id}/backup-continuation"
                )
                denied_human = await request(
                    app, "POST", human_path, headers=human_headers, payload=body
                )
                self.assertEqual(denied_human.status_code, 403, denied_human.text)
                self.assertEqual(denied_human.json()["error"]["code"], "authorization_denied")
            applied = await request(app, "POST", path, headers=headers, payload=body)
            self.assertEqual(applied.status_code, 200, applied.text)
            self.assertEqual(applied.json()["status"], "queued")
            replay = await request(app, "POST", path, headers=headers, payload=body)
            self.assertEqual(replay.status_code, 200, replay.text)
            self.assertEqual(replay.json()["status"], "replayed")
        self.assertEqual(
            fixture.store.read_release_review_decision_record(
                product=fixture.profile.product, record_id=accepted.record_id
            ),
            accepted,
        )

    @scoped_agent_test
    async def test_scoped_permissions_cannot_supply_director_trust(self) -> None:
        fixture = release_fixtures.GenericWebClientReleaseTests("runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        accepted, operation_id = fixture.stopped_precapture_backup()
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            bearer_identity_config=_local_operator_bearer_config(),
            authz_policy=LaunchplaneAuthzPolicy(
                schema_version=2,
                local_operators=(
                    LocalOperatorPolicyRule(
                        subjects=("local-owner-agent",),
                        token_labels=("local-owner-read",),
                        products=(fixture.profile.product,),
                        contexts=(fixture.context,),
                        instances=("prod",),
                        actions=(
                            "product_environment.read",
                            "production_backup_gate.execute",
                            BACKUP_CONTINUATION_ACTION,
                        ),
                    ),
                ),
            ),
            record_store_factory=lambda: fixture.store,
            control_plane_root_path=fixture.root,
        )
        path = f"/v1/products/{fixture.profile.product}/releases/{accepted.record_id}/backup-continuation"
        headers = {"Authorization": "Bearer local-operator-token"}
        with patch(
            "control_plane.client_release_backup_continuation.guest_capture_metadata",
            return_value=(),
        ):
            planned = await request(
                app,
                "POST",
                path,
                headers=headers,
                payload={"reason": "Capture enrollment qualified"},
            )
            self.assertEqual(planned.status_code, 200, planned.text)
            self.assertFalse(planned.json()["apply_authorized"])
            denied = await request(
                app,
                "POST",
                path,
                headers=headers,
                payload={
                    "reason": "Capture enrollment qualified",
                    "apply": True,
                    "expected_digest": planned.json()["review_digest"],
                },
            )
        self.assertEqual(denied.status_code, 403, denied.text)
        self.assertEqual(denied.json()["error"]["code"], "authorization_denied")
        self.assertEqual(
            fixture.store.read_verireel_prod_backup_gate_operation_record(operation_id).status,
            "fail",
        )

    async def test_director_terminal_continuation_uses_the_original_acceptance(self) -> None:
        fixture = release_fixtures.GenericWebClientReleaseTests("runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        accepted, operation_id = fixture.stopped_precapture_backup()
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            bearer_identity_config=BearerIdentityConfig(
                terminal_agent_token="director-terminal-token",
                terminal_agent_subject="director-terminal",
                terminal_agent_token_label="director-terminal",
            ),
            authz_policy=LaunchplaneAuthzPolicy(),
            record_store_factory=lambda: fixture.store,
            control_plane_root_path=fixture.root,
        )
        path = f"/v1/products/{fixture.profile.product}/releases/{accepted.record_id}/backup-continuation"
        headers = {"Authorization": "Bearer director-terminal-token"}
        with patch(
            "control_plane.client_release_backup_continuation.guest_capture_metadata",
            return_value=(),
        ):
            planned = await request(
                app,
                "POST",
                path,
                headers=headers,
                payload={"reason": "Capture enrollment qualified"},
            )
            self.assertEqual(planned.status_code, 200, planned.text)
            self.assertTrue(planned.json()["apply_authorized"])
            applied = await request(
                app,
                "POST",
                path,
                headers=headers,
                payload={
                    "reason": "Capture enrollment qualified",
                    "apply": True,
                    "expected_digest": planned.json()["review_digest"],
                },
            )
        self.assertEqual(applied.status_code, 200, applied.text)
        current = fixture.store.read_verireel_prod_backup_gate_operation_record(operation_id)
        assert current.authorization is not None
        self.assertEqual(current.authorization.release_decision_record_id, accepted.record_id)
        self.assertEqual(current.status, "pending")

    async def test_legacy_capture_route_cannot_enqueue_an_operation(self) -> None:
        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(
                database_url=f"sqlite+pysqlite:///{Path(directory) / 'state.sqlite3'}"
            )
            store.ensure_schema()
            try:
                app = create_launchplane_fastapi_app(
                    verifier=_RejectingVerifier(),
                    authz_policy=LaunchplaneAuthzPolicy(),
                    record_store_factory=lambda: store,
                )
                response = await request(
                    app,
                    "POST",
                    "/v1/drivers/verireel/prod-backup-gate",
                    payload={
                        "product": "verireel",
                        "backup_gate": {"backup_record_id": "retired-capture"},
                    },
                )
                self.assertEqual(response.status_code, 404, response.text)
                self.assertEqual(store.list_verireel_prod_backup_gate_operation_records(), ())
            finally:
                store.close()

    async def test_capture_denial_missing_key_and_non_postgres_fail_before_enqueue(self) -> None:
        for actions, key, status, code in (
            (("production_backup_authority.read",), "key", 403, "authorization_denied"),
            (("production_backup_gate.execute",), "", 400, "missing_idempotency_key"),
            (("production_backup_gate.execute",), "key", 503, "database_storage_required"),
        ):
            with self.subTest(code=code), TemporaryDirectory() as directory:
                store = PostgresRecordStore(
                    database_url=f"sqlite+pysqlite:///{Path(directory) / 'state.sqlite3'}"
                )
                store.ensure_schema()
                try:
                    app = create_launchplane_fastapi_app(
                        verifier=_RejectingVerifier(),
                        bearer_identity_config=_local_operator_bearer_config(),
                        authz_policy=LaunchplaneAuthzPolicy(
                            schema_version=2,
                            local_operators=(
                                LocalOperatorPolicyRule(
                                    subjects=("local-owner-agent",),
                                    token_labels=("local-owner-read",),
                                    actions=actions,
                                    products=("example-product",),
                                    contexts=("example-product",),
                                    instances=("prod",),
                                ),
                            ),
                        ),
                        record_store_factory=lambda: store,
                    )
                    response = await request(
                        app,
                        "POST",
                        "/v1/production-backup-gates",
                        headers={
                            "Authorization": "Bearer local-operator-token",
                            "Idempotency-Key": key,
                        },
                        payload=_binding().request.model_dump(mode="json"),
                    )
                    self.assertEqual(response.status_code, status, response.text)
                    self.assertEqual(response.json()["error"]["code"], code)
                    self.assertEqual(store.list_verireel_prod_backup_gate_operation_records(), ())
                finally:
                    store.close()

    async def test_signed_in_session_reaches_the_gate_only_with_csrf(self) -> None:
        for drop_csrf, bearer, status, code in (
            (False, "", 503, "database_storage_required"),
            (True, "", 403, "browser_mutation_denied"),
            (False, "Bearer terminal-agent-token", 403, "authorization_denied"),
        ):
            with self.subTest(code=code), TemporaryDirectory() as directory:
                store = PostgresRecordStore(
                    database_url=f"sqlite+pysqlite:///{Path(directory) / 'state.sqlite3'}"
                )
                store.ensure_schema()
                session_manager = HumanSessionManager(
                    config=_github_oauth_config(), session_store=InMemoryHumanSessionStore()
                )
                human_session = session_manager.issue(_github_human_identity())
                try:
                    app = create_launchplane_fastapi_app(
                        verifier=_RejectingVerifier(),
                        bearer_identity_config=BearerIdentityConfig(
                            terminal_agent_token="terminal-agent-token",
                            terminal_agent_subject="terminal-agent",
                            terminal_agent_token_label="terminal-agent-read",
                        ),
                        authz_policy=LaunchplaneAuthzPolicy.model_validate(
                            {
                                "github_humans": [
                                    {
                                        "logins": ["example-operator"],
                                        "roles": ["admin"],
                                        "actions": ["production_backup_gate.execute"],
                                        "products": ["example-product"],
                                        "contexts": ["example-product"],
                                    }
                                ],
                                "terminal_agents": [
                                    {
                                        "subjects": ["terminal-agent"],
                                        "token_labels": ["terminal-agent-read"],
                                        "actions": ["production_backup_gate.execute"],
                                        "products": ["example-product"],
                                        "contexts": ["example-product"],
                                    }
                                ],
                            }
                        ),
                        record_store_factory=lambda: store,
                        human_session_manager=session_manager,
                    )
                    headers = _browser_mutation_headers(session_manager, human_session)
                    headers["Idempotency-Key"] = "ui-backup"
                    if drop_csrf:
                        headers.pop("X-CSRF-Token")
                    if bearer:
                        headers = {"Authorization": bearer, "Idempotency-Key": "ui-backup"}
                    response = await request(
                        app,
                        "POST",
                        "/v1/production-backup-gates",
                        headers=headers,
                        payload=_binding().request.model_dump(mode="json"),
                    )
                    self.assertEqual(response.status_code, status, response.text)
                    self.assertEqual(response.json()["error"]["code"], code)
                    self.assertEqual(store.list_verireel_prod_backup_gate_operation_records(), ())
                finally:
                    store.close()

    @scoped_agent_test
    async def test_read_denial_precedes_operation_lookup(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            bearer_identity_config=_local_operator_bearer_config(),
            authz_policy=LaunchplaneAuthzPolicy(),
            record_store_factory=object,
        )
        query = urlencode(
            {"product": "example-product", "context": "example-product", "instance": "prod"}
        )
        response = await get(
            app,
            f"/v1/production-backup-gates/operations/missing?{query}",
            headers={"Authorization": "Bearer local-operator-token"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_read_shows_an_older_driver_operation_without_its_provider_text(
        self,
    ) -> None:
        request_payload = VeriReelProdBackupGateRequest(
            backup_record_id="backup-gate-verireel-prod-run-1"
        )
        operation = VeriReelProdBackupGateOperationRecord(
            operation_id="verireel-operation-1",
            product="verireel",
            context="verireel",
            instance="prod",
            backup_record_id=request_payload.backup_record_id,
            request_fingerprint=request_payload.model_dump_json(),
            request=request_payload,
            status="fail",
            phase="failed",
            created_at="2026-07-23T03:30:00Z",
            updated_at="2026-07-23T03:31:00Z",
            finished_at="2026-07-23T03:31:00Z",
            error_message="ssh to pve.internal (10.4.5.6) refused",
            result=VeriReelProdBackupGateResult(
                backup_record_id=request_payload.backup_record_id,
                backup_status="fail",
                error_message="ssh to pve.internal (10.4.5.6) refused",
                evidence={
                    "snapshot_name": "launchplane-verireel-prod-1",
                    "error_message": "ssh to pve.internal (10.4.5.6) refused",
                },
            ),
        )
        policy = LaunchplaneAuthzPolicy(
            local_operators=(
                LocalOperatorPolicyRule(
                    subjects=("local-owner-agent",),
                    token_labels=("local-owner-read",),
                    products=("verireel",),
                    contexts=("verireel",),
                    actions=("production_backup_authority.read",),
                ),
            )
        )
        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(
                database_url=f"sqlite+pysqlite:///{Path(directory) / 'state.db'}"
            )
            store.ensure_schema()
            store.write_verireel_prod_backup_gate_operation_record(operation)
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                bearer_identity_config=_local_operator_bearer_config(),
                authz_policy=policy,
                record_store_factory=lambda: store,
            )
            query = urlencode({"product": "verireel", "context": "verireel", "instance": "prod"})
            response = await get(
                app,
                f"/v1/production-backup-gates/operations/verireel-operation-1?{query}",
                headers={"Authorization": "Bearer local-operator-token"},
            )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["operation_status"], "fail")
        self.assertEqual(payload["error_code"], "backup_failed")
        self.assertEqual(payload["error_description"], "The backup failed.")
        self.assertEqual(payload["evidence"], {"snapshot_name": "launchplane-verireel-prod-1"})
        self.assertNotIn("pve.internal", response.text)
        self.assertNotIn("10.4.5.6", response.text)


@unittest.skipUnless(
    os.environ.get("LAUNCHPLANE_TEST_POSTGRES_URL"), "Real PostgreSQL test URL is required"
)
class ProductionBackupGatePostgresHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_accepted_capture_replays_and_rejects_a_changed_request(self) -> None:
        with _store_for_fresh_head_database() as store:
            binding = _binding()
            store.write_production_backup_target_record(binding.source_target)
            store.write_production_backup_target_record(binding.destination_target)
            store.write_production_backup_policy_record(binding.policy)
            policy_payload = _authz_policy().model_dump(mode="json")
            policy_payload["github_actions"][0]["actions"] += ["production_backup_gate.execute"]
            policy = LaunchplaneAuthzPolicy.model_validate(policy_payload)
            digest = authz_policy_sha256(policy)
            store.seed_authz_policy_if_absent(
                LaunchplaneAuthzPolicyRecord(
                    record_id=build_authz_policy_record_id(revision=1, policy_sha256=digest),
                    source="test:shared-backup-http",
                    updated_at="2026-09-26T00:00:00Z",
                    policy_sha256=digest,
                    policy=policy,
                )
            )
            identity = _identity(
                repository="example/example-product",
                workflow_ref=_WORKFLOW_REF,
                job_workflow_ref=_JOB_WORKFLOW_REF,
                event_name="workflow_dispatch",
                repository_owner_id="1000",
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(identity),
                authz_policy=policy,
                record_store_factory=lambda: store,
            )
            headers = {"Authorization": "Bearer valid-token", "Idempotency-Key": "shared-capture"}
            payload = binding.request.model_dump(mode="json")
            accepted = await request(
                app, "POST", "/v1/production-backup-gates", headers=headers, payload=payload
            )
            self.assertEqual(accepted.status_code, 200, accepted.text)
            self.assertEqual(accepted.json()["operation_status"], "pending")
            replay = await request(
                app, "POST", "/v1/production-backup-gates", headers=headers, payload=payload
            )
            self.assertEqual(replay.status_code, 200, replay.text)
            self.assertEqual(replay.json()["operation_id"], accepted.json()["operation_id"])
            conflict = await request(
                app,
                "POST",
                "/v1/production-backup-gates",
                headers=headers,
                payload=payload | {"backup_record_id": "different-backup"},
            )
            self.assertEqual(conflict.status_code, 409, conflict.text)
            self.assertEqual(len(store.list_verireel_prod_backup_gate_operation_records()), 1)
            operation_id = accepted.json()["operation_id"]
            query = urlencode(
                {"product": "example-product", "context": "example-product", "instance": "prod"}
            )
            read = await get(
                app,
                f"/v1/production-backup-gates/operations/{operation_id}?{query}",
                headers=headers,
            )
            self.assertEqual(read.status_code, 200, read.text)
            self.assertNotIn("proxmox.example.invalid", read.text)
            self.assertNotIn("pbs-production", read.text)
            legacy_cancel = await request(
                app,
                "POST",
                f"/v1/drivers/verireel/prod-backup-gate/operations/{operation_id}/cancel",
                headers=headers,
                payload={"reason": "Wrong route"},
            )
            self.assertEqual(legacy_cancel.status_code, 404, legacy_cancel.text)
            cancelled = await request(
                app,
                "POST",
                f"/v1/production-backup-gates/operations/{operation_id}/cancel?{query}",
                headers=headers,
                payload={"reason": "Cancel pending test capture"},
            )
            self.assertEqual(cancelled.status_code, 200, cancelled.text)
            self.assertEqual(cancelled.json()["operation_status"], "cancelled")
            self.assertEqual(
                store.read_backup_gate_record(binding.request.backup_record_id).source,
                "launchplane-production-backup-gate-cancellation",
            )

            second = await request(
                app,
                "POST",
                "/v1/production-backup-gates",
                headers=headers | {"Idempotency-Key": "capture-2"},
                payload=payload | {"backup_record_id": "backup-2"},
            )
            self.assertEqual(second.status_code, 200, second.text)
            second_id = second.json()["operation_id"]
            failed = store.read_verireel_prod_backup_gate_operation_record(second_id).model_copy(
                update={
                    "status": "fail",
                    "phase": "failed",
                    "finished_at": "2026-09-26T12:00:00Z",
                    "error_code": "operation_authorization_revoked",
                    "error_message": "operation_authorization_revoked",
                    "result": VeriReelProdBackupGateResult(
                        backup_record_id="backup-2",
                        backup_status="fail",
                        error_message="operation_authorization_revoked",
                        evidence={"snapshot_name": "partial-snapshot"},
                    ),
                }
            )
            store.write_verireel_prod_backup_gate_operation_record(failed)
            read_failure = await get(
                app,
                f"/v1/production-backup-gates/operations/{second_id}?{query}",
                headers=headers,
            )
            self.assertEqual(read_failure.status_code, 200, read_failure.text)
            self.assertEqual(read_failure.json()["error_code"], "operation_authorization_revoked")
            self.assertEqual(
                read_failure.json()["error_description"],
                "The backup's authorization was removed or narrowed before it ran.",
            )
            self.assertEqual(read_failure.json()["evidence"], {"snapshot_name": "partial-snapshot"})
            cancel_failure = await request(
                app,
                "POST",
                f"/v1/production-backup-gates/operations/{second_id}/cancel?{query}",
                headers=headers,
                payload={"reason": "Cannot cancel terminal work"},
            )
            self.assertEqual(cancel_failure.status_code, 409, cancel_failure.text)

    async def test_signed_in_policy_administrator_enqueues_without_a_product_rule(self) -> None:
        with _store_for_fresh_head_database() as store:
            binding = _binding()
            store.write_production_backup_target_record(binding.source_target)
            store.write_production_backup_target_record(binding.destination_target)
            store.write_production_backup_policy_record(binding.policy)
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "schema_version": 2,
                    "github_humans": [
                        {
                            "github_ids": [123],
                            "roles": ["admin"],
                            "actions": ["authz_policy_grant.write"],
                            "products": ["launchplane"],
                            "contexts": ["launchplane"],
                        }
                    ],
                }
            )
            digest = authz_policy_sha256(policy)
            store.seed_authz_policy_if_absent(
                LaunchplaneAuthzPolicyRecord(
                    record_id=build_authz_policy_record_id(revision=1, policy_sha256=digest),
                    source="test:administrator-backup-http",
                    updated_at="2026-09-30T00:00:00Z",
                    policy_sha256=digest,
                    policy=policy,
                )
            )
            session_manager = HumanSessionManager(
                config=_github_oauth_config(), session_store=InMemoryHumanSessionStore()
            )
            human_session = session_manager.issue(_github_human_identity())
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=policy,
                record_store_factory=lambda: store,
                human_session_manager=session_manager,
            )
            headers = _browser_mutation_headers(session_manager, human_session)
            headers["Idempotency-Key"] = "administrator-backup"

            response = await request(
                app,
                "POST",
                "/v1/production-backup-gates",
                headers=headers,
                payload=binding.request.model_dump(mode="json"),
            )

            self.assertEqual(response.status_code, 200, response.text)
            operation = store.read_verireel_prod_backup_gate_operation_record(
                response.json()["operation_id"]
            )
            assert operation.authorization is not None
            self.assertEqual(operation.authorization.grant, "policy_administrator")
            self.assertEqual(operation.authorization.caller.github_id, 123)


if __name__ == "__main__":
    unittest.main()
