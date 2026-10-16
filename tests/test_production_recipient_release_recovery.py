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


class RecipientReleaseRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.RecipientCutoverTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f = self.fixture
        checklist = ReleaseChecklist(
            product=f.record.product,
            repository=f.fixture.profile.repository,
            owner_github_id="123",
            testing_url="",
            production=ReleaseVersion(artifact_id="old", source_commit="b" * 40),
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
        replacement = patch(
            "control_plane.production_recipient_release_recovery.releases._covering_decision",
            return_value=(f.fixture.profile, self.decision),
        )
        replacement.start()
        self.addCleanup(replacement.stop)
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
            patch("control_plane.client_release.client_release_grant_allows", return_value=True),
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
