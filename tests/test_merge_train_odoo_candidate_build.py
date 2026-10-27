from types import SimpleNamespace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from typing import Any, cast

from control_plane.contracts.merge_train_batch import build_merge_train_batch_candidate_record
from control_plane.contracts.merge_train_effect import MergeTrainSemanticEffectExecutor
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.merge_train import MergeTrainDryRunSnapshot, build_merge_train_dry_run_result
from control_plane.merge_train_controller_run_once import (
    MergeTrainControllerRunOnceEnvelope,
    _advance_active_candidate_record,
    _advance_passed_candidate_record,
)
from control_plane.merge_train_github import GitHubMergeTrainClient
from control_plane.storage.filesystem import FilesystemRecordStore
from tests.support.profiles import _odoo_preview_profile_payload
from tests.test_merge_train import _pull_request
from tests.test_merge_train_batch_pull_request import _BatchProvider
from tests.test_merge_train_head_check_reuse import _ReuseTransport
from tests.test_merge_train_github import _batch_candidate, _required_check_run
from tests.merge_train_policy_fixtures import (
    dependency_repository_policy,
    build_test_merge_train_policy,
)


class _OdooBuildTransport(_ReuseTransport):
    def __init__(self) -> None:
        super().__init__()
        self.build_status = "completed"
        self.build_conclusion = "success"
        self.build_app = "github-actions"
        self.build_present = True
        self.batch: _BatchProvider | None = None

    def request(self, *, method: str, path: str, body: dict[str, object] | None = None) -> object:
        if self.batch is not None and (
            "/pulls?" in path
            or (method == "POST" and path.endswith("/pulls"))
            or path.endswith(f"/pulls/{self.batch.number}")
            or f"/issues/{self.batch.number}/comments" in path
        ):
            return self.batch.request(method=method, path=path, body=body)
        if "/check-runs?" in path:
            sha = path.split("/commits/")[1].split("/")[0]
            checks = [
                _required_check_run(name, "completed", "success") | {"head_sha": sha}
                for name in self.required_names
            ]
            if self.build_present:
                checks.append(
                    _required_check_run("build", self.build_status, self.build_conclusion)
                    | {"head_sha": sha, "app": {"id": 15368, "slug": self.build_app}}
                )
            return {"check_runs": checks}
        if "/status" in path:
            return {"sha": path.split("/commits/")[1].split("/")[0], "statuses": []}
        return super().request(method=method, path=path, body=body)


class OdooCandidateBuildTests(unittest.TestCase):
    def setUp(self) -> None:
        self.transport = _OdooBuildTransport()
        payload = _odoo_preview_profile_payload("example-odoo")
        payload["repository"] = self.transport.repository_full_name
        self.profile = LaunchplaneProductProfileRecord.model_validate(payload)
        self.store = SimpleNamespace(list_product_profile_records=lambda: (self.profile,))
        self.client = GitHubMergeTrainClient(
            transport=self.transport, branch_refresh_store=self.store
        )
        self.client.begin_snapshot_pass(
            policy=dependency_repository_policy(20, repository=self.transport.repository_full_name),
            read_only=False,
        )
        candidate = _batch_candidate()
        self.candidate = candidate.model_copy(
            update={"entries": candidate.entries[:1], "candidate_sha": "candidate-after-1"}
        )

    def test_equal_tree_odoo_candidate_is_published_and_waits_for_its_own_build(self) -> None:
        built = self.client.build_batch_candidate(candidate=self.candidate)
        self.assertIsNone(built.head_check_reuse)
        self.assertEqual(built.status, "ready_for_checks")
        self.assertEqual(built.candidate_ref, self.candidate.candidate_ref)
        self.assertEqual(
            self.transport.refs[built.candidate_ref.removeprefix("refs/heads/")],
            built.candidate_sha,
        )

    def test_optional_build_blocks_candidate_until_success(self) -> None:
        for present, status, conclusion, publisher, expected in (
            (False, "completed", "success", "github-actions", "pending"),
            (True, "in_progress", "", "github-actions", "pending"),
            (True, "completed", "failure", "github-actions", "fail"),
            (True, "completed", "skipped", "github-actions", "pending"),
            (True, "completed", "success", "other-app", "pending"),
            (True, "completed", "success", "github-actions", "pass"),
        ):
            with self.subTest(
                present=present, status=status, conclusion=conclusion, publisher=publisher
            ):
                self.transport.build_present = present
                self.transport.build_status = status
                self.transport.build_conclusion = conclusion
                self.transport.build_app = publisher
                result = self.client.observe_batch_candidate_checks(candidate=self.candidate)
                self.assertEqual(result.required_checks_status, expected)
                if expected != "pass":
                    self.assertIn("build", result.required_checks_observation_error.lower())

    def test_live_technical_read_rechecks_optional_candidate_build(self) -> None:
        for conclusion, expected in (("failure", "fail"), ("success", "pass")):
            with self.subTest(conclusion=conclusion):
                self.transport.build_conclusion = conclusion
                result = self.client.read_technical_checks(
                    repository=self.candidate.repository,
                    base_branch=self.candidate.base_branch,
                    base_sha=self.candidate.base_sha,
                    head_sha=self.candidate.candidate_sha,
                    evaluated_at="2026-10-26T00:00:00Z",
                )
                self.assertEqual(result.status, expected)

    def test_non_odoo_retains_optional_build_and_head_reuse(self) -> None:
        self.profile = self.profile.model_copy(update={"driver_id": "generic-web"})
        self.transport.build_conclusion = "failure"
        self.assertIsNotNone(
            self.client.build_batch_candidate(candidate=self.candidate).head_check_reuse
        )
        result = self.client.observe_batch_candidate_checks(candidate=self.candidate)
        self.assertEqual(result.required_checks_status, "pass")

    def test_generated_build_pr_covers_combined_and_original_landing_routes(self) -> None:
        for individual, entries in (
            (False, _batch_candidate().entries),
            (True, self.candidate.entries),
        ):
            with self.subTest(individual=individual), TemporaryDirectory() as directory:
                store = FilesystemRecordStore(Path(directory))
                store.write_product_profile_record(self.profile)
                client = GitHubMergeTrainClient(
                    transport=self.transport, branch_refresh_store=store
                )
                candidate = client.build_batch_candidate(
                    candidate=self.candidate.model_copy(update={"entries": entries})
                )
                self.transport.batch = _BatchProvider(candidate)
                policy = build_test_merge_train_policy(repository=candidate.repository)
                queue = build_merge_train_dry_run_result(
                    policy=policy,
                    snapshot=MergeTrainDryRunSnapshot(
                        repository=candidate.repository,
                        base_branch=candidate.base_branch,
                        base_sha=candidate.base_sha,
                        pull_requests=tuple(
                            _pull_request(entry.pull_request_number).model_copy(
                                update={"requires_individual_landing": individual}
                            )
                            for entry in entries
                        ),
                    ),
                    batch_landing=True,
                )
                record = build_merge_train_batch_candidate_record(
                    candidate=candidate, source="test", updated_at=candidate.updated_at
                )
                lease = Mock()
                lease.record.ordinary_job_binding = None
                kwargs: dict[str, Any] = dict(
                    request=MergeTrainControllerRunOnceEnvelope(
                        repository=candidate.repository, mutate=True
                    ),
                    policy=policy,
                    policy_sha256=candidate.policy_sha256,
                    repository_policy=policy.policies[0],
                    transport=self.transport,
                    github_client=client,
                    trace_id="odoo-build-test",
                    recorded_at=candidate.updated_at,
                    candidate_store=store,
                    stack_collapse_store=store,
                    lease=lease,
                )
                self.transport.build_present = False
                with patch(
                    "control_plane.merge_train_controller_run_once._reflow_stale_candidate_record",
                    return_value=(None, queue),
                ):
                    waiting: Any = _advance_active_candidate_record(
                        active_candidate_record=record, **kwargs
                    )
                    self.assertEqual(waiting["candidate"]["required_checks_status"], "pending")
                    self.assertTrue(self.transport.batch.created)
                    self.assertEqual(
                        self.transport.batch.candidate.candidate_sha, candidate.candidate_sha
                    )
                    self.transport.build_present = True
                    passed = client.observe_batch_candidate_checks(candidate=candidate)
                    passed_record = build_merge_train_batch_candidate_record(
                        candidate=passed, source="test", updated_at=candidate.updated_at
                    )
                    self.transport.build_present = False
                    held = _advance_passed_candidate_record(
                        passed_candidate_record=passed_record, landing_store=store, **kwargs
                    )
                    self.assertEqual(held["controller_action"], "observe_candidate")
                    self.transport.build_present = True
                    ready: Any = _advance_passed_candidate_record(
                        passed_candidate_record=passed_record, landing_store=store, **kwargs
                    )
                    self.assertEqual(ready["controller_action"], "plan_landing")
                    self.assertEqual(
                        ready["landing_plan"].get("candidate_pull_request_number"),
                        None if individual else self.transport.batch.number,
                    )
                if individual:
                    plan = store.list_merge_train_batch_landing_plan_records()[0].landing_plan
                    client.cleanup_batch_candidate_ref(
                        landing_plan=plan,
                        effect_executor=cast(MergeTrainSemanticEffectExecutor, Mock()),
                    )
                    self.assertTrue(self.transport.batch.closed)

    def test_failed_full_build_participates_in_existing_failure_recovery(self) -> None:
        self.transport.build_conclusion = "failure"
        failures = self.client.read_candidate_check_failures(candidate=self.candidate)
        self.assertTrue(failures.conclusive)
        self.assertIn("build", failures.check_names)
        self.assertFalse(failures.infrastructure_only)
        self.transport.build_conclusion = "success"
        base = self.client.read_base_check_observation(
            candidate=self.candidate, attribution_requirements=failures.attribution_requirements
        )
        self.assertEqual(base.status, "pass")


if __name__ == "__main__":
    unittest.main()
