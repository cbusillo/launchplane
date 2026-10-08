import unittest
from contextlib import ExitStack, nullcontext

import click
from pathlib import Path
from tempfile import TemporaryDirectory
from collections.abc import Callable
from typing import Any, Literal, cast
from unittest.mock import patch

from control_plane.client_release import (
    advance_client_releases,
    client_release_grant,
    client_release_grant_allows,
    client_release_step_operation_id,
    client_release_steps,
    read_client_release_run,
    release_start_for_acceptance,
)
from control_plane.contracts.product_environment_read_model import build_product_activity_read_model
from control_plane.contracts.promotion_record import (
    ArtifactIdentityReference,
    DeploymentEvidence,
    HealthcheckEvidence,
    PostDeployUpdateEvidence,
)
from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.durable_operation_authorization import (
    DurableOperationAuthorization,
    DurableOperationCallerIdentity,
)
from control_plane.contracts.odoo_prod_promotion_operation import (
    OdooProdPromotionRunResult,
    OdooProdPromotionOperationRecord,
)
from control_plane.contracts.odoo_prod_rollback_operation import (
    OdooProdRollbackResult,
    OdooProdRollbackOperationRecord,
)
from control_plane.contracts.production_backup_authority import ProductionBackupPolicyRecord
from control_plane.contracts.production_backup_gate import ProductionBackupGateWorkerRequest
from control_plane.contracts.verireel_prod_backup_gate import VeriReelProdBackupGateResult
from control_plane.contracts.product_profile_record import ReleaseOnAcceptance
from control_plane.contracts.release_review import ReleaseReviewDecisionRecord, ReleaseStart
from control_plane.contracts.backup_gate_record import BackupGateRecord
from control_plane.contracts.odoo_stable_target_replacement import (
    OdooStableTargetReplacementApplyResult,
)
from control_plane.odoo_release_recovery import (
    odoo_release_recovery_allows,
    odoo_release_recovery_source,
)
from control_plane.workflows.odoo_stable_operation_worker import (
    run_odoo_stable_operation_worker_once,
    OdooStableOperationWorkerResult,
)
from control_plane.workflows.odoo_stable_target_replacement import (
    TARGET_REPLACEMENT_FIRST_PROVIDER_WRITE,
)
from control_plane.dokploy import DokployTargetDefinition
from control_plane.dokploy.api import DokployScheduleExecutionFailed
from control_plane.workflows.odoo_post_deploy import execute_odoo_post_deploy, OdooPostDeployRequest
from control_plane.workflows.production_promotion_backup import ProductionPromotionBackupGuard
from control_plane.workflows.odoo_prod_backup_gate import (
    BACKUP_GATE_SOURCE,
    OdooProdBackupGateResult,
)
from control_plane.contracts.release_tuple_record import ReleaseTupleRecord
from control_plane.durable_operation_authorization import (
    DurableOperationAuthorizationDeniedError,
    DurableOperationAuthorizationGuard,
)
from control_plane.release_review import build_release_review
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.odoo_prod_promotion_inputs import OdooProdPromotionInputsResult
from control_plane.contracts.odoo_prod_promotion_operation import OdooProdPromotionRunRequest
from control_plane.workflows.odoo_prod_promotion_run import (
    OdooProdPromotionRunAdmission,
    OdooProdPromotionRunStore,
    admit_odoo_prod_promotion_run,
)
from tests.test_production_backup_provider import _binding
from tests.test_release_review import BASE, HEAD, decision, github_read, profile, seed

PRODUCT = "example-site"
CONTEXT = "example-site"
StepStatus = Literal["pass", "fail"]


def _deployment(record_id: str, artifact_id: str, *, passed: bool = True) -> DeploymentRecord:
    status: Literal["pass", "fail"] = "pass" if passed else "fail"
    return DeploymentRecord(
        record_id=record_id,
        artifact_identity=ArtifactIdentityReference(artifact_id=artifact_id),
        context=CONTEXT,
        instance="prod",
        source_git_ref=BASE,
        deploy=DeploymentEvidence(
            target_name="example-prod",
            target_type="compose",
            deploy_mode="dokploy-compose-api",
            deployment_id="control-plane-dokploy",
            status="pass",
        ),
        post_deploy_update=PostDeployUpdateEvidence(attempted=True, status=status),
        destination_health=HealthcheckEvidence(status="skipped"),
    )


def _backup_binding(store: object, request: object) -> object:
    """The test binding, moved to the example product's scope."""

    binding = _binding()
    policy = binding.policy.model_dump(mode="json")
    for derived in ("policy_id", "record_id", "policy_digest"):
        policy.pop(derived)
    policy.update(
        product=PRODUCT,
        context=CONTEXT,
        promotion_action=getattr(request, "promotion_action"),
    )
    return ProductionBackupGateWorkerRequest(
        request=request,  # type: ignore[arg-type]
        policy=ProductionBackupPolicyRecord.model_validate(policy),
        source_target=binding.source_target,
        destination_target=binding.destination_target,
    )


class ClientReleaseTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = PostgresRecordStore(
            database_url=f"sqlite+pysqlite:///{self.root / 'state.sqlite3'}"
        )
        self.addCleanup(self.store.close)
        self.store.ensure_schema()
        seed(self.store)
        self.store.write_deployment_record(_deployment("deployment-prod-1", "artifact-prod"))
        self.review_read = github_read
        for target, replacement in (
            (
                "control_plane.client_release.current_release_review",
                lambda **kwargs: build_release_review(
                    store=self.store, profile=kwargs["profile"], read=self.review_read
                ),
            ),
            (
                "control_plane.client_release.admit_odoo_prod_promotion_run",
                self._admission,
            ),
            (
                "control_plane.client_release.resolve_odoo_prod_rollback_target",
                lambda **kwargs: None,
            ),
            (
                "control_plane.workflows.production_backup_gate.resolve_production_backup_binding",
                _backup_binding,
            ),
        ):
            replacement_patch = patch(target, side_effect=replacement)
            replacement_patch.start()
            self.addCleanup(replacement_patch.stop)

    def _admission(self, **kwargs: object) -> OdooProdPromotionRunAdmission:
        testing = self.store.read_release_tuple_record(context_name=CONTEXT, channel_name="testing")
        return OdooProdPromotionRunAdmission(
            inputs_result=OdooProdPromotionInputsResult(
                context=CONTEXT,
                from_instance="testing",
                to_instance="prod",
                request_id="admission",
                input_status="ready",
                artifact_id=testing.artifact_id,
            )
        )

    def switch(self, mode: ReleaseOnAcceptance) -> None:
        current = self.store.read_product_profile_record(PRODUCT)
        self.store.write_product_profile_record(
            current.model_copy(update={"release_on_acceptance": mode})
        )

    def accept(self, *, date: str = "2026-09-23T01:00:00Z") -> ReleaseReviewDecisionRecord:
        accepted = decision(self.store, date=date).model_copy(
            update={
                "release_start": release_start_for_acceptance(
                    store=self.store, profile=self.store.read_product_profile_record(PRODUCT)
                )
            }
        )
        self.store.write_release_review_decision_record(accepted)
        return accepted

    def advance(self) -> tuple[str, ...]:
        return advance_client_releases(store=self.store, control_plane_root=self.root)

    def set_prod(self, artifact_id: str) -> None:
        self.store.write_release_tuple_record(
            ReleaseTupleRecord(
                tuple_id=f"tuple-prod-{artifact_id}",
                context=CONTEXT,
                channel="prod",
                artifact_id=artifact_id,
                repo_shas={"example/site": BASE if artifact_id == "artifact-prod" else HEAD},
                provenance="ship",
                minted_at="2026-09-23T03:00:00Z",
            )
        )

    def finish(self, operation_id: str, status: StepStatus = "pass") -> None:
        finished = {"status": status, "finished_at": "2026-09-23T04:00:00Z"}
        error = {} if status == "pass" else {"error_message": "failed", "error_code": "failed"}
        if operation_id.startswith("production-backup-gate-"):
            backup = self.store.read_verireel_prod_backup_gate_operation_record(operation_id)
            self.store.write_verireel_prod_backup_gate_operation_record(
                backup.model_validate(
                    {
                        **backup.model_dump(mode="json"),
                        **finished,
                        **error,
                        "result": VeriReelProdBackupGateResult(
                            backup_record_id=backup.backup_record_id, backup_status=status
                        ).model_dump(mode="json"),
                    }
                )
            )
            return
        terminal = {
            **finished,
            **error,
            "phase": "completed" if status == "pass" else "failed",
        }
        try:
            promotion = self.store.read_odoo_prod_promotion_operation_record(operation_id)
        except FileNotFoundError:
            rollback = self.store.read_odoo_prod_rollback_operation_record(operation_id)
            self.store.write_odoo_prod_rollback_operation_record(
                rollback.model_validate(
                    {
                        **rollback.model_dump(mode="json"),
                        **terminal,
                        "result": OdooProdRollbackResult(
                            context=CONTEXT,
                            instance="prod",
                            source_channel="previous-deployment",
                            artifact_id=rollback.target.artifact_id,
                            promotion_record_id="promotion-1",
                            rollback_status=status,
                        ).model_dump(mode="json"),
                    }
                )
            )
            return
        self.store.write_odoo_prod_promotion_operation_record(
            promotion.model_validate(
                {
                    **promotion.model_dump(mode="json"),
                    **terminal,
                    "result": OdooProdPromotionRunResult(
                        context=CONTEXT,
                        from_instance="testing",
                        to_instance="prod",
                        request_id=promotion.request.request_id,
                        run_status=status,
                        input_status="ready",
                    ).model_dump(mode="json"),
                }
            )
        )

    def test_client_acceptance_promotes_runs_the_drill_and_repromotes_the_same_artifact(
        self,
    ) -> None:
        # A newer passing deployment of another artifact must not become the drill target.
        self.store.write_deployment_record(_deployment("deployment-prod-2", "artifact-other"))
        self.switch("promote_with_rollback_drill")
        accepted = self.accept()
        self.assertEqual(accepted.release_start, "promote_with_rollback_drill")

        (backup_id,) = self.advance()
        self.assertEqual(self.advance(), (), "a queued step is never queued twice")
        backup = self.store.read_verireel_prod_backup_gate_operation_record(backup_id)
        assert backup.authorization is not None
        self.assertEqual(backup.authorization.grant, "client_release_acceptance")
        self.assertEqual(backup.authorization.release_decision_record_id, accepted.record_id)
        self.finish(backup_id)

        (promotion_id,) = self.advance()
        promotion = self.store.read_odoo_prod_promotion_operation_record(promotion_id)
        self.assertEqual(promotion.request.infrastructure_backup_record_id, backup.backup_record_id)
        self.assertEqual(
            promotion.request.expected_artifact_id, accepted.checklist.candidate.artifact_id
        )
        self.finish(promotion_id)
        self.set_prod("artifact-testing")
        self.store.write_deployment_record(_deployment("deployment-prod-3", "artifact-testing"))

        (rollback_id,) = self.advance()
        rollback = self.store.read_odoo_prod_rollback_operation_record(rollback_id)
        self.assertEqual(rollback.target.artifact_id, accepted.checklist.production.artifact_id)
        self.assertEqual(rollback.target.deployment_record_id, "deployment-prod-1")
        self.finish(rollback_id)
        self.set_prod("artifact-prod")

        (second_backup_id,) = self.advance()
        self.assertNotEqual(second_backup_id, backup_id)
        self.finish(second_backup_id)
        (repromotion_id,) = self.advance()
        self.assertNotEqual(repromotion_id, promotion_id)
        self.finish(repromotion_id)
        self.set_prod("artifact-testing")
        self.assertEqual(self.advance(), ())

        profile_record = self.store.read_product_profile_record(PRODUCT)
        run = read_client_release_run(store=self.store, profile=profile_record, decision=accepted)
        assert run is not None
        self.assertEqual(run.state, "passed")
        self.assertEqual(len(run.steps), 5)
        # The product drilled once; its next acceptance only promotes.
        self.assertEqual(
            release_start_for_acceptance(store=self.store, profile=profile_record), "promote"
        )

    def test_nothing_starts_when_held_prelaunch_overridden_or_unsupported(self) -> None:
        self.assertEqual(self.accept().release_start, "")
        self.assertEqual(self.advance(), ())
        self.switch("promote")
        current = self.store.read_product_profile_record(PRODUCT)
        for update in ({"production_use": "prelaunch"}, {"driver_id": "unsupported"}):
            with self.subTest(update=update):
                self.assertEqual(
                    release_start_for_acceptance(
                        store=self.store, profile=current.model_copy(update=update)
                    ),
                    "",
                )
        self.store.write_release_review_decision_record(
            decision(self.store, outcome="overridden", date="2026-09-23T02:00:00Z")
        )
        self.assertEqual(self.advance(), ())

    def test_a_stale_acceptance_starts_nothing(self) -> None:
        self.switch("promote")
        self.accept()
        cases: dict[str, Callable[[], object]] = {
            "a later testing build": lambda: self.store.write_release_tuple_record(
                self.store.read_release_tuple_record(
                    context_name=CONTEXT, channel_name="testing"
                ).model_copy(update={"artifact_id": "artifact-newer"})
            ),
            "changed test notes": lambda: setattr(
                self,
                "review_read",
                lambda path: (
                    github_read(path)
                    if "/compare/" in path
                    else [{**github_read(path)[0], "body": "## Owner test notes\nNew."}]  # type: ignore[index]
                ),
            ),
            "a newer decision": lambda: self.store.write_release_review_decision_record(
                decision(self.store, outcome="changes_requested", date="2026-09-23T02:00:00Z")
            ),
            "held releases": lambda: self.switch("held"),
        }
        for label, make_stale in cases.items():
            with self.subTest(label=label):
                self.setUp()
                self.switch("promote")
                self.accept()
                make_stale()
                self.assertEqual(self.advance(), ())

    def test_a_failed_step_stops_the_release(self) -> None:
        self.switch("promote")
        accepted = self.accept()
        (backup_id,) = self.advance()
        self.finish(backup_id, "fail")
        self.assertEqual(self.advance(), ())
        run = read_client_release_run(
            store=self.store,
            profile=self.store.read_product_profile_record(PRODUCT),
            decision=accepted,
        )
        assert run is not None
        self.assertEqual(run.state, "stopped")

    def test_failed_backup_reason_and_trace_reach_release_and_activity_reads(self) -> None:
        self.switch("promote")
        accepted = self.accept()
        (operation_id,) = self.advance()
        self.finish(operation_id, "fail")
        operation = self.store.read_verireel_prod_backup_gate_operation_record(operation_id)
        self.store.write_verireel_prod_backup_gate_operation_record(
            operation.model_copy(
                update={
                    "error_code": "backup_failed",
                    "error_message": "Backup capture failed. password=do-not-publish https://private.invalid/log",
                }
            )
        )
        profile = self.store.read_product_profile_record(PRODUCT)
        run = read_client_release_run(store=self.store, profile=profile, decision=accepted)
        assert run is not None and run.steps[0].failure is not None
        failure = run.steps[0].failure
        self.assertEqual(failure.code, "backup_failed")
        self.assertIn("Backup capture failed.", failure.reason)
        self.assertEqual(failure.record_id, operation.backup_record_id)
        self.assertEqual(failure.trace_id, operation.runner_trace_id)
        self.assertTrue(failure.trace_id)
        self.assertNotIn("do-not-publish", run.model_dump_json())
        self.assertNotIn("private.invalid", run.model_dump_json())
        activity = build_product_activity_read_model(record_store=self.store, product=PRODUCT)
        event = next(
            event for event in activity.events if event.event_type == "client_release_step"
        )
        self.assertIn(failure.reason, event.summary)
        self.assertIn(failure.code, event.summary)
        ids = {link.record_id for link in event.records}
        self.assertTrue({operation_id, failure.record_id, failure.trace_id} <= ids)
        self.assertEqual(self.advance(), (), "reporting a failure never resumes the release")

    def test_rollback_failure_names_failed_deployment_and_legacy_missing_trace(self) -> None:
        self.switch("promote_with_rollback_drill")
        accepted = self.accept()
        (backup_id,) = self.advance()
        self.finish(backup_id)
        (promotion_id,) = self.advance()
        self.finish(promotion_id)
        self.set_prod("artifact-testing")
        (rollback_id,) = self.advance()
        self.finish(rollback_id, "fail")
        operation = self.store.read_odoo_prod_rollback_operation_record(rollback_id)
        self.assertTrue(operation.runner_trace_id)
        assert operation.result is not None
        reason = "Odoo post-deploy did not prove the requested website company sender was saved."
        self.store.write_odoo_prod_rollback_operation_record(
            operation.model_copy(
                update={
                    "runner_trace_id": "",
                    "error_code": "rollback_fail",
                    "error_message": reason,
                    "result": operation.result.model_copy(
                        update={"deployment_record_id": "failed-deployment"}
                    ),
                }
            )
        )
        run = read_client_release_run(
            store=self.store,
            profile=self.store.read_product_profile_record(PRODUCT),
            decision=accepted,
        )
        assert run is not None and run.steps[2].failure is not None
        self.assertEqual(run.steps[2].failure.reason, reason)
        self.assertEqual(run.steps[2].failure.record_id, "failed-deployment")
        self.assertEqual(run.steps[2].failure.trace_id, "")
        self.assertEqual(run.steps[3].status, "not_started")
        # A failed redeploy that never made a deployment must not name the
        # passing promotion that supplied the rollback target as its failure.
        self.store.write_odoo_prod_rollback_operation_record(
            self.store.read_odoo_prod_rollback_operation_record(rollback_id).model_copy(
                update={
                    "result": operation.result.model_copy(update={"deployment_record_id": ""}),
                }
            )
        )
        run = read_client_release_run(
            store=self.store,
            profile=self.store.read_product_profile_record(PRODUCT),
            decision=accepted,
        )
        assert run is not None and run.steps[2].failure is not None
        self.assertEqual(run.steps[2].failure.record_id, rollback_id)

    def test_failed_promotion_does_not_name_its_passing_backup_as_failure(self) -> None:
        self.switch("promote")
        accepted = self.accept()
        (backup_id,) = self.advance()
        self.finish(backup_id)
        (promotion_id,) = self.advance()
        self.finish(promotion_id, "fail")
        operation = self.store.read_odoo_prod_promotion_operation_record(promotion_id)
        assert operation.result is not None
        self.store.write_odoo_prod_promotion_operation_record(
            operation.model_copy(
                update={
                    "result": operation.result.model_copy(
                        update={
                            "backup_record_id": "passing-backup",
                            "promotion_record_id": "passing-promotion",
                        }
                    ),
                }
            )
        )
        run = read_client_release_run(
            store=self.store,
            profile=self.store.read_product_profile_record(PRODUCT),
            decision=accepted,
        )
        assert run is not None and run.steps[1].failure is not None
        self.assertEqual(run.steps[1].failure.record_id, promotion_id)

    def test_activity_history_does_not_run_unstarted_release_readiness_checks(self) -> None:
        self.switch("promote_with_rollback_drill")
        accepted = self.accept()
        for index in range(3):
            self.store.write_release_review_decision_record(
                accepted.model_copy(
                    update={
                        "record_id": f"historical-unstarted-release-{index}",
                    }
                )
            )
        with patch(
            "control_plane.client_release.pin_odoo_release_recovery_target",
            side_effect=AssertionError("Activity must not scan recovery candidates"),
        ):
            activity = build_product_activity_read_model(record_store=self.store, product=PRODUCT)
        self.assertFalse(
            any(event.event_type == "client_release_step" for event in activity.events)
        )

    def test_the_worker_recheck_follows_the_decision_client_and_hold(self) -> None:
        self.switch("promote")
        accepted = self.accept()
        grant = client_release_grant(
            decision=accepted,
            action="odoo_prod_promotion_run.execute",
            context=CONTEXT,
            authorized_at="2026-09-23T03:00:00Z",
        )
        self.assertTrue(client_release_grant_allows(self.store, grant))
        self.assertFalse(
            client_release_grant_allows(
                self.store, grant.model_copy(update={"release_decision_record_id": "other"})
            )
        )
        current = self.store.read_product_profile_record(PRODUCT)
        self.store.write_product_profile_record(
            current.model_copy(
                update={"owner": current.owner.model_copy(update={"github_id": "4242"})}
            )
        )
        self.assertFalse(client_release_grant_allows(self.store, grant))
        self.store.write_product_profile_record(current)
        self.assertTrue(client_release_grant_allows(self.store, grant))
        # A later testing build stops every step, the drill's rollback included.
        testing = self.store.read_release_tuple_record(context_name=CONTEXT, channel_name="testing")
        self.store.write_release_tuple_record(
            testing.model_copy(update={"artifact_id": "artifact-b"})
        )
        self.assertFalse(client_release_grant_allows(self.store, grant))
        self.store.write_release_tuple_record(testing)
        self.store.write_product_profile_record(
            current.model_copy(update={"release_on_acceptance": "held"})
        )
        self.assertFalse(client_release_grant_allows(self.store, grant))

        # Only a guard a Client release path builds accepts the grant at all.
        with self.assertRaises(DurableOperationAuthorizationDeniedError) as denied:
            DurableOperationAuthorizationGuard(
                authorization=grant,
                policy_record_reader=lambda: None,  # type: ignore[arg-type,return-value]
            ).authorize_execution()
        self.assertEqual(denied.exception.code, "operation_authorization_client_release_refused")

    def test_the_grant_names_its_decision_and_carries_no_policy_rule(self) -> None:
        self.switch("promote")
        grant = client_release_grant(
            decision=self.accept(),
            action="odoo_prod_promotion_run.execute",
            context=CONTEXT,
            authorized_at="2026-09-23T03:00:00Z",
        )
        payload = grant.model_dump(mode="json")
        for invalid in (
            {"release_decision_record_id": ""},
            {"policy_record_id": "policy-1"},
            {"caller": {**payload["caller"], "role": "admin"}},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                DurableOperationAuthorization.model_validate({**payload, **invalid})
        with self.assertRaises(ValueError):
            DurableOperationAuthorization.model_validate(
                {**payload, "grant": "policy_administrator"}
            )
        with self.assertRaises(ValueError):
            DurableOperationCallerIdentity(identity_type="github_human", login="x", github_id=1)

    def queued_promotion(
        self,
    ) -> tuple[ReleaseReviewDecisionRecord, OdooProdPromotionOperationRecord]:
        self.switch("promote_with_rollback_drill")
        accepted = self.accept()
        (backup_id,) = self.advance()
        self.finish(backup_id)
        (operation_id,) = self.advance()
        return accepted, self.store.read_odoo_prod_promotion_operation_record(operation_id)

    def run_release_worker(self) -> OdooStableOperationWorkerResult:
        result = run_odoo_stable_operation_worker_once(
            record_store=self.store, control_plane_root_path=self.root, lease_owner="release-worker"
        )
        self.assertTrue(result.terminal_write_committed)
        return result

    def test_source_read_outage_does_not_stop_accepted_promotion_or_queue_recovery(self) -> None:
        from control_plane.lane_movement import LaneMovementRefused

        accepted, source = self.queued_promotion()
        with patch(
            "control_plane.workflows.odoo_stable_operation_worker.execute_odoo_prod_promotion_run",
            side_effect=LaneMovementRefused("source_order_unavailable"),
        ) as execute:
            self.run_release_worker()
            for _ in range(3):
                result = run_odoo_stable_operation_worker_once(
                    record_store=self.store,
                    control_plane_root_path=self.root,
                    lease_owner="release-worker",
                )
                self.assertEqual(result.status, "idle")
                operation = self.store.read_odoo_prod_promotion_operation_record(
                    source.operation_id
                )
                self.assertEqual(operation.status, "pending")
                self.assertEqual(operation.error_code, "lane_movement.source_order_unavailable")
                run = read_client_release_run(
                    store=self.store,
                    profile=self.store.read_product_profile_record(PRODUCT),
                    decision=accepted,
                )
                assert run is not None
                self.assertNotEqual(run.state, "stopped")
                self.assertEqual(self.store.list_odoo_prod_rollback_operation_records(), ())
            execute.assert_called_once()

    def promotion_providers(
        self, *, failure: str = "post_deploy", after_write: Callable[[], None] | None = None
    ) -> ExitStack:
        # Run the real production workflow and promotion record writes; replace
        # remote backups/deploys with deterministic provider effects.
        stack = ExitStack()
        self.addCleanup(stack.close)
        from tests.support.promotion_backup import stub_verified_promotion_backup

        stub_verified_promotion_backup(self, "control_plane.workflows.odoo_prod_promotion_run")
        stub_verified_promotion_backup(self, "control_plane.workflows.odoo_prod_promotion")
        for module in ("odoo_prod_promotion_run", "odoo_prod_promotion"):
            stack.enter_context(patch(f"control_plane.workflows.{module}.require_release_approval"))

        def inputs(**kwargs: Any) -> OdooProdPromotionInputsResult:
            return self._admission(**kwargs).inputs_result.model_copy(
                update={
                    "backup_record_id": "logical-backup",
                    "source_git_ref": HEAD,
                }
            )

        stack.enter_context(
            patch(
                "control_plane.workflows.odoo_prod_promotion_run.resolve_odoo_prod_promotion_inputs",
                side_effect=inputs,
            )
        )

        def backup(**kwargs: Any) -> OdooProdBackupGateResult:
            self.store.write_backup_gate_record(
                BackupGateRecord(
                    record_id="logical-backup",
                    context=CONTEXT,
                    instance="prod",
                    source=BACKUP_GATE_SOURCE,
                    status="pass",
                    created_at="2026-09-23T03:00:00Z",
                    evidence={"snapshot": "verified-logical-backup"},
                )
            )
            return OdooProdBackupGateResult(
                context=CONTEXT,
                instance="prod",
                backup_record_id="logical-backup",
                backup_status="pass",
            )

        stack.enter_context(
            patch(
                "control_plane.workflows.odoo_prod_promotion_run.execute_odoo_prod_backup_gate",
                side_effect=backup,
            )
        )

        def replacement(**kwargs: Any) -> OdooStableTargetReplacementApplyResult:
            checkpoint = kwargs["provider_effect_checkpoint"]
            if failure == "pre_write":
                raise click.ClickException("Preparation refused before any write.")
            checkpoint(TARGET_REPLACEMENT_FIRST_PROVIDER_WRITE)
            if after_write:
                after_write()
            if failure == "unknown":
                raise TimeoutError("The deployment response was lost.")
            if failure in {"post_deploy", "post_deploy_timeout"}:

                def remote_command(**provider_kwargs: Any) -> dict[str, str]:
                    provider_kwargs["before_provider_mutation"]("post_deploy_schedule_trigger")
                    raise DokployScheduleExecutionFailed(
                        schedule_id="module-upgrade",
                        deployment_id="failed-upgrade",
                        deployment_status="failed" if failure == "post_deploy" else "running",
                        cause="remote_command_exit"
                        if failure == "post_deploy"
                        else "execution_timeout",
                    )

                with (
                    patch(
                        "control_plane.workflows.odoo_post_deploy._resolve_compose_target_definition",
                        return_value=DokployTargetDefinition(
                            context=CONTEXT,
                            instance="prod",
                            target_type="compose",
                            target_id="example-prod",
                            target_name="example-prod",
                        ),
                    ),
                    patch(
                        "control_plane.workflows.odoo_post_deploy.dokploy_source.read_dokploy_config",
                        return_value=("https://provider.example", "test-token"),
                    ),
                    patch(
                        "control_plane.workflows.odoo_post_deploy.dokploy_post_deploy.run_compose_post_deploy_update",
                        side_effect=remote_command,
                    ),
                ):
                    post_deploy = execute_odoo_post_deploy(
                        control_plane_root=self.root,
                        record_store=self.store,
                        request=OdooPostDeployRequest(
                            context=CONTEXT, instance="prod", phase="deploy"
                        ),
                        provider_effect_checkpoint=checkpoint,
                        hold_uncertain_effects=kwargs["hold_uncertain_effects"],
                    )
                self.assertEqual(post_deploy.post_deploy_status, "fail")
            deployment = _deployment(
                "failed-candidate-deployment", "artifact-testing", passed=False
            )
            if failure == "health":
                deployment = deployment.model_copy(
                    update={
                        "post_deploy_update": PostDeployUpdateEvidence(
                            attempted=True, status="pass"
                        ),
                        "destination_health": HealthcheckEvidence(status="fail"),
                    }
                )
            self.store.write_deployment_record(deployment)
            return OdooStableTargetReplacementApplyResult(
                product=PRODUCT,
                context=CONTEXT,
                instance="prod",
                strategy="recreate-in-place",
                artifact_id="artifact-testing",
                deployment_record_id=deployment.record_id,
                deploy_status="fail",
                post_deploy_status=cast(
                    Literal["pass", "fail", "skipped"], deployment.post_deploy_update.status
                ),
                health_status=cast(
                    Literal["pass", "fail", "skipped"], deployment.destination_health.status
                ),
                error_message="Candidate verification failed.",
            )

        stack.enter_context(
            patch(
                "control_plane.workflows.odoo_prod_promotion.execute_odoo_stable_target_replacement_apply",
                side_effect=replacement,
            )
        )
        return stack

    def test_failed_release_recovers_pinned_baseline_and_never_enters_the_drill(self) -> None:
        for failure in ("post_deploy", "health"):
            with self.subTest(failure=failure):
                self.setUp()
                accepted, source = self.queued_promotion()
                with self.promotion_providers(failure=failure):
                    self.run_release_worker()
                failed = self.store.read_odoo_prod_promotion_operation_record(source.operation_id)
                self.assertEqual(failed.status, "fail")
                assert failed.result is not None
                (recovery,) = self.store.list_odoo_prod_rollback_operation_records()
                self.assertEqual(
                    recovery.target.artifact_id, accepted.checklist.production.artifact_id
                )
                self.assertEqual(recovery.target.deployment_record_id, "deployment-prod-1")
                self.assertEqual(
                    recovery.request.promotion_record_id, failed.result.promotion_record_id
                )
                self.assertEqual(odoo_release_recovery_source(recovery), source.operation_id)
                self.assertTrue(odoo_release_recovery_allows(self.store, recovery))
                self.assertEqual(self.advance(), ())
                self.store.write_deployment_record(
                    _deployment("newer-passing-deployment", "artifact-other")
                )

                def recovered(**kwargs: Any) -> OdooStableTargetReplacementApplyResult:
                    self.assertEqual(kwargs["request"].artifact_id, recovery.target.artifact_id)
                    self.assertEqual(kwargs["request"].data_source_mode, "existing")
                    kwargs["provider_effect_checkpoint"](TARGET_REPLACEMENT_FIRST_PROVIDER_WRITE)
                    deployment = _deployment(
                        "recovered-deployment", recovery.target.artifact_id
                    ).model_copy(
                        update={
                            "post_deploy_update": PostDeployUpdateEvidence(
                                attempted=True, status="pass"
                            ),
                            "destination_health": HealthcheckEvidence(
                                status="pass",
                                verified=True,
                                urls=("https://example.prod/health",),
                                timeout_seconds=30,
                            ),
                        }
                    )
                    self.store.write_deployment_record(deployment)
                    self.set_prod(recovery.target.artifact_id)
                    return OdooStableTargetReplacementApplyResult(
                        product=PRODUCT,
                        context=CONTEXT,
                        instance="prod",
                        strategy="recreate-in-place",
                        artifact_id=recovery.target.artifact_id,
                        deployment_record_id=deployment.record_id,
                        deploy_status="pass",
                        post_deploy_status="pass",
                        health_status="pass",
                        canonical_status="pass",
                        logo_status="pass",
                    )

                with patch(
                    "control_plane.workflows.odoo_prod_rollback.execute_odoo_stable_target_replacement_apply",
                    side_effect=recovered,
                ):
                    self.run_release_worker()
                done = self.store.read_odoo_prod_rollback_operation_record(recovery.operation_id)
                self.assertEqual(done.status, "pass")
                assert done.result is not None
                self.assertEqual(done.result.artifact_id, recovery.target.artifact_id)
                self.assertEqual(done.result.rollback_health_status, "pass")
                promotion = self.store.read_promotion_record(recovery.request.promotion_record_id)
                self.assertEqual(promotion.deploy.status, "fail")
                self.assertEqual(promotion.rollback.status, "pass")
                self.assertTrue(promotion.rollback_health.verified)
                self.assertEqual(self.advance(), ())
                run = read_client_release_run(
                    store=self.store,
                    profile=self.store.read_product_profile_record(PRODUCT),
                    decision=accepted,
                )
                assert run is not None
                self.assertEqual(run.state, "stopped")
                self.assertEqual(
                    next(step.status for step in run.steps if step.step == "failure-recovery-1"),
                    "pass",
                )
                self.assertEqual(
                    self.store.read_release_tuple_record(
                        context_name=CONTEXT, channel_name="prod"
                    ).artifact_id,
                    recovery.target.artifact_id,
                )

    def test_pre_write_failure_does_not_queue_recovery_and_unknown_effect_holds_the_lane(
        self,
    ) -> None:
        for failure, status in (
            ("pre_write", "fail"),
            ("unknown", "reconciliation_required"),
            ("post_deploy_timeout", "reconciliation_required"),
        ):
            with self.subTest(failure=failure):
                self.setUp()
                _, source = self.queued_promotion()
                with self.promotion_providers(failure=failure):
                    self.run_release_worker()
                finished = self.store.read_odoo_prod_promotion_operation_record(source.operation_id)
                self.assertEqual(finished.status, status)
                self.assertEqual(self.store.list_odoo_prod_rollback_operation_records(), ())
                self.assertEqual(self.advance(), ())
                if status == "reconciliation_required":
                    self.assertEqual(finished.lease_owner, "")
                    self.assertIsNone(finished.result)
                    persisted, created = (
                        self.store.create_odoo_prod_promotion_operation_record_if_no_active_lane(
                            source.model_copy(
                                update={
                                    "operation_id": "other-operation",
                                    "idempotency_key": "other",
                                }
                            )
                        )
                    )
                    self.assertFalse(created)
                    self.assertEqual(persisted.operation_id, source.operation_id)

    def test_recovery_retains_admitted_authority_after_forward_authority_changes(self) -> None:
        _, source = self.queued_promotion()

        def change_authority() -> None:
            self.switch("held")
            self.store.write_release_review_decision_record(
                decision(self.store, outcome="changes_requested", date="2026-09-23T05:00:00Z")
            )
            testing = self.store.read_release_tuple_record(
                context_name=CONTEXT, channel_name="testing"
            )
            self.store.write_release_tuple_record(
                testing.model_copy(update={"artifact_id": "new-testing"})
            )

        with self.promotion_providers(after_write=change_authority):
            self.run_release_worker()
        (recovery,) = self.store.list_odoo_prod_rollback_operation_records()
        self.assertFalse(client_release_grant_allows(self.store, recovery.authorization))
        self.assertTrue(odoo_release_recovery_allows(self.store, recovery))
        for update in (
            {"target": recovery.target.model_copy(update={"artifact_id": "new-testing"})},
            {
                "authorization": recovery.authorization.model_copy(
                    update={"release_decision_record_id": "another-decision"}
                )
            },
        ):
            self.assertFalse(
                odoo_release_recovery_allows(self.store, recovery.model_copy(update=update))
            )

        def rollback(**kwargs: Any) -> OdooProdRollbackResult:
            kwargs["provider_effect_checkpoint"](TARGET_REPLACEMENT_FIRST_PROVIDER_WRITE)
            return OdooProdRollbackResult(
                context=CONTEXT,
                instance="prod",
                source_channel="previous-deployment",
                artifact_id=recovery.target.artifact_id,
                promotion_record_id=recovery.request.promotion_record_id,
                rollback_status="fail",
                error_message="Recovery verification failed.",
            )

        with patch(
            "control_plane.workflows.odoo_stable_operation_worker.execute_odoo_prod_rollback",
            side_effect=rollback,
        ):
            self.run_release_worker()
        self.assertEqual(
            self.store.read_odoo_prod_rollback_operation_record(recovery.operation_id).status,
            "fail",
        )
        self.assertEqual(
            self.store.read_odoo_prod_promotion_operation_record(source.operation_id).status, "fail"
        )

    def test_recovery_timeout_stays_uncertain_and_is_not_replayed(self) -> None:
        _, source = self.queued_promotion()
        with self.promotion_providers():
            self.run_release_worker()
        (recovery,) = self.store.list_odoo_prod_rollback_operation_records()

        def rollback(**kwargs: Any) -> OdooProdRollbackResult:
            kwargs["provider_effect_checkpoint"](TARGET_REPLACEMENT_FIRST_PROVIDER_WRITE)
            raise TimeoutError("Recovery may still be deploying.")

        with patch(
            "control_plane.workflows.odoo_stable_operation_worker.execute_odoo_prod_rollback",
            side_effect=rollback,
        ):
            self.run_release_worker()
            self.assertEqual(
                run_odoo_stable_operation_worker_once(
                    record_store=self.store,
                    control_plane_root_path=self.root,
                    lease_owner="another-worker",
                ).operation_kind,
                "",
            )
        self.assertEqual(
            self.store.read_odoo_prod_rollback_operation_record(recovery.operation_id).status,
            "reconciliation_required",
        )
        self.assertEqual(
            self.store.read_odoo_prod_promotion_operation_record(source.operation_id).status, "fail"
        )

    def test_claim_and_pre_write_retry_preserve_the_pinned_recovery_binding(self) -> None:
        _, source = self.queued_promotion()
        claimed = self.store.claim_next_odoo_prod_promotion_operation_record(
            lease_owner="worker",
            claimed_at="2026-09-23T05:00:00Z",
            lease_expires_at="2026-09-23T05:01:00Z",
        )
        assert claimed is not None
        self.assertEqual(claimed.checkpoints[0].evidence, source.checkpoints[0].evidence)
        self.store.recover_expired_odoo_prod_promotion_operation_records(
            now="2026-09-23T05:02:00Z",
            safe_phases=("created", "running", "validated"),
            max_attempts=3,
        )
        claimed_again = self.store.claim_next_odoo_prod_promotion_operation_record(
            lease_owner="worker-2",
            claimed_at="2026-09-23T05:03:00Z",
            lease_expires_at="2026-09-23T05:04:00Z",
        )
        assert claimed_again is not None
        self.assertEqual(claimed_again.checkpoints[0].evidence, source.checkpoints[0].evidence)

    def test_failed_promotion_and_recovery_enqueue_commit_atomically(self) -> None:
        _, source = self.queued_promotion()
        original = self.store._release_operation_row

        def reject_recovery(row_type: Any, record: Any) -> Any:
            if isinstance(record, OdooProdRollbackOperationRecord):
                raise RuntimeError("Simulated recovery insert failure.")
            return original(row_type, record)

        with (
            self.promotion_providers(),
            patch.object(self.store, "_release_operation_row", side_effect=reject_recovery),
            self.assertRaisesRegex(RuntimeError, "insert failure"),
        ):
            self.run_release_worker()
        self.assertEqual(
            self.store.read_odoo_prod_promotion_operation_record(source.operation_id).status,
            "running",
        )
        self.assertEqual(self.store.list_odoo_prod_rollback_operation_records(), ())
        persisted, created = (
            self.store.create_odoo_prod_promotion_operation_record_if_no_active_lane(
                source.model_copy(
                    update={"operation_id": "other-operation", "idempotency_key": "other"}
                )
            )
        )
        self.assertFalse(created)
        self.assertEqual(persisted.operation_id, source.operation_id)

    def test_failure_on_repromotion_recovers_the_same_pinned_baseline(self) -> None:
        accepted, first = self.queued_promotion()
        self.finish(first.operation_id)
        self.set_prod(accepted.checklist.candidate.artifact_id)
        (drill_id,) = self.advance()
        self.finish(drill_id)
        self.set_prod(accepted.checklist.production.artifact_id)
        (backup_id,) = self.advance()
        self.finish(backup_id)
        (second_id,) = self.advance()
        with self.promotion_providers():
            self.run_release_worker()
        recoveries = [
            record
            for record in self.store.list_odoo_prod_rollback_operation_records()
            if odoo_release_recovery_source(record)
        ]
        self.assertEqual(len(recoveries), 1)
        self.assertEqual(odoo_release_recovery_source(recoveries[0]), second_id)
        self.assertEqual(
            recoveries[0].target.artifact_id, accepted.checklist.production.artifact_id
        )
        self.assertNotEqual(recoveries[0].operation_id, drill_id)
        self.assertEqual(self.advance(), ())

    def test_backup_guard_refusal_before_first_write_does_not_queue_recovery(self) -> None:
        _, source = self.queued_promotion()

        def backup_guard(**kwargs: Any) -> Any:
            self.store.write_promotion_record(kwargs["pending_promotion"])

            def refused(_phase: str) -> None:
                raise click.ClickException("Backup evidence became stale before the first write.")

            return nullcontext(ProductionPromotionBackupGuard(refused, {}))

        with (
            self.promotion_providers(),
            patch(
                "control_plane.workflows.odoo_prod_promotion.production_promotion_backup_guard",
                side_effect=backup_guard,
            ),
        ):
            self.run_release_worker()
        finished = self.store.read_odoo_prod_promotion_operation_record(source.operation_id)
        self.assertEqual(finished.status, "fail")
        self.assertFalse(
            any(
                checkpoint.evidence.get("production_write_started")
                for checkpoint in finished.checkpoints
            )
        )
        self.assertEqual(self.store.list_odoo_prod_rollback_operation_records(), ())

    def test_missing_passing_baseline_blocks_before_backup_and_explains_the_wait(self) -> None:
        self.store.write_deployment_record(
            _deployment("deployment-prod-1", "artifact-prod", passed=False)
        )
        self.switch("promote")
        accepted = self.accept()
        self.assertEqual(self.advance(), ())
        self.assertEqual(self.store.list_verireel_prod_backup_gate_operation_records(), ())
        run = read_client_release_run(
            store=self.store,
            profile=self.store.read_product_profile_record(PRODUCT),
            decision=accepted,
        )
        assert run is not None
        self.assertEqual(run.state, "waiting")
        self.assertTrue(run.blocked_reason)

    def test_connection_reset_inside_real_rollback_holds_the_recovery_lane(self) -> None:
        _, source = self.queued_promotion()
        with self.promotion_providers():
            self.run_release_worker()
        (recovery,) = self.store.list_odoo_prod_rollback_operation_records()

        def disconnected(**kwargs: Any) -> OdooStableTargetReplacementApplyResult:
            kwargs["provider_effect_checkpoint"](TARGET_REPLACEMENT_FIRST_PROVIDER_WRITE)
            raise ConnectionResetError("Provider accepted the write but disconnected.")

        with patch(
            "control_plane.workflows.odoo_prod_rollback.execute_odoo_stable_target_replacement_apply",
            side_effect=disconnected,
        ):
            self.run_release_worker()
        self.assertEqual(
            self.store.read_odoo_prod_rollback_operation_record(recovery.operation_id).status,
            "reconciliation_required",
        )
        self.assertEqual(
            self.store.read_promotion_record(recovery.request.promotion_record_id).rollback.status,
            "pending",
        )
        self.assertEqual(self.advance(), ())


class AcceptedArtifactAdmissionTests(unittest.TestCase):
    def test_a_run_for_an_accepted_artifact_blocks_when_testing_moved(self) -> None:
        inputs = OdooProdPromotionInputsResult(
            context=CONTEXT,
            from_instance="testing",
            to_instance="prod",
            request_id="release",
            input_status="ready",
            artifact_id="artifact-b",
        )
        with (
            patch(
                "control_plane.workflows.odoo_prod_promotion_run.resolve_odoo_prod_promotion_inputs",
                return_value=inputs,
            ),
            patch(
                "control_plane.workflows.odoo_prod_promotion_run.require_release_approval"
            ) as approval,
        ):
            admission = admit_odoo_prod_promotion_run(
                control_plane_root=Path("."),
                record_store=cast(OdooProdPromotionRunStore, object()),
                request=OdooProdPromotionRunRequest(
                    context=CONTEXT,
                    product=PRODUCT,
                    request_id="release",
                    expected_artifact_id="artifact-a",
                ),
            )
        self.assertIn("new decision", admission.blocked_reason)
        approval.assert_not_called()


class ReleaseStepTests(unittest.TestCase):
    def test_step_operation_ids_differ_per_decision_and_step(self) -> None:
        start: ReleaseStart = "promote_with_rollback_drill"
        steps = client_release_steps(start)
        accepted = ReleaseReviewDecisionRecord.model_construct(record_id="decision-a")
        other = ReleaseReviewDecisionRecord.model_construct(record_id="decision-b")
        ids = {
            client_release_step_operation_id(profile=profile(), decision=record, step=step)
            for record in (accepted, other)
            for step in steps
        }
        self.assertEqual(len(ids), 2 * len(steps))


if __name__ == "__main__":
    unittest.main()
