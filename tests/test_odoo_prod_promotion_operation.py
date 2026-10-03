import os
import unittest
from dataclasses import replace
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
from unittest.mock import patch

from fastapi import FastAPI
from urllib.parse import urlencode

from control_plane.contracts.authz_policy_record import (
    LaunchplaneAuthzPolicyRecord,
    authz_policy_sha256,
    build_authz_policy_record_id,
)
from control_plane.contracts.durable_operation_authorization import (
    DurableOperationCancellation,
    DurableOperationCallerIdentity,
    DurableOperationReconciliationAttestation,
)
from control_plane.contracts.odoo_prod_promotion_operation import (
    ODOO_PROD_PROMOTION_RUN_ACTION,
    ODOO_PROD_PROMOTION_SAFE_RETRY_PHASES,
    OdooProdPromotionOperationPhase,
    OdooProdPromotionOperationRecord,
    OdooProdPromotionRunRequest,
    OdooProdPromotionRunResult,
    build_odoo_prod_promotion_operation_id,
    odoo_prod_promotion_request_fingerprint,
)
from control_plane.contracts.odoo_prod_rollback_operation import (
    ODOO_PROD_ROLLBACK_ACTION,
    OdooProdRollbackOperationRecord,
    OdooProdRollbackRequest,
    OdooProdRollbackResult,
    OdooProdRollbackTarget,
    build_odoo_prod_rollback_operation_id,
    odoo_prod_rollback_request_fingerprint,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.durable_operation_authorization import capture_durable_operation_authorization
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.odoo_stable_lane import OdooStableLaneOperationConflictError
from control_plane.service_auth import BearerIdentityConfig, LaunchplaneAuthzPolicy
from control_plane.service_human_auth import (
    HumanSessionManager,
    InMemoryHumanSessionStore,
    LaunchplaneHumanSession,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.odoo_prod_backup_gate import OdooProdBackupGateResult
from control_plane.workflows.odoo_prod_promotion_inputs import OdooProdPromotionInputsResult
from control_plane.workflows.odoo_prod_promotion_run import OdooProdPromotionRunAdmission
from control_plane.workflows.odoo_stable_operation_worker import (
    run_odoo_stable_operation_worker_once,
)
from tests.http_app_test_support import (
    _browser_mutation_headers,
    _github_human_identity,
    _github_oauth_config,
    _RejectingVerifier,
)
from tests.support.auth import _identity as _workflow_identity, _StubVerifier
from tests.support.http import get, request
from tests.support.profiles import _odoo_preview_profile_payload
from tests.test_odoo_stable_operation_worker import _restore_operation
from tests.test_postgres_integration import _store_for_fresh_head_database

_ADMINISTRATOR_POLICY = LaunchplaneAuthzPolicy.model_validate(
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
        "terminal_agents": [
            {
                "subjects": ["terminal-agent"],
                "token_labels": ["terminal-agent-read"],
                "actions": [ODOO_PROD_PROMOTION_RUN_ACTION],
                "products": ["odoo-tenant-cm"],
                "contexts": ["cm"],
                "instances": ["testing", "prod"],
            }
        ],
    }
)


def _policy_record(
    policy: LaunchplaneAuthzPolicy, revision: int = 1
) -> LaunchplaneAuthzPolicyRecord:
    digest = authz_policy_sha256(policy)
    return LaunchplaneAuthzPolicyRecord(
        record_id=build_authz_policy_record_id(revision=revision, policy_sha256=digest),
        revision=revision,
        source="test:odoo-prod-promotion-operation",
        updated_at="2026-09-30T00:00:00Z",
        policy_sha256=digest,
        policy=policy,
    )


def _store(directory: str) -> PostgresRecordStore:
    store = PostgresRecordStore(database_url=f"sqlite+pysqlite:///{Path(directory) / 'state.db'}")
    store.ensure_schema()
    profile = _odoo_preview_profile_payload()
    profile["lanes"] = tuple(
        {
            "instance": instance,
            "context": "cm",
            "base_url": f"https://{instance}.cm.example.test",
            "health_url": f"https://{instance}.cm.example.test/web/health",
        }
        for instance in ("testing", "prod")
    )
    store.write_product_profile_record(LaunchplaneProductProfileRecord.model_validate(profile))
    store.seed_authz_policy_if_absent(_policy_record(_ADMINISTRATOR_POLICY))
    return store


def _run_request(request_id: str = "release-1") -> OdooProdPromotionRunRequest:
    return OdooProdPromotionRunRequest(
        context="cm",
        product="odoo-tenant-cm",
        request_id=request_id,
        infrastructure_backup_record_id=f"infrastructure-{request_id}",
    )


def _operation(key: str = "release-1") -> OdooProdPromotionOperationRecord:
    run_request = _run_request(key)
    scope = "github-human|example-operator|123"
    return OdooProdPromotionOperationRecord(
        operation_id=build_odoo_prod_promotion_operation_id(
            product="odoo-tenant-cm", context="cm", idempotency_key=key, idempotency_scope=scope
        ),
        product="odoo-tenant-cm",
        context="cm",
        instance="prod",
        idempotency_key=key,
        idempotency_scope=scope,
        request_fingerprint=odoo_prod_promotion_request_fingerprint(run_request),
        request=run_request,
        authorization=capture_durable_operation_authorization(
            identity=_github_human_identity(),
            action=ODOO_PROD_PROMOTION_RUN_ACTION,
            product="odoo-tenant-cm",
            context="cm",
            instances=("prod",),
            policy_record=_policy_record(_ADMINISTRATOR_POLICY),
            authorized_at="2026-09-30T00:00:00Z",
        ),
        created_at="2026-09-30T00:00:00Z",
        updated_at="2026-09-30T00:00:00Z",
    )


def _rollback_operation(key: str = "rollback-1") -> OdooProdRollbackOperationRecord:
    rollback_request = OdooProdRollbackRequest(context="cm", reason="Drill")
    scope = "github-human|example-operator|123"
    return OdooProdRollbackOperationRecord(
        operation_id=build_odoo_prod_rollback_operation_id(
            product="odoo-tenant-cm", context="cm", idempotency_key=key, idempotency_scope=scope
        ),
        product="odoo-tenant-cm",
        context="cm",
        instance="prod",
        idempotency_key=key,
        idempotency_scope=scope,
        request_fingerprint=odoo_prod_rollback_request_fingerprint(
            product="odoo-tenant-cm", request=rollback_request
        ),
        request=rollback_request,
        target=OdooProdRollbackTarget(
            artifact_id="artifact-cm-previous", deployment_record_id="deployment-cm-prod-previous"
        ),
        authorization=capture_durable_operation_authorization(
            identity=_github_human_identity(),
            action=ODOO_PROD_ROLLBACK_ACTION,
            product="odoo-tenant-cm",
            context="cm",
            instances=("prod",),
            policy_record=_policy_record(_ADMINISTRATOR_POLICY),
            authorized_at="2026-09-30T00:00:00Z",
        ),
        created_at="2026-09-30T00:00:00Z",
        updated_at="2026-09-30T00:00:00Z",
    )


def _passing_result(request_id: str = "release-1") -> OdooProdPromotionRunResult:
    return OdooProdPromotionRunResult(
        context="cm",
        from_instance="testing",
        to_instance="prod",
        request_id=request_id,
        run_status="pass",
        input_status="ready",
        backup_status="pass",
        promotion_status="pass",
        deployment_status="pass",
        post_deploy_status="pass",
        destination_health_status="pass",
        artifact_id="artifact-cm-new",
        promotion_record_id="promotion-cm-testing-to-prod",
        deployment_record_id="deployment-cm-prod",
    )


class OdooProdPromotionOperationStorageTests(unittest.TestCase):
    def test_one_active_promotion_per_lane_and_the_same_id_replays(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(directory)
            first = _operation("release-1")

            self.assertEqual(
                store.create_odoo_prod_promotion_operation_record_if_no_active_lane(first),
                (first, True),
            )
            replay, created = store.create_odoo_prod_promotion_operation_record_if_no_active_lane(
                first
            )
            self.assertFalse(created)
            self.assertEqual(replay.operation_id, first.operation_id)
            active, created = store.create_odoo_prod_promotion_operation_record_if_no_active_lane(
                _operation("release-2")
            )
            self.assertFalse(created)
            self.assertEqual(active.operation_id, first.operation_id)
            # A queued promotion holds the lane against the other Odoo lane operations.
            with self.assertRaises(OdooStableLaneOperationConflictError) as conflict:
                store.create_odoo_prod_backup_restore_operation_record_if_no_active_lane(
                    _restore_operation()
                )
            self.assertEqual(conflict.exception.owner.operation_kind, "prod_promotion")
            with self.assertRaises(OdooStableLaneOperationConflictError) as rollback_conflict:
                store.create_odoo_prod_rollback_operation_record_if_no_active_lane(
                    _rollback_operation()
                )
            self.assertEqual(rollback_conflict.exception.owner.operation_kind, "prod_promotion")

    def test_only_an_administrator_grant_can_back_a_queued_release(self) -> None:
        managed_rule_grant = _operation().authorization.model_copy(
            update={
                "grant": "policy_rule",
                "managed_set_id": "operator.releases",
                "managed_rule_id": "cm-prod-release",
            }
        )
        for operation in (_operation(), _rollback_operation()):
            with self.subTest(type(operation).__name__), self.assertRaises(ValueError):
                type(operation).model_validate(
                    {
                        **operation.model_dump(mode="json"),
                        "authorization": {
                            **managed_rule_grant.model_dump(mode="json"),
                            "action": operation.authorization.action,
                        },
                    }
                )

    def test_expired_lease_reruns_only_before_any_provider_effect(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(directory)
            operation = _operation()
            store.create_odoo_prod_promotion_operation_record_if_no_active_lane(operation)

            def claim_and_reach(phases: tuple[OdooProdPromotionOperationPhase, ...]) -> None:
                claimed = store.claim_next_odoo_prod_promotion_operation_record(
                    lease_owner="worker-a",
                    lease_expires_at="2026-09-30T00:05:00Z",
                    claimed_at="2026-09-30T00:00:00Z",
                )
                self.assertIsNotNone(claimed)
                for phase in phases:
                    self.assertIsNotNone(
                        store.checkpoint_odoo_prod_promotion_operation_record(
                            operation_id=operation.operation_id,
                            lease_owner="worker-a",
                            phase=phase,
                            checkpointed_at="2026-09-30T00:01:00Z",
                            evidence={},
                        )
                    )

            def recover() -> OdooProdPromotionOperationRecord:
                store.recover_expired_odoo_prod_promotion_operation_records(
                    now="2026-09-30T00:10:00Z",
                    safe_phases=ODOO_PROD_PROMOTION_SAFE_RETRY_PHASES,
                    max_attempts=3,
                )
                return store.read_odoo_prod_promotion_operation_record(operation.operation_id)

            claim_and_reach(("validated",))
            requeued = recover()
            self.assertEqual((requeued.status, requeued.phase), ("pending", "created"))

            claim_and_reach(("validated", "logical_backup_started"))
            held = recover()
            self.assertEqual(held.status, "reconciliation_required")
            self.assertEqual(held.phase, "logical_backup_started")
            self.assertIsNone(
                store.claim_next_odoo_prod_promotion_operation_record(
                    lease_owner="worker-b",
                    lease_expires_at="2026-09-30T00:20:00Z",
                    claimed_at="2026-09-30T00:15:00Z",
                )
            )

            cancelled = held.model_copy(
                update={
                    "status": "cancelled",
                    "phase": "cancelled",
                    "finished_at": "2026-09-30T00:30:00Z",
                    "error_code": "",
                    "error_message": "",
                    "cancellation": DurableOperationCancellation(
                        reason="Checked prod: the old artifact is still deployed.",
                        cancelled_at="2026-09-30T00:30:00Z",
                        caller=DurableOperationCallerIdentity(
                            identity_type="github_human",
                            login="example-operator",
                            github_id=123,
                            role="admin",
                        ),
                        reconciliation_attestation=DurableOperationReconciliationAttestation(
                            provider_inspected_at="2026-09-30T00:20:00Z",
                            provider_state="previous artifact still running",
                            evidence_reference="deployment-cm-prod-previous",
                            safe_to_release=True,
                        ),
                    ),
                }
            )
            self.assertTrue(store.cancel_pending_odoo_prod_promotion_operation_record(cancelled))
            _created_after_release, created = (
                store.create_odoo_prod_promotion_operation_record_if_no_active_lane(
                    _operation("release-2")
                )
            )
            self.assertTrue(created)


class OdooProdPromotionWorkerTests(unittest.TestCase):
    def _run_worker(
        self,
        store: PostgresRecordStore,
        side_effect: Callable[..., OdooProdPromotionRunResult],
    ) -> None:
        with patch(
            "control_plane.workflows.odoo_stable_operation_worker.execute_odoo_prod_promotion_run",
            side_effect=side_effect,
        ):
            result = run_odoo_stable_operation_worker_once(
                record_store=cast(object, store),  # type: ignore[arg-type]
                control_plane_root_path=Path("."),
                lease_owner="worker-a",
            )
        self.assertEqual(result.operation_kind, "odoo_prod_promotion")
        self.assertTrue(result.terminal_write_committed)

    def test_worker_records_phases_and_the_final_result(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(directory)
            operation = _operation()
            store.create_odoo_prod_promotion_operation_record_if_no_active_lane(operation)

            def run(**kwargs: object) -> OdooProdPromotionRunResult:
                phase_checkpoint = cast(Callable[[str], None], kwargs["phase_checkpoint"])
                provider_effect = cast(Callable[[str], None], kwargs["provider_effect_checkpoint"])
                self.assertEqual(kwargs["request"], operation.request)
                phase_checkpoint("validated")
                provider_effect("odoo_logical_backup")
                for phase in ("logical_backup_started", "logical_backup_completed"):
                    phase_checkpoint(phase)
                phase_checkpoint("promotion_started")
                return _passing_result()

            self._run_worker(store, run)

            finished = store.read_odoo_prod_promotion_operation_record(operation.operation_id)
            self.assertEqual((finished.status, finished.phase), ("pass", "completed"))
            assert finished.result is not None
            self.assertEqual(finished.result.deployment_record_id, "deployment-cm-prod")
            self.assertEqual(
                tuple(checkpoint.phase for checkpoint in finished.checkpoints),
                (
                    "validated",
                    "logical_backup_started",
                    "logical_backup_completed",
                    "promotion_started",
                ),
            )

    def test_worker_refuses_before_effects_once_the_administrator_is_removed(self) -> None:
        narrowed_policy = LaunchplaneAuthzPolicy.model_validate(
            {
                **_ADMINISTRATOR_POLICY.model_dump(mode="json", exclude_none=True),
                "github_humans": [],
            }
        )
        with TemporaryDirectory() as directory:
            store = _store(directory)
            operation = _operation()
            store.create_odoo_prod_promotion_operation_record_if_no_active_lane(operation)
            effects: list[str] = []

            def run(**kwargs: object) -> OdooProdPromotionRunResult:
                cast(Callable[[str], None], kwargs["provider_effect_checkpoint"])(
                    "odoo_logical_backup"
                )
                effects.append("logical_backup")
                return _passing_result()

            with patch(
                "control_plane.workflows.odoo_stable_operation_worker."
                "read_active_authz_policy_record",
                return_value=_policy_record(narrowed_policy, revision=2),
            ):
                self._run_worker(store, run)

            finished = store.read_odoo_prod_promotion_operation_record(operation.operation_id)
            self.assertEqual(finished.status, "fail")
            self.assertEqual(finished.error_code, "operation_authorization_administrator_revoked")
            self.assertEqual(effects, [])


class OdooProdReleaseBoundaryTests(unittest.TestCase):
    def test_administrator_removed_during_the_logical_backup_stops_the_deploy(self) -> None:
        narrowed = _policy_record(
            LaunchplaneAuthzPolicy.model_validate(
                {
                    **_ADMINISTRATOR_POLICY.model_dump(mode="json", exclude_none=True),
                    "github_humans": [],
                }
            ),
            revision=2,
        )
        current_policy = [_policy_record(_ADMINISTRATOR_POLICY)]
        deploys: list[str] = []

        def take_logical_backup(**_kwargs: object) -> OdooProdBackupGateResult:
            current_policy[0] = narrowed
            return OdooProdBackupGateResult(
                context="cm", instance="prod", backup_record_id="backup-1", backup_status="pass"
            )

        def deploy(**_kwargs: object) -> object:
            deploys.append("deploy")
            raise AssertionError("The deploy must not start after the administrator is removed.")

        with TemporaryDirectory() as directory:
            store = _store(directory)
            operation = _operation()
            store.create_odoo_prod_promotion_operation_record_if_no_active_lane(operation)
            run_module = "control_plane.workflows.odoo_prod_promotion_run."
            with (
                patch(
                    run_module + "admit_odoo_prod_promotion_run",
                    return_value=OdooProdPromotionOperationHttpTests._admission(),
                ),
                patch(
                    run_module + "execute_odoo_prod_backup_gate", side_effect=take_logical_backup
                ),
                patch(run_module + "execute_odoo_prod_promotion", side_effect=deploy),
                patch(
                    "control_plane.workflows.odoo_stable_operation_worker."
                    "read_active_authz_policy_record",
                    side_effect=lambda _store: current_policy[0],
                ),
            ):
                run_odoo_stable_operation_worker_once(
                    record_store=cast(object, store),  # type: ignore[arg-type]
                    control_plane_root_path=Path("."),
                    lease_owner="worker-a",
                )

            finished = store.read_odoo_prod_promotion_operation_record(operation.operation_id)
            self.assertEqual(deploys, [])
            self.assertEqual(finished.status, "fail")
            self.assertEqual(finished.error_code, "operation_authorization_administrator_revoked")
            self.assertEqual(
                tuple(checkpoint.phase for checkpoint in finished.checkpoints),
                ("validated", "logical_backup_started", "logical_backup_completed"),
            )

    def test_authority_removed_while_the_deploy_prepares_stops_its_first_write(self) -> None:
        narrowed = _policy_record(
            LaunchplaneAuthzPolicy.model_validate(
                {
                    **_ADMINISTRATOR_POLICY.model_dump(mode="json", exclude_none=True),
                    "github_humans": [],
                }
            ),
            revision=2,
        )
        current_policy = [_policy_record(_ADMINISTRATOR_POLICY)]
        writes: list[str] = []

        def deploy(**kwargs: object) -> object:
            checkpoint = cast(Callable[[str], None], kwargs["provider_effect_checkpoint"])
            current_policy[0] = narrowed
            checkpoint("target_replacement_raw_source")
            writes.append("raw_source")
            raise AssertionError("The deploy must not write after its authority is removed.")

        with TemporaryDirectory() as directory:
            store = _store(directory)
            operation = _operation()
            store.create_odoo_prod_promotion_operation_record_if_no_active_lane(operation)
            run_module = "control_plane.workflows.odoo_prod_promotion_run."
            with (
                patch(
                    run_module + "admit_odoo_prod_promotion_run",
                    return_value=OdooProdPromotionOperationHttpTests._admission(),
                ),
                patch(
                    run_module + "execute_odoo_prod_backup_gate",
                    return_value=OdooProdBackupGateResult(
                        context="cm",
                        instance="prod",
                        backup_record_id="backup-1",
                        backup_status="pass",
                    ),
                ),
                patch(run_module + "execute_odoo_prod_promotion", side_effect=deploy),
                patch(
                    "control_plane.workflows.odoo_stable_operation_worker."
                    "read_active_authz_policy_record",
                    side_effect=lambda _store: current_policy[0],
                ),
            ):
                run_odoo_stable_operation_worker_once(
                    record_store=cast(object, store),  # type: ignore[arg-type]
                    control_plane_root_path=Path("."),
                    lease_owner="worker-a",
                )

            finished = store.read_odoo_prod_promotion_operation_record(operation.operation_id)
            self.assertEqual(writes, [])
            self.assertEqual(finished.error_code, "operation_authorization_administrator_revoked")

    def test_worker_rolls_back_to_the_target_fixed_at_enqueue(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(directory)
            operation = _rollback_operation()
            store.create_odoo_prod_rollback_operation_record_if_no_active_lane(operation)

            def rollback(**kwargs: object) -> OdooProdRollbackResult:
                self.assertEqual(kwargs["target"], operation.target)
                checkpoint = cast(Callable[[str], None], kwargs["provider_effect_checkpoint"])
                checkpoint("target_replacement_raw_source")
                checkpoint("target_replacement_deploy")
                return OdooProdRollbackResult(
                    context="cm",
                    instance="prod",
                    source_channel="previous-deployment",
                    artifact_id="artifact-cm-previous",
                    promotion_record_id="promotion-cm-testing-to-prod",
                    deployment_record_id="deployment-cm-prod-rollback",
                    rollback_status="pass",
                    post_deploy_status="pass",
                )

            with patch(
                "control_plane.workflows.odoo_stable_operation_worker.execute_odoo_prod_rollback",
                side_effect=rollback,
            ):
                result = run_odoo_stable_operation_worker_once(
                    record_store=cast(object, store),  # type: ignore[arg-type]
                    control_plane_root_path=Path("."),
                    lease_owner="worker-a",
                )

            self.assertEqual(result.operation_kind, "odoo_prod_rollback")
            finished = store.read_odoo_prod_rollback_operation_record(operation.operation_id)
            self.assertEqual((finished.status, finished.phase), ("pass", "completed"))
            self.assertEqual(
                tuple(checkpoint.phase for checkpoint in finished.checkpoints),
                ("validated", "rollback_started"),
            )

    def test_rollback_lease_expiry_after_the_redeploy_started_is_never_rerun(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(directory)
            operation = _rollback_operation()
            store.create_odoo_prod_rollback_operation_record_if_no_active_lane(operation)
            store.claim_next_odoo_prod_rollback_operation_record(
                lease_owner="worker-a",
                lease_expires_at="2026-09-30T00:05:00Z",
                claimed_at="2026-09-30T00:00:00Z",
            )
            for phase in ("validated", "rollback_started"):
                store.checkpoint_odoo_prod_rollback_operation_record(
                    operation_id=operation.operation_id,
                    lease_owner="worker-a",
                    phase=phase,
                    checkpointed_at="2026-09-30T00:01:00Z",
                    evidence={},
                )
            store.recover_expired_odoo_prod_rollback_operation_records(
                now="2026-09-30T00:10:00Z",
                safe_phases=("created", "running", "validated"),
                max_attempts=3,
            )

            held = store.read_odoo_prod_rollback_operation_record(operation.operation_id)
            self.assertEqual(held.status, "reconciliation_required")
            self.assertEqual(held.target, operation.target)


class OdooProdPromotionOperationHttpTests(unittest.IsolatedAsyncioTestCase):
    def _app(
        self, store: PostgresRecordStore
    ) -> tuple[FastAPI, HumanSessionManager, LaunchplaneHumanSession]:
        session_manager = HumanSessionManager(
            config=_github_oauth_config(), session_store=InMemoryHumanSessionStore()
        )
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=_ADMINISTRATOR_POLICY,
            record_store_factory=lambda: store,
            human_session_manager=session_manager,
            bearer_identity_config=BearerIdentityConfig(
                terminal_agent_token="terminal-agent-token",
                terminal_agent_subject="terminal-agent",
                terminal_agent_token_label="terminal-agent-read",
            ),
        )
        return app, session_manager, session_manager.issue(_github_human_identity())

    @staticmethod
    def _payload(request_id: str = "release-1") -> dict[str, object]:
        return {
            "product": "odoo-tenant-cm",
            "run": {
                "context": "cm",
                "request_id": request_id,
                "infrastructure_backup_record_id": f"infrastructure-{request_id}",
            },
        }

    @staticmethod
    def _admission(blocked_reason: str = "") -> OdooProdPromotionRunAdmission:
        return OdooProdPromotionRunAdmission(
            inputs_result=OdooProdPromotionInputsResult(
                context="cm",
                from_instance="testing",
                to_instance="prod",
                request_id="release-1",
                input_status="ready",
                artifact_id="artifact-cm-new",
                backup_record_id="backup-gate-cm-prod-release-1",
            ),
            blocked_reason=blocked_reason,
        )

    async def test_signed_in_administrator_queues_one_promotion_and_polls_it(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(directory)
            app, session_manager, human_session = self._app(store)

            async def enqueue(
                key: str, payload: dict[str, object]
            ) -> tuple[int, dict[str, object]]:
                headers = _browser_mutation_headers(session_manager, human_session)
                headers["Idempotency-Key"] = key
                response = await request(
                    app, "POST", "/v1/odoo-prod-promotions", headers=headers, payload=payload
                )
                return response.status_code, response.json()

            with patch(
                "control_plane.http_routes.odoo_prod_release_operation."
                "admit_odoo_prod_promotion_run",
                return_value=self._admission(),
            ) as admit:
                status, accepted = await enqueue("ui-release-1", self._payload())
                replay_status, replay = await enqueue("ui-release-1", self._payload())
                other_status, other = await enqueue("ui-release-2", self._payload("release-2"))
                reused_status, reused = await enqueue("ui-release-1", self._payload("release-9"))

            self.assertEqual(status, 200, accepted)
            operation = cast(dict[str, object], accepted["operation"])
            self.assertEqual((operation["status"], operation["phase"]), ("pending", "created"))
            self.assertEqual(replay_status, 200, replay)
            self.assertEqual(
                cast(dict[str, object], replay["operation"])["operation_id"],
                operation["operation_id"],
            )
            self.assertEqual(other_status, 409, other)
            self.assertEqual(
                cast(dict[str, dict[str, str]], other)["error"]["code"],
                "promotion_already_active",
            )
            self.assertEqual(reused_status, 409, reused)
            self.assertEqual(
                cast(dict[str, dict[str, str]], reused)["error"]["code"],
                "idempotency_key_reused",
            )
            # Only the first request is admitted; the rest answer from the stored operation.
            self.assertEqual(admit.call_count, 1)
            stored = store.read_odoo_prod_promotion_operation_record(str(operation["operation_id"]))
            self.assertEqual(stored.authorization.grant, "policy_administrator")

            query = urlencode({"product": "odoo-tenant-cm", "context": "cm"})
            read = await get(
                app,
                f"/v1/odoo-prod-promotions/operations/{operation['operation_id']}?{query}",
                headers={"Cookie": session_manager.session_cookie_header(human_session)},
            )
            self.assertEqual(read.status_code, 200, read.text)
            self.assertEqual(read.json()["operation"]["request_id"], "release-1")
            self.assertNotIn("authorization", read.json()["operation"])

    async def test_promotion_that_is_not_ready_is_refused_before_queueing(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(directory)
            app, session_manager, human_session = self._app(store)
            headers = _browser_mutation_headers(session_manager, human_session)
            headers["Idempotency-Key"] = "ui-release-1"
            with patch(
                "control_plane.http_routes.odoo_prod_release_operation."
                "admit_odoo_prod_promotion_run",
                return_value=self._admission("Release is not approved by the site owner."),
            ):
                response = await request(
                    app,
                    "POST",
                    "/v1/odoo-prod-promotions",
                    headers=headers,
                    payload=self._payload(),
                )

            self.assertEqual(response.status_code, 409, response.text)
            self.assertEqual(response.json()["error"]["code"], "promotion_not_ready")
            self.assertIn("not approved", response.json()["error"]["message"])
            self.assertEqual(store.list_odoo_prod_promotion_operation_records(), ())

    async def test_terminal_agent_token_cannot_queue_a_promotion(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(directory)
            app, _session_manager, _human_session = self._app(store)
            response = await request(
                app,
                "POST",
                "/v1/odoo-prod-promotions",
                headers={
                    "Authorization": "Bearer terminal-agent-token",
                    "Idempotency-Key": "agent-release",
                },
                payload=self._payload(),
            )

            self.assertEqual(response.status_code, 403, response.text)
            self.assertEqual(response.json()["error"]["code"], "authorization_denied")
            self.assertEqual(store.list_odoo_prod_promotion_operation_records(), ())

    async def test_a_session_with_only_a_product_rule_cannot_queue_a_release(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(directory)
            app, session_manager, _admin_session = self._app(store)
            operator_session = session_manager.issue(
                replace(_github_human_identity(), github_id=456, login="site-operator")
            )
            for route, payload in (
                ("/v1/odoo-prod-promotions", self._payload()),
                ("/v1/odoo-prod-rollbacks", self._rollback_payload()),
            ):
                headers = _browser_mutation_headers(session_manager, operator_session)
                headers["Idempotency-Key"] = "operator-release"
                response = await request(app, "POST", route, headers=headers, payload=payload)
                self.assertEqual(response.status_code, 403, response.text)
                self.assertEqual(response.json()["error"]["code"], "authorization_denied")
            self.assertEqual(store.list_odoo_prod_promotion_operation_records(), ())
            self.assertEqual(store.list_odoo_prod_rollback_operation_records(), ())

    @staticmethod
    def _rollback_payload(reason: str = "Drill") -> dict[str, object]:
        return {"product": "odoo-tenant-cm", "rollback": {"context": "cm", "reason": reason}}

    async def test_rollback_fixes_its_target_once_and_replays_it(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(directory)
            app, session_manager, human_session = self._app(store)

            async def enqueue(
                key: str, payload: dict[str, object]
            ) -> tuple[int, dict[str, object]]:
                headers = _browser_mutation_headers(session_manager, human_session)
                headers["Idempotency-Key"] = key
                response = await request(
                    app, "POST", "/v1/odoo-prod-rollbacks", headers=headers, payload=payload
                )
                return response.status_code, response.json()

            with patch(
                "control_plane.http_routes.odoo_prod_release_operation."
                "resolve_odoo_prod_rollback_target",
                return_value=OdooProdRollbackTarget(
                    artifact_id="artifact-cm-previous",
                    deployment_record_id="deployment-cm-prod-previous",
                ),
            ) as resolve:
                status, accepted = await enqueue("ui-rollback-1", self._rollback_payload())
                replay_status, replay = await enqueue("ui-rollback-1", self._rollback_payload())
                other_status, other = await enqueue("ui-rollback-2", self._rollback_payload("x"))
                with patch(
                    "control_plane.http_routes.odoo_prod_release_operation."
                    "admit_odoo_prod_promotion_run",
                    return_value=self._admission(),
                ):
                    headers = _browser_mutation_headers(session_manager, human_session)
                    headers["Idempotency-Key"] = "ui-release-1"
                    promotion = await request(
                        app,
                        "POST",
                        "/v1/odoo-prod-promotions",
                        headers=headers,
                        payload=self._payload(),
                    )

            self.assertEqual(status, 200, accepted)
            operation = cast(dict[str, object], accepted["operation"])
            self.assertEqual(operation["target_artifact_id"], "artifact-cm-previous")
            self.assertEqual(
                operation["target_deployment_record_id"], "deployment-cm-prod-previous"
            )
            self.assertEqual(replay_status, 200, replay)
            self.assertEqual(
                cast(dict[str, object], replay["operation"])["operation_id"],
                operation["operation_id"],
            )
            resolve.assert_called_once()
            self.assertEqual(other_status, 409, other)
            self.assertEqual(
                cast(dict[str, dict[str, str]], other)["error"]["code"],
                "rollback_already_active",
            )
            self.assertEqual(promotion.status_code, 409, promotion.text)
            self.assertEqual(promotion.json()["error"]["code"], "lane_busy")
            stored = store.read_odoo_prod_rollback_operation_record(str(operation["operation_id"]))
            self.assertEqual(stored.authorization.grant, "policy_administrator")

            query = urlencode({"product": "odoo-tenant-cm", "context": "cm"})
            read = await get(
                app,
                f"/v1/odoo-prod-rollbacks/operations/{operation['operation_id']}?{query}",
                headers={"Cookie": session_manager.session_cookie_header(human_session)},
            )
            self.assertEqual(read.status_code, 200, read.text)
            self.assertEqual(read.json()["operation"]["reason"], "Drill")

    async def test_rollback_with_no_earlier_deployment_is_refused_before_queueing(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(directory)
            app, session_manager, human_session = self._app(store)
            headers = _browser_mutation_headers(session_manager, human_session)
            headers["Idempotency-Key"] = "ui-rollback-1"
            response = await request(
                app,
                "POST",
                "/v1/odoo-prod-rollbacks",
                headers=headers,
                payload=self._rollback_payload(),
            )

            self.assertEqual(response.status_code, 409, response.text)
            self.assertEqual(response.json()["error"]["code"], "rollback_target_missing")
            self.assertEqual(store.list_odoo_prod_rollback_operation_records(), ())

    async def test_synchronous_promotions_refuse_while_a_queued_release_holds_the_lane(
        self,
    ) -> None:
        promotion_payload = {
            "product": "odoo-tenant-cm",
            "promotion": {
                "context": "cm",
                "artifact_id": "artifact-cm-new",
                "backup_record_id": "backup-gate-cm-prod-run-1",
                "infrastructure_backup_record_id": "infrastructure-cm-prod",
                "source_git_ref": "848bf1b69ff3adbe9b255c61c7b8f5ca04efbcbb",
            },
        }
        for route, payload, target in (
            (
                "/v1/drivers/odoo/prod-promotion-run",
                self._payload(),
                "control_plane.odoo_prod_promotion_http.execute_odoo_prod_promotion_run",
            ),
            (
                "/v1/drivers/odoo/prod-promotion",
                promotion_payload,
                "control_plane.odoo_prod_promotion_http.execute_odoo_prod_promotion",
            ),
        ):
            with self.subTest(route), TemporaryDirectory() as directory:
                store = _store(directory)
                store.create_odoo_prod_rollback_operation_record_if_no_active_lane(
                    _rollback_operation()
                )
                # The realistic caller: a site's release workflow over OIDC.
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_workflow_identity()),
                    authz_policy=LaunchplaneAuthzPolicy.model_validate(
                        {
                            "github_actions": [
                                {
                                    "repository": "every/verireel",
                                    "actions": [
                                        "odoo_prod_promotion.execute",
                                        ODOO_PROD_PROMOTION_RUN_ACTION,
                                    ],
                                    "products": ["odoo-tenant-cm"],
                                    "contexts": ["cm"],
                                }
                            ]
                        }
                    ),
                    record_store_factory=lambda: store,
                )
                headers = {
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "workflow-promotion",
                }
                with patch(target) as execute:
                    response = await request(app, "POST", route, headers=headers, payload=payload)

                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(response.json()["error"]["code"], "lane_busy")
                execute.assert_not_called()

    async def test_synchronous_rollback_refuses_while_a_queued_release_holds_the_lane(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            store = _store(directory)
            store.create_odoo_prod_promotion_operation_record_if_no_active_lane(_operation())
            app, session_manager, human_session = self._app(store)
            headers = _browser_mutation_headers(session_manager, human_session)
            headers["Idempotency-Key"] = "workflow-rollback"
            with patch(
                "control_plane.odoo_prod_rollback_http.execute_odoo_prod_rollback"
            ) as execute:
                response = await request(
                    app,
                    "POST",
                    "/v1/drivers/odoo/prod-rollback",
                    headers=headers,
                    payload=self._rollback_payload(),
                )

            self.assertEqual(response.status_code, 409, response.text)
            self.assertEqual(response.json()["error"]["code"], "lane_busy")
            execute.assert_not_called()

    async def test_operations_read_grant_reads_release_status_without_free_text(self) -> None:
        promotion = OdooProdPromotionOperationRecord.model_validate(
            {
                **_operation().model_dump(mode="json"),
                "status": "fail",
                "phase": "failed",
                "finished_at": "2026-09-30T00:05:00Z",
                "error_code": "promotion_failed",
                "error_message": "Backup host backup.internal at 10.9.8.7 refused.",
            }
        )
        rollback_record = _rollback_operation().model_dump(mode="json")
        rollback = OdooProdRollbackOperationRecord.model_validate(
            {
                **rollback_record,
                "request": {
                    **rollback_record["request"],
                    "reason": "Restore tenant_live on backup.internal (10.9.8.7)",
                },
                "status": "fail",
                "phase": "failed",
                "finished_at": "2026-09-30T00:05:00Z",
                "error_code": "10.9.8.7",
                "error_message": "Provider at 10.9.8.7 refused.",
            }
        )

        def app_with(actions: tuple[str, ...], store: PostgresRecordStore) -> FastAPI:
            return create_launchplane_fastapi_app(
                verifier=_StubVerifier(_workflow_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate(
                    {
                        "schema_version": 2,
                        "github_actions": [
                            {
                                "repository": "every/verireel",
                                "actions": list(actions),
                                "products": ["launchplane"],
                                "contexts": ["cm"],
                                "instances": ["prod"],
                            }
                        ],
                    }
                ),
                record_store_factory=lambda: store,
            )

        query = urlencode({"product": "odoo-tenant-cm", "context": "cm"})
        headers = {"Authorization": "Bearer valid-token"}
        with TemporaryDirectory() as directory:
            store = _store(directory)
            store.write_odoo_prod_promotion_operation_record(promotion)
            store.write_odoo_prod_rollback_operation_record(rollback)
            reader = app_with(("operations.read",), store)
            promotion_read = await get(
                reader,
                f"/v1/odoo-prod-promotions/operations/{promotion.operation_id}?{query}",
                headers=headers,
            )
            rollback_read = await get(
                reader,
                f"/v1/odoo-prod-rollbacks/operations/{rollback.operation_id}?{query}",
                headers=headers,
            )
            denied = await get(
                app_with(("deployment.read",), store),
                f"/v1/odoo-prod-promotions/operations/{promotion.operation_id}?{query}",
                headers=headers,
            )

        self.assertEqual(promotion_read.status_code, 200, promotion_read.text)
        promotion_view = promotion_read.json()["operation"]
        self.assertEqual(
            (promotion_view["status"], promotion_view["phase"], promotion_view["error_code"]),
            ("fail", "failed", "promotion_failed"),
        )
        self.assertEqual(promotion_view["error_message"], "")
        self.assertIsNone(promotion_view.get("result"))
        self.assertEqual(rollback_read.status_code, 200, rollback_read.text)
        rollback_view = rollback_read.json()["operation"]
        self.assertEqual(rollback_view["error_code"], "unrecognized_code")
        self.assertEqual(rollback_view["reason"], "")
        for response in (promotion_read, rollback_read):
            self.assertNotIn("backup.internal", response.text)
            self.assertNotIn("10.9.8.7", response.text)
        self.assertEqual(denied.status_code, 403)


@unittest.skipUnless(
    os.environ.get("LAUNCHPLANE_TEST_POSTGRES_URL"), "Real PostgreSQL test URL is required"
)
class OdooSynchronousLaneReservationPostgresTests(unittest.TestCase):
    """A synchronous release route and the queued operations exclude each other."""

    lane = {"product": "odoo-tenant-cm", "context": "cm", "instance": "prod"}

    def test_a_synchronous_run_blocks_enqueues_and_is_released_after_success(self) -> None:
        with _store_for_fresh_head_database() as store:
            with store.odoo_synchronous_lane_reservation(**self.lane) as owner:
                self.assertIsNone(owner)
                with self.assertRaises(OdooStableLaneOperationConflictError) as conflict:
                    store.create_odoo_prod_promotion_operation_record_if_no_active_lane(
                        _operation()
                    )
                self.assertEqual(conflict.exception.owner.operation_kind, "synchronous_release")
                with self.assertRaises(OdooStableLaneOperationConflictError):
                    store.create_odoo_prod_rollback_operation_record_if_no_active_lane(
                        _rollback_operation()
                    )
                with store.odoo_synchronous_lane_reservation(**self.lane) as second:
                    assert second is not None
                    self.assertEqual(second.operation_kind, "synchronous_release")
                # Another lane is unaffected.
                with store.odoo_synchronous_lane_reservation(
                    product="odoo-tenant-cm", context="cm", instance="testing"
                ) as other_lane:
                    self.assertIsNone(other_lane)

            _created_operation, created = (
                store.create_odoo_prod_promotion_operation_record_if_no_active_lane(_operation())
            )
            self.assertTrue(created)

    def test_the_reservation_is_released_when_the_run_fails(self) -> None:
        with _store_for_fresh_head_database() as store:
            with self.assertRaises(RuntimeError):
                with store.odoo_synchronous_lane_reservation(**self.lane) as owner:
                    self.assertIsNone(owner)
                    raise RuntimeError("deploy failed")

            _created_operation, created = (
                store.create_odoo_prod_rollback_operation_record_if_no_active_lane(
                    _rollback_operation()
                )
            )
            self.assertTrue(created)

    def test_a_queued_operation_refuses_a_synchronous_run(self) -> None:
        with _store_for_fresh_head_database() as store:
            store.create_odoo_prod_promotion_operation_record_if_no_active_lane(_operation())

            with store.odoo_synchronous_lane_reservation(**self.lane) as owner:
                assert owner is not None
                self.assertEqual(owner.operation_kind, "prod_promotion")


if __name__ == "__main__":
    unittest.main()
