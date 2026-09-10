"""Recovery observes immutable commits and bounds paginated evidence."""

import unittest
from copy import deepcopy

from control_plane.contracts import ordinary_agent_effect as effects
from control_plane.contracts.merge_train_effect import (
    CandidateHeadMergeEffect,
    MergeTrainEffectLineage,
    PullRequestLandingEffect,
    StackChildCommentEffect,
    StackChildLabelEffect,
)
from control_plane.merge_train_github import RecordingMergeTrainGitHubTransport
from control_plane.ordinary_agent_github_transport import (
    DeadlineMergeTrainGitHubTransport,
    OrdinaryAgentProviderEvidenceError,
)
from control_plane.ordinary_agent_reconciliation_reader import read_ordinary_effect_observation
from tests.test_ordinary_agent_effect_lifecycle import effect_record


class OrdinaryReconciliationReaderTests(unittest.TestCase):
    def transport(
        self, responses: tuple[object, ...]
    ) -> tuple[DeadlineMergeTrainGitHubTransport, RecordingMergeTrainGitHubTransport]:
        inner = RecordingMergeTrainGitHubTransport(responses=responses)
        return DeadlineMergeTrainGitHubTransport(
            transport=inner, work_deadline=90, token_deadline=300, monotonic=lambda: 0
        ), inner

    def landing_record(self) -> effects.OrdinaryAgentEffectRecord:
        return effect_record(
            effects.PullRequestLandingCommand(
                effect=PullRequestLandingEffect(
                    lineage=MergeTrainEffectLineage(repository="example/repo", base_branch="main"),
                    pull_request_number=12,
                    head_sha="b" * 40,
                    rolling_base_sha="a" * 40,
                    admission_id="admission-one",
                    merge_method="merge",
                )
            )
        )

    def landing_pull_request(self, *, base_ref: str = "main") -> dict[str, object]:
        return {
            "number": 12,
            "state": "closed",
            "merged": True,
            "merge_commit_sha": "c" * 40,
            "head": {"sha": "b" * 40, "ref": "feature", "repo": {"id": 123}},
            "base": {"sha": "a" * 40, "ref": base_ref, "repo": {"id": 123}},
        }

    def landing_merge_commit(self) -> dict[str, object]:
        return {
            "sha": "c" * 40,
            "tree": {"sha": "e" * 40},
            "parents": [{"sha": "a" * 40}, {"sha": "b" * 40}],
            "message": "merge",
        }

    def test_landing_observes_immutable_ahead_base_containment(self) -> None:
        observed_base_sha = "d" * 40
        responses = (
            self.landing_pull_request(),
            self.landing_merge_commit(),
            {"ref": "refs/heads/main", "object": {"sha": observed_base_sha}},
            {
                "sha": observed_base_sha,
                "tree": {"sha": "f" * 40},
                "parents": [{"sha": "c" * 40}],
                "message": "later base commit",
            },
            {
                "status": "ahead",
                "base_commit": {"sha": "c" * 40},
                "merge_base_commit": {"sha": "c" * 40},
            },
        )
        transport, inner = self.transport(responses)

        observation = read_ordinary_effect_observation(transport, self.landing_record())

        assert isinstance(observation, effects.OrdinaryAgentPullRequestObservation)
        self.assertEqual(observation.merge_commit_tree_sha, "e" * 40)
        self.assertEqual(observation.merge_commit_parents, ("a" * 40, "b" * 40))
        self.assertEqual(observation.observed_base_sha, observed_base_sha)
        self.assertEqual(observation.observed_base_tree_sha, "f" * 40)
        self.assertTrue(observation.base_contains_merge_commit)
        self.assertEqual(
            inner.requests[-1].path,
            f"/repos/example/repo/compare/{'c' * 40}...{observed_base_sha}",
        )
        self.assertTrue(all(request.method == "GET" for request in inner.requests))

    def test_landing_equal_base_reuses_merge_commit_without_compare(self) -> None:
        transport, inner = self.transport(
            (
                self.landing_pull_request(),
                self.landing_merge_commit(),
                {"ref": "refs/heads/main", "object": {"sha": "c" * 40}},
            )
        )

        observation = read_ordinary_effect_observation(transport, self.landing_record())

        assert isinstance(observation, effects.OrdinaryAgentPullRequestObservation)
        self.assertEqual(observation.observed_base_sha, "c" * 40)
        self.assertEqual(observation.observed_base_tree_sha, "e" * 40)
        self.assertTrue(observation.base_contains_merge_commit)
        self.assertEqual(len(inner.requests), 3)

    def test_landing_distinguishes_false_containment_from_invalid_compare_evidence(self) -> None:
        observed_base_sha = "d" * 40
        base_reads = (
            self.landing_pull_request(),
            self.landing_merge_commit(),
            {"ref": "refs/heads/main", "object": {"sha": observed_base_sha}},
            {
                "sha": observed_base_sha,
                "tree": {"sha": "f" * 40},
                "parents": [{"sha": "0" * 40}],
                "message": "diverged base",
            },
        )
        transport, _ = self.transport(
            (
                *base_reads,
                {
                    "status": "diverged",
                    "base_commit": {"sha": "c" * 40},
                    "merge_base_commit": {"sha": "0" * 40},
                },
            )
        )
        observation = read_ordinary_effect_observation(transport, self.landing_record())
        assert isinstance(observation, effects.OrdinaryAgentPullRequestObservation)
        self.assertFalse(observation.base_contains_merge_commit)

        invalid_comparisons = (
            {
                "status": "ahead",
                "base_commit": {"sha": "0" * 40},
                "merge_base_commit": {"sha": "c" * 40},
            },
            {
                "status": "ahead",
                "base_commit": {"sha": "c" * 40},
                "merge_base_commit": {"sha": "0" * 40},
            },
            {
                "status": "identical",
                "base_commit": {"sha": "c" * 40},
                "merge_base_commit": {"sha": "c" * 40},
            },
        )
        for comparison in invalid_comparisons:
            with self.subTest(comparison=comparison):
                transport, inner = self.transport((*base_reads, comparison))
                with self.assertRaises(OrdinaryAgentProviderEvidenceError):
                    read_ordinary_effect_observation(transport, self.landing_record())
                self.assertEqual(len(inner.requests), 5)

    def test_landing_rejects_mismatched_base_before_unbound_reads(self) -> None:
        transport, inner = self.transport((self.landing_pull_request(base_ref="release"),))
        with self.assertRaises(OrdinaryAgentProviderEvidenceError):
            read_ordinary_effect_observation(transport, self.landing_record())
        self.assertEqual(len(inner.requests), 1)

        transport, inner = self.transport(
            (
                self.landing_pull_request(),
                self.landing_merge_commit(),
                {"ref": "refs/heads/release", "object": {"sha": "d" * 40}},
            )
        )
        with self.assertRaises(OrdinaryAgentProviderEvidenceError):
            read_ordinary_effect_observation(transport, self.landing_record())
        self.assertEqual(len(inner.requests), 3)

    def test_noop_containment_compares_immutable_observed_commit_and_rejects_wrong_commit(
        self,
    ) -> None:
        command = effects.CandidateHeadMergeCommand(
            effect=CandidateHeadMergeEffect(
                lineage=MergeTrainEffectLineage(
                    repository="example/repo", base_branch="main", batch_id="batch"
                ),
                candidate_ref="refs/heads/candidate",
                rolling_parent_sha="a" * 40,
                pull_request_number=12,
                head_sha="b" * 40,
            )
        )
        responses = (
            {"ref": command.effect.candidate_ref, "object": {"sha": "a" * 40}},
            {
                "sha": "a" * 40,
                "tree": {"sha": "c" * 40},
                "parents": [{"sha": "b" * 40}],
                "message": "prior merge",
            },
            {
                "status": "ahead",
                "base_commit": {"sha": "b" * 40},
                "merge_base_commit": {"sha": "b" * 40},
            },
        )
        transport, inner = self.transport(responses)
        observation = read_ordinary_effect_observation(transport, effect_record(command))
        assert isinstance(observation, effects.OrdinaryAgentRefObservation)
        self.assertEqual(observation.contained_head_sha, command.effect.head_sha)
        self.assertEqual(
            inner.requests[-1].path, f"/repos/example/repo/compare/{'b' * 40}...{'a' * 40}"
        )
        changed = deepcopy(responses)
        changed[1]["sha"] = "d" * 40
        transport, inner = self.transport(changed)
        with self.assertRaises(OrdinaryAgentProviderEvidenceError):
            read_ordinary_effect_observation(transport, effect_record(command))
        self.assertEqual(len(inner.requests), 2)

    def test_comment_scan_stops_at_three_pages_without_inventing_absence(self) -> None:
        command = effects.StackChildCommentCommand(
            effect=StackChildCommentEffect(
                lineage=MergeTrainEffectLineage(repository="example/repo", base_branch="main"),
                pull_request_number=12,
                body="collapsed",
            )
        )
        transport, inner = self.transport(
            tuple(
                [{"id": index + 1, "body": "unrelated"} for index in range(100)] for _ in range(3)
            )
        )
        observation = read_ordinary_effect_observation(transport, effect_record(command))
        assert isinstance(observation, effects.OrdinaryAgentCommentObservation)
        self.assertFalse(observation.exhausted)
        self.assertIsNone(observation.matching_comment_id)
        self.assertEqual(len(inner.requests), 3)
        self.assertTrue(all(request.method == "GET" for request in inner.requests))

    def test_existing_comment_requires_exact_body_and_effect_marker(self) -> None:
        command = effects.StackChildCommentCommand(
            effect=StackChildCommentEffect(
                lineage=MergeTrainEffectLineage(repository="example/repo", base_branch="main"),
                pull_request_number=12,
                body="collapsed",
            )
        )
        record = effect_record(command)
        body = command.effect.body + "\n\n" + f"<!-- launchplane-effect:{record.effect_id} -->"
        transport, inner = self.transport(
            ([{"id": 4, "body": command.effect.body}, {"id": 5, "body": body}],)
        )
        observation = read_ordinary_effect_observation(transport, record)
        assert isinstance(observation, effects.OrdinaryAgentCommentObservation)
        self.assertEqual(observation.matching_comment_id, "5")
        self.assertEqual(len(inner.requests), 1)

    def test_truncated_label_list_cannot_prove_absence(self) -> None:
        command = effects.StackChildLabelCommand(
            effect=StackChildLabelEffect(
                lineage=MergeTrainEffectLineage(repository="example/repo", base_branch="main"),
                pull_request_number=12,
                label="collapsed",
            )
        )
        transport, inner = self.transport(([{"name": f"other-{index}"} for index in range(100)],))
        with self.assertRaises(OrdinaryAgentProviderEvidenceError):
            read_ordinary_effect_observation(transport, effect_record(command))
        self.assertEqual(len(inner.requests), 1)
