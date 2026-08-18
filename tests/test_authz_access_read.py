from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from pydantic import ValidationError

from control_plane.contracts.authz_access_read import (
    AUTHZ_DENIAL_EXPLANATION_READ_ACTION,
    EFFECTIVE_ACCESS_READ_ACTION,
    EffectiveAccessEvaluateRequest,
)
from control_plane.contracts.authz_denial_record import build_authz_denial_record
from control_plane.contracts.authz_policy_record import (
    LaunchplaneAuthzPolicyRecord,
    authz_policy_sha256,
    build_authz_policy_record_id,
)
from control_plane.http_app import LaunchplaneAuthzPolicyRuntime, create_launchplane_fastapi_app
from control_plane.service_auth import (
    AuthzEvaluation,
    BearerIdentityConfig,
    GitHubActionsIdentity,
    LaunchplaneAuthzPolicy,
)
from control_plane.storage.postgres import PostgresRecordStore
from tests.support.http import lifespan_client


class _RejectingVerifier:
    def verify(self, token: str) -> GitHubActionsIdentity:
        raise ValueError(f"Unexpected GitHub token: {token}")


def _database_url(path: Path) -> str:
    return f"sqlite+pysqlite:///{path}"


def _seed_policy(
    store: PostgresRecordStore,
    policy: LaunchplaneAuthzPolicy,
) -> LaunchplaneAuthzPolicyRecord:
    digest = authz_policy_sha256(policy)
    return store.seed_authz_policy_if_absent(
        LaunchplaneAuthzPolicyRecord(
            record_id=build_authz_policy_record_id(revision=1, policy_sha256=digest),
            revision=1,
            status="active",
            source="test:authz-access-read",
            updated_at="2026-08-18T12:00:00+00:00",
            policy_sha256=digest,
            policy=policy,
        )
    )


def _support_reader_policy() -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "schema_version": 2,
            "local_operators": [
                {
                    "subjects": ["support-reader"],
                    "token_labels": ["support-reader-label"],
                    "products": ["launchplane"],
                    "contexts": ["launchplane"],
                    "actions": [AUTHZ_DENIAL_EXPLANATION_READ_ACTION],
                }
            ],
        }
    )


def _app(
    *,
    root: Path,
    store: PostgresRecordStore,
    record: LaunchplaneAuthzPolicyRecord,
) -> FastAPI:
    return create_launchplane_fastapi_app(
        verifier=_RejectingVerifier(),
        authz_policy=record.policy,
        authz_policy_runtime=LaunchplaneAuthzPolicyRuntime(
            record.policy,
            policy_sha256=record.policy_sha256,
            source="db",
            record_id=record.record_id,
            revision=record.revision,
        ),
        record_store_factory=lambda: store,
        bearer_identity_config=BearerIdentityConfig(
            local_admin_token="admin-token",
            local_admin_subject="authz-admin",
            local_admin_token_label="authz-admin-label",
            local_operator_token="support-token",
            local_operator_subject="support-reader",
            local_operator_token_label="support-reader-label",
        ),
        control_plane_root_path=root,
        state_dir=root / "state",
    )


class AuthzAccessReadHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_administrator_evaluates_one_explicit_principal_without_selector_leakage(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = PostgresRecordStore(database_url=_database_url(root / "launchplane.sqlite3"))
            store.ensure_schema()
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "schema_version": 2,
                    "local_admins": [
                        {
                            "subjects": ["authz-admin"],
                            "token_labels": ["authz-admin-label"],
                            "products": ["launchplane"],
                            "contexts": ["launchplane"],
                            "actions": [EFFECTIVE_ACCESS_READ_ACTION],
                        }
                    ],
                    "local_operators": [
                        {
                            "subjects": ["evaluated-principal-secret"],
                            "token_labels": ["evaluated-token-label-secret"],
                            "products": ["example-product"],
                            "contexts": ["example-context"],
                            "actions": ["example_access.read"],
                        }
                    ],
                }
            )
            record = _seed_policy(store, policy)
            app = _app(root=root, store=store, record=record)
            paths = app.openapi()["paths"]
            self.assertIn("/v1/authz-diagnostics/effective-access/evaluate", paths)
            self.assertIn("/v1/authz-diagnostics/denials/{trace_id}", paths)
            request_payload = {
                "principal": {
                    "principal_type": "local_operator",
                    "subject": "evaluated-principal-secret",
                    "token_label": "evaluated-token-label-secret",
                },
                "action": "example_access.read",
                "product": "example-product",
                "context": "example-context",
                "target_scope": "context",
            }
            try:
                async with lifespan_client(app) as client:
                    allowed_response = await client.post(
                        "/v1/authz-diagnostics/effective-access/evaluate",
                        headers={"Authorization": "Bearer admin-token"},
                        json=request_payload,
                    )
                    denied_response = await client.post(
                        "/v1/authz-diagnostics/effective-access/evaluate",
                        headers={"Authorization": "Bearer admin-token"},
                        json={**request_payload, "action": "example_access.write"},
                    )
                    instance_response = await client.post(
                        "/v1/authz-diagnostics/effective-access/evaluate",
                        headers={"Authorization": "Bearer admin-token"},
                        json={
                            **request_payload,
                            "target_scope": "instance",
                            "instance": "prod",
                        },
                    )
                    active_records_after_reads = store.list_authz_policy_records(
                        status="active",
                        limit=2,
                    )
            finally:
                store.close()

        self.assertEqual(allowed_response.status_code, 200, allowed_response.text)
        self.assertEqual(allowed_response.json()["evaluation"]["decision"], "allowed")
        self.assertEqual(denied_response.status_code, 200, denied_response.text)
        self.assertEqual(denied_response.json()["evaluation"]["decision"], "denied")
        self.assertEqual(
            denied_response.json()["evaluation"]["reason_code"],
            "no_matching_grant",
        )
        self.assertEqual(instance_response.status_code, 200, instance_response.text)
        self.assertEqual(instance_response.json()["request"]["target_scope"], "instance")
        self.assertEqual(instance_response.json()["evaluation"]["decision"], "denied")
        self.assertEqual(active_records_after_reads, (record,))
        rendered = json.dumps(
            {"allowed": allowed_response.json(), "denied": denied_response.json()},
            sort_keys=True,
        )
        self.assertNotIn("evaluated-principal-secret", rendered)
        self.assertNotIn("evaluated-token-label-secret", rendered)

    async def test_support_reader_explains_persisted_denial_without_policy_write_authority(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = PostgresRecordStore(database_url=_database_url(root / "launchplane.sqlite3"))
            store.ensure_schema()
            policy = _support_reader_policy()
            record = _seed_policy(store, policy)
            app = _app(root=root, store=store, record=record)
            try:
                async with lifespan_client(app) as client:
                    denied_response = await client.get(
                        "/v1/service/runtime",
                        headers={"Authorization": "Bearer support-token"},
                    )
                    denied_trace_id = denied_response.json()["trace_id"]
                    explanation_response = await client.get(
                        f"/v1/authz-diagnostics/denials/{denied_trace_id}",
                        headers={"Authorization": "Bearer support-token"},
                    )
                    evaluate_response = await client.post(
                        "/v1/authz-diagnostics/effective-access/evaluate",
                        headers={"Authorization": "Bearer support-token"},
                        json={
                            "principal": {
                                "principal_type": "local_operator",
                                "subject": "support-reader",
                                "token_label": "support-reader-label",
                            },
                            "action": "launchplane_service.read",
                            "product": "launchplane",
                            "context": "launchplane",
                            "target_scope": "context",
                        },
                    )
                    stored_record = store.read_authz_denial_record(
                        trace_id=denied_trace_id,
                        observed_at="2026-08-18T12:01:00+00:00",
                    )
            finally:
                store.close()

        self.assertEqual(denied_response.status_code, 403, denied_response.text)
        self.assertEqual(explanation_response.status_code, 200, explanation_response.text)
        self.assertEqual(explanation_response.json()["trace_id"], denied_trace_id)
        self.assertEqual(
            explanation_response.json()["reason_code"],
            "no_matching_grant",
        )
        self.assertEqual(evaluate_response.status_code, 403, evaluate_response.text)
        self.assertIsNotNone(stored_record)
        rendered = json.dumps(explanation_response.json(), sort_keys=True)
        self.assertNotIn("support-reader", rendered)
        self.assertNotIn("support-reader-label", rendered)
        self.assertNotIn("authz_policy_grant.write", rendered)

    async def test_unknown_denial_trace_is_not_distinguishable_from_expired_evidence(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = PostgresRecordStore(database_url=_database_url(root / "launchplane.sqlite3"))
            store.ensure_schema()
            policy = _support_reader_policy()
            record = _seed_policy(store, policy)
            store.write_authz_denial_record(
                build_authz_denial_record(
                    trace_id="expired-trace",
                    recorded_at="2026-07-01T00:00:00+00:00",
                    expires_at="2026-07-31T00:00:00+00:00",
                    route_path="/v1/service/runtime",
                    evaluation=AuthzEvaluation(
                        decision="denied",
                        reason_code="no_matching_grant",
                        principal_type="local_operator",
                        action="launchplane_service.read",
                        product="launchplane",
                        context="launchplane",
                        target_scope="context",
                        instance_specified=False,
                    ),
                    policy_record_id=record.record_id,
                    policy_revision=record.revision,
                    policy_sha256=record.policy_sha256,
                )
            )
            app = _app(root=root, store=store, record=record)
            try:
                async with lifespan_client(app) as client:
                    unknown_response = await client.get(
                        "/v1/authz-diagnostics/denials/unknown-trace",
                        headers={"Authorization": "Bearer support-token"},
                    )
                    expired_response = await client.get(
                        "/v1/authz-diagnostics/denials/expired-trace",
                        headers={"Authorization": "Bearer support-token"},
                    )
            finally:
                store.close()

        self.assertEqual(unknown_response.status_code, 404, unknown_response.text)
        self.assertEqual(expired_response.status_code, 404, expired_response.text)
        self.assertEqual(unknown_response.json()["error"], expired_response.json()["error"])

    async def test_unauthorized_denial_explanation_skips_route_database_reads(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = PostgresRecordStore(database_url=_database_url(root / "launchplane.sqlite3"))
            store.ensure_schema()
            record = _seed_policy(store, _support_reader_policy())
            app = _app(root=root, store=store, record=record)
            try:
                async with lifespan_client(app) as client:
                    with (
                        patch.object(
                            store,
                            "list_authz_policy_records",
                            side_effect=AssertionError("authorization must precede policy reads"),
                        ),
                        patch.object(
                            store,
                            "read_authz_denial_record",
                            side_effect=AssertionError("authorization must precede denial reads"),
                        ),
                    ):
                        response = await client.get(
                            "/v1/authz-diagnostics/denials/unknown-trace",
                            headers={"Authorization": "Bearer admin-token"},
                        )
            finally:
                store.close()

        self.assertEqual(response.status_code, 403, response.text)


class AuthzDenialRecordStoreTests(unittest.TestCase):
    def test_effective_access_target_scope_requires_one_exact_instance(self) -> None:
        base_request = {
            "principal": {
                "principal_type": "local_operator",
                "subject": "operator",
                "token_label": "operator-label",
            },
            "action": "example_access.read",
            "product": "example-product",
            "context": "example-context",
        }

        with self.assertRaises(ValidationError):
            EffectiveAccessEvaluateRequest.model_validate(
                {**base_request, "target_scope": "instance"}
            )
        with self.assertRaises(ValidationError):
            EffectiveAccessEvaluateRequest.model_validate(
                {**base_request, "target_scope": "context", "instance": "prod"}
            )
        with self.assertRaises(ValidationError):
            EffectiveAccessEvaluateRequest.model_validate(
                {**base_request, "target_scope": "instance", "instance": "prod*"}
            )

    def test_denial_records_are_immutable_by_trace_id(self) -> None:
        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(
                database_url=_database_url(Path(directory) / "launchplane.sqlite3")
            )
            store.ensure_schema()
            evaluation = AuthzEvaluation(
                decision="denied",
                reason_code="no_matching_grant",
                principal_type="local_operator",
                action="example_access.read",
                product="example-product",
                context="example-context",
                target_scope="context",
                instance_specified=False,
            )
            first_record = build_authz_denial_record(
                trace_id="trace-immutable",
                recorded_at="2026-08-18T12:00:00+00:00",
                expires_at="2026-09-17T12:00:00+00:00",
                route_path="/v1/deployments",
                evaluation=evaluation,
                policy_record_id="authz-policy-1",
                policy_revision=1,
                policy_sha256="a" * 64,
            )
            store.write_authz_denial_record(first_record)
            with self.assertRaisesRegex(ValueError, "different evidence"):
                store.write_authz_denial_record(
                    first_record.model_copy(update={"route_path": "/v1/promotions"})
                )
            stored_record = store.read_authz_denial_record(
                trace_id="trace-immutable",
                observed_at="2026-08-18T12:01:00+00:00",
            )
            store.close()

        self.assertEqual(stored_record, first_record)
