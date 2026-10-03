import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from collections.abc import Callable
from typing import Literal
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
from control_plane.contracts.odoo_prod_promotion_operation import OdooProdPromotionRunResult
from control_plane.contracts.odoo_prod_rollback_operation import OdooProdRollbackResult
from control_plane.contracts.production_backup_authority import ProductionBackupPolicyRecord
from control_plane.contracts.production_backup_gate import ProductionBackupGateWorkerRequest
from control_plane.contracts.verireel_prod_backup_gate import VeriReelProdBackupGateResult
from control_plane.contracts.product_profile_record import ReleaseOnAcceptance
from control_plane.contracts.release_review import ReleaseReviewDecisionRecord, ReleaseStart
from control_plane.contracts.release_tuple_record import ReleaseTupleRecord
from control_plane.durable_operation_authorization import (
    DurableOperationAuthorizationDeniedError,
    DurableOperationAuthorizationGuard,
)
from control_plane.release_review import build_release_review
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.odoo_prod_promotion_inputs import OdooProdPromotionInputsResult
from control_plane.workflows.odoo_prod_promotion_run import OdooProdPromotionRunAdmission
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

    def test_nothing_starts_when_held_prelaunch_overridden_or_not_odoo(self) -> None:
        self.assertEqual(self.accept().release_start, "")
        self.assertEqual(self.advance(), ())
        self.switch("promote")
        current = self.store.read_product_profile_record(PRODUCT)
        for update in ({"production_use": "prelaunch"}, {"driver_id": "generic-web"}):
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
