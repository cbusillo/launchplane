from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
import unittest
from unittest.mock import patch

from control_plane.authz_candidate_preparation import (
    ORDINARY_AGENT_DELIVERY_ADMINISTRATION_ACTIONS,
    ORDINARY_AGENT_DELIVERY_ADMINISTRATION_MANAGED_RULE_ID,
    ORDINARY_AGENT_DELIVERY_ADMINISTRATION_MANAGED_SET_ID,
)
from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
from control_plane.contracts.privileged_operation import (
    ManagedAuthzPolicySetExecutionEvidence,
    ManagedAuthzPolicySetHumanEvidence,
    ManagedAuthzPolicySetProposalInput,
    PrivilegedOperationActor,
    PrivilegedOperationApproval,
    PrivilegedOperationRecord,
    privileged_operation_pre_state_digest,
)
from control_plane.privileged_operation_service import (
    PrivilegedOperationNotApprovableError,
    approve_privileged_operation,
    create_typed_privileged_operation_plan,
    privileged_operation_semantic_review,
)
from control_plane.privileged_operation_worker import execute_approved_privileged_operations_once
from control_plane.service_auth import GitHubHumanPolicyRule, LaunchplaneAuthzPolicy
from control_plane.storage.postgres import PostgresRecordStore
from tests.support.stores import _sqlite_database_url
from tests.test_ordinary_agent_activation_storage import _event, _record, _scope
from tests.test_privileged_operation_worker import _policy_admin_record


class _ClockStore(PostgresRecordStore):
    clock = datetime.now(timezone.utc).replace(microsecond=0)

    def _database_mutation_timestamp(self, session: Any) -> str:
        return self.clock.isoformat()


def _observed_at(store: PostgresRecordStore) -> datetime:
    with store._session_factory() as session:
        return datetime.fromisoformat(store._database_mutation_timestamp(session))


def _seed_policy(
    store: PostgresRecordStore, *, baseline: LaunchplaneAuthzPolicy | None = None
) -> LaunchplaneAuthzPolicyRecord:
    baseline = baseline or _policy_admin_record(administrator_quorum=1).policy
    administration = GitHubHumanPolicyRule(
        managed_set_id=ORDINARY_AGENT_DELIVERY_ADMINISTRATION_MANAGED_SET_ID,
        managed_rule_id=ORDINARY_AGENT_DELIVERY_ADMINISTRATION_MANAGED_RULE_ID,
        github_ids=(123,),
        roles=("admin",),
        products=("launchplane",),
        contexts=("launchplane",),
        actions=ORDINARY_AGENT_DELIVERY_ADMINISTRATION_ACTIONS,
    )
    policy = LaunchplaneAuthzPolicyRecord(
        record_id="test-retirement-current-policy",
        revision=397,
        source="test:retirement",
        updated_at=_observed_at(store).isoformat(),
        policy=baseline.model_copy(
            update={
                "schema_version": 3,
                "github_humans": (*baseline.github_humans, administration),
            }
        ),
    )
    # Simulate an already-installed schema-3 policy without bypassing its apply fence.
    with store._session_factory() as session:
        session.add(store._authz_policy_row(policy))
        session.commit()
    return policy


def _history(
    store: PostgresRecordStore, *, expires_at: datetime, repository_id: int = 1001
) -> None:
    activation = _record(
        operation_id=f"historical-setup-{repository_id}",
        installed_at=(_observed_at(store) - timedelta(days=2)).isoformat(),
        expires_at=expires_at.isoformat(),
        scope=_scope(repository_id=repository_id),
    )
    event = _event(
        activation, action="installed", source_operation_id=activation.source_setup_operation_id
    )
    # Historical records are fixtures; the retirement itself uses the real locked writer.
    with store._session_factory() as session:
        session.add(store._ordinary_agent_delivery_activation_row(activation))
        session.add(store._ordinary_agent_delivery_activation_event_row(event))
        session.commit()


def _plan(
    store: PostgresRecordStore, *, source_event_id: str = "retire-administration"
) -> PrivilegedOperationRecord:
    return create_typed_privileged_operation_plan(
        record_store=store,
        descriptor_id="managed-authz-policy-set",
        actor=PrivilegedOperationActor(
            identity_type="github_human", github_id=123, login="test-administrator"
        ),
        source_kind="browser_api",
        source_event_id=source_event_id,
        request=ManagedAuthzPolicySetProposalInput(
            managed_set_id=ORDINARY_AGENT_DELIVERY_ADMINISTRATION_MANAGED_SET_ID,
            desired_policy=LaunchplaneAuthzPolicy(schema_version=3),
            reason="Retire only the approved delivery administration rule.",
        ),
        now=lambda: _observed_at(store),
    ).record


def _approve(
    store: PostgresRecordStore,
    plan: PrivilegedOperationRecord,
    policy: LaunchplaneAuthzPolicyRecord,
) -> None:
    assert isinstance(plan.requested_by, PrivilegedOperationActor)
    approve_privileged_operation(
        record_store=store,
        operation_id=plan.operation_id,
        approval=PrivilegedOperationApproval(
            approver=plan.requested_by,
            descriptor_id=plan.descriptor_id,
            descriptor_version=plan.descriptor_version,
            request_digest=plan.request_digest,
            evidence_digest=plan.evidence_digest,
            plan_digest=plan.evidence.plan_digest,
            pre_state_digest=privileged_operation_pre_state_digest(plan.evidence),
            policy_record_id=policy.record_id,
            policy_revision=policy.revision,
            policy_sha256=policy.policy_sha256,
            policy_source=policy.source,
            managed_set_id="privileged-operations.policy-execution",
            managed_rule_id="human-policy-approver",
            expires_at=plan.expires_at,
            reason="Reviewed the exact one-rule retirement.",
            rollback_class="policy_cas",
        ),
        source_event_id=f"approve-{plan.operation_id}",
        now=lambda: _observed_at(store),
    )


class DeliveryAdministrationRetirementTests(unittest.TestCase):
    def test_both_approved_removals_execute_with_only_expired_history(self) -> None:
        for attempt in ("original-removal", "replacement-removal"):
            with (
                self.subTest(attempt=attempt),
                TemporaryDirectory() as directory,
                closing(
                    _ClockStore(
                        database_url=_sqlite_database_url(Path(directory) / "state.sqlite3")
                    )
                ) as store,
            ):
                store.ensure_schema()
                policy = _seed_policy(store)
                _history(store, expires_at=store.clock - timedelta(days=1))
                history = store.list_ordinary_agent_delivery_activation_records()
                events = store.list_ordinary_agent_delivery_activation_event_records()
                plan = _plan(store, source_event_id=attempt)
                self.assertEqual(plan.evidence.result_status, "ok")
                _approve(store, plan, policy)
                execute_approved_privileged_operations_once(
                    record_store=store, now=lambda: store.clock
                )
                result = store.read_privileged_operation_record(plan.operation_id)
                self.assertEqual(result.status, "executed")
                self.assertIsInstance(result.execution, ManagedAuthzPolicySetExecutionEvidence)
                assert isinstance(result.execution, ManagedAuthzPolicySetExecutionEvidence)
                self.assertTrue(result.execution.changed)
                self.assertFalse(result.execution.reconciliation_required)
                current = store.list_authz_policy_records(status="active")[0]
                self.assertEqual(current.revision, policy.revision + 1)
                self.assertEqual(
                    current.policy,
                    policy.policy.model_copy(
                        update={"github_humans": policy.policy.github_humans[:-1]}
                    ),
                )
                self.assertEqual(store.list_ordinary_agent_delivery_activation_records(), history)
                self.assertEqual(
                    store.list_ordinary_agent_delivery_activation_event_records(), events
                )
                self.assertEqual(
                    execute_approved_privileged_operations_once(
                        record_store=store, now=lambda: store.clock
                    ),
                    (),
                )
                self.assertEqual(store.list_authz_policy_records(status="active"), (current,))

    def test_native_plan_blocks_unexpired_activation_but_not_exact_expiry(self) -> None:
        for remaining in (timedelta(seconds=1), timedelta(0)):
            with (
                self.subTest(remaining=remaining),
                TemporaryDirectory() as directory,
                closing(
                    _ClockStore(
                        database_url=_sqlite_database_url(Path(directory) / "state.sqlite3")
                    )
                ) as store,
            ):
                store.ensure_schema()
                policy = _seed_policy(store)
                _history(store, expires_at=store.clock + remaining)
                plan = _plan(store)
                if remaining:
                    assert isinstance(plan.evidence, ManagedAuthzPolicySetHumanEvidence)
                    self.assertEqual(plan.evidence.result_status, "blocked")
                    self.assertEqual(
                        [blocker.code for blocker in plan.evidence.diff.policy_safety_blockers],
                        ["authz_policy_delivery_activation_active"],
                    )
                    review = privileged_operation_semantic_review(
                        record=plan, generated_at=store.clock
                    )
                    self.assertIn("authz_policy_delivery_activation_active", review.blockers.codes)
                    self.assertFalse(review.can_approve)
                    with self.assertRaises(PrivilegedOperationNotApprovableError):
                        _approve(store, plan, policy)
                else:
                    self.assertEqual(plan.evidence.result_status, "ok")
                self.assertEqual(store.list_authz_policy_records(status="active"), (policy,))

    def test_activation_installed_after_planning_is_truthful_pre_effect_failure(self) -> None:
        with (
            TemporaryDirectory() as directory,
            closing(
                _ClockStore(database_url=_sqlite_database_url(Path(directory) / "state.sqlite3"))
            ) as store,
        ):
            store.ensure_schema()
            policy = _seed_policy(store)
            _history(store, expires_at=store.clock - timedelta(days=1))
            plan = _plan(store)
            _approve(store, plan, policy)
            compare_and_write = store.compare_and_write_authz_policy_record

            def install_before_write(**kwargs: Any) -> Any:
                _history(store, expires_at=store.clock + timedelta(hours=1), repository_id=1002)
                return compare_and_write(**kwargs)

            with patch.object(
                store, "compare_and_write_authz_policy_record", side_effect=install_before_write
            ):
                execute_approved_privileged_operations_once(
                    record_store=store, now=lambda: store.clock
                )
            result = store.read_privileged_operation_record(plan.operation_id)
            self.assertEqual(result.status, "execution_failed")
            assert isinstance(result.execution, ManagedAuthzPolicySetExecutionEvidence)
            self.assertEqual(
                result.execution.failure_code, "authz_policy_delivery_activation_active"
            )
            self.assertFalse(result.execution.changed)
            self.assertFalse(result.execution.reconciliation_required)
            self.assertEqual(store.list_authz_policy_records(status="active"), (policy,))
            review = privileged_operation_semantic_review(record=result, generated_at=store.clock)
            self.assertIn("authz_policy_delivery_activation_active", review.blockers.codes)


class DeliveryAdministrationRetirementHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_late_activation_refusal_preserves_database_and_runtime_policy(self) -> None:
        from control_plane.authz_grant_service import AuthzManagedPolicyReconcileEnvelope
        from control_plane.http_app import (
            LaunchplaneAuthzPolicyRuntime,
            create_launchplane_fastapi_app,
        )
        from control_plane.http_routes.mutation_support import idempotency_scope
        from tests.support.auth import _StubVerifier
        from tests.support.http import lifespan_client
        from tests.test_authz_empty_set_contraction import _policy as _contraction_policy
        from tests.test_authz_grant_service import _identity

        with (
            TemporaryDirectory() as directory,
            closing(
                _ClockStore(database_url=_sqlite_database_url(Path(directory) / "state.sqlite3"))
            ) as store,
        ):
            store.ensure_schema()
            active = _seed_policy(store, baseline=_contraction_policy())
            runtime = LaunchplaneAuthzPolicyRuntime(
                active.policy,
                policy_sha256=active.policy_sha256,
                source="db",
                record_id=active.record_id,
                revision=active.revision,
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=active.policy,
                authz_policy_runtime=runtime,
                record_store_factory=lambda: store,
            )
            request = AuthzManagedPolicyReconcileEnvelope(
                product="launchplane",
                managed_set_id=ORDINARY_AGENT_DELIVERY_ADMINISTRATION_MANAGED_SET_ID,
                desired_policy=LaunchplaneAuthzPolicy(schema_version=3),
                reason="Retire the approved administration rule.",
            )
            route = "/v1/authz-policies/managed-rule-sets/reconcile"
            compare_and_write = store.compare_and_write_authz_policy_record

            def install_before_write(**kwargs: Any) -> Any:
                _history(store, expires_at=store.clock + timedelta(hours=1))
                return compare_and_write(**kwargs)

            async with lifespan_client(app) as client:
                dry_run = await client.post(
                    route,
                    headers={"Authorization": "Bearer valid-token"},
                    json=request.model_dump(mode="json"),
                )
                self.assertEqual(dry_run.status_code, 202, dry_run.text)
                apply = request.model_copy(
                    update={
                        "mode": "apply",
                        "reviewed_plan_sha256": dry_run.json()["result"]["diff"]["plan_sha256"],
                    }
                )
                with patch.object(
                    store, "compare_and_write_authz_policy_record", side_effect=install_before_write
                ):
                    response = await client.post(
                        route,
                        headers={
                            "Authorization": "Bearer valid-token",
                            "Idempotency-Key": "activation-raced-removal",
                        },
                        json=apply.model_dump(mode="json"),
                    )
            self.assertEqual(response.status_code, 409, response.text)
            self.assertEqual(
                response.json()["error"]["code"], "authz_policy_delivery_activation_active"
            )
            self.assertEqual(runtime.policy_sha256, active.policy_sha256)
            self.assertEqual(runtime.policy, active.policy)
            self.assertEqual(store.list_authz_policy_records(status="active"), (active,))
            self.assertIsNone(
                store.read_idempotency_record(
                    scope=idempotency_scope(_identity()),
                    route_path=route,
                    idempotency_key="activation-raced-removal",
                )
            )
