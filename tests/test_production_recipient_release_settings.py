import unittest
from collections.abc import Callable
from datetime import datetime
from functools import partial
from contextlib import nullcontext
from pathlib import Path
from typing import Any
from unittest.mock import patch

from control_plane import client_release as releases
from control_plane.client_release_backup_continuation import (
    BackupContinuationRefused,
    BackupContinuationReview,
    continue_release_backup,
)
from control_plane.contracts.deployment_record import DeploymentRecord, ResolvedTargetEvidence
from control_plane.contracts.promotion_record import ArtifactIdentityReference
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.release_review import ReleaseReviewDecisionRecord
from control_plane.production_recipient import custody
from control_plane.production_recipient_cutover import RecipientCutoverAdapter
from control_plane.dokploy.post_deploy import ODOO_POST_DEPLOY_SCHEDULE_TRIGGER
from control_plane.generic_web_promotion_http import GENERIC_WEB_PROD_PROMOTION_ROUTE
from control_plane.release_request import record_release_request
from control_plane.workflows.production_promotion_backup import GENERIC_WEB_PROMOTION_BACKUP_ACTION
from control_plane.release_review import (
    ReleaseEvidenceUnavailable,
    checklist_digest,
    current_release_review,
    release_version,
)
from tests import test_production_recipient_cutover as cutover_tests
from tests import test_production_recipient_release_recovery as recovery_tests
from tests import test_production_recipient_runtime as runtime_tests
from tests.test_generic_web_client_release import expired_backup_clock


class RecipientReleaseSettingsTests(unittest.TestCase):
    database_url = ""

    def setUp(self) -> None:
        seed = cutover_tests.RecipientCutoverTests.seed

        def seed_resource_settings(case: cutover_tests.RecipientCutoverTests) -> None:
            seed(case)
            seed_fixture = case.publication_case.fixture
            seed_inventory = case.publication_case.inventory
            seed_identity = seed_inventory.runtime_identity
            assert seed_identity is not None
            seed_inventory = seed_inventory.model_copy(
                update={
                    "artifact_identity": ArtifactIdentityReference(
                        artifact_id=seed_identity.image_reference
                    ),
                    "runtime_identity": seed_identity.model_copy(
                        update={"artifact_id": seed_identity.image_reference}
                    ),
                }
            )
            case.publication_case.inventory = seed_inventory
            seed_fixture.store.write_environment_inventory(seed_inventory)
            keys = set(seed_fixture.body.ports) | set(seed_fixture.body.volume_keys.values())
            seed_fixture.store.write_runtime_environment_record(
                RuntimeEnvironmentRecord(
                    scope="instance",
                    context=seed_fixture.context,
                    instance="prod",
                    env={key: seed_fixture.values[key] for key in keys},
                    updated_at=seed_fixture.old.updated_at,
                    source_label="seed_fixture",
                )
            )

        self.case = recovery_tests.RecipientReleaseRecoveryTests()
        self.case.database_url = self.database_url
        with patch.object(cutover_tests.RecipientCutoverTests, "seed", seed_resource_settings):
            self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.profile, self.testing = self.case.qualified_testing_source()
        fixture = self.case.fixture
        inventory = fixture.store.read_environment_inventory(
            context_name=fixture.record.context, instance_name="prod"
        )
        identity = inventory.runtime_identity
        assert identity is not None
        target = fixture.record.retained_provider
        fixture.store.write_deployment_record(
            DeploymentRecord(
                record_id=inventory.deployment_record_id,
                context=inventory.context,
                instance=inventory.instance,
                artifact_identity=ArtifactIdentityReference(artifact_id=identity.artifact_id),
                source_git_ref=inventory.source_git_ref,
                runtime_identity=identity,
                deploy=inventory.deploy,
                destination_health=inventory.destination_health,
                resolved_target=ResolvedTargetEvidence(
                    target_type="compose",
                    target_id=target.target_id,
                    target_name=target.display_name,
                ),
                deployed_target=target.to_deployed_target_reference(),
            )
        )
        self.enterContext(
            patch("control_plane.release_review.read_release_changes", return_value=((), ()))
        )
        review = current_release_review(
            record_store=fixture.store,
            control_plane_root=fixture.fixture.root,
            profile=self.profile,
        )
        assert review.checklist is not None
        checklist = review.checklist.model_copy(
            update={"candidate": self.case.decision.checklist.candidate}
        )
        self.case.decision = self.case.decision.model_copy(
            update={
                "checklist": checklist,
                "checklist_digest": checklist_digest(checklist),
                "decided_at": datetime.fromisoformat(fixture.record.qualified_at).isoformat(
                    timespec="microseconds"
                ),
            }
        )
        fixture.store.write_release_review_decision_record(self.case.decision)
        self.enterContext(
            patch(
                "control_plane.production_recipient_cutover.run_recipient_program",
                side_effect=self.runtime_program,
            )
        )
        self.enterContext(
            patch(
                "control_plane.production_recipient_inverse.writer_action",
                side_effect=fixture.writer,
            )
        )
        self.enterContext(
            patch(
                "control_plane.production_recipient_inverse._verify_health_evidence_with_identity",
                side_effect=fixture.health,
            )
        )
        self.enterContext(
            patch("socket.create_connection", side_effect=AssertionError("Unexpected network call"))
        )

    @staticmethod
    def runtime_program(
        _root: Path,
        _record: Any,
        spec: dict[str, Any],
        _program: str,
        checkpoint: Callable[[str], None],
        **_kwargs: Any,
    ) -> dict[str, str]:
        runtime = runtime_tests.RecipientRuntimeProgramTests()
        runtime.prepare_runtime(spec)
        checkpoint(ODOO_POST_DEPLOY_SCHEDULE_TRIGGER)
        return runtime.run_program()

    def advance(self) -> tuple[str, ...]:
        fixture = self.case.fixture
        operations = releases.advance_client_releases(
            store=fixture.store, control_plane_root=fixture.fixture.root
        )
        self.assertTrue(operations)
        return operations

    def capture_next_backup(self) -> str:
        fixture = self.case.fixture
        queued = self.advance()
        operation = fixture.store.read_verireel_prod_backup_gate_operation_record(queued[0])
        policy = next(
            row
            for row in fixture.store.list_production_backup_policy_records(
                product=fixture.record.product, status="active"
            )
            if row.promotion_action == GENERIC_WEB_PROMOTION_BACKUP_ACTION
        )
        with patch(
            "tests.test_production_recipient_cutover.enqueue_production_backup_gate",
            return_value=operation,
        ):
            fixture.capture(policy, operation.backup_record_id)
        return operation.backup_record_id

    def test_unarmed_other_candidate_retains_native_admission(self) -> None:
        fixture = self.case.fixture
        self.case.continue_recipient(source_commit="e" * 40)
        self.assertEqual(
            release_version(store=fixture.store, profile=self.profile, instance="testing"),
            self.case.decision.checklist.candidate,
        )
        with self.assertRaises(releases.ClientReleaseNotReady):
            releases._run_generic_web_promotion(
                fixture.store,
                fixture.fixture.root,
                self.profile,
                self.case.decision,
                releases.ClientReleaseStep("promote", 1),
                fixture.old_backup,
            )
        self.assertFalse(fixture.actions)

    def test_unavailable_custody_never_selects_ordinary_settings(self) -> None:
        fixture = self.case.fixture
        with patch(
            "control_plane.storage.postgres.PostgresRecordStore.read_production_recipient",
            side_effect=ValueError,
        ):
            with self.assertRaises(ReleaseEvidenceUnavailable):
                release_version(store=fixture.store, profile=self.profile, instance="testing")

    def test_absent_and_retired_custody_keep_ordinary_settings(self) -> None:
        fixture = self.case.fixture
        with patch(
            "control_plane.storage.postgres.PostgresRecordStore.read_production_recipient",
            return_value=None,
        ):
            missing = release_version(store=fixture.store, profile=self.profile, instance="testing")
        current = custody(fixture.store, fixture.record.product, fixture.record.context)
        assert current is not None
        fixture.store.write_production_recipient(
            expected=current.model_dump(mode="json"),
            replacement=current.model_copy(update={"phase": "retired"}).model_dump(mode="json"),
        )
        retired = release_version(store=fixture.store, profile=self.profile, instance="testing")
        self.assertIsNotNone(missing.runtime_settings)
        self.assertEqual(missing.runtime_settings, retired.runtime_settings)

    def test_native_drill_keeps_authority_through_publication_and_inverse(self) -> None:
        self.complete_native_drill()

    def complete_native_drill(self) -> None:
        fixture = self.case.fixture
        run = releases.read_client_release_run(
            store=fixture.store, profile=self.profile, decision=self.case.decision
        )
        assert run is not None
        self.assertFalse(run.blocked_reason)
        self.capture_next_backup()
        self.advance()
        initial_deploys = fixture.deploys
        self.assertEqual(
            release_version(store=fixture.store, profile=self.profile, instance="prod"),
            self.case.decision.checklist.candidate,
        )
        self.assertEqual(
            release_version(store=fixture.store, profile=self.profile, instance="testing"),
            self.case.decision.checklist.candidate,
        )
        self.advance()
        self.assertEqual(
            release_version(store=fixture.store, profile=self.profile, instance="prod"),
            self.case.decision.checklist.production,
        )
        self.assertEqual(
            release_version(store=fixture.store, profile=self.profile, instance="testing"),
            self.case.decision.checklist.candidate,
        )
        final_backup = self.capture_next_backup()
        self.advance()
        run = releases.read_client_release_run(
            store=fixture.store, profile=self.profile, decision=self.case.decision
        )
        assert run is not None
        self.assertEqual(run.state, "passed")
        self.assertTrue(run.steps)
        self.assertTrue(all(step.status == "pass" for step in run.steps))
        self.assertEqual(fixture.deploys, initial_deploys)
        self.assertTrue(fixture.new_running)
        self.assertFalse(fixture.old_running)
        self.assert_ordinary_settings_handoff(final_backup)

    def test_native_second_backup_can_continue_after_completed_inverse(self) -> None:
        fixture = self.case.fixture
        self.capture_next_backup()
        self.advance()
        self.advance()
        backup_id = self.capture_next_backup()
        self.continue_expired_backup(backup_id)
        self.advance()
        run = releases.read_client_release_run(
            store=fixture.store, profile=self.profile, decision=self.case.decision
        )
        assert run is not None
        self.assertEqual(run.state, "passed")
        self.assertTrue(fixture.new_running)
        self.assertFalse(fixture.old_running)

    def test_later_ordinary_release_can_continue_after_completed_native_publication(self) -> None:
        self.complete_native_drill()
        fixture = self.case.fixture
        decision = fixture.store.list_release_review_decision_records(
            product=self.profile.product, limit=1
        )[0]
        self.assertNotEqual(decision.record_id, self.case.decision.record_id)
        backup_id = self.capture_next_backup()
        self.continue_expired_backup(backup_id, decision)
        resumed = next(
            row
            for row in fixture.store.list_verireel_prod_backup_gate_operation_records()
            if row.backup_record_id == backup_id
        )
        self.assertEqual(resumed.status, "pass")
        self.assertIsNotNone(resumed.previous_attempt)

    def test_native_first_backup_can_continue_before_any_publication(self) -> None:
        backup_id = self.capture_next_backup()
        self.continue_expired_backup(backup_id)
        self.advance()
        self.assertTrue(self.case.fixture.new_running)

    def test_expired_native_preflight_refusal_shows_reconciliation_instead_of_continuation(
        self,
    ) -> None:
        fixture = self.case.fixture
        backup_id = self.capture_next_backup()
        operation = next(
            row
            for row in fixture.store.list_verireel_prod_backup_gate_operation_records()
            if row.backup_record_id == backup_id
        )
        with expired_backup_clock(fixture.store, operation.operation_id):
            continuable = releases.read_client_release_run(
                store=fixture.store, profile=self.profile, decision=self.case.decision
            )
        assert continuable is not None
        with patch.object(
            RecipientCutoverAdapter,
            "verify_upstreams",
            side_effect=ValueError("Upstream preflight unavailable"),
        ):
            self.assertEqual(
                releases.advance_client_releases(
                    store=fixture.store, control_plane_root=fixture.fixture.root
                ),
                (),
            )
        before = custody(fixture.store, fixture.record.product, fixture.record.context)
        with (
            expired_backup_clock(fixture.store, operation.operation_id),
            patch(
                "control_plane.client_release_backup_continuation.guest_capture_metadata",
                return_value=(),
            ),
        ):
            run = releases.read_client_release_run(
                store=fixture.store, profile=self.profile, decision=self.case.decision
            )
            with self.assertRaises(BackupContinuationRefused):
                continue_release_backup(
                    store=fixture.store,
                    root=fixture.fixture.root,
                    product=self.profile.product,
                    decision_record_id=self.case.decision.record_id,
                    review=BackupContinuationReview(reason="Inspect the expired capture"),
                    actor="local:fixture",
                    trace_id="fixture-preflight-refusal",
                )
        assert run is not None
        self.assertEqual(run.state, "waiting")
        self.assertTrue(run.blocked_reason)
        self.assertNotEqual(run.blocked_reason, continuable.blocked_reason)
        self.assertEqual(
            custody(fixture.store, fixture.record.product, fixture.record.context), before
        )
        self.assertFalse(fixture.actions)
        self.assertEqual(fixture.deploys, 0)

    def test_busy_native_source_leaves_the_release_waiting_without_delivery_failure(self) -> None:
        fixture = self.case.fixture
        self.capture_next_backup()
        before = custody(fixture.store, fixture.record.product, fixture.record.context)
        with patch.object(
            fixture.store, "production_backup_source_lock", return_value=nullcontext(None)
        ):
            for _ in range(2):
                self.assertEqual(
                    releases.advance_client_releases(
                        store=fixture.store, control_plane_root=fixture.fixture.root
                    ),
                    (),
                )
        receipt = fixture.store.read_idempotency_record(
            scope=releases.CLIENT_RELEASE_IDEMPOTENCY_SCOPE,
            route_path=GENERIC_WEB_PROD_PROMOTION_ROUTE,
            idempotency_key=releases._step_key(
                self.case.decision, releases.ClientReleaseStep("promote", 1)
            ),
        )
        self.assertIsNone(receipt)
        self.assertEqual(
            custody(fixture.store, fixture.record.product, fixture.record.context), before
        )
        self.assertFalse(fixture.actions)
        self.assertFalse(
            fixture.store.list_public_ingress_incident_records(
                product=self.profile.product, status="open"
            )
        )
        self.advance()
        self.assertTrue(fixture.new_running)

    def continue_expired_backup(
        self, backup_id: str, decision: ReleaseReviewDecisionRecord | None = None
    ) -> None:
        fixture = self.case.fixture
        decision = decision or self.case.decision
        operation = next(
            row
            for row in fixture.store.list_verireel_prod_backup_gate_operation_records()
            if row.backup_record_id == backup_id
        )
        before = custody(fixture.store, fixture.record.product, fixture.record.context)
        with (
            expired_backup_clock(fixture.store, operation.operation_id),
            patch(
                "control_plane.client_release_backup_continuation.guest_capture_metadata",
                return_value=(),
            ),
        ):
            self.assertEqual(
                releases.advance_client_releases(
                    store=fixture.store, control_plane_root=fixture.fixture.root
                ),
                (),
            )
            continue_backup = partial(
                continue_release_backup,
                store=fixture.store,
                root=fixture.fixture.root,
                product=fixture.record.product,
                decision_record_id=decision.record_id,
                actor="local:fixture",
                trace_id="fixture-backup-continuation",
            )
            plan = continue_backup(review=BackupContinuationReview(reason="Expired unused capture"))
            self.assertEqual(plan.operation_id, operation.operation_id)
            continue_backup(
                review=BackupContinuationReview(
                    reason="Expired unused capture", expected_digest=plan.review_digest, apply=True
                ),
            )
        resumed = fixture.store.read_verireel_prod_backup_gate_operation_record(
            operation.operation_id
        )
        self.assertEqual(resumed.previous_attempt, operation)
        self.assertEqual(
            custody(fixture.store, fixture.record.product, fixture.record.context), before
        )
        assert operation.binding is not None
        with patch(
            "tests.test_production_recipient_cutover.enqueue_production_backup_gate",
            return_value=resumed,
        ):
            fixture.capture(operation.binding.policy, backup_id)

    def assert_ordinary_settings_handoff(self, original_backup: str) -> None:
        fixture = self.case.fixture
        store = fixture.store
        profile = store.read_product_profile_record(self.profile.product).model_copy(
            update={"release_on_acceptance": "director_standing"}
        )
        store.write_product_profile_record(profile)
        runtime = next(
            row
            for row in store.list_runtime_environment_records()
            if row.scope == "instance"
            and row.context == fixture.record.context
            and row.instance == "prod"
        )
        store.write_runtime_environment_record(
            runtime.model_copy(update={"env": {**runtime.env, "FEATURE": "enabled"}})
        )
        review = current_release_review(
            record_store=store, control_plane_root=fixture.fixture.root, profile=profile
        )
        requested = record_release_request(
            store=store,
            profile=profile,
            review=review,
            source="director_request",
            reason="Release the staged fixture setting after the native drill.",
            decision_reference="fixture:settings-request",
            actor="local:fixture",
        )
        with patch(
            "control_plane.client_release.publish_release_decision",
            return_value="https://github.com/example/site/issues/2",
        ):
            releases._record_standing_acceptance(store, fixture.fixture.root, requested)
        future = store.list_release_review_decision_records(product=profile.product, limit=1)[0]
        original = self.case.decision
        self.assertNotEqual(future.record_id, original.record_id)
        self.assertEqual(
            future.checklist.candidate.artifact_id, original.checklist.candidate.artifact_id
        )
        self.assertEqual(
            future.checklist.candidate.source_commit, original.checklist.candidate.source_commit
        )
        assert future.checklist.candidate.runtime_settings is not None
        self.assertEqual(future.checklist.candidate.runtime_settings.values["FEATURE"], "enabled")

        class OrdinarySelected(Exception):
            pass

        effects = (fixture.deploys, list(fixture.actions))
        with patch(
            "control_plane.client_release.GenericWebProdPromotionProviderMutationAdapter",
            side_effect=OrdinarySelected,
        ) as ordinary:
            with self.assertRaises(OrdinarySelected):
                releases._run_generic_web_promotion(
                    store,
                    fixture.fixture.root,
                    requested,
                    future,
                    releases.ClientReleaseStep("promote", 1),
                    "unexecuted-fixture-backup",
                )
            ordinary.assert_called_once()
        self.assertEqual((fixture.deploys, fixture.actions), effects)
        step = releases.ClientReleaseStep("promote", 2)
        original_receipt = store.read_idempotency_record(
            scope=releases.CLIENT_RELEASE_IDEMPOTENCY_SCOPE,
            route_path=GENERIC_WEB_PROD_PROMOTION_ROUTE,
            idempotency_key=releases._step_key(original, step),
        )
        assert original_receipt is not None
        with patch(
            "control_plane.client_release.GenericWebProdPromotionProviderMutationAdapter",
            side_effect=AssertionError("The completed native original changed adapters."),
        ):
            receipt = releases._run_generic_web_promotion(
                store, fixture.fixture.root, requested, original, step, original_backup
            )
        self.assertEqual(receipt, original_receipt.record_id)
        self.assertEqual((fixture.deploys, fixture.actions), effects)
        self.assertEqual(
            store.read_idempotency_record(
                scope=original_receipt.scope,
                route_path=original_receipt.route_path,
                idempotency_key=original_receipt.idempotency_key,
            ),
            original_receipt,
        )
