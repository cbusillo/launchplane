from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from control_plane.contracts.authz_policy_record import (
    LaunchplaneAuthzPolicyRecord,
    authz_policy_sha256,
    build_authz_policy_record_id,
)
from control_plane.contracts.authz_policy_write_transition import (
    AuthzPolicyGitHubActionsCallerBinding,
    AuthzPolicyImmutableHumanCallerBinding,
    AuthzPolicySchemaV3MaintenanceEvidence,
    AuthzPolicySchemaV3TransitionDeniedError,
)
from control_plane.service_auth import (
    GitHubActionsPolicyRule,
    GitHubHumanPolicyRule,
    LaunchplaneAuthzPolicy,
)
from control_plane.storage.postgres import PostgresRecordStore


def _policy() -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy(
        schema_version=3,
        administrator_quorum=2,
        github_humans=(
            GitHubHumanPolicyRule(
                github_ids=(101, 202),
                roles=("admin",),
                actions=("authz_policy_grant.write",),
                products=("launchplane",),
                contexts=("launchplane",),
            ),
        ),
    )


def _workflow_policy() -> LaunchplaneAuthzPolicy:
    return _policy().model_copy(
        update={
            "github_actions": (
                GitHubActionsPolicyRule(
                    managed_set_id="operator.launchplane",
                    managed_rule_id="authz-maintenance",
                    repository="cbusillo/launchplane",
                    repository_id="123",
                    repository_owner_id="456",
                    workflow_refs=(
                        "cbusillo/launchplane/.github/workflows/manage.yml@refs/heads/main",
                    ),
                    job_workflow_refs=(
                        "cbusillo/launchplane/.github/workflows/reusable-manage.yml@" + "a" * 40,
                    ),
                    products=("launchplane",),
                    contexts=("launchplane",),
                    actions=("authz_policy_grant.write",),
                ),
            )
        }
    )


def _record(policy: LaunchplaneAuthzPolicy, revision: int) -> LaunchplaneAuthzPolicyRecord:
    digest = authz_policy_sha256(policy)
    return LaunchplaneAuthzPolicyRecord(
        record_id=build_authz_policy_record_id(revision=revision, policy_sha256=digest),
        revision=revision,
        status="active",
        source="test:a5-store-fence",
        updated_at=f"2026-09-10T00:0{revision}:00Z",
        policy=policy,
    )


class AuthzPolicyWriteFenceStoreTests(unittest.TestCase):
    def test_schema_v3_exact_noop_needs_no_evidence(self) -> None:
        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(
                database_url=f"sqlite+pysqlite:///{Path(directory) / 'store.sqlite3'}"
            )
            store.ensure_schema()
            current = _record(_policy(), 1)
            store._write_row(store._authz_policy_row(current))

            result = store.compare_and_write_authz_policy_record(
                expected_record=current, replacement_record=None
            )

            self.assertEqual(result.status, "unchanged")
            self.assertEqual(result.current_record, current)
            store.close()

    def test_schema_v3_maintenance_requires_and_accepts_bound_evidence(self) -> None:
        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(
                database_url=f"sqlite+pysqlite:///{Path(directory) / 'store.sqlite3'}"
            )
            store.ensure_schema()
            current = _record(_policy(), 1)
            candidate = _record(_policy(), 2)
            store._write_row(store._authz_policy_row(current))

            with self.assertRaises(AuthzPolicySchemaV3TransitionDeniedError) as raised:
                store.compare_and_write_authz_policy_record(
                    expected_record=current, replacement_record=candidate
                )
            self.assertEqual(raised.exception.reason_code, "write_evidence_missing")
            self.assertEqual(store.list_authz_policy_records(status="active"), (current,))

            result = store.compare_and_write_authz_policy_record(
                expected_record=current,
                replacement_record=candidate,
                schema_v3_write_evidence=AuthzPolicySchemaV3MaintenanceEvidence(
                    caller=AuthzPolicyImmutableHumanCallerBinding(github_id=101),
                    expected_record_id=current.record_id,
                    expected_revision=current.revision,
                    expected_policy_sha256=current.policy_sha256,
                    candidate_policy_sha256=candidate.policy_sha256,
                ),
            )
            self.assertEqual(result.status, "written")
            self.assertEqual(result.current_record, candidate)
            store.close()

    def test_schema_v3_denial_rolls_back_new_mutation_reservation(self) -> None:
        from control_plane.storage.postgres import DbOnlyMutationRequest

        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(
                database_url=f"sqlite+pysqlite:///{Path(directory) / 'store.sqlite3'}"
            )
            store.ensure_schema()
            current = _record(_policy(), 1)
            candidate = _record(_policy(), 2)
            store._write_row(store._authz_policy_row(current))
            mutation = DbOnlyMutationRequest(
                scope="test:a5",
                route_path="/test/a5",
                idempotency_key="a5-denial",
                request_fingerprint="fingerprint",
                lease_owner="test-owner",
                response_status_code=200,
                response_trace_id="trace-a5",
                response_payload={"status": "ok"},
            )

            with self.assertRaises(AuthzPolicySchemaV3TransitionDeniedError):
                store.compare_and_write_authz_policy_record(
                    expected_record=current,
                    replacement_record=candidate,
                    mutation=mutation,
                )

            self.assertEqual(store.list_authz_policy_records(status="active"), (current,))
            self.assertIsNone(
                store.read_idempotency_record(
                    scope=mutation.scope,
                    route_path=mutation.route_path,
                    idempotency_key=mutation.idempotency_key,
                )
            )
            store.close()

    def test_schema_v3_maintenance_preserves_authorized_workflow_caller(self) -> None:
        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(
                database_url=f"sqlite+pysqlite:///{Path(directory) / 'store.sqlite3'}"
            )
            store.ensure_schema()
            current = _record(_workflow_policy(), 1)
            candidate = _record(_workflow_policy(), 2)
            store._write_row(store._authz_policy_row(current))
            caller = AuthzPolicyGitHubActionsCallerBinding(
                repository="cbusillo/launchplane",
                repository_owner="cbusillo",
                workflow_ref="cbusillo/launchplane/.github/workflows/manage.yml@refs/heads/main",
                job_workflow_ref=(
                    "cbusillo/launchplane/.github/workflows/reusable-manage.yml@" + "a" * 40
                ),
                ref="refs/heads/main",
                ref_type="branch",
                event_name="workflow_dispatch",
                environment="",
                subject="repo:cbusillo/launchplane:ref:refs/heads/main",
                sha="b" * 40,
                raw_claims={},
                repository_id="123",
                repository_owner_id="456",
            )

            result = store.compare_and_write_authz_policy_record(
                expected_record=current,
                replacement_record=candidate,
                schema_v3_write_evidence=AuthzPolicySchemaV3MaintenanceEvidence(
                    caller=caller,
                    expected_record_id=current.record_id,
                    expected_revision=current.revision,
                    expected_policy_sha256=current.policy_sha256,
                    candidate_policy_sha256=candidate.policy_sha256,
                ),
            )

            self.assertEqual(result.status, "written")
            self.assertEqual(result.current_record, candidate)
            store.close()


if __name__ == "__main__":
    unittest.main()
