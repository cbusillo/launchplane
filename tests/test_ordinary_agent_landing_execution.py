"""Fresh landing joins real storage and the supported custody lifecycle."""

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from control_plane.contracts.merge_train_batch import MergeTrainBatchLandingEntry
from control_plane.contracts.ordinary_agent_effect import (
    OrdinaryAgentCompletedOutcome,
    OrdinaryAgentLandingPreparation,
    OrdinaryAgentProviderQuotaKey,
    OrdinaryAgentProviderWaitObservation,
)
from control_plane.contracts.ordinary_agent_snapshot import OrdinaryAgentLandingEvidence
from control_plane.github_app_identity import GitHubAppInstallationToken
from control_plane.merge_admission import GuardedMergeAdmission, MergeAdmissionDeniedError
from control_plane.merge_train_github import (
    MergeTrainGitHubError,
    RecordingMergeTrainGitHubTransport,
)
from control_plane.ordinary_agent_admission_store import OrdinaryAgentAdmissionAdapter
from control_plane.ordinary_agent_controller_store import (
    OrdinaryAgentControllerAdapter,
    OrdinaryAgentProgressAdapter,
)
from control_plane.ordinary_agent_custody import OrdinaryAgentCustodyCleanupUnknown
from control_plane.ordinary_agent_github_transport import (
    OrdinaryAgentProviderDeferred,
    OrdinaryAgentProviderEvidenceError,
)
from control_plane.ordinary_agent_landing_execution import (
    OrdinaryLandingRecoveryRequired,
    execute_fresh_ordinary_landing,
)
from tests import test_ordinary_agent_landing_storage as landing_support
from tests.merge_train_policy_fixtures import build_test_merge_train_policy


class OrdinaryAgentLandingExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = landing_support.OrdinaryAgentLandingStorageTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store = self.fixture.store
        self.now = self.fixture.fixture.fixture.now
        self.inner = RecordingMergeTrainGitHubTransport()
        self.preparation: OrdinaryAgentLandingPreparation | None = None
        self.read_error: Exception | None = None
        self.merge_error: Exception | None = None
        self.checkpoint_error: Exception | None = None
        self.guard_error: Exception | None = None
        self.checkpoints: list[MergeTrainBatchLandingEntry] = []
        self.mints = 0
        self.elapsed = 0.0
        self.result_sha = "9" * 40
        self.revoke_error: Exception | None = None

    def mint(self, **kwargs: Any) -> GitHubAppInstallationToken:
        kwargs["before_token_mint"](kwargs["identity"].app_id, 77)
        self.mints += 1
        return GitHubAppInstallationToken(
            token="test-token",
            app_id=kwargs["identity"].app_id,
            installation_id=77,
            repository_id=self.fixture.request.target.repository_id,
            repository=self.fixture.request.target.repository,
            expires_at=datetime.fromtimestamp(self.now + 300, timezone.utc).isoformat(),
        )

    def evidence(self, **kwargs: Any) -> OrdinaryAgentLandingEvidence:
        p = kwargs["preparation"]
        self.preparation = p
        kwargs["transport"].require_remaining(61)
        if self.read_error is not None:
            raise self.read_error
        self.inner.responses = [
            self.merge_error or {"merged": True, "sha": self.result_sha},
            {
                "data": {
                    "rateLimit": {"cost": 1},
                    "repository": {
                        "databaseId": p.target.repository_id,
                        "ref": {
                            "target": {
                                "oid": self.result_sha,
                                "tree": {"oid": p.expected_merge_tree_sha},
                                "parents": {
                                    "totalCount": 2,
                                    "pageInfo": {"hasNextPage": False},
                                    "nodes": [
                                        {"oid": p.expected_base_sha},
                                        {"oid": p.entry.expected_head_sha},
                                    ],
                                },
                            }
                        },
                    },
                }
            },
        ]
        return self.fixture.evidence(p)

    def checkpoint(self, entry: MergeTrainBatchLandingEntry) -> None:
        assert self.preparation is not None
        finalization = self.store.read_ordinary_landing_finalization(
            preparation_id=self.preparation.preparation_id
        )
        assert finalization is not None
        history = self.store.read_ordinary_agent_effect_history(
            effect_id=finalization.effect.effect_id
        )
        self.assertEqual(history.effect.state, "completed")
        self.assertEqual(
            self.store.read_ordinary_agent_custody_issue_attempt(
                self.preparation.custody_attempt_id
            ).state,
            "issued",
        )
        if self.checkpoint_error:
            raise self.checkpoint_error
        self.checkpoints.append(entry)

    def guard(
        self, preparation: OrdinaryAgentLandingPreparation, evidence: OrdinaryAgentLandingEvidence
    ) -> GuardedMergeAdmission:
        if self.guard_error:
            raise self.guard_error
        guard = self.fixture.guard(preparation)
        controller = OrdinaryAgentControllerAdapter(
            claimed=self.fixture.claimed, store=self.store, reader=self.store
        )
        guard.record_store = OrdinaryAgentAdmissionAdapter(
            controller=controller,
            progress=OrdinaryAgentProgressAdapter(controller=controller, reader=self.store),
            reader=self.store,
        )
        return guard

    def provider_request(self, **kwargs: object) -> object:
        if kwargs.get("path") == "/installation/token" and self.revoke_error:
            raise self.revoke_error
        return None

    def run_landing(
        self, predecessor_preparation_id: str | None = None
    ) -> MergeTrainBatchLandingEntry:
        target = self.fixture.request.target
        with (
            patch(
                "control_plane.ordinary_agent_custody.resolve_ordinary_agent_github_app_identity",
                side_effect=lambda **kw: SimpleNamespace(
                    identity=SimpleNamespace(app_id=kw["candidate"].expected_app_id)
                ),
            ),
            patch(
                "control_plane.ordinary_agent_custody.mint_ordinary_agent_installation_token",
                side_effect=self.mint,
            ),
            patch(
                "control_plane.ordinary_agent_landing_execution.read_ordinary_agent_landing_evidence",
                side_effect=self.evidence,
            ),
        ):
            return execute_fresh_ordinary_landing(
                store=self.store,
                request_id=self.fixture.request.request_id,
                binding_revision=1,
                controller_fence=self.fixture.fence,
                pull_request_number=self.fixture.pull_request,
                semantic_ordinal=1,
                candidate_record=self.fixture.candidate,
                landing_plan_record=self.fixture.plan,
                repository_owner_id=202,
                repository_policy=build_test_merge_train_policy(
                    repository=target.repository
                ).find_repository_policy(
                    repository=target.repository, base_branch=target.base_branch
                ),
                guard_factory=self.guard,
                checkpoint=self.checkpoint,
                predecessor_preparation_id=predecessor_preparation_id,
                api_request=self.provider_request,
                transport_factory=lambda token: self.inner,
                monotonic=lambda: self.elapsed,
                utc_now=lambda: datetime.fromtimestamp(self.now + self.elapsed, timezone.utc),
            )

    def test_success_records_outcome_and_progress_before_revoke_and_replay_never_mints(
        self,
    ) -> None:
        result = self.run_landing()
        self.assertEqual((result.status, result.merge_commit_sha), ("merged", self.result_sha))
        self.assertEqual(self.checkpoints, [result])
        assert self.preparation is not None
        self.assertEqual(
            self.store.read_ordinary_agent_custody_issue_attempt(
                self.preparation.custody_attempt_id
            ).state,
            "closed",
        )
        with self.assertRaises(OrdinaryLandingRecoveryRequired):
            self.run_landing()
        self.assertEqual(self.mints, 1)
        self.assertEqual([item.method for item in self.inner.requests], ["PUT", "POST"])

    def test_evidence_deadline_closes_preparation_without_dispatch(self) -> None:
        self.read_error = OrdinaryAgentProviderDeferred()
        with self.assertRaises(OrdinaryAgentProviderDeferred):
            self.run_landing()
        assert self.preparation is not None
        result = self.store.read_ordinary_landing_preparation(
            preparation_id=self.preparation.preparation_id
        )
        self.assertEqual(
            (result.state, result.reason_code), ("terminal", "provider_attempt_deadline")
        )
        self.assertEqual(self.inner.requests, [])
        self.assertEqual(self.checkpoints, [])

    def test_ambiguous_provider_response_preserves_consumed_preparation_and_unknown(self) -> None:
        self.merge_error = MergeTrainGitHubError("transport response lost")
        with self.assertRaises(MergeTrainGitHubError):
            self.run_landing()
        assert self.preparation is not None
        finalization = self.store.read_ordinary_landing_finalization(
            preparation_id=self.preparation.preparation_id
        )
        assert finalization is not None
        self.assertEqual(finalization.preparation.state, "consumed")
        assert finalization is not None
        history = self.store.read_ordinary_agent_effect_history(
            effect_id=finalization.effect.effect_id
        )
        assert history.outcome is not None
        self.assertEqual(
            (history.effect.state, history.outcome.kind), ("reconciliation_required", "unknown")
        )
        self.assertEqual(len(self.inner.requests), 1)
        self.assertEqual(self.checkpoints, [])

    def test_progress_failure_retains_success_for_read_only_recovery(self) -> None:
        self.checkpoint_error = RuntimeError("checkpoint interrupted")
        with self.assertRaisesRegex(RuntimeError, "checkpoint interrupted"):
            self.run_landing()
        assert self.preparation is not None
        finalization = self.store.read_ordinary_landing_finalization(
            preparation_id=self.preparation.preparation_id
        )
        assert finalization is not None
        history = self.store.read_ordinary_agent_effect_history(
            effect_id=finalization.effect.effect_id
        )
        assert isinstance(history.outcome, OrdinaryAgentCompletedOutcome)
        self.assertEqual(
            (history.effect.state, history.outcome.result_sha), ("completed", self.result_sha)
        )
        with self.assertRaises(OrdinaryLandingRecoveryRequired):
            self.run_landing()
        self.assertEqual(self.mints, 1)

    def test_preparation_read_latency_is_charged_once(self) -> None:
        read = self.store.read_ordinary_landing_preparation

        def delayed_read(**kwargs: Any) -> OrdinaryAgentLandingPreparation:
            result = read(**kwargs)
            self.elapsed += 8
            self.fixture.fixture.fixture.clock.return_value = datetime.fromtimestamp(
                self.now + self.elapsed, timezone.utc
            ).isoformat()
            return result

        with patch.object(
            self.store, "read_ordinary_landing_preparation", side_effect=delayed_read
        ):
            result = self.run_landing()
        self.assertEqual(result.status, "merged")

    def test_failed_preparation_cleanup_preserves_both_failures(self) -> None:
        self.read_error = OrdinaryAgentProviderDeferred()
        cleanup_error = RuntimeError("database unavailable during cleanup")
        with patch.object(
            self.store, "close_ordinary_landing_preparation", side_effect=cleanup_error
        ):
            with self.assertRaises(ExceptionGroup) as raised:
                self.run_landing()
        self.assertEqual(raised.exception.exceptions, (self.read_error, cleanup_error))
        self.assertEqual(self.inner.requests, [])

    def test_provider_wait_before_mint_preserves_its_reason(self) -> None:
        self.store.record_provider_wait(
            quota_key=OrdinaryAgentProviderQuotaKey(
                authority_kind="installation", authority_id=77, resource_class="core"
            ),
            observation=OrdinaryAgentProviderWaitObservation(
                retry_not_before=self.now + 300, classification="primary_rate_limit"
            ),
        )
        with self.assertRaises(OrdinaryAgentProviderDeferred) as raised:
            self.run_landing()
        self.assertEqual(raised.exception.reason_code, "provider_wait")
        preparation = self.fixture.reserve().preparation
        self.assertEqual(preparation.reason_code, "provider_wait")
        self.assertEqual(self.mints, 0)
        self.assertEqual(self.inner.requests, [])

    def test_revoke_failure_keeps_completed_landing_available_for_recovery(self) -> None:
        self.revoke_error = RuntimeError("revoke transport lost")
        with self.assertRaises(OrdinaryAgentCustodyCleanupUnknown):
            self.run_landing()
        assert self.preparation is not None
        finalization = self.store.read_ordinary_landing_finalization(
            preparation_id=self.preparation.preparation_id
        )
        assert finalization is not None
        history = self.store.read_ordinary_agent_effect_history(
            effect_id=finalization.effect.effect_id
        )
        self.assertEqual(history.effect.state, "completed")
        self.assertEqual(len(self.checkpoints), 1)
        self.assertEqual(
            self.store.read_ordinary_agent_custody_issue_attempt(
                self.preparation.custody_attempt_id
            ).state,
            "cleanup_unknown",
        )

    def test_invalid_provider_evidence_is_a_denial_not_an_interruption(self) -> None:
        self.read_error = OrdinaryAgentProviderEvidenceError("landing_base_tree_mismatch")
        with self.assertRaises(OrdinaryAgentProviderEvidenceError):
            self.run_landing()
        self.assertEqual(self.fixture.reserve().preparation.reason_code, "evidence_denied")
        self.assertEqual(self.inner.requests, [])

    def test_guard_denial_is_retained_before_any_merge_dispatch(self) -> None:
        self.guard_error = MergeAdmissionDeniedError("owner evidence no longer current")
        with self.assertRaises(MergeAdmissionDeniedError):
            self.run_landing()
        self.assertEqual(self.fixture.reserve().preparation.reason_code, "evidence_denied")
        self.assertEqual(self.inner.requests, [])
