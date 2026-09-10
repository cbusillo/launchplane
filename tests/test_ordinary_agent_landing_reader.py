"""Prepared landing reads compose exact identities, policy, diff and authorship."""

import unittest
from copy import deepcopy
from typing import Any

from control_plane.contracts.ordinary_agent_snapshot import OrdinaryAgentLandingEvidence
from control_plane.contracts.merge_train_batch import build_merge_train_batch_landing_plan
from control_plane.merge_train_github import RecordingMergeTrainGitHubTransport
from control_plane.ordinary_agent_github_transport import (
    DeadlineMergeTrainGitHubTransport,
    OrdinaryAgentProviderEvidenceError,
)
from control_plane.ordinary_agent_landing_reader import read_ordinary_agent_landing_evidence
from tests import test_ordinary_agent_landing_storage as landing_support
from tests.merge_train_policy_fixtures import build_test_merge_train_policy


class LandingReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = landing_support.OrdinaryAgentLandingStorageTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.preparation = self.fixture.reserve().preparation
        p = self.preparation
        c = self.fixture.candidate.candidate
        self.policy = build_test_merge_train_policy(repository=c.repository).find_repository_policy(
            repository=c.repository,
            base_branch=c.base_branch,
        )
        identity = {"databaseId": p.target.repository_id, "nameWithOwner": c.repository}
        self.user = {"id": 202, "login": "operator", "type": "User"}
        self.response: dict[str, Any] = {
            "data": {
                "rateLimit": {"cost": 1},
                "repository": {
                    **identity,
                    "owner": {"databaseId": 202},
                    "ref": {
                        "name": c.base_branch,
                        "target": {
                            "oid": p.expected_base_sha,
                            "tree": {"oid": p.expected_base_tree_sha},
                        },
                        "branchProtectionRule": {
                            "requiresStatusChecks": True,
                            "requiresStrictStatusChecks": True,
                            "requiredStatusChecks": [{"context": "ci", "app": {"databaseId": 100}}],
                        },
                        "compare": {
                            "status": "AHEAD",
                            "baseTarget": {"oid": p.expected_base_sha},
                            "headTarget": {"oid": c.candidate_sha},
                        },
                    },
                    "candidate": {
                        "oid": c.candidate_sha,
                        "tree": {"oid": c.candidate_tree_sha},
                        "statusCheckRollup": {
                            "contexts": {
                                "totalCount": 1,
                                "pageInfo": {"hasNextPage": False},
                                "nodes": [
                                    {
                                        "__typename": "CheckRun",
                                        "name": "ci",
                                        "status": "COMPLETED",
                                        "conclusion": "SUCCESS",
                                        "checkSuite": {"app": {"databaseId": 100}},
                                    }
                                ],
                            }
                        },
                    },
                    "head0": {
                        "oid": p.entry.expected_head_sha,
                        "tree": {"oid": p.entry.expected_head_tree_sha},
                    },
                    "pr0": {
                        "number": p.entry.pull_request_number,
                        "headRefOid": p.entry.expected_head_sha,
                        "baseRefOid": p.expected_base_sha,
                        "baseRefName": c.base_branch,
                        "updatedAt": "2026-09-09T00:00:00Z",
                        "state": "OPEN",
                        "mergeCommit": None,
                        "headRef": {"name": "topic"},
                        "headRefName": "topic",
                        "headRepository": identity,
                        "baseRepository": identity,
                        "url": "https://example.test/pull/1",
                        "title": "Change",
                        "createdAt": "2026-09-09T00:00:00Z",
                        "isDraft": False,
                        "mergeable": "MERGEABLE",
                        "authorAssociation": "OWNER",
                        "author": {"__typename": "User", "databaseId": 202, "login": "operator"},
                        "labels": {
                            "totalCount": 1,
                            "pageInfo": {"hasNextPage": False},
                            "nodes": [{"name": self.policy.enqueue_label}],
                        },
                    },
                },
            }
        }

    def read(
        self, final: object
    ) -> tuple[OrdinaryAgentLandingEvidence, RecordingMergeTrainGitHubTransport]:
        inner = RecordingMergeTrainGitHubTransport(
            responses=(
                self.response,
                [],
                [
                    {
                        "filename": "control_plane/new.py",
                        "previous_filename": "control_plane/old.py",
                        "status": "renamed",
                    }
                ],
                *(
                    ()
                    if self.response["data"]["repository"]["pr0"]["author"] is None
                    else ([{"sha": "a" * 40, "author": self.user, "committer": self.user}],)
                ),
                final,
            )
        )
        transport = DeadlineMergeTrainGitHubTransport(
            transport=inner,
            work_deadline=75,
            token_deadline=300,
            monotonic=lambda: 0,
        )
        result = read_ordinary_agent_landing_evidence(
            transport=transport,
            preparation=self.preparation,
            candidate_record=self.fixture.candidate,
            landing_plan_record=self.fixture.plan,
            repository_owner_id=202,
            repository_policy=self.policy,
            utc_seconds=lambda: self.preparation.reserved_at,
        )
        return result, inner

    def configure_selected_no_op(self, *, state: str) -> None:
        candidate = self.fixture.candidate.candidate
        provenance = candidate.structural_provenance
        assert provenance is not None
        step = provenance.steps[0]
        no_op_step = step.model_copy(
            update={
                "result_sha": step.parent_sha,
                "result_tree_sha": step.parent_tree_sha,
                "kind": "no_op_already_contained",
            }
        )
        provenance_payload = provenance.model_dump(mode="python")
        provenance_payload.update(
            {
                "steps": (no_op_step,),
                "candidate_sha": step.parent_sha,
                "candidate_tree_sha": step.parent_tree_sha,
                "provenance_sha256": "",
                "candidate_sha256": "",
            }
        )
        no_op_provenance = type(provenance).model_validate(provenance_payload)
        candidate_payload = candidate.model_dump(mode="python")
        candidate_payload.update(
            {
                "candidate_sha": step.parent_sha,
                "candidate_tree_sha": step.parent_tree_sha,
                "candidate_sha256": "",
                "structural_provenance": no_op_provenance,
            }
        )
        no_op_candidate = type(candidate).model_validate(candidate_payload)
        self.fixture.candidate = self.fixture.candidate.model_copy(
            update={"candidate": no_op_candidate}
        )
        landing_plan = build_merge_train_batch_landing_plan(
            candidate=no_op_candidate,
            merge_method="merge",
            created_at=self.fixture.plan.landing_plan.created_at,
        )
        self.fixture.plan = self.fixture.plan.model_copy(update={"landing_plan": landing_plan})
        selected = landing_plan.entries[0]
        self.preparation = self.preparation.model_copy(
            update={
                "landing_plan_sha256": landing_plan.landing_plan_sha256,
                "entry": selected,
                "expected_merge_tree_sha": selected.recorded_candidate_result_tree_sha,
            }
        )
        repository = self.response["data"]["repository"]
        repository["candidate"]["oid"] = no_op_candidate.candidate_sha
        repository["candidate"]["tree"]["oid"] = no_op_candidate.candidate_tree_sha
        repository["ref"]["compare"]["headTarget"]["oid"] = no_op_candidate.candidate_sha
        pull_request = repository["pr0"]
        pull_request["state"] = state
        if state == "OPEN":
            pull_request["mergeCommit"] = None
        else:
            pull_request["headRef"] = None
            pull_request["headRefName"] = None
            pull_request["mergeCommit"] = {"oid": "f" * 40} if state == "MERGED" else None

    def test_prepared_reader_preserves_rename_and_authorship_without_source_check_invention(
        self,
    ) -> None:
        evidence, inner = self.read(deepcopy(self.response))
        repository = evidence.repository_evidence
        assert repository.authorship is not None
        self.assertEqual(repository.authorship.resolution, "resolved")
        self.assertEqual(repository.authorship.contributor_github_ids, (202,))
        self.assertEqual(repository.changed_files[0].previous_path, "control_plane/old.py")
        self.assertEqual(evidence.technical_checks.status, "pass")
        self.assertEqual(evidence.snapshot.pull_requests[0].required_checks_status, "unknown")
        self.assertEqual(evidence.snapshot.pull_requests[0].actor_role, "repo_owner")
        self.assertEqual(
            evidence.counts.rest_core_requests + evidence.counts.graphql_requests,
            len(inner.requests),
        )
        self.assertEqual(len(inner.requests), 5)
        self.assertEqual(evidence.observed_at, self.preparation.reserved_at)

    def test_changed_base_during_diff_acquisition_never_returns_evidence(self) -> None:
        changed = deepcopy(self.response)
        changed["data"]["repository"]["pr0"]["baseRefOid"] = "f" * 40
        with self.assertRaises(OrdinaryAgentProviderEvidenceError):
            self.read(changed)

    def test_mismatched_preparation_head_is_rejected_before_provider_io(self) -> None:
        self.preparation = self.preparation.model_copy(
            update={
                "entry": self.preparation.entry.model_copy(update={"expected_head_sha": "f" * 40})
            }
        )
        with self.assertRaisesRegex(
            OrdinaryAgentProviderEvidenceError, "landing_read_scope_mismatch"
        ):
            self.read(deepcopy(self.response))

    def test_deleted_author_stays_unknown_without_permission_lookup(self) -> None:
        self.response["data"]["repository"]["pr0"]["author"] = None
        evidence, inner = self.read(deepcopy(self.response))
        entry = evidence.snapshot.pull_requests[0]
        self.assertIsNone(entry.actor_id)
        self.assertEqual(entry.actor_role, "unknown")
        self.assertFalse(any("/collaborators/" in item.path for item in inner.requests))

    def test_selected_proven_no_op_preserves_explicit_lifecycle_in_queue_evidence(self) -> None:
        preparation = self.preparation
        candidate = self.fixture.candidate
        plan = self.fixture.plan
        response = deepcopy(self.response)
        for state in ("OPEN", "CLOSED", "MERGED"):
            with self.subTest(state=state):
                self.preparation = preparation
                self.fixture.candidate = candidate
                self.fixture.plan = plan
                self.response = deepcopy(response)
                self.configure_selected_no_op(state=state)

                evidence, _ = self.read(deepcopy(self.response))

                self.assertEqual(len(evidence.snapshot.pull_requests), 1)
                self.assertEqual(evidence.snapshot.pull_requests[0].state, state.lower())
                self.assertEqual(
                    evidence.snapshot.pull_requests[0].head_sha,
                    self.preparation.entry.expected_head_sha,
                )

    def test_non_no_op_selected_entry_still_requires_open_lifecycle(self) -> None:
        self.response["data"]["repository"]["pr0"]["state"] = "CLOSED"
        self.response["data"]["repository"]["pr0"]["headRef"] = None
        self.response["data"]["repository"]["pr0"]["headRefName"] = None

        with self.assertRaisesRegex(
            OrdinaryAgentProviderEvidenceError, "landing_entry_identity_mismatch"
        ):
            self.read(deepcopy(self.response))
