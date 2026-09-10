"""Real custody/history joins recover a lost merge response using only reads."""

from datetime import datetime, timezone
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from control_plane.contracts.ordinary_agent_effect import (
    OrdinaryAgentEffectHistory,
    OrdinaryAgentPullRequestObservation,
    OrdinaryAgentUnknownOutcome,
)
from control_plane.github_app_identity import GitHubAppInstallationToken
from control_plane.contracts.ordinary_agent_lifecycle import OrdinaryAgentEnrollApplyEnvelope
from control_plane.merge_train_github import (
    RecordingMergeTrainGitHubTransport,
    MergeTrainGitHubError,
)
from control_plane.ordinary_agent_effect_reconciliation import reconcile_ordinary_effect_once
from control_plane.ordinary_agent_effect_recovery import recover_ordinary_effect
from control_plane.ordinary_agent_session_lifecycle import OrdinaryAgentSessionAdmissionDenied
from tests import test_ordinary_agent_landing_dispatch as dispatch_support
from tests.support.ordinary_agent_lifecycle import (
    apply_test_enrollment,
    enrollment_mutation,
    revocation_envelope,
)


class OrdinaryEffectReconciliationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = dispatch_support.LandingDispatchTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store = self.fixture.fixture.store
        self.request = self.fixture.fixture.request
        self.preparation = self.fixture.finalized.preparation
        child = self.fixture.finalized.child
        self.store.record_ordinary_semantic_outcome(
            child_id=child.child_id,
            typed_outcome=OrdinaryAgentUnknownOutcome(reason="transport_ambiguous"),
        )
        self.store.close_ordinary_agent_custody_issue_attempt(
            attempt_id=child.custody_attempt_id, reason="confirmed_revoked"
        )
        self.session = self.fixture.fixture.fixture.fixture
        self.now = self.session.now + 16
        self.session.clock.return_value = datetime.fromtimestamp(self.now, timezone.utc).isoformat()
        token = GitHubAppInstallationToken(
            token="test-read-token",
            app_id=42,
            installation_id=77,
            repository_id=self.request.target.repository_id,
            repository=self.request.target.repository,
            expires_at=datetime.fromtimestamp(self.now + 300, timezone.utc).isoformat(),
        )
        self.enterContext(
            patch(
                "control_plane.ordinary_agent_custody.resolve_ordinary_agent_github_app_identity",
                return_value=SimpleNamespace(identity=SimpleNamespace(app_id=42)),
            )
        )
        self.mint = self.enterContext(
            patch(
                "control_plane.ordinary_agent_custody.mint_ordinary_agent_installation_token",
                return_value=token,
            )
        )
        self.api = Mock(return_value=None)

    def responses(
        self, *, wrong_tree: bool = False, base_contains_merge_commit: bool = True
    ) -> tuple[object, ...]:
        p = self.preparation
        observed_base_sha = "7" * 40
        return (
            {
                "number": p.entry.pull_request_number,
                "state": "closed",
                "merged": True,
                "merge_commit_sha": self.fixture.result_sha,
                "head": {
                    "sha": p.entry.expected_head_sha,
                    "ref": "feature",
                    "repo": {"id": p.target.repository_id},
                },
                "base": {
                    "sha": self.fixture.result_sha,
                    "ref": p.target.base_branch,
                    "repo": {"id": p.target.repository_id},
                },
            },
            {
                "sha": self.fixture.result_sha,
                "tree": {"sha": "8" * 40 if wrong_tree else p.expected_merge_tree_sha},
                "parents": [{"sha": p.expected_base_sha}, {"sha": p.entry.expected_head_sha}],
                "message": "merge",
            },
            {
                "ref": "refs/heads/" + p.target.base_branch,
                "object": {"sha": observed_base_sha},
            },
            {
                "sha": observed_base_sha,
                "tree": {"sha": "6" * 40},
                "parents": [
                    {"sha": self.fixture.result_sha if base_contains_merge_commit else "5" * 40}
                ],
                "message": "later base commit",
            },
            {
                "status": "ahead" if base_contains_merge_commit else "diverged",
                "base_commit": {"sha": self.fixture.result_sha},
                "merge_base_commit": {
                    "sha": self.fixture.result_sha if base_contains_merge_commit else "5" * 40
                },
            },
        )

    def run_recovery(
        self, transport: RecordingMergeTrainGitHubTransport
    ) -> OrdinaryAgentEffectHistory:
        return reconcile_ordinary_effect_once(
            store=self.store,
            request=self.request,
            effect_id=self.fixture.finalized.effect.effect_id,
            api_request=self.api,
            transport_factory=lambda _: transport,
            monotonic=lambda: 0,
            utc_now=lambda: datetime.fromtimestamp(self.now, timezone.utc),
        )

    def test_lost_landing_response_recovers_without_another_mutation_or_replay_mint(self) -> None:
        transport = RecordingMergeTrainGitHubTransport(responses=self.responses())
        history = self.run_recovery(transport)
        self.assertEqual(history.effect.state, "completed_observed")
        self.assertEqual(history.effect.dispatch_count, 1)
        self.assertEqual([request.method for request in transport.requests], ["GET"] * 5)
        proof = history.reconciliations[-1].observation
        assert isinstance(proof, OrdinaryAgentPullRequestObservation)
        self.assertTrue(proof.base_contains_merge_commit)
        self.assertEqual(recover_ordinary_effect(history).disposition, "replay")
        self.assertEqual(self.run_recovery(transport), history)
        self.mint.assert_called_once()
        self.assertEqual(len(transport.requests), 5)

    def test_false_base_containment_remains_scheduled_without_redispatch(self) -> None:
        transport = RecordingMergeTrainGitHubTransport(
            responses=self.responses(base_contains_merge_commit=False)
        )

        history = self.run_recovery(transport)

        self.assertEqual(history.effect.state, "reconciliation_required")
        self.assertIsNotNone(history.effect.next_observation_at)
        self.assertEqual(history.effect.dispatch_count, 1)
        self.assertEqual(history.effect.reconciliation_count, 1)
        observation = history.reconciliations[-1].observation
        assert isinstance(observation, OrdinaryAgentPullRequestObservation)
        self.assertFalse(observation.base_contains_merge_commit)
        self.assertEqual(recover_ordinary_effect(history).disposition, "observe")
        self.assertEqual([request.method for request in transport.requests], ["GET"] * 5)

    def test_wrong_result_tree_is_terminal_even_after_provider_reports_merged(self) -> None:
        transport = RecordingMergeTrainGitHubTransport(responses=self.responses(wrong_tree=True))
        history = self.run_recovery(transport)
        self.assertEqual(history.effect.reason_code, "landing_result_tree_mismatch")
        self.assertEqual(recover_ordinary_effect(history).disposition, "terminal")
        self.assertEqual(history.effect.dispatch_count, 1)

    def test_failed_read_keeps_unknown_history_and_retry_only_observes(self) -> None:
        transport = RecordingMergeTrainGitHubTransport(
            responses=(MergeTrainGitHubError("read lost"),)
        )
        history = self.run_recovery(transport)
        self.assertEqual(history.effect.state, "reconciliation_required")
        self.assertEqual(history.reconciliations[0].observation.kind, "incomplete_read")
        self.assertEqual(recover_ordinary_effect(history).disposition, "observe")
        assert history.effect.next_observation_at is not None
        self.now = history.effect.next_observation_at
        self.session.clock.return_value = datetime.fromtimestamp(self.now, timezone.utc).isoformat()
        retry = RecordingMergeTrainGitHubTransport(responses=self.responses())
        recovered = self.run_recovery(retry)
        self.assertEqual(recovered.effect.dispatch_count, 1)
        self.assertEqual(recovered.effect.state, "completed_observed")
        self.assertTrue(
            all(request.method == "GET" for request in (*transport.requests, *retry.requests))
        )

    def test_repeated_incomplete_reads_exhaust_the_existing_budget(self) -> None:
        history = None
        for _ in range(3):
            transport = RecordingMergeTrainGitHubTransport(
                responses=(MergeTrainGitHubError("read lost"),)
            )
            history = self.run_recovery(transport)
            if history.effect.next_observation_at is not None:
                self.now = history.effect.next_observation_at
                self.session.clock.return_value = datetime.fromtimestamp(
                    self.now, timezone.utc
                ).isoformat()
        assert history is not None
        self.assertEqual(history.effect.reason_code, "reconciliation_exhausted")
        self.assertEqual(recover_ordinary_effect(history).disposition, "terminal")
        self.assertEqual(history.effect.dispatch_count, 1)
        mints = self.mint.call_count
        transport = RecordingMergeTrainGitHubTransport(responses=())
        self.assertEqual(self.run_recovery(transport), history)
        self.assertEqual(self.mint.call_count, mints)

    def test_third_observation_can_recover_after_two_failed_reads(self) -> None:
        for _ in range(2):
            history = self.run_recovery(
                RecordingMergeTrainGitHubTransport(responses=(MergeTrainGitHubError("read lost"),))
            )
            assert history.effect.next_observation_at is not None
            self.now = history.effect.next_observation_at
            self.session.clock.return_value = datetime.fromtimestamp(
                self.now, timezone.utc
            ).isoformat()
        history = self.run_recovery(RecordingMergeTrainGitHubTransport(responses=self.responses()))
        self.assertEqual(history.effect.state, "completed_observed")
        self.assertEqual(recover_ordinary_effect(history).disposition, "replay")
        self.assertEqual(history.effect.dispatch_count, 1)

    def test_append_failure_recovers_closed_read_as_incomplete_before_another_read(self) -> None:
        with patch.object(
            self.store,
            "append_ordinary_effect_reconciliation",
            side_effect=RuntimeError("database unavailable"),
        ):
            with self.assertRaisesRegex(RuntimeError, "database unavailable"):
                self.run_recovery(RecordingMergeTrainGitHubTransport(responses=self.responses()))
        with self.assertRaisesRegex(OrdinaryAgentSessionAdmissionDenied, "observation_not_due"):
            self.run_recovery(RecordingMergeTrainGitHubTransport(responses=()))
        self.mint.assert_called_once()
        history = self.store.read_ordinary_agent_effect_history(
            effect_id=self.fixture.finalized.effect.effect_id
        )
        self.assertEqual(history.reconciliations[0].observation.kind, "incomplete_read")
        assert history.effect.next_observation_at is not None
        self.now = history.effect.next_observation_at
        self.session.clock.return_value = datetime.fromtimestamp(self.now, timezone.utc).isoformat()
        history = self.run_recovery(RecordingMergeTrainGitHubTransport(responses=self.responses()))
        self.assertEqual(history.effect.state, "completed_observed")
        self.assertEqual(history.effect.dispatch_count, 1)

    def test_malformed_provider_identity_is_recorded_as_incomplete_read(self) -> None:
        responses = self.responses()
        assert isinstance(responses[1], dict)
        responses[1]["tree"] = {"sha": "f" * 65}
        transport = RecordingMergeTrainGitHubTransport(responses=responses)
        history = self.run_recovery(transport)
        self.assertEqual(history.effect.state, "reconciliation_required")
        self.assertEqual(history.reconciliations[0].observation.kind, "incomplete_read")
        self.assertEqual(history.effect.dispatch_count, 1)

    def test_unknown_landing_can_be_observed_after_request_expiry_with_current_read_authority(
        self,
    ) -> None:
        self.now = (self.request.continuation_expires_at or self.request.expires_at) + 1
        self.session.clock.return_value = datetime.fromtimestamp(self.now, timezone.utc).isoformat()
        transport = RecordingMergeTrainGitHubTransport(responses=self.responses())
        history = self.run_recovery(transport)
        self.assertEqual(history.effect.state, "completed_observed")
        self.assertEqual(history.effect.dispatch_count, 1)
        self.assertEqual([request.method for request in transport.requests], ["GET"] * 5)

    def test_observation_wait_denies_before_mint_or_read(self) -> None:
        self.now = self.session.now
        self.session.clock.return_value = datetime.fromtimestamp(self.now, timezone.utc).isoformat()
        transport = RecordingMergeTrainGitHubTransport(responses=())
        with self.assertRaisesRegex(OrdinaryAgentSessionAdmissionDenied, "observation_not_due"):
            self.run_recovery(transport)
        self.mint.assert_not_called()
        self.assertEqual(transport.requests, [])

    def test_revoked_unusable_token_does_not_consume_observation_or_strand_recovery(self) -> None:
        good = self.mint.return_value
        self.mint.return_value = replace(
            good, expires_at=datetime.fromtimestamp(self.now - 1, timezone.utc).isoformat()
        )
        transport = RecordingMergeTrainGitHubTransport(responses=())
        with self.assertRaisesRegex(ValueError, "expiry"):
            self.run_recovery(transport)
        self.assertEqual(transport.requests, [])
        self.mint.return_value = good
        history = self.run_recovery(RecordingMergeTrainGitHubTransport(responses=self.responses()))
        self.assertEqual(history.effect.state, "completed_observed")
        self.assertEqual(history.effect.reconciliation_count, 1)
        self.assertEqual(history.effect.reconciliation_custody_count, 2)
        self.assertEqual(self.mint.call_args.kwargs["effect_profile"], "effect_reconciliation")

    def test_repeated_unusable_tokens_stop_at_existing_mint_limit(self) -> None:
        self.mint.return_value = replace(
            self.mint.return_value,
            expires_at=datetime.fromtimestamp(self.now - 1, timezone.utc).isoformat(),
        )
        for _ in range(3):
            with self.assertRaisesRegex(ValueError, "expiry"):
                self.run_recovery(RecordingMergeTrainGitHubTransport(responses=()))
        with self.assertRaisesRegex(
            OrdinaryAgentSessionAdmissionDenied, "custody_attempts_exhausted"
        ):
            self.run_recovery(RecordingMergeTrainGitHubTransport(responses=()))
        history = self.run_recovery(RecordingMergeTrainGitHubTransport(responses=()))
        self.assertEqual(recover_ordinary_effect(history).disposition, "terminal")
        self.assertEqual(history.effect.dispatch_count, 1)
        self.assertEqual(history.reconciliations, ())
        self.assertEqual(self.mint.call_count, 3)

    def test_revoked_current_credential_cannot_observe_old_effect(self) -> None:
        principal = self.store.read_current_ordinary_agent_principal(principal_id="agent_one")
        assert principal is not None
        assert isinstance(self.session.envelope, OrdinaryAgentEnrollApplyEnvelope)
        revoke = revocation_envelope(
            enrolled=self.session.envelope,
            principal_record_id=principal.record_id,
            principal_revision=principal.principal_revision,
            principal_sha256=principal.record_sha256,
        )
        apply_test_enrollment(self.store, envelope=revoke, mutation=enrollment_mutation(revoke))
        transport = RecordingMergeTrainGitHubTransport(responses=())
        with self.assertRaisesRegex(OrdinaryAgentSessionAdmissionDenied, "credential_unavailable"):
            self.run_recovery(transport)
        self.mint.assert_not_called()
        self.assertEqual(transport.requests, [])
        history = self.store.read_ordinary_agent_effect_history(
            effect_id=self.fixture.finalized.effect.effect_id
        )
        self.assertEqual(history.effect.dispatch_count, 1)
        self.assertEqual(history.reconciliations, ())
