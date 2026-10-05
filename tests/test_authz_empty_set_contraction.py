from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from control_plane.authz_grant_service import (
    AuthzManagedPolicyReconcileEnvelope,
    AuthzPolicyConflictError,
    AuthzPolicySafetyError,
    AuthzPolicySchemaConflictError,
    execute_managed_authz_policy_reconcile,
    plan_managed_authz_policy_reconcile,
)
from control_plane.contracts.authz_policy_write_transition import (
    AuthzPolicyGitHubActionsCallerBinding,
    AuthzPolicySchemaV3MaintenanceEvidence,
)
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.http_routes.mutation_support import idempotency_scope
from control_plane.service_auth import LaunchplaneAuthzPolicy
from control_plane.storage.postgres import PostgresRecordStore
from tests.support.auth import _StubVerifier
from tests.support.http import lifespan_client
from tests.support.stores import _sqlite_database_url
from tests.test_authz_grant_service import _AuthzPolicyStore, _active_record_for_policy, _identity
from tests.test_authz_policy_write_transition import _ordinary_rule


def _policy() -> LaunchplaneAuthzPolicy:
    identity = _identity()
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "schema_version": 3,
            "administrator_quorum": 1,
            "github_actions": [
                {
                    "managed_set_id": "test.policy-writers",
                    "managed_rule_id": "worker",
                    "repository": identity.repository,
                    "repository_id": identity.repository_id,
                    "repository_owner_id": identity.repository_owner_id,
                    "workflow_refs": [identity.workflow_ref],
                    "products": ["launchplane"],
                    "contexts": ["launchplane"],
                    "actions": ["authz_policy_grant.write"],
                }
            ],
            "github_humans": [
                {
                    "managed_set_id": "test.policy-administrators",
                    "managed_rule_id": "human",
                    "github_ids": [101],
                    "roles": ["admin"],
                    "products": ["launchplane"],
                    "contexts": ["launchplane"],
                    "actions": ["authz_policy_grant.write"],
                },
                {
                    "managed_set_id": "operator.manager-preview-approval",
                    "managed_rule_id": "manager",
                    "github_ids": [102],
                    "roles": ["admin"],
                    "actions": ["manager_preview_approval.write"],
                },
            ],
            "terminal_agents": [{"subjects": ["agent"], "actions": ["product_profile.read"]}],
            "local_operators": [{"subjects": ["reader"], "actions": ["product_profile.read"]}],
            "local_admins": [{"subjects": ["admin"], "actions": ["product_profile.read"]}],
            "ordinary_agents": [_ordinary_rule()],
        }
    )


def _request(**overrides: object) -> AuthzManagedPolicyReconcileEnvelope:
    return AuthzManagedPolicyReconcileEnvelope.model_validate(
        {
            "product": "launchplane",
            "managed_set_id": "operator.manager-preview-approval",
            "desired_policy": {"schema_version": 2},
            "reason": "Retire only the approved set.",
            "related_issue": "example/launchplane#1",
            **overrides,
        }
    )


class EmptySetContractionTests(unittest.TestCase):
    def test_reviewed_apply_preserves_schema_quorum_and_all_unrelated_collections(self) -> None:
        policy = _policy()
        active = _active_record_for_policy(policy)
        store = _AuthzPolicyStore((active,))
        dry_run = execute_managed_authz_policy_reconcile(
            record_store=store,
            request=_request(),
            identity=_identity(),
            trace_id="test-dry-run",
            now_timestamp=lambda: active.updated_at,
            authorized_policy_sha256=active.policy_sha256,
        )
        self.assertEqual(store.records, (active,))
        diff = dry_run.driver_result["diff"]
        assert isinstance(diff, dict)
        self.assertFalse(diff["schema_migrated"])
        self.assertFalse(diff["administrator_quorum_changed"])
        self.assertFalse(diff["policy_safety_blockers"])
        self.assertEqual({change["change"] for change in diff["changes"]}, {"removed"})
        apply = execute_managed_authz_policy_reconcile(
            record_store=store,
            request=_request(mode="apply", reviewed_plan_sha256=diff["plan_sha256"]),
            identity=_identity(),
            trace_id="test-apply",
            now_timestamp=lambda: active.updated_at,
            authorized_policy_sha256=active.policy_sha256,
        )
        expected = policy.model_copy(update={"github_humans": policy.github_humans[:1]})
        self.assertEqual(apply.updated_policy, expected)
        evidence = apply.schema_v3_write_evidence
        self.assertIsInstance(evidence, AuthzPolicySchemaV3MaintenanceEvidence)
        assert isinstance(evidence, AuthzPolicySchemaV3MaintenanceEvidence)
        self.assertIsInstance(evidence.caller, AuthzPolicyGitHubActionsCallerBinding)
        self.assertEqual(evidence.expected_record_id, active.record_id)
        self.assertEqual(evidence.expected_policy_sha256, active.policy_sha256)
        self.assertEqual(evidence.candidate_policy_sha256, apply.authz_policy_record.policy_sha256)

    def test_stale_review_and_authorization_still_refuse(self) -> None:
        active = _active_record_for_policy(_policy())
        _, _, _, diff = plan_managed_authz_policy_reconcile(
            record_store=_AuthzPolicyStore((active,)),
            request=_request(),
        )
        for changed in (
            active.model_copy(update={"revision": active.revision + 1}),
            _active_record_for_policy(active.policy.model_copy(update={"administrator_quorum": 2})),
        ):
            with self.subTest(record=changed.record_id, revision=changed.revision):
                with self.assertRaisesRegex(AuthzPolicyConflictError, "reviewed_plan_sha256"):
                    plan_managed_authz_policy_reconcile(
                        record_store=_AuthzPolicyStore((changed,)),
                        request=_request(mode="apply", reviewed_plan_sha256=diff.plan_sha256),
                    )
        changed = _active_record_for_policy(
            active.policy.model_copy(update={"administrator_quorum": 2})
        )
        with self.assertRaisesRegex(AuthzPolicyConflictError, "caller was authorized"):
            execute_managed_authz_policy_reconcile(
                record_store=_AuthzPolicyStore((changed,)),
                request=_request(),
                identity=_identity(),
                trace_id="stale-auth",
                now_timestamp=lambda: active.updated_at,
                authorized_policy_sha256=active.policy_sha256,
            )

    def test_mismatch_exception_does_not_allow_other_changes(self) -> None:
        policy = _policy()
        for request in (
            _request(
                desired_policy={
                    "schema_version": 2,
                    "github_humans": [policy.github_humans[1].model_dump(mode="json")],
                }
            ),
            _request(unmanaged_adoption="adopt_matching"),
            _request(administrator_quorum_change=2),
            _request(schema_migration="migrate_v1_to_v2"),
        ):
            with (
                self.subTest(request=request.model_dump()),
                self.assertRaisesRegex(AuthzPolicySchemaConflictError, "downgrade"),
            ):
                plan_managed_authz_policy_reconcile(
                    record_store=_AuthzPolicyStore((_active_record_for_policy(policy),)),
                    request=request,
                )

    def test_empty_fragment_still_cannot_remove_an_administrator(self) -> None:
        active = _active_record_for_policy(_policy())
        request = _request(managed_set_id="test.policy-administrators")
        _, _, _, diff = plan_managed_authz_policy_reconcile(
            record_store=_AuthzPolicyStore((active,)),
            request=request,
        )
        with self.assertRaises(AuthzPolicySafetyError):
            execute_managed_authz_policy_reconcile(
                record_store=_AuthzPolicyStore((active,)),
                request=request.model_copy(
                    update={"mode": "apply", "reviewed_plan_sha256": diff.plan_sha256}
                ),
                identity=_identity(),
                trace_id="unsafe-apply",
                now_timestamp=lambda: active.updated_at,
            )


class EmptySetContractionHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_wrong_reviewed_digest_on_stable_policy_is_not_reported_as_drift(self) -> None:
        policy = _policy()
        active = _active_record_for_policy(policy)
        with (
            TemporaryDirectory() as directory,
            closing(
                PostgresRecordStore(
                    database_url=_sqlite_database_url(Path(directory) / "state.sqlite")
                )
            ) as store,
        ):
            store.ensure_schema()
            with patch.object(store, "list_authz_policy_records", return_value=(active,)):
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=policy,
                    record_store_factory=lambda: store,
                )
                request = _request(
                    mode="apply",
                    desired_policy={"schema_version": policy.schema_version},
                    reviewed_plan_sha256="0" * 64,
                )
                async with lifespan_client(app) as client:
                    response = await client.post(
                        "/v1/authz-policies/managed-rule-sets/reconcile",
                        headers={
                            "Authorization": "Bearer valid-token",
                            "Idempotency-Key": "mistyped-reviewed-digest",
                        },
                        json=request.model_dump(mode="json"),
                    )
                self.assertEqual(response.status_code, 409, response.text)
                error = response.json()["error"]
                self.assertEqual(error["code"], "authz_policy_reviewed_plan_conflict")
                self.assertIn("Reviewed plan digest", error["message"])
                self.assertNotIn("changed", error["message"])
                self.assertNotIn("retry", error["message"])
                self.assertNotIn(active.policy_sha256, response.text)
                self.assertNotIn(request.managed_set_id, response.text)
                self.assertNotIn(_identity().workflow_ref, response.text)
                self.assertIsNone(
                    store.read_idempotency_record(
                        scope=idempotency_scope(_identity()),
                        route_path="/v1/authz-policies/managed-rule-sets/reconcile",
                        idempotency_key="mistyped-reviewed-digest",
                    )
                )

    async def test_policy_drift_after_authorization_keeps_drift_diagnosis(self) -> None:
        policy = _policy()
        changed = _active_record_for_policy(policy.model_copy(update={"administrator_quorum": 2}))
        with (
            TemporaryDirectory() as directory,
            closing(
                PostgresRecordStore(
                    database_url=_sqlite_database_url(Path(directory) / "state.sqlite")
                )
            ) as store,
        ):
            store.ensure_schema()
            with patch.object(store, "list_authz_policy_records", return_value=(changed,)):
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=policy,
                    record_store_factory=lambda: store,
                )
                async with lifespan_client(app) as client:
                    response = await client.post(
                        "/v1/authz-policies/managed-rule-sets/reconcile",
                        headers={"Authorization": "Bearer valid-token"},
                        json=_request().model_dump(mode="json"),
                    )
                self.assertEqual(response.status_code, 409, response.text)
                error = response.json()["error"]
                self.assertEqual(error["code"], "authz_policy_conflict")
                self.assertIn("changed", error["message"])
                self.assertNotIn(changed.policy_sha256, response.text)

    async def test_schema_mismatch_reports_incompatibility_without_retry_advice(self) -> None:
        policy = _policy()
        active = _active_record_for_policy(policy)
        # Model a pre-existing v3 DB record; do not create a raw schema-v3 seed.
        with (
            TemporaryDirectory() as directory,
            closing(
                PostgresRecordStore(
                    database_url=_sqlite_database_url(Path(directory) / "state.sqlite")
                )
            ) as store,
        ):
            store.ensure_schema()
            with patch.object(store, "list_authz_policy_records", return_value=(active,)):
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=policy,
                    record_store_factory=lambda: store,
                )
                request = _request(
                    desired_policy={
                        "schema_version": 2,
                        "github_humans": [policy.github_humans[1].model_dump(mode="json")],
                    }
                )
                async with lifespan_client(app) as client:
                    response = await client.post(
                        "/v1/authz-policies/managed-rule-sets/reconcile",
                        headers={"Authorization": "Bearer valid-token"},
                        content=json.dumps(request.model_dump(mode="json")),
                    )
                self.assertEqual(response.status_code, 409)
                error = response.json()["error"]
                self.assertEqual(error["code"], "authz_policy_schema_conflict")
                self.assertIn("downgrade", error["message"])
                self.assertNotIn("retry", error["message"])
