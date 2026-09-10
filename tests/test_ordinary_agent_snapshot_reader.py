"""Concrete source/check observations preserve identity and never invent passing checks."""

from copy import deepcopy
import unittest

from pydantic import ValidationError

from control_plane.contracts.ordinary_agent import OrdinaryAgentPullRequest
from control_plane.contracts.ordinary_agent_snapshot import (
    OrdinaryAgentMergeTrainSnapshotResult,
    OrdinaryAgentReadmissionObservation,
)

from control_plane.merge_train import build_merge_train_dry_run_result
from control_plane.merge_train_github import RecordingMergeTrainGitHubTransport
from control_plane.ordinary_agent_github_transport import (
    DeadlineMergeTrainGitHubTransport,
    OrdinaryAgentProviderEvidenceError,
)
from control_plane.ordinary_agent_snapshot_reader import (
    read_ordinary_controller_snapshot,
    read_ordinary_candidate_check,
)
from tests import test_ordinary_agent_landing_reader as reader_support
from tests.merge_train_policy_fixtures import build_test_merge_train_policy


class OrdinarySnapshotReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = reader_support.LandingReaderTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.request = self.fixture.fixture.request
        self.response = deepcopy(self.fixture.response)
        repository = self.response["data"]["repository"]
        repository["pr0"]["mergeable"] = "MERGEABLE"
        repository["pr0"]["labels"]["nodes"] = [{"name": self.fixture.policy.enqueue_label}]
        repository["pr0"]["labels"]["totalCount"] = 1
        repository["head0"]["statusCheckRollup"] = deepcopy(
            repository["candidate"]["statusCheckRollup"]
        )
        repository["ref"]["compare0"] = deepcopy(repository["ref"]["compare"])
        repository["ref"]["compare0"]["headTarget"]["oid"] = self.request.pull_requests[0].head_sha

    def transport(
        self,
    ) -> tuple[RecordingMergeTrainGitHubTransport, DeadlineMergeTrainGitHubTransport]:
        inner = RecordingMergeTrainGitHubTransport(responses=(self.response, []))
        return inner, DeadlineMergeTrainGitHubTransport(
            transport=inner, work_deadline=90, token_deadline=300, monotonic=lambda: 0
        )

    def test_initial_source_checks_and_identity_are_observed_with_one_policy_read(self) -> None:
        inner, transport = self.transport()
        result = read_ordinary_controller_snapshot(
            transport=transport,
            request=self.request,
            repository_owner_id=202,
            repository_policy=self.fixture.policy,
        )
        assert isinstance(result, OrdinaryAgentMergeTrainSnapshotResult)
        self.assertEqual(result.snapshot.pull_requests[0].required_checks_status, "pass")
        self.assertEqual(
            result.head_identities[0].identity.sha, self.request.pull_requests[0].head_sha
        )
        self.assertEqual(
            (
                result.counts.rest_core_requests,
                result.counts.graphql_requests,
                result.counts.graphql_points,
            ),
            (1, 1, 1),
        )
        self.assertEqual([item.method for item in inner.requests], ["POST", "GET"])

    def test_unknown_source_checks_cannot_enter_candidate_planning(self) -> None:
        self.response["data"]["repository"]["head0"]["statusCheckRollup"] = None
        _, transport = self.transport()
        result = read_ordinary_controller_snapshot(
            transport=transport,
            request=self.request,
            repository_owner_id=202,
            repository_policy=self.fixture.policy,
        )
        assert isinstance(result, OrdinaryAgentMergeTrainSnapshotResult)
        self.assertEqual(result.snapshot.pull_requests[0].required_checks_status, "unknown")
        decision = build_merge_train_dry_run_result(
            policy=build_test_merge_train_policy(repository=self.request.target.repository),
            snapshot=result.snapshot,
        )
        self.assertEqual(decision.intended_next_action, "wait_for_checks")

    def test_source_drift_returns_exact_evidence_before_rule_or_role_requests(self) -> None:
        cases = (
            ("base", "OPEN", "d" * 40, self.request.pull_requests[0].head_sha),
            ("head", "CLOSED", self.request.base_sha, "e" * 40),
            ("base_and_head", "MERGED", "d" * 40, "e" * 40),
        )
        for drift, lifecycle, base_sha, head_sha in cases:
            with self.subTest(drift=drift, lifecycle=lifecycle):
                self.response = deepcopy(self.fixture.response)
                repository = self.response["data"]["repository"]
                repository["ref"]["target"]["oid"] = base_sha
                repository["pr0"]["headRefOid"] = head_sha
                repository["pr0"]["state"] = lifecycle
                inner, transport = self.transport()
                result = read_ordinary_controller_snapshot(
                    transport=transport,
                    request=self.request,
                    repository_owner_id=202,
                    repository_policy=self.fixture.policy,
                    utc_seconds=lambda: 1_789_000_000,
                )
                self.assertIsInstance(result, OrdinaryAgentReadmissionObservation)
                assert isinstance(result, OrdinaryAgentReadmissionObservation)
                self.assertEqual(result.drift, drift)
                self.assertEqual(result.observed_at, 1_789_000_000)
                self.assertEqual(result.target, self.request.target)
                self.assertEqual(result.captured_base_sha, self.request.base_sha)
                self.assertEqual(result.captured_pull_requests, self.request.pull_requests)
                self.assertEqual(result.base_identity.sha, base_sha)
                self.assertEqual(result.pull_requests[0].head_sha, head_sha)
                self.assertEqual(result.pull_requests[0].lifecycle, lifecycle.lower())
                self.assertEqual(
                    (
                        result.counts.rest_core_requests,
                        result.counts.graphql_requests,
                        result.counts.graphql_points,
                    ),
                    (0, 1, 1),
                )
                self.assertEqual([item.method for item in inner.requests], ["POST"])

    def test_drift_does_not_hide_invalid_pull_request_identity(self) -> None:
        repository = self.response["data"]["repository"]
        repository["ref"]["target"]["oid"] = "d" * 40
        repository["pr0"]["headRepository"]["databaseId"] = 999
        inner, transport = self.transport()
        with self.assertRaisesRegex(
            OrdinaryAgentProviderEvidenceError, "snapshot_repository_mismatch"
        ):
            read_ordinary_controller_snapshot(
                transport=transport,
                request=self.request,
                repository_owner_id=202,
                repository_policy=self.fixture.policy,
            )
        self.assertEqual([item.method for item in inner.requests], ["POST"])

    def test_malformed_observed_base_identity_fails_closed(self) -> None:
        for field in ("oid", "tree"):
            with self.subTest(field=field):
                self.response = deepcopy(self.fixture.response)
                target = self.response["data"]["repository"]["ref"]["target"]
                if field == "tree":
                    target["tree"]["oid"] = "not-a-commit-identity"
                else:
                    target[field] = "not-a-commit-identity"
                inner, transport = self.transport()
                with self.assertRaisesRegex(
                    OrdinaryAgentProviderEvidenceError, "snapshot_identity_missing"
                ):
                    read_ordinary_controller_snapshot(
                        transport=transport,
                        request=self.request,
                        repository_owner_id=202,
                        repository_policy=self.fixture.policy,
                    )
                self.assertEqual([item.method for item in inner.requests], ["POST"])

    def test_readmission_evidence_preserves_ordered_heads_and_validates_digest(self) -> None:
        repository = self.response["data"]["repository"]
        second_captured_head = "c" * 40
        second_observed_head = "d" * 40
        request = self.request.model_copy(
            update={
                "pull_requests": self.request.pull_requests
                + (OrdinaryAgentPullRequest(number=13, head_sha=second_captured_head),)
            }
        )
        repository["pr0"]["headRefOid"] = "e" * 40
        repository["pr1"] = deepcopy(repository["pr0"])
        repository["pr1"].update(number=13, headRefOid=second_observed_head)
        repository["head1"] = deepcopy(repository["head0"])
        repository["head1"]["oid"] = second_captured_head
        repository["ref"]["compare1"] = deepcopy(repository["ref"]["compare0"])
        repository["ref"]["compare1"]["headTarget"]["oid"] = second_captured_head
        inner, transport = self.transport()
        result = read_ordinary_controller_snapshot(
            transport=transport,
            request=request,
            repository_owner_id=202,
            repository_policy=self.fixture.policy,
        )
        self.assertIsInstance(result, OrdinaryAgentReadmissionObservation)
        assert isinstance(result, OrdinaryAgentReadmissionObservation)
        self.assertEqual(
            tuple((item.number, item.head_sha) for item in result.pull_requests),
            ((request.pull_requests[0].number, "e" * 40), (13, second_observed_head)),
        )
        self.assertEqual([item.method for item in inner.requests], ["POST"])
        payload = result.model_dump(mode="python")
        payload["observation_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValidationError, "digest does not match"):
            OrdinaryAgentReadmissionObservation.model_validate(payload)

    def test_candidate_checks_are_bound_to_candidate_instead_of_source_head(self) -> None:
        inner, transport = self.transport()
        candidate_sha = self.response["data"]["repository"]["candidate"]["oid"]
        result = read_ordinary_candidate_check(
            transport=transport,
            request=self.request,
            repository_owner_id=202,
            candidate_sha=candidate_sha,
        )
        self.assertEqual((result.candidate_identity.sha, result.status), (candidate_sha, "pass"))
        self.assertEqual(len(inner.requests), 2)

    def test_strict_base_drift_requests_refresh_instead_of_reporting_failed_ci(self) -> None:
        self.response["data"]["repository"]["ref"]["compare0"]["status"] = "DIVERGED"
        _, transport = self.transport()
        result = read_ordinary_controller_snapshot(
            transport=transport,
            request=self.request,
            repository_owner_id=202,
            repository_policy=self.fixture.policy,
        )
        assert isinstance(result, OrdinaryAgentMergeTrainSnapshotResult)
        decision = build_merge_train_dry_run_result(
            policy=build_test_merge_train_policy(repository=self.request.target.repository),
            snapshot=result.snapshot,
        )
        self.assertEqual(result.snapshot.pull_requests[0].required_checks_status, "pass")
        self.assertEqual(decision.intended_next_action, "update_branch")

    def test_two_source_heads_share_policy_and_roles_without_sharing_check_results(self) -> None:
        repository = self.response["data"]["repository"]
        second_head = "c" * 40
        request = self.request.model_copy(
            update={
                "pull_requests": self.request.pull_requests
                + (OrdinaryAgentPullRequest(number=13, head_sha=second_head),)
            }
        )
        repository["pr1"] = deepcopy(repository["pr0"])
        repository["pr1"].update(number=13, headRefOid=second_head)
        repository["head1"] = deepcopy(repository["head0"])
        repository["head1"]["oid"] = second_head
        repository["head1"]["statusCheckRollup"]["contexts"]["nodes"][0].update(
            status="IN_PROGRESS", conclusion=None
        )
        repository["ref"]["compare1"] = deepcopy(repository["ref"]["compare0"])
        repository["ref"]["compare1"]["headTarget"]["oid"] = second_head
        for index in (0, 1):
            repository[f"pr{index}"]["authorAssociation"] = "MEMBER"
            repository[f"pr{index}"]["author"] = {
                "__typename": "User",
                "databaseId": 500 + index,
                "login": f"admin-{index}",
            }
        admins = [
            {"id": 500 + i, "login": f"admin-{i}", "permissions": {"admin": True}} for i in (0, 1)
        ]
        inner = RecordingMergeTrainGitHubTransport(responses=(self.response, [], admins))
        transport = DeadlineMergeTrainGitHubTransport(
            transport=inner, work_deadline=90, token_deadline=300, monotonic=lambda: 0
        )
        result = read_ordinary_controller_snapshot(
            transport=transport,
            request=request,
            repository_owner_id=202,
            repository_policy=self.fixture.policy,
        )
        assert isinstance(result, OrdinaryAgentMergeTrainSnapshotResult)
        self.assertEqual(
            [item.required_checks_status for item in result.snapshot.pull_requests],
            ["pass", "pending"],
        )
        self.assertEqual(
            [item.actor_role for item in result.snapshot.pull_requests],
            ["repo_admin", "repo_admin"],
        )
        self.assertEqual((result.counts.rest_core_requests, result.counts.graphql_requests), (2, 1))
        self.assertEqual(len(inner.requests), 3)
