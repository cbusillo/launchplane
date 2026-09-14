import unittest

from pydantic import ValidationError

from control_plane.contracts.merge_train_batch import (
    MergeTrainBatchLandingEntry,
    MergeTrainBatchLandingPlan,
)
from control_plane.contracts.merge_train_historical_completion import (
    MergeTrainHistoricalCompletionEvidence,
    MergeTrainHistoricalCompletionSelector,
)
from control_plane.merge_train_github import (
    GitHubMergeTrainClient,
    MergeTrainHistoricalCompletionProofError,
    MergeTrainGitHubError,
    RecordingMergeTrainGitHubTransport,
)


def _branch(sha: str, tree_sha: str) -> dict[str, object]:
    return {"commit": {"sha": sha, "commit": {"tree": {"sha": tree_sha}}}}


def _merged_pull_request(
    number: int,
    *,
    head_sha: str,
    merge_commit_sha: str,
    state: str = "closed",
    merged: bool = True,
) -> dict[str, object]:
    return {
        "state": state,
        "merged": merged,
        "base": {"ref": "main"},
        "head": {"sha": head_sha},
        "merge_commit_sha": merge_commit_sha,
        "number": number,
    }


def _commit(
    sha: str,
    tree_sha: str,
    *,
    parents: tuple[str, ...] = (),
) -> dict[str, object]:
    return {
        "sha": sha,
        "tree": {"sha": tree_sha},
        "parents": [{"sha": parent} for parent in parents],
    }


def _compare(merge_sha: str, *, status: str = "ahead") -> dict[str, object]:
    return {"status": status, "merge_base_commit": {"sha": merge_sha}}


def _entry(
    number: int,
    position: int,
    *,
    parent_sha: str,
    parent_tree_sha: str,
    result_sha: str,
    result_tree_sha: str,
) -> MergeTrainBatchLandingEntry:
    return MergeTrainBatchLandingEntry(
        pull_request_number=number,
        position=position,
        expected_head_sha=f"head-{number}",
        expected_head_tree_sha=f"tree-head-{number}",
        expected_base_sha="base-0",
        merge_method="merge",
        recorded_candidate_parent_sha=parent_sha,
        recorded_candidate_parent_tree_sha=parent_tree_sha,
        recorded_candidate_result_sha=result_sha,
        recorded_candidate_result_tree_sha=result_tree_sha,
    )


def _plan(
    *, entries: tuple[MergeTrainBatchLandingEntry, ...] | None = None
) -> MergeTrainBatchLandingPlan:
    return MergeTrainBatchLandingPlan(
        plan_id="plan-1",
        batch_id="batch-1",
        repository="example/repository",
        base_branch="main",
        candidate_ref="refs/heads/candidate",
        candidate_sha="candidate-sha",
        policy_key="policy-key",
        policy_sha256="policy-sha",
        entries=entries
        or (
            _entry(
                1,
                1,
                parent_sha="base-0",
                parent_tree_sha="tree-base-0",
                result_sha="candidate-result-1",
                result_tree_sha="tree-merge-1",
            ),
            _entry(
                2,
                2,
                parent_sha="merge-1",
                parent_tree_sha="tree-merge-1",
                result_sha="candidate-result-2",
                result_tree_sha="tree-merge-2",
            ),
        ),
        created_at="2026-09-13T12:00:00Z",
    )


def _responses(
    *, final_sha: str = "pinned-base", final_tree_sha: str = "pinned-tree"
) -> tuple[object, ...]:
    return (
        _branch("pinned-base", "pinned-tree"),
        _merged_pull_request(1, head_sha="head-1", merge_commit_sha="merge-1"),
        _commit("head-1", "tree-head-1"),
        _commit("merge-1", "tree-merge-1", parents=("base-0", "head-1")),
        _commit("base-0", "tree-base-0"),
        _compare("merge-1"),
        _merged_pull_request(2, head_sha="head-2", merge_commit_sha="merge-2"),
        _commit("head-2", "tree-head-2"),
        _commit("merge-2", "tree-merge-2", parents=("merge-1", "head-2")),
        _commit("merge-1", "tree-merge-1"),
        _compare("merge-2"),
        _branch(final_sha, final_tree_sha),
    )


class _ReadOnlyHistoricalCompletionTransport(RecordingMergeTrainGitHubTransport):
    def request(self, *, method: str, path: str, body: dict[str, object] | None = None) -> object:
        if method != "GET":
            raise AssertionError(f"historical completion provider attempted {method}")
        return super().request(method=method, path=path, body=body)


class HistoricalCompletionProviderTests(unittest.TestCase):
    def test_observes_ordered_multi_entry_completion_without_provider_effect(self) -> None:
        transport = _ReadOnlyHistoricalCompletionTransport(responses=_responses())

        evidence = GitHubMergeTrainClient(transport=transport).observe_historical_batch_completion(
            landing_plan=_plan(),
            observed_at="2026-09-13T12:01:00Z",
        )

        self.assertFalse(evidence.provider_effect_attempted)
        self.assertEqual(evidence.observed_base_sha, "pinned-base")
        self.assertEqual(evidence.observed_base_tree_sha, "pinned-tree")
        self.assertEqual(evidence.final_observed_base_sha, "pinned-base")
        self.assertEqual(
            tuple(
                (item.pull_request_number, item.observed_parent_sha) for item in evidence.entries
            ),
            ((1, "base-0"), (2, "merge-1")),
        )
        self.assertEqual(
            tuple(item.observed_merge_commit_tree_sha for item in evidence.entries),
            ("tree-merge-1", "tree-merge-2"),
        )
        self.assertTrue(all(request.method == "GET" for request in transport.requests))
        self.assertIn(
            "/compare/merge-2...pinned-base",
            transport.requests[-2].path,
        )

    def test_rejects_wrong_head_before_following_provider_reads(self) -> None:
        responses = list(_responses())
        responses[1] = _merged_pull_request(
            1, head_sha="different-head", merge_commit_sha="merge-1"
        )
        transport = _ReadOnlyHistoricalCompletionTransport(responses=tuple(responses))

        with self.assertRaises(MergeTrainHistoricalCompletionProofError) as raised:
            GitHubMergeTrainClient(transport=transport).observe_historical_batch_completion(
                landing_plan=_plan(), observed_at="observed"
            )

        self.assertEqual(raised.exception.proof_status, "unsupported")
        self.assertEqual(raised.exception.reason_code, "provider_binding_mismatch")
        self.assertEqual(len(transport.requests), 2)
        self.assertTrue(all(request.method == "GET" for request in transport.requests))

    def test_rejects_wrong_parent_tree_and_result_tree(self) -> None:
        for response_index in (3, 4):
            responses = list(_responses())
            if response_index == 3:
                responses[3] = _commit(
                    "merge-1", "tree-merge-1", parents=("different-parent", "head-1")
                )
            else:
                responses[3] = _commit(
                    "merge-1", "different-result-tree", parents=("base-0", "head-1")
                )
            transport = _ReadOnlyHistoricalCompletionTransport(responses=tuple(responses))

            with (
                self.subTest(response_index=response_index),
                self.assertRaises(MergeTrainHistoricalCompletionProofError) as raised,
            ):
                GitHubMergeTrainClient(transport=transport).observe_historical_batch_completion(
                    landing_plan=_plan(), observed_at="observed"
                )
            self.assertEqual(raised.exception.reason_code, "provider_binding_mismatch")

    def test_provider_failures_are_closed_without_provider_details(self) -> None:
        unavailable = _ReadOnlyHistoricalCompletionTransport(
            responses=(
                _branch("pinned-base", "pinned-tree"),
                MergeTrainGitHubError("secret provider payload", status_code=503),
            )
        )
        with self.assertRaises(MergeTrainHistoricalCompletionProofError) as raised:
            GitHubMergeTrainClient(transport=unavailable).observe_historical_batch_completion(
                landing_plan=_plan(), observed_at="observed"
            )
        self.assertEqual(raised.exception.proof_status, "indeterminate")
        self.assertEqual(raised.exception.reason_code, "provider_unavailable")
        self.assertNotIn("secret", str(raised.exception))

        malformed = _ReadOnlyHistoricalCompletionTransport(
            responses=(_branch("pinned-base", "pinned-tree"), [])
        )
        with self.assertRaises(MergeTrainHistoricalCompletionProofError) as raised:
            GitHubMergeTrainClient(transport=malformed).observe_historical_batch_completion(
                landing_plan=_plan(), observed_at="observed"
            )
        self.assertEqual(raised.exception.proof_status, "indeterminate")
        self.assertEqual(raised.exception.reason_code, "provider_response_malformed")

        malformed_compare = list(_responses())
        malformed_compare[5] = _compare("merge-1", status="unknown")
        transport = _ReadOnlyHistoricalCompletionTransport(responses=tuple(malformed_compare))
        with self.assertRaises(MergeTrainHistoricalCompletionProofError) as raised:
            GitHubMergeTrainClient(transport=transport).observe_historical_batch_completion(
                landing_plan=_plan(), observed_at="observed"
            )
        self.assertEqual(raised.exception.proof_status, "indeterminate")
        self.assertEqual(raised.exception.reason_code, "provider_response_malformed")

    def test_rejects_bad_containment_and_target_movement(self) -> None:
        responses = list(_responses())
        responses[5] = _compare("different-merge", status="behind")
        transport = _ReadOnlyHistoricalCompletionTransport(responses=tuple(responses))
        with self.assertRaises(MergeTrainHistoricalCompletionProofError) as raised:
            GitHubMergeTrainClient(transport=transport).observe_historical_batch_completion(
                landing_plan=_plan(), observed_at="observed"
            )
        self.assertEqual(raised.exception.reason_code, "base_not_contains_merge")
        self.assertEqual(len(transport.requests), 6)

        responses = list(_responses(final_sha="moved-base"))
        transport = _ReadOnlyHistoricalCompletionTransport(responses=tuple(responses))
        with self.assertRaises(MergeTrainHistoricalCompletionProofError) as raised:
            GitHubMergeTrainClient(transport=transport).observe_historical_batch_completion(
                landing_plan=_plan(), observed_at="observed"
            )
        self.assertEqual(raised.exception.proof_status, "indeterminate")
        self.assertEqual(raised.exception.reason_code, "target_moved")
        self.assertEqual(len(transport.requests), 12)

    def test_invalid_plan_unmerged_entry_and_bound_never_write_provider(self) -> None:
        invalid = _plan(
            entries=(_plan().entries[0].model_copy(update={"expected_head_tree_sha": ""}),)
        )
        transport = _ReadOnlyHistoricalCompletionTransport(responses=())
        with self.assertRaises(MergeTrainHistoricalCompletionProofError) as raised:
            GitHubMergeTrainClient(transport=transport).observe_historical_batch_completion(
                landing_plan=invalid, observed_at="observed"
            )
        self.assertEqual(raised.exception.reason_code, "plan_invalid")
        self.assertEqual(transport.requests, [])

        unmerged_responses = list(_responses())
        unmerged_responses[1] = _merged_pull_request(
            1, head_sha="head-1", merge_commit_sha="merge-1", state="open", merged=False
        )
        transport = _ReadOnlyHistoricalCompletionTransport(responses=tuple(unmerged_responses))
        with self.assertRaises(MergeTrainHistoricalCompletionProofError) as raised:
            GitHubMergeTrainClient(transport=transport).observe_historical_batch_completion(
                landing_plan=_plan(), observed_at="observed"
            )
        self.assertEqual(raised.exception.reason_code, "plan_unmerged")
        self.assertEqual(len(transport.requests), 2)

        oversized_entries = tuple(
            _entry(
                number,
                number,
                parent_sha="base-0",
                parent_tree_sha="tree-base-0",
                result_sha=f"merge-{number}",
                result_tree_sha=f"tree-merge-{number}",
            )
            for number in range(1, 27)
        )
        transport = _ReadOnlyHistoricalCompletionTransport(responses=())
        with self.assertRaises(MergeTrainHistoricalCompletionProofError) as raised:
            GitHubMergeTrainClient(transport=transport).observe_historical_batch_completion(
                landing_plan=_plan(entries=oversized_entries), observed_at="observed"
            )
        self.assertEqual(raised.exception.reason_code, "plan_bound")
        self.assertEqual(transport.requests, [])

    def test_contracts_are_strict_and_trim_required_text(self) -> None:
        selector = MergeTrainHistoricalCompletionSelector.model_validate(
            {
                "expected_active_record_id": " active ",
                "expected_effect_sha": " effect ",
                "expected_policy_sha256": " policy ",
                "expected_landing_plan_id": " plan ",
                "expected_entries": (
                    {
                        "position": 1,
                        "pull_request_number": 1,
                        "expected_head_sha": "head",
                        "expected_head_tree_sha": "tree",
                    },
                ),
            }
        )
        self.assertEqual(selector.expected_active_record_id, "active")
        with self.assertRaises(ValidationError):
            MergeTrainHistoricalCompletionSelector.model_validate(
                {
                    "expected_active_record_id": "active",
                    "expected_effect_sha": "effect",
                    "expected_policy_sha256": "policy",
                    "expected_landing_plan_id": "plan",
                    "expected_entries": (
                        {
                            "position": 1,
                            "pull_request_number": 1,
                            "expected_head_sha": "head",
                            "expected_head_tree_sha": "tree",
                        },
                    ),
                    "extra": "forbidden",
                }
            )
        with self.assertRaises(ValidationError):
            MergeTrainHistoricalCompletionEvidence.model_validate(
                {
                    "classification": "observed_merged_without_admission",
                    "authority_state": "observation_only",
                    "source_landing_plan_record_id": "record",
                    "source_landing_plan_sha256": "plan-sha",
                    "controller_key": "controller",
                    "repository": "repo",
                    "base_branch": "main",
                    "landing_plan_id": "plan",
                    "batch_id": "batch",
                    "candidate_sha": "candidate",
                    "candidate_sha256": "candidate-sha",
                    "policy_key": "policy",
                    "policy_sha256": "policy-sha",
                    "trace_id": "trace",
                    "provider_evidence": {},
                    "unexpected": "field",
                }
            )
