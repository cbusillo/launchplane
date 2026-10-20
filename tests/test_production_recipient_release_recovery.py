"""Reconciliation retains an existing decision's admitted step and original key."""

from typing import Any
import unittest
from unittest.mock import patch
import click
from control_plane import client_release as releases
from control_plane.contracts.release_review import (
    ReleaseChecklist,
    ReleaseReviewDecisionRecord,
    ReleaseVersion,
)
from control_plane.production_recipient_release_recovery import (
    RecipientReleaseRecoveryRequest,
    recover_release_step,
)
from control_plane.generic_web_promotion_http import GENERIC_WEB_PROD_PROMOTION_ROUTE
from tests import test_production_recipient_cutover as fixtures
from tests.support.profiles import _generic_site_profile_payload
from control_plane.contracts.deployment_record import DeploymentRecord, ResolvedTargetEvidence
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductOwnerProfile,
)
from control_plane.contracts.promotion_record import (
    ArtifactIdentityReference,
    DeploymentEvidence,
    PostDeployUpdateEvidence,
)
from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.production_recipient import custody
from control_plane.production_recipient_cutover import RecipientCutoverAdapter
from control_plane.workflows.inventory import build_environment_inventory
from control_plane.generic_web_source_qualification import GenericWebPromotionSourceRefused


class RecipientReleaseRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.RecipientCutoverTests()
        payload = _generic_site_profile_payload()
        payload.update(
            production_use="live",
            release_on_acceptance="promote_with_rollback_drill",
            owner={"github_id": "123", "github_login": "fixture"},
        )
        with patch(
            "tests.test_production_recipient._generic_site_profile_payload", return_value=payload
        ):
            self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f = self.fixture
        checklist = ReleaseChecklist(
            product=f.record.product,
            repository=f.fixture.profile.repository,
            owner_github_id="123",
            testing_url="",
            production=ReleaseVersion(artifact_id="old-artifact", source_commit="b" * 40),
            candidate=ReleaseVersion(
                artifact_id=f.record.image_reference, source_commit=f.record.request.source_commit
            ),
            items=(),
        )
        self.decision = ReleaseReviewDecisionRecord(
            record_id="accepted-recipient",
            product=f.record.product,
            checklist_digest="d" * 64,
            checklist=checklist,
            decision="accepted",
            actor_github_id="123",
            actor_github_login="fixture",
            decided_at="2026-10-16T08:00:00Z",
            release_issue_url="https://github.com/example/site/issues/1",
            release_start="promote_with_rollback_drill",
        )
        self.request = RecipientReleaseRecoveryRequest(
            context=f.record.context,
            preparation_plan_sha256=f.record.plan_sha256,
            decision_record_id=self.decision.record_id,
            kind="promote",
            reason="Reconcile original outcome",
        )
        f.store.write_release_review_decision_record(self.decision)
        self.key = releases._step_key(self.decision, releases.ClientReleaseStep("promote", 1))

    def recover(self, request: Any = None, key: str | None = None) -> Any:
        f = self.fixture
        return recover_release_step(
            f.fixture.root,
            f.store,
            f.record.product,
            request or self.request,
            self.key if key is None else key,
        )

    def reserve(self) -> None:
        self.fixture.store.reserve_mutation(
            scope=releases.CLIENT_RELEASE_IDEMPOTENCY_SCOPE,
            route_path=GENERIC_WEB_PROD_PROMOTION_ROUTE,
            idempotency_key=self.key,
            request_fingerprint="e" * 64,
            lease_owner="original-release",
            reconciliation_key=self.fixture.adapter.reconciliation_key(),
        )

    def test_no_recovery_without_original_step_or_with_an_alternate_key(self) -> None:
        with self.assertRaises(ValueError):
            self.recover()
        self.reserve()
        with self.assertRaises(ValueError):
            self.recover(key="replacement")
        with self.assertRaises(ValueError):
            self.recover(self.request.model_copy(update={"kind": "rollback", "attempt": 2}))

    def test_saved_review_resumes_only_the_existing_operation(self) -> None:
        self.reserve()
        draft = self.recover(key="")
        self.assertEqual(draft.original_key, self.key)
        with patch(
            "control_plane.production_recipient_release_recovery.releases._run_generic_web_promotion"
        ) as run:
            with self.assertRaises(ValueError):
                self.recover(
                    self.request.model_copy(
                        update={"mode": "apply", "reviewed_plan_sha256": "f" * 64}
                    )
                )
            run.assert_not_called()
            result = self.recover(
                self.request.model_copy(
                    update={"mode": "apply", "reviewed_plan_sha256": draft.plan_sha256}
                )
            )
            self.assertEqual(result.operation_id, draft.operation_id)
            run.assert_called_once()
            self.assertEqual(run.call_args.args[4], releases.ClientReleaseStep("promote", 1))

    def test_temporary_hotfix_grant_cannot_start_the_fresh_production_cutover(self) -> None:
        from control_plane.contracts.product_profile_record import ProductOwnerProfile

        f = self.fixture
        profile = f.fixture.profile.model_copy(
            update={"owner": ProductOwnerProfile(github_id="123", github_login="fixture")}
        )
        hotfix = self.decision.model_copy(update={"decision": "temporary_hotfix"})
        with (
            patch("control_plane.client_release._require_current_release"),
        ):
            with self.assertRaises(click.ClickException):
                releases._run_generic_web_promotion(
                    f.store,
                    f.fixture.root,
                    profile,
                    hotfix,
                    releases.ClientReleaseStep("promote", 1),
                    f.old_backup,
                )
        self.assertEqual(f.actions, [])
        self.assertEqual(f.deploys, 0)

    def qualified_testing_source(self) -> tuple[LaunchplaneProductProfileRecord, DeploymentRecord]:
        f = self.fixture
        profile = f.fixture.profile.model_copy(
            update={
                "owner": ProductOwnerProfile(github_id="123", github_login="fixture"),
            }
        )
        testing_context = next(lane.context for lane in profile.lanes if lane.instance == "testing")
        testing = DeploymentRecord(
            record_id="qualified-recipient-testing",
            context=testing_context,
            instance="testing",
            artifact_identity=ArtifactIdentityReference(
                artifact_id=self.decision.checklist.candidate.artifact_id
            ),
            source_git_ref=self.decision.checklist.candidate.source_commit,
            runtime_identity=RuntimeIdentity(
                product=profile.product,
                context=testing_context,
                instance="testing",
                deployment_record_id="qualified-recipient-testing",
                artifact_id=self.decision.checklist.candidate.artifact_id,
                source_git_ref=self.decision.checklist.candidate.source_commit,
            ),
            deploy=DeploymentEvidence(
                target_name="testing",
                target_type="application",
                deploy_mode="fixture",
                status="pass",
            ),
            resolved_target=ResolvedTargetEvidence(
                target_type="application", target_id="testing", target_name="testing"
            ),
        )
        f.store.write_deployment_record(testing)
        f.store.write_environment_inventory(
            build_environment_inventory(deployment_record=testing, updated_at=profile.updated_at)
        )
        return profile, testing

    def advance_testing(self, testing: DeploymentRecord) -> None:
        assert testing.runtime_identity is not None and testing.artifact_identity is not None
        record_id = testing.record_id + "-next"
        artifact_id = self.fixture.fixture.profile.image.repository + "@sha256:" + "e" * 64
        later = testing.model_copy(
            update={
                "record_id": record_id,
                "source_git_ref": "e" * 40,
                "artifact_identity": testing.artifact_identity.model_copy(
                    update={"artifact_id": artifact_id}
                ),
                "runtime_identity": testing.runtime_identity.model_copy(
                    update={
                        "deployment_record_id": record_id,
                        "artifact_id": artifact_id,
                        "source_git_ref": "e" * 40,
                    }
                ),
            }
        )
        self.fixture.store.write_deployment_record(later)
        self.fixture.store.write_environment_inventory(
            build_environment_inventory(
                deployment_record=later, updated_at=self.fixture.fixture.profile.updated_at
            )
        )

    def test_resumed_recipient_rechecks_testing_before_forward_bootstrap(self) -> None:
        f = self.fixture
        profile, testing = self.qualified_testing_source()

        def run() -> None:
            releases._run_generic_web_promotion(
                f.store,
                f.fixture.root,
                profile,
                self.decision,
                releases.ClientReleaseStep("promote", 1),
                f.old_backup,
            )

        with (
            patch("control_plane.client_release._require_current_release"),
            patch.object(
                RecipientCutoverAdapter,
                "verify_upstreams",
                side_effect=ValueError("interrupt before provider dispatch"),
            ) as upstreams,
        ):
            with self.assertRaises(ValueError):
                run()
            upstreams.assert_called_once()
            saved = custody(f.store, f.record.product, f.record.context)
            assert saved is not None
            self.assertTrue(saved.execution)
            f.store.write_deployment_record(
                testing.model_copy(
                    update={
                        "post_deploy_update": PostDeployUpdateEvidence(
                            attempted=True, status="fail"
                        )
                    }
                )
            )
            upstreams.reset_mock()
            with patch(
                "control_plane.client_release._require_current_release",
                side_effect=AssertionError(
                    "Resumption must not compare the changed production baseline"
                ),
            ):
                with self.assertRaises(GenericWebPromotionSourceRefused):
                    run()
            upstreams.assert_not_called()
            f.store.write_deployment_record(testing)
            with self.assertRaises(ValueError):
                run()
            upstreams.assert_called_once()
        self.assertEqual(f.actions, [])
        self.assertEqual(f.deploys, 0)

    def test_fresh_source_refusal_preserves_type_and_original_step_retry(self) -> None:
        f = self.fixture
        profile, testing = self.qualified_testing_source()
        f.store.write_deployment_record(
            testing.model_copy(
                update={
                    "post_deploy_update": PostDeployUpdateEvidence(attempted=True, status="fail")
                }
            )
        )
        with (
            patch("control_plane.client_release._require_current_release"),
            patch.object(RecipientCutoverAdapter, "verify_runtime"),
        ):
            for _ in range(2):
                with self.assertRaises(GenericWebPromotionSourceRefused):
                    releases._run_generic_web_promotion(
                        f.store,
                        f.fixture.root,
                        profile,
                        self.decision,
                        releases.ClientReleaseStep("promote", 1),
                        f.old_backup,
                    )
                self.assertIsNone(
                    f.store.read_idempotency_record(
                        scope=releases.CLIENT_RELEASE_IDEMPOTENCY_SCOPE,
                        route_path=GENERIC_WEB_PROD_PROMOTION_ROUTE,
                        idempotency_key=self.key,
                    )
                )
                self.assertEqual(f.actions, [])
                self.assertEqual(f.deploys, 0)
            f.store.write_deployment_record(testing)
            self.assertTrue(
                releases._run_generic_web_promotion(
                    f.store,
                    f.fixture.root,
                    profile,
                    self.decision,
                    releases.ClientReleaseStep("promote", 1),
                    f.old_backup,
                )
            )
        saved = custody(f.store, f.record.product, f.record.context)
        assert saved is not None
        self.assertEqual(saved.phase, "published")

    def test_source_change_after_resource_effect_does_not_abort_admitted_recipient(self) -> None:
        f = self.fixture
        profile, testing = self.qualified_testing_source()

        def upstreams(_record: Any, checkpoint: Any, _lease: Any) -> None:
            checkpoint("resource_schedule_upsert")
            f.store.write_deployment_record(
                testing.model_copy(
                    update={
                        "post_deploy_update": PostDeployUpdateEvidence(
                            attempted=True, status="fail"
                        )
                    }
                )
            )
            checkpoint("resource_schedule_trigger")

        with (
            patch("control_plane.client_release._require_current_release"),
            patch.object(RecipientCutoverAdapter, "verify_runtime"),
            patch.object(RecipientCutoverAdapter, "verify_upstreams", side_effect=upstreams),
        ):
            self.assertTrue(
                releases._run_generic_web_promotion(
                    f.store,
                    f.fixture.root,
                    profile,
                    self.decision,
                    releases.ClientReleaseStep("promote", 1),
                    f.old_backup,
                )
            )
        reservation = f.store.read_idempotency_record(
            scope=releases.CLIENT_RELEASE_IDEMPOTENCY_SCOPE,
            route_path=GENERIC_WEB_PROD_PROMOTION_ROUTE,
            idempotency_key=self.key,
        )
        assert reservation is not None
        self.assertEqual(reservation.state, "completed")
        saved = custody(f.store, f.record.product, f.record.context)
        assert saved is not None
        self.assertEqual(saved.phase, "published")
        self.assertTrue(f.new_running)
        self.assertFalse(f.old_running)

    def test_started_recipient_finishes_accepted_artifact_after_testing_advances(self) -> None:
        self.started_recipient_with_new_testing(resume=False)

    def test_resumed_started_recipient_keeps_original_source_admission(self) -> None:
        self.started_recipient_with_new_testing(resume=True)

    def test_inverse_original_key_recovery_uses_real_authority_without_testing(self) -> None:
        from control_plane.generic_web_rollback_http import GENERIC_WEB_ROLLBACK_ROUTE

        self.started_recipient_with_new_testing(resume=False)
        f = self.fixture
        profile = f.store.read_product_profile_record(f.record.product)
        inventory = f.store.read_environment_inventory(
            context_name=next(lane.context for lane in profile.lanes if lane.instance == "testing"),
            instance_name="testing",
        )
        testing = f.store.read_deployment_record(inventory.deployment_record_id)
        f.store.write_deployment_record(
            testing.model_copy(
                update={
                    "post_deploy_update": PostDeployUpdateEvidence(attempted=True, status="fail")
                }
            )
        )
        step = releases.ClientReleaseStep("rollback", 1)
        key = releases._step_key(self.decision, step)
        grant = releases.client_release_grant(
            decision=self.decision,
            action="generic_web_rollback.execute",
            context=f.record.context,
            authorized_at=self.decision.decided_at,
        )
        self.assertFalse(releases.client_release_grant_allows(f.store, grant))

        def lose_pause_reply(*args: Any, **kwargs: Any) -> None:
            f.writer(*args, **kwargs)
            if not kwargs.get("old") and kwargs.get("action") == "pause":
                raise TimeoutError("replacement writer pause reply lost")

        with (
            patch(
                "control_plane.production_recipient_inverse.writer_action",
                side_effect=lose_pause_reply,
            ),
            patch(
                "control_plane.production_recipient_inverse._verify_health_evidence_with_identity",
                side_effect=f.health,
            ),
        ):
            self.assertTrue(
                releases._queue_recipient_inverse(
                    f.store,
                    f.fixture.root,
                    profile,
                    self.decision,
                    step,
                    f.record.context,
                    self.decision.decided_at,
                )
            )
        original = f.store.read_idempotency_record(
            scope=releases.CLIENT_RELEASE_IDEMPOTENCY_SCOPE,
            route_path=GENERIC_WEB_ROLLBACK_ROUTE,
            idempotency_key=key,
        )
        assert original is not None
        self.assertEqual(original.state, "reconcile_required")
        request = self.request.model_copy(update={"kind": "rollback"})
        plan = self.recover(request, key="")
        with (
            patch("control_plane.production_recipient_inverse.writer_action", side_effect=f.writer),
            patch(
                "control_plane.production_recipient_inverse._verify_health_evidence_with_identity",
                side_effect=f.health,
            ),
        ):
            result = self.recover(
                request.model_copy(
                    update={
                        "mode": "apply",
                        "reviewed_plan_sha256": plan.plan_sha256,
                    }
                ),
                key=key,
            )
        self.assertEqual(result.operation_id, original.record_id)
        self.assertEqual(result.original_key, key)
        self.assertEqual(result.state, "completed")
        current = custody(f.store, f.record.product, f.record.context)
        assert current is not None
        self.assertEqual(current.phase, "rolled_back")
        self.assertTrue(f.old_running)
        self.assertFalse(f.new_running)
        self.assertEqual(
            f.store.read_environment_inventory(context_name=f.record.context, instance_name="prod"),
            f.publication_case.inventory,
        )

    def test_admitted_inverse_restores_baseline_after_hold(self) -> None:
        self.inverse_revocation_after_pause(new_decision=False, abort=False)

    def test_admitted_inverse_restores_baseline_after_new_human_decision(self) -> None:
        self.inverse_revocation_after_pause(new_decision=True, abort=False)

    def test_inverse_abort_restores_replacement_after_hold(self) -> None:
        self.inverse_revocation_after_pause(new_decision=False, abort=True)

    def inverse_revocation_after_pause(self, *, new_decision: bool, abort: bool) -> None:
        from datetime import UTC, datetime, timedelta
        from control_plane.generic_web_rollback_http import GENERIC_WEB_ROLLBACK_ROUTE
        from control_plane.production_recipient_publication import RecipientOwnedAuthorityChanged

        self.started_recipient_with_new_testing(resume=False)
        f = self.fixture
        profile = f.store.read_product_profile_record(f.record.product)

        def revoke_after_pause(*args: Any, **kwargs: Any) -> None:
            f.writer(*args, **kwargs)
            if not kwargs.get("old") and kwargs.get("action") == "pause":
                if new_decision:
                    f.store.write_release_review_decision_record(
                        self.decision.model_copy(
                            update={
                                "record_id": self.decision.record_id + "-new-human-decision",
                                "decided_at": (
                                    datetime.now(UTC) + timedelta(seconds=1)
                                ).isoformat(),
                            }
                        )
                    )
                else:
                    f.store.write_product_profile_record(
                        profile.model_copy(update={"release_on_acceptance": "held"})
                    )

        step = releases.ClientReleaseStep("rollback", 1)
        key = releases._step_key(self.decision, step)
        with (
            patch(
                "control_plane.production_recipient_inverse.writer_action",
                side_effect=revoke_after_pause,
            ),
            patch(
                "control_plane.production_recipient_inverse._verify_health_evidence_with_identity",
                side_effect=f.health,
            ),
        ):
            if abort:
                with patch(
                    "control_plane.production_recipient_inverse.rollback_publication",
                    side_effect=RecipientOwnedAuthorityChanged("changed owned row"),
                ):
                    self.assertTrue(
                        releases._queue_recipient_inverse(
                            f.store,
                            f.fixture.root,
                            profile,
                            self.decision,
                            step,
                            f.record.context,
                            self.decision.decided_at,
                        )
                    )
            else:
                self.assertTrue(
                    releases._queue_recipient_inverse(
                        f.store,
                        f.fixture.root,
                        profile,
                        self.decision,
                        step,
                        f.record.context,
                        self.decision.decided_at,
                    )
                )
        reservation = f.store.read_idempotency_record(
            scope=releases.CLIENT_RELEASE_IDEMPOTENCY_SCOPE,
            route_path=GENERIC_WEB_ROLLBACK_ROUTE,
            idempotency_key=key,
        )
        assert reservation is not None
        self.assertEqual(reservation.state, "completed")
        saved = custody(f.store, f.record.product, f.record.context)
        assert saved is not None
        self.assertEqual(saved.phase, "published" if abort else "rolled_back")
        self.assertEqual(f.old_running, not abort)
        self.assertEqual(f.new_running, abort)

    def test_inverse_hold_before_pause_starts_no_writer_effect(self) -> None:
        from control_plane.generic_web_rollback_http import GENERIC_WEB_ROLLBACK_ROUTE

        self.started_recipient_with_new_testing(resume=False)
        f = self.fixture
        profile = f.store.read_product_profile_record(f.record.product)
        f.store.write_product_profile_record(
            profile.model_copy(update={"release_on_acceptance": "held"})
        )
        actions = list(f.actions)
        step = releases.ClientReleaseStep("rollback", 1)
        with self.assertRaises(ValueError):
            releases._queue_recipient_inverse(
                f.store,
                f.fixture.root,
                profile,
                self.decision,
                step,
                f.record.context,
                self.decision.decided_at,
            )
        self.assertEqual(f.actions, actions)
        self.assertFalse(f.old_running)
        self.assertTrue(f.new_running)
        self.assertIsNone(
            f.store.read_idempotency_record(
                scope=releases.CLIENT_RELEASE_IDEMPOTENCY_SCOPE,
                route_path=GENERIC_WEB_ROLLBACK_ROUTE,
                idempotency_key=releases._step_key(self.decision, step),
            )
        )

    def started_recipient_with_new_testing(self, *, resume: bool) -> None:
        f = self.fixture
        profile, testing = self.qualified_testing_source()

        def writer(*args: Any, **kwargs: Any) -> None:
            f.writer(*args, **kwargs)
            if kwargs.get("old") and kwargs.get("action") == "pause":
                self.advance_testing(testing)

        def deploy(**kwargs: Any) -> None:
            f.deploy(**kwargs)
            if resume:
                raise TimeoutError("original provider reply lost")

        def run() -> None:
            releases._run_generic_web_promotion(
                f.store,
                f.fixture.root,
                profile,
                self.decision,
                releases.ClientReleaseStep("promote", 1),
                f.old_backup,
            )

        with (
            patch("control_plane.client_release._require_current_release"),
            patch.object(RecipientCutoverAdapter, "verify_runtime"),
            patch("control_plane.production_recipient_cutover.writer_action", side_effect=writer),
            patch(
                "control_plane.production_recipient_cutover.execute_dokploy_artifact_deploy",
                side_effect=deploy,
            ),
            patch.object(
                RecipientCutoverAdapter,
                "terminal_deployment",
                return_value=None,
            ) as terminal,
        ):
            run()
            if resume:
                saved = custody(f.store, f.record.product, f.record.context)
                assert saved is not None
                self.assertNotEqual(saved.phase, "published")
                terminal.return_value = {
                    "title": saved.execution["runtime_identity"]["deployment_record_id"],
                    "status": "done",
                }
                with patch(
                    "control_plane.client_release._require_current_release",
                    side_effect=AssertionError("Started cutover cannot chase latest testing"),
                ):
                    run()
        saved = custody(f.store, f.record.product, f.record.context)
        assert saved is not None
        self.assertEqual(saved.phase, "published")
        self.assertEqual(f.deploys, 1)
        self.assertTrue(f.new_running)
        self.assertFalse(f.old_running)
        production = f.store.read_environment_inventory(
            context_name=f.record.context, instance_name="prod"
        )
        assert production.artifact_identity is not None
        self.assertEqual(
            production.artifact_identity.artifact_id, self.decision.checklist.candidate.artifact_id
        )
        self.assertNotIn(("resume", True), f.actions)
