"""Recover exact Client promotions using real storage and a fake runtime."""

import hashlib
import json
import unittest
from datetime import timedelta
from threading import Event, Thread, current_thread
from typing import Any
from unittest.mock import patch

from control_plane.client_release import (
    CLIENT_RELEASE_IDEMPOTENCY_SCOPE,
    client_release_promotion_request,
)
from control_plane.generic_web_promotion_http import (
    GENERIC_WEB_PROD_PROMOTION_ROUTE,
    GenericWebProdPromotionEnvelope,
)
from control_plane.generic_web_promotion_provider_adapter import (
    GenericWebProdPromotionProviderMutationAdapter,
)
from control_plane.storage.postgres import MutationReservationCompletionResult
from control_plane.contracts.idempotency_record import parse_launchplane_mutation_timestamp
from control_plane.workflows.generic_web_deploy_provider import GenericWebRuntimeArtifactObservation
from control_plane.workflows import generic_web_promotion as promotion_workflow
from tests import test_generic_web_client_release as fixtures
from tests.test_generic_web_deploy_recovery import _create_recovery_app, _OPERATOR_TOKEN
from tests.test_service import _invoke_app
from tests.support.auth import local_operator_policy
from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord


class PromotionRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.case = fixtures.GenericWebClientReleaseTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.store = self.case.store
        self.provider = self.case.provider
        self.runtime = self.enterContext(
            patch.object(
                self.provider,
                "observe_runtime_artifact",
                side_effect=self.observe_runtime,
                create=True,
            )
        )
        self.enterContext(
            patch(
                "control_plane.generic_web_promotion_recovery.default_generic_web_deploy_provider",
                return_value=self.provider,
            )
        )
        if self.store.database_dialect_name == "postgresql":
            self.store.seed_authz_policy_if_absent(
                LaunchplaneAuthzPolicyRecord(
                    record_id="promotion-recovery-test-policy",
                    source="test",
                    updated_at=self.case.target.updated_at,
                    policy=local_operator_policy(
                        actions=("product_environment.read", "generic_web_prod_promotion.execute"),
                        products=(self.case.profile.product,),
                        contexts=(self.case.context,),
                    ),
                )
            )
        self.app = _create_recovery_app(
            root=self.case.root,
            store=self.store,
            actions=("product_environment.read", "generic_web_prod_promotion.execute"),
            contexts=(self.case.context,),
        )
        self.decision = self.case.accept()
        self.initial_inventory = self.store.read_environment_inventory(
            context_name=self.case.context, instance_name="prod"
        )
        self.case.advance()
        self.case.capture()
        self.path = f"/v1/admin/generic-web/promotion-recovery/{self.case.profile.product}/{self.decision.record_id}"

    def observe_runtime(self, **kwargs: object) -> GenericWebRuntimeArtifactObservation:
        del kwargs
        identity = self.provider.running
        assert identity is not None
        return GenericWebRuntimeArtifactObservation(
            target_artifact_reference=identity.artifact_id,
            running_container_images=(identity.artifact_id,),
            running_container_deployment_record_ids=(identity.deployment_record_id,),
        )

    def reservation(self) -> Any:
        return self.store.read_idempotency_record(
            scope=CLIENT_RELEASE_IDEMPOTENCY_SCOPE,
            route_path=GENERIC_WEB_PROD_PROMOTION_ROUTE,
            idempotency_key=f"{self.decision.record_id}:promote-1",
        )

    def interrupt_completion(self, *, rollback: bool = False, hold: bool = True) -> None:
        self.case.fail_health = rollback

        def lose_completion(*, completion: Any) -> MutationReservationCompletionResult:
            if not hold:
                return MutationReservationCompletionResult(
                    status="owner_mismatch", record=self.reservation()
                )
            held = self.store.mark_mutation_reconcile_required(
                reservation=self.reservation(), reconciliation_key=completion.reconciliation_key
            )
            return MutationReservationCompletionResult(
                status="reservation_mismatch", record=held.record
            )

        with patch.object(self.store, "complete_mutation_reservation", side_effect=lose_completion):
            self.case.advance()
        self.assertEqual(self.reservation().state, "reconcile_required" if hold else "running")

    def request(
        self, suffix: str = "", payload: dict[str, object] | None = None
    ) -> tuple[int, dict[str, Any]]:
        return _invoke_app(
            self.app,
            method="POST" if suffix else "GET",
            path=self.path + suffix,
            authorization=f"Bearer {_OPERATOR_TOKEN}",
            payload=payload,
        )

    def dry_run(self) -> dict[str, Any]:
        status, plan = self.request("/dry-run", {"reason": "Inspect interrupted promotion."})
        self.assertEqual(status, 200, plan)
        return plan

    def apply(self, plan: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        return self.request(
            "/apply",
            {
                "reason": "Inspect interrupted promotion.",
                "recovery_reference": plan["recovery_reference"],
                "expected_recovery_digest": plan["recovery_digest"],
            },
        )

    def snapshot(self) -> object:
        return (
            self.reservation(),
            self.store.list_promotion_records(),
            self.store.list_deployment_records(),
            self.store.list_environment_inventory(),
        )

    def test_read_and_dry_run_are_redacted_and_write_nothing(self) -> None:
        self.interrupt_completion()
        before = self.snapshot()
        status, selected = self.request()
        self.runtime.assert_not_called()
        plan = self.dry_run()
        self.assertEqual(status, 200, selected)
        self.assertEqual(selected["recovery_reference"], self.reservation().record_id)
        self.assertEqual(plan["proposed_action"], "adopt_promotion")
        self.assertEqual(self.snapshot(), before)
        output = json.dumps((selected, plan))
        for private in (
            self.reservation().idempotency_key,
            self.reservation().reconciliation_key,
            self.reservation().provider_target_key,
            self.case.target.target_id,
            self.decision.checklist.candidate.artifact_id,
        ):
            self.assertNotIn(private, output)

    def test_adopts_terminal_promotion_once_without_provider_effect(self) -> None:
        self.interrupt_completion()
        plan = self.dry_run()
        effects = list(self.provider.deployed_artifacts)
        status, applied = self.apply(plan)
        self.assertEqual(status, 202, applied)
        self.assertEqual(self.reservation().state, "completed")
        self.assertEqual(self.reservation().response_payload["result"]["promotion_status"], "pass")
        self.assertEqual(self.apply(plan)[0], 202)
        self.assertEqual(self.provider.deployed_artifacts, effects)
        self.assertEqual(self.case.advance(), ())

    def test_adopts_verified_rollback_and_preserves_failed_release(self) -> None:
        self.interrupt_completion(rollback=True)
        plan = self.dry_run()
        self.assertEqual(plan["proposed_action"], "adopt_rollback")
        self.assertEqual(self.apply(plan)[0], 202)
        result = self.reservation().response_payload["result"]
        self.assertEqual((result["promotion_status"], result["rollback_status"]), ("fail", "pass"))
        self.assertEqual(self.case.advance(), ())

    def test_changed_runtime_identity_or_health_stays_held(self) -> None:
        self.interrupt_completion()
        plan = self.dry_run()
        assert self.provider.running is not None
        self.provider.running = self.provider.running.model_copy(
            update={"deployment_record_id": "unrelated"}
        )
        self.assertEqual(self.apply(plan)[0], 409)
        held = self.dry_run()
        self.assertEqual(held["proposed_action"], "hold_unknown")
        self.assertEqual(self.apply(held)[0], 409)
        self.assertEqual(self.reservation().state, "reconcile_required")

    def test_changed_acceptance_or_record_rejects_reviewed_digest(self) -> None:
        self.interrupt_completion()
        plan = self.dry_run()
        self.store.write_release_review_decision_record(
            self.decision.model_copy(update={"reason": "changed"})
        )
        status, response = self.apply(plan)
        self.assertEqual(status, 409, response)
        self.assertEqual(self.reservation().state, "reconcile_required")

    def test_atomic_adoption_refuses_late_record_change(self) -> None:
        self.interrupt_completion()
        plan = self.dry_run()
        adopt = self.store.adopt_reconciled_mutation

        def race(**kwargs: Any) -> Any:
            profile = self.store.read_product_profile_record(self.case.profile.product)
            self.store.write_product_profile_record(
                profile.model_copy(update={"display_name": "Changed"})
            )
            return adopt(**kwargs)

        with patch.object(self.store, "adopt_reconciled_mutation", side_effect=race):
            status, response = self.apply(plan)
        self.assertEqual(status, 409, response)
        self.assertEqual(self.reservation().state, "reconcile_required")

    def test_interrupted_health_check_finishes_only_after_current_health_passes(self) -> None:
        self.interrupt_completion()
        promotion = self.store.list_promotion_records()[0]
        self.store.write_promotion_record(
            promotion.model_copy(
                update={
                    "deployment_record_id": "",
                    "deploy": promotion.deploy.model_copy(update={"status": "pending"}),
                    "destination_health": promotion.destination_health.model_copy(
                        update={"verified": False, "status": "pending"}
                    ),
                }
            )
        )
        self.case.fail_health = True
        plan = self.dry_run()
        self.assertEqual(plan["proposed_action"], "hold_unknown")
        self.assertEqual(self.apply(plan)[0], 409)
        self.case.fail_health = False
        plan = self.dry_run()
        self.assertEqual(plan["proposed_action"], "adopt_promotion")
        self.assertEqual(self.apply(plan)[0], 202)
        self.assertEqual(
            self.store.read_promotion_record(promotion.record_id).destination_health.status, "pass"
        )

    def test_no_effect_and_in_progress_checkpoints_remain_fenced(self) -> None:
        backup = self.store.list_verireel_prod_backup_gate_operation_records()[0]
        request = client_release_promotion_request(
            self.case.profile, self.decision, backup.backup_record_id
        )
        adapter = GenericWebProdPromotionProviderMutationAdapter(
            control_plane_root=self.case.root,
            record_store=self.store,
            promotion_request=GenericWebProdPromotionEnvelope(
                product=self.case.profile.product, promotion=request
            ),
            profile=self.case.profile,
            lane=self.case.profile.lanes[-1],
            trace_id="test",
            validate_before_effect=lambda _target: None,
            deploy_provider=self.provider,
        )
        reserved = self.store.reserve_mutation(
            scope=CLIENT_RELEASE_IDEMPOTENCY_SCOPE,
            route_path=GENERIC_WEB_PROD_PROMOTION_ROUTE,
            idempotency_key=f"{self.decision.record_id}:promote-1",
            request_fingerprint=hashlib.sha256(request.model_dump_json().encode()).hexdigest(),
            lease_owner="interrupted",
            reconciliation_key=adapter.reconciliation_key(),
            provider_target_key=adapter.target_key(),
        ).record
        assert reserved is not None
        self.assertEqual(self.dry_run()["proposed_action"], "wait_for_active_lease")
        reconciled = self.store.mark_mutation_reconcile_required(
            reservation=reserved, reconciliation_key=reserved.reconciliation_key
        ).record
        assert reconciled is not None
        plan = self.dry_run()
        self.assertEqual(plan["proposed_action"], "hold_unknown")
        self.assertEqual(self.apply(plan)[0], 409)
        self.assertEqual(self.provider.deployed_artifacts, [])

    def test_missing_cross_product_and_denied_read_select_nothing(self) -> None:
        self.assertEqual(self.request()[0], 404)
        self.interrupt_completion()
        self.app = _create_recovery_app(
            root=self.case.root,
            store=self.store,
            actions=("product_environment.read",),
            contexts=("another-lane",),
        )
        self.assertEqual(self.request()[0], 403)
        self.runtime.assert_not_called()

    def test_completed_reservation_is_read_only_replay(self) -> None:
        self.case.advance()
        before = self.snapshot()
        plan = self.dry_run()
        self.assertEqual(plan["proposed_action"], "replay_completed")
        self.assertEqual(self.apply(plan)[0], 202)
        self.assertEqual(self.snapshot(), before)
        self.runtime.assert_not_called()

    def test_machine_workflow_identity_cannot_recover(self) -> None:
        self.interrupt_completion()
        before = self.snapshot()
        for suffix in ("", "/dry-run", "/apply"):
            status, response = _invoke_app(
                self.app,
                method="GET" if not suffix else "POST",
                path=self.path + suffix,
                authorization="Bearer valid-token",
                payload={
                    "reason": "Inspect",
                    "recovery_reference": "untrusted",
                    "expected_recovery_digest": "0" * 64,
                }
                if suffix == "/apply"
                else ({"reason": "Inspect"} if suffix else None),
            )
            self.assertEqual(status, 403, response)
        self.assertEqual(self.snapshot(), before)
        self.runtime.assert_not_called()

    def test_expired_running_completion_adopts_atomically_without_intermediate_write(self) -> None:
        self.interrupt_completion(hold=False)
        expired_at = parse_launchplane_mutation_timestamp(
            self.reservation().lease_expires_at, field_name="lease_expires_at"
        ) + timedelta(seconds=1)
        with patch.object(
            self.store, "_database_mutation_timestamp", return_value=expired_at.isoformat()
        ):
            before = self.reservation()
            plan = self.dry_run()
            self.assertEqual(plan["proposed_action"], "adopt_promotion")
            self.assertEqual(self.reservation(), before)
            self.assertEqual(self.apply(plan)[0], 202)
        self.assertEqual(self.reservation().state, "completed")

    def test_adoption_repairs_inventory_lost_after_final_checks(self) -> None:
        self.interrupt_completion()
        self.store.write_environment_inventory(self.initial_inventory)
        plan = self.dry_run()
        self.assertEqual(plan["proposed_action"], "adopt_promotion")
        self.assertEqual(self.apply(plan)[0], 202)
        inventory = self.store.read_environment_inventory(
            context_name=self.case.context, instance_name="prod"
        )
        deployment = self.store.read_deployment_record(inventory.deployment_record_id)
        self.assertEqual(inventory.runtime_identity, deployment.runtime_identity)
        assert self.provider.running is not None and inventory.runtime_identity is not None
        self.assertEqual(
            inventory.runtime_identity.deployment_record_id,
            self.provider.running.deployment_record_id,
        )

    def test_failed_rollback_deployment_remains_held(self) -> None:
        self.interrupt_completion(rollback=True)
        promotion = self.store.list_promotion_records()[0]
        self.store.write_promotion_record(
            promotion.model_copy(
                update={"rollback": promotion.rollback.model_copy(update={"status": "fail"})}
            )
        )
        failed_deployment = self.store.read_deployment_record(
            promotion.rollback.deployment_record_id
        )
        self.store.write_deployment_record(
            failed_deployment.model_copy(
                update={"deploy": failed_deployment.deploy.model_copy(update={"status": "fail"})}
            )
        )
        plan = self.dry_run()
        self.assertEqual(plan["proposed_action"], "hold_unknown")
        self.assertEqual(self.apply(plan)[0], 409)
        self.assertEqual(self.reservation().state, "reconcile_required")

    def test_interrupted_rollback_health_is_verified_without_redeploying(self) -> None:
        class InterruptedRollback(BaseException):
            pass

        self.case.fail_health = True
        verify = promotion_workflow._verify_health_evidence_with_identity

        def checked_crash(evidence: Any, **kwargs: Any) -> Any:
            if (
                kwargs["expected_runtime_identity"].artifact_id
                == self.decision.checklist.production.artifact_id
            ):
                raise InterruptedRollback()
            return verify(evidence, **kwargs)

        with patch.object(
            promotion_workflow, "_verify_health_evidence_with_identity", side_effect=checked_crash
        ):
            with self.assertRaises(InterruptedRollback):
                self.case.advance()
        held = self.reservation()
        self.assertTrue(held.provider_effect_phase.startswith("rollback_"))
        self.store.mark_mutation_reconcile_required(
            reservation=held, reconciliation_key=held.reconciliation_key
        )
        promotion = self.store.list_promotion_records()[0]
        self.assertFalse(promotion.rollback.attempted)
        self.assertEqual(
            promotion.rollback.target_deployment_record_id,
            self.initial_inventory.deployment_record_id,
        )
        effects = list(self.provider.deployed_artifacts)
        plan = self.dry_run()
        self.assertEqual(plan["proposed_action"], "adopt_rollback")
        self.assertEqual(self.apply(plan)[0], 202)
        result = self.reservation().response_payload["result"]
        self.assertEqual((result["promotion_status"], result["rollback_status"]), ("fail", "pass"))
        self.assertEqual(self.provider.deployed_artifacts, effects)
        inventory = self.store.read_environment_inventory(
            context_name=self.case.context, instance_name="prod"
        )
        self.assertEqual(inventory.promotion_record_id, self.initial_inventory.promotion_record_id)
        self.assertEqual(
            inventory.promoted_from_instance, self.initial_inventory.promoted_from_instance
        )

    def test_recorded_rollback_health_failure_is_not_rewritten(self) -> None:
        import click

        self.case.fail_health = True
        with patch(
            "control_plane.workflows.generic_web_promotion.wait_for_runtime_identity_healthcheck_with_retry",
            side_effect=click.ClickException("Recorded health failure"),
        ):
            self.case.advance()
        before = self.snapshot()
        self.assertEqual(self.store.list_promotion_records()[0].rollback.status, "fail")
        plan = self.dry_run()
        self.assertEqual(plan["proposed_action"], "hold_unknown")
        self.assertEqual(self.apply(plan)[0], 409)
        self.assertEqual(self.snapshot(), before)

    def test_paused_original_worker_cannot_overwrite_recovered_evidence(self) -> None:
        import click

        reached = Event()
        resume = Event()
        self.addCleanup(resume.set)
        verify = promotion_workflow._verify_health_evidence_with_identity

        def stalled_check(evidence: Any, **kwargs: Any) -> Any:
            if current_thread().name == "paused-original-promotion":
                reached.set()
                if not resume.wait(30):
                    raise AssertionError("recovery never released paused worker")
                raise click.ClickException("Late worker health failure")
            return verify(evidence, **kwargs)

        with (
            patch.object(
                promotion_workflow,
                "_verify_health_evidence_with_identity",
                side_effect=stalled_check,
            ),
            patch(
                "control_plane.provider_operations._ReservationHeartbeat._run", return_value=None
            ),
        ):
            worker = Thread(target=self.case.advance, name="paused-original-promotion")
            worker.start()
            self.assertTrue(reached.wait(30))
            held = self.reservation()
            expired = parse_launchplane_mutation_timestamp(
                held.lease_expires_at, field_name="lease_expires_at"
            ) + timedelta(seconds=1)
            with patch.object(
                self.store, "_database_mutation_timestamp", return_value=expired.isoformat()
            ):
                plan = self.dry_run()
                self.assertEqual(plan["proposed_action"], "adopt_promotion")
                self.assertEqual(self.apply(plan)[0], 202)
            before = self.snapshot()
            resume.set()
            worker.join(30)
            self.assertFalse(worker.is_alive())
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(
            self.provider.deployed_artifacts, [self.decision.checklist.candidate.artifact_id]
        )

    def test_ambiguous_promotion_and_unavailable_provider_remain_held(self) -> None:
        import click

        self.interrupt_completion()
        with patch.object(
            self.provider,
            "observe_runtime_artifact",
            side_effect=click.ClickException("Unavailable"),
        ):
            plan = self.dry_run()
            self.assertEqual(plan["proposed_action"], "hold_unknown")
            self.assertEqual(self.apply(plan)[0], 409)
        promotion = self.store.list_promotion_records()[0]
        self.store.write_promotion_record(
            promotion.model_copy(update={"record_id": "ambiguous-promotion"})
        )
        plan = self.dry_run()
        self.assertEqual(plan["proposed_action"], "hold_unknown")
        self.assertEqual(self.apply(plan)[0], 409)
        self.assertEqual(self.reservation().state, "reconcile_required")

    def test_read_grant_cannot_apply_and_cross_product_reference_fails(self) -> None:
        self.interrupt_completion()
        plan = self.dry_run()
        self.path = self.path.replace(self.case.profile.product, "other-product")
        self.assertEqual(self.apply(plan)[0], 404)
        self.path = self.path.replace("other-product", self.case.profile.product)
        self.app = _create_recovery_app(
            root=self.case.root,
            store=self.store,
            actions=("product_environment.read",),
            contexts=(self.case.context,),
        )
        self.assertEqual(self.apply(plan)[0], 403)
        self.assertEqual(self.reservation().state, "reconcile_required")
