import unittest
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import quote
from click.testing import CliRunner

from control_plane.config_authority_comparison import (
    config_authority_comparison_id,
    current_config_authority_status,
)
from control_plane.contracts.advisory_check_projection import CONFIG_AUTHORITY_CHECK_NAME
from control_plane.contracts.merge_train_policy import MergeTrainPolicyRecord
from control_plane.contracts.merge_train_branch_refresh_record import MergeTrainBranchRefreshRecord
from control_plane.contracts.merge_train_batch import (
    MergeTrainBatchCandidateRecord,
    MergeTrainBatchCandidate,
    build_merge_train_batch_candidate_record,
)
from control_plane.merge_train_github import (
    GitHubMergeTrainClient,
    GitHubMergeTrainSnapshotReader,
    RecordingMergeTrainGitHubTransport,
)
from control_plane.product_config_authority_events import request_product_config_authority_event
from tests.merge_train_policy_fixtures import build_test_merge_train_policy_record
from tests.test_config_authority_audit import CLI_MAIN
from tests.test_github_app_webhook import _inventory
from tests.test_merge_train_github import (
    _batch_candidate,
    _combined_status,
    _protected_branch_with_checks,
    _required_check_run,
)


class ComparisonStore:
    def __init__(self, record: MergeTrainPolicyRecord) -> None:
        self.record = record
        self.candidates: tuple[MergeTrainBatchCandidateRecord, ...] = ()

    def list_merge_train_policy_records(
        self, *, status: str = "", limit: int | None = None
    ) -> tuple[MergeTrainPolicyRecord, ...]:
        records = (self.record,) if not status or self.record.status == status else ()
        return records[:limit] if limit is not None else records

    @staticmethod
    def list_merge_train_branch_refresh_records(
        **_filters: object,
    ) -> tuple[MergeTrainBranchRefreshRecord, ...]:
        return ()

    @staticmethod
    def read_merge_train_pr_feedback_record(_feedback_id: str) -> None:
        return None

    def list_merge_train_batch_candidate_records(
        self, *, repository: str = ""
    ) -> tuple[MergeTrainBatchCandidateRecord, ...]:
        return tuple(
            record
            for record in self.candidates
            if not repository or record.candidate.repository.casefold() == repository.casefold()
        )


class CurrentComparisonTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = "a" * 40
        self.head = "b" * 40
        self.app_id = 77
        self.repository = "example/merge-train-repo"
        self.record = build_test_merge_train_policy_record(repository=self.repository)
        self.record.policy.policies[0].config_authority_events_enabled = True
        self.policy = self.record.policy.policies[0]
        assert self.policy.github_token.github_app is not None
        self.repository_id = str(self.policy.github_token.github_app.repository_id)
        self.store = ComparisonStore(self.record)

    def check(
        self,
        *,
        event: str = "pull_request",
        branch: str = "main",
        base: str = "",
        status: str = "completed",
        conclusion: str | None = "success",
    ) -> dict[str, object]:
        return {
            "id": 1,
            "name": f"{CONFIG_AUTHORITY_CHECK_NAME}/{event.replace('_', '-')}/{quote(branch, safe='')}",
            "head_sha": self.head,
            "app": {"id": self.app_id},
            "external_id": config_authority_comparison_id(
                repository_id=self.repository_id,
                base_sha=base or self.base,
                head_sha=self.head,
                event=event,
                base_branch=branch,
            ),
            "status": status,
            "conclusion": conclusion,
        }

    def state(self, checks: list[object], app_id: int | None = 77) -> str:
        return current_config_authority_status(
            check_runs=checks,
            repository_id=self.repository_id,
            base_branch="main",
            base_sha=self.base,
            head_sha=self.head,
            app_id=app_id,
        )

    def test_missing_queued_stale_untrusted_and_neutral_are_not_passing(self) -> None:
        cases: list[list[object]] = [
            [],
            [self.check(status="queued", conclusion=None)],
            [self.check(status="in_progress", conclusion=None)],
            [self.check(base="c" * 40)],
            [self.check(branch="release/stable")],
            [{**self.check(), "head_sha": "d" * 40}],
            [{**self.check(), "app": {"id": self.app_id + 1}}],
            [{**self.check(), "external_id": "e" * 64}],
            [self.check(conclusion="neutral")],
        ]
        for checks in cases:
            with self.subTest(checks=checks):
                self.assertEqual(self.state(checks), "pending")
        self.assertEqual(self.state([self.check()], app_id=None), "pending")

    def test_current_failure_refuses_and_exact_pair_events_are_equivalent(self) -> None:
        for event in ("pull_request", "push", "merge_group"):
            with self.subTest(event=event):
                self.assertEqual(self.state([self.check(event=event)]), "pass")
                self.assertEqual(
                    self.state([self.check(event=event, conclusion="failure")]), "fail"
                )
        self.assertEqual(
            self.state(
                [
                    self.check(),
                    self.check(event="push", conclusion="failure"),
                ]
            ),
            "fail",
        )

    def test_later_retry_controls_same_comparison_and_retarget_does_not_poison_it(self) -> None:
        failed = self.check(conclusion="failure")
        retry = {**self.check(), "id": 2}
        self.assertEqual(self.state([failed, retry]), "pass")
        self.assertEqual(
            self.state(
                [
                    self.check(branch="release/stable", conclusion="failure"),
                    retry,
                ]
            ),
            "pass",
        )
        self.assertEqual(
            self.state([retry, {**self.check(status="in_progress"), "id": 3}]), "pending"
        )

    def candidate(self) -> MergeTrainBatchCandidate:
        return _batch_candidate().model_copy(
            update={
                "base_sha": self.base,
                "candidate_sha": self.head,
                "status": "ready_for_checks",
            }
        )

    def test_candidate_and_pr_checks_require_current_pair_despite_green_ci(self) -> None:
        for source, expected in (
            ([], "pending"),
            ([self.check()], "pass"),
            ([self.check(conclusion="failure")], "fail"),
            ([self.check(base="c" * 40)], "pending"),
            ([self.check(), self.check(branch="old", conclusion="failure")], "pass"),
        ):
            for candidate in (False, True):
                with self.subTest(expected=expected, candidate=candidate):
                    runs = [
                        _required_check_run("ci-gate", "completed", "success"),
                        _required_check_run("security-gate", "completed", "success"),
                        *source,
                    ]
                    transport = RecordingMergeTrainGitHubTransport(
                        responses=tuple(
                            ([_protected_branch_with_checks()] if candidate else [])
                            + [_combined_status(), {"check_runs": runs}]
                        )
                    )
                    with patch(
                        "control_plane.merge_train_github._owner_review_advisory_app_id",
                        return_value=self.app_id,
                    ):
                        if candidate:
                            result = (
                                GitHubMergeTrainClient(
                                    transport=transport,
                                    branch_refresh_store=self.store,
                                )
                                .observe_batch_candidate_checks(candidate=self.candidate())
                                .required_checks_status
                            )
                        else:
                            from control_plane.merge_train_github import _config_comparison

                            result = GitHubMergeTrainSnapshotReader(
                                transport=transport
                            )._required_checks_status(
                                repository_path=self.repository,
                                head_sha=self.head,
                                config_comparison=_config_comparison(
                                    self.store,
                                    repository=self.repository,
                                    base_branch="main",
                                    base_sha=self.base,
                                    head_sha=self.head,
                                ),
                            )
                    self.assertEqual(result, expected)

    def test_recorded_candidate_creation_queues_original_base_to_final_head(self) -> None:
        candidate = self.candidate()
        record = build_merge_train_batch_candidate_record(
            candidate=candidate, source="test", updated_at=candidate.updated_at
        )
        self.store.candidates = (record,)
        inventory = _inventory(repository=self.repository)
        event: dict[str, object] = {
            "ref": candidate.candidate_ref,
            "before": "0" * 40,
            "after": self.head,
            "created": True,
        }
        result = request_product_config_authority_event(self.store, inventory, "push", event)
        self.assertEqual(result["status"], "pending")
        self.assertEqual(
            result["request"],
            {
                "before": self.base,
                "after": self.head,
                "base_branch": "main",
                "candidate": True,
            },
        )
        for changed in (
            {"after": "c" * 40},
            {"deleted": True},
            {"ref": "refs/heads/launchplane/train/unrecorded"},
        ):
            with self.subTest(changed=changed):
                self.assertEqual(
                    request_product_config_authority_event(
                        self.store, inventory, "push", {**event, **changed}
                    ),
                    {},
                )
        self.policy.config_authority_events_enabled = False
        self.assertEqual(
            request_product_config_authority_event(self.store, inventory, "push", event), {}
        )

    def test_enabled_candidate_cannot_reuse_head_only_checks(self) -> None:
        transport = RecordingMergeTrainGitHubTransport(responses=())
        client = GitHubMergeTrainClient(
            transport=transport,
            branch_refresh_store=self.store,
        )
        self.assertIsNone(client.read_head_check_reuse(candidate=self.candidate()))
        self.assertEqual(transport.requests, [])

    def test_supported_offline_plan_preserves_existing_checks_and_has_no_apply(self) -> None:
        baseline = [{"context": "Build", "app_id": 42}]
        with TemporaryDirectory() as directory:
            path = Path(directory) / "checks.json"
            path.write_text(json.dumps(baseline))
            args = [
                "service",
                "plan-config-authority-transition",
                "--base-branch",
                "release/stable",
                "--required-checks-file",
                str(path),
            ]
            with patch("urllib.request.urlopen", side_effect=AssertionError("No provider calls")):
                blocked = CliRunner().invoke(CLI_MAIN, args)
                planned = CliRunner().invoke(CLI_MAIN, [*args, "--checks-app-id", str(self.app_id)])
            self.assertEqual(blocked.exit_code, 0, blocked.output)
            self.assertEqual(planned.exit_code, 0, planned.output)
            waiting = json.loads(blocked.output)
            result = json.loads(planned.output)
            self.assertEqual(waiting["status"], "blocked")
            self.assertEqual(waiting["next_required_checks"], baseline)
            self.assertEqual(result["status"], "planned")
            self.assertEqual(
                result["next_required_checks"], baseline + [result["proposed_required_check"]]
            )
            self.assertEqual(result["proposed_required_check"]["app_id"], self.app_id)
            self.assertEqual(result["consumer_removals"], [])
            self.assertFalse(result["live_policy_applied"])
            self.assertEqual(json.loads(path.read_text()), baseline)
            path.write_text("{}")
            self.assertNotEqual(CliRunner().invoke(CLI_MAIN, args).exit_code, 0)

    def test_final_readiness_uses_pair_bound_signal_instead_of_old_branch_checks(self) -> None:
        from control_plane.contracts.merge_readiness import MergeReadinessTarget
        from control_plane.merge_readiness import _technical_check_evidence
        from control_plane.tenant_admission_controller import (
            TenantAdmissionTechnicalChecks,
            TenantAdmissionRequiredTechnicalCheck,
            TenantAdmissionTechnicalCheckSignal,
        )

        old_name = self.check(branch="old")["name"]
        assert isinstance(old_name, str)
        initial = TenantAdmissionTechnicalChecks(
            evaluated_at="2026-10-13T14:00:00Z",
            head_sha=self.head,
            base_sha=self.base,
            strict=True,
            base_up_to_date=True,
            status="fail",
            required_checks=(TenantAdmissionRequiredTechnicalCheck(name="Build", app_id=42),),
            signals=(
                TenantAdmissionTechnicalCheckSignal(
                    source="check_run", name="Build", app_id=42, state="pass"
                ),
                TenantAdmissionTechnicalCheckSignal(
                    source="check_run", name=old_name, state="fail"
                ),
            ),
        )
        for checks, expected in (
            ([], "pending"),
            ([self.check()], "pass"),
            ([self.check(conclusion="failure")], "fail"),
        ):
            with self.subTest(expected=expected):
                transport = RecordingMergeTrainGitHubTransport(responses=({"check_runs": checks},))
                with (
                    patch(
                        "control_plane.merge_train_github._required_branch_checks",
                        return_value=(("Build", 42),),
                    ),
                    patch(
                        "control_plane.tenant_admission_controller.read_technical_checks_for_requirements",
                        return_value=initial,
                    ),
                    patch(
                        "control_plane.merge_train_github._owner_review_advisory_app_id",
                        return_value=self.app_id,
                    ),
                ):
                    evidence = GitHubMergeTrainClient(
                        transport=transport, branch_refresh_store=self.store
                    ).read_technical_checks(
                        repository=self.repository,
                        base_branch="main",
                        base_sha=self.base,
                        head_sha=self.head,
                        evaluated_at="2026-10-13T14:00:00Z",
                    )
                self.assertEqual(evidence.status, expected)
                target = MergeReadinessTarget(
                    repository=self.repository,
                    base_branch="main",
                    pull_request_number=7,
                    queue_position=1,
                    pull_request_head_sha=self.head,
                    pull_request_tree_sha="c" * 40,
                    base_sha=self.base,
                    expected_effect_sha=self.head,
                )
                self.assertEqual(
                    _technical_check_evidence(
                        target=target, evidence=evidence, structural_candidate_status="exact"
                    ).status,
                    expected,
                )
