import unittest

from control_plane.merge_train_github import (
    GitHubMergeTrainClient,
    RecordingMergeTrainGitHubTransport,
    MergeTrainGitHubError,
    _required_branch_checks,
    _required_checks_status,
)
from control_plane.tenant_admission_controller import (
    TenantAdmissionRequiredTechnicalCheck,
    TenantAdmissionControllerError,
    read_technical_checks_for_requirements,
)
from tests.test_merge_train_github import (
    _batch_candidate,
    _combined_status,
    _protected_branch_with_checks,
    _required_check_run,
)

HEAD = "candidate-sha"


def _actions_check(
    name: str, run: int, job: int, suite: int, status: str, conclusion: str | None
) -> dict[str, object]:
    return {
        **_required_check_run(name, status, conclusion),
        "app": {"id": 15368, "slug": "github-actions"},
        "id": job,
        "head_sha": HEAD,
        "check_suite": {"id": suite},
        "details_url": f"https://github.com/example/tool/actions/runs/{run}/job/{job}",
    }


def _run(
    run_id: int,
    workflow_id: int,
    run_number: int,
    suite: int,
    status: str,
    conclusion: str | None,
    started: str,
) -> dict[str, object]:
    return {
        "id": run_id,
        "workflow_id": workflow_id,
        "run_number": run_number,
        "run_attempt": 1,
        "head_sha": HEAD,
        "head_branch": "work/feature",
        "head_repository": {"id": 9},
        "event": "pull_request",
        "pull_requests": [{"number": 14, "base": {"ref": "main", "repo": {"id": 9}}}],
        "check_suite_id": suite,
        "status": status,
        "conclusion": conclusion,
        "run_started_at": started,
    }


def _superseded_with_unrelated_running() -> tuple[list[dict[str, object]], dict[str, object]]:
    checks = [
        _actions_check("ci-gate", 11, 101, 10, "completed", "failure"),
        _actions_check("ci-gate", 12, 102, 20, "completed", "success"),
        _actions_check("preview-deploy", 13, 103, 30, "in_progress", None),
    ]
    runs = [
        _run(12, 7, 2, 20, "completed", "success", "2026-01-01T10:01:00Z"),
        _run(11, 7, 1, 10, "completed", "failure", "2026-01-01T10:00:00Z"),
        _run(13, 8, 1, 30, "in_progress", None, "2026-01-01T10:01:30Z"),
    ]
    return checks, {"total_count": 3, "workflow_runs": runs}


class GitHubRequiredCheckVerdictTests(unittest.TestCase):
    def test_admission_reads_required_commit_status_on_later_page(self) -> None:
        for state, page_sha, expected in (
            ("success", HEAD, "pass"),
            ("failure", HEAD, "fail"),
            ("success", "other-head", None),
            ("success", None, None),
        ):
            with self.subTest(state=state, page_sha=page_sha):
                transport = RecordingMergeTrainGitHubTransport(
                    responses=(
                        {
                            "sha": HEAD,
                            "total_count": 101,
                            "statuses": [
                                {"context": f"optional-{index}", "state": "success"}
                                for index in range(100)
                            ],
                        },
                        {
                            **({"sha": page_sha} if page_sha is not None else {}),
                            "total_count": 101,
                            "statuses": [{"context": "ci-gate", "state": state}],
                        },
                        {"check_runs": []},
                    )
                )

                def read_status() -> str:
                    return read_technical_checks_for_requirements(
                        transport=transport,
                        merge_client=GitHubMergeTrainClient(transport=transport),
                        repository="example/tool",
                        base_sha="base-sha",
                        head_sha=HEAD,
                        evaluated_at="2026-01-01T10:03:00Z",
                        strict=False,
                        required_checks=(TenantAdmissionRequiredTechnicalCheck(name="ci-gate"),),
                    ).status

                if expected is None:
                    with self.assertRaises(TenantAdmissionControllerError):
                        read_status()
                else:
                    self.assertEqual(read_status(), expected)

    def test_waiting_and_requested_checks_are_pending_for_head_and_admission(self) -> None:
        for status in ("waiting", "requested"):
            for consumer in ("head", "admission"):
                with self.subTest(status=status, consumer=consumer):
                    responses: tuple[object, ...] = (
                        *(
                            (_protected_branch_with_checks("ci-gate"), [])
                            if consumer == "head"
                            else ()
                        ),
                        {**_combined_status(statuses=()), "sha": HEAD},
                        {
                            "check_runs": [
                                {
                                    **_required_check_run("ci-gate", status, None),
                                    "head_sha": HEAD,
                                }
                            ]
                        },
                    )
                    transport = RecordingMergeTrainGitHubTransport(responses=responses)
                    verdict: str
                    if consumer == "head":
                        verdict = _required_checks_status(
                            transport=transport,
                            repository_path="example/tool",
                            encoded_head_sha=HEAD,
                            base_branch="main",
                        )
                    else:
                        verdict = read_technical_checks_for_requirements(
                            transport=transport,
                            merge_client=GitHubMergeTrainClient(transport=transport),
                            repository="example/tool",
                            base_sha="base-sha",
                            head_sha=HEAD,
                            evaluated_at="2026-01-01T10:03:00Z",
                            strict=False,
                            required_checks=(
                                TenantAdmissionRequiredTechnicalCheck(name="ci-gate"),
                            ),
                        ).status
                    self.assertEqual(verdict, "pending")

    def test_matching_status_and_check_must_both_pass(self) -> None:
        for status_state, conclusion in (("success", "failure"), ("failure", "success")):
            with self.subTest(status_state=status_state, conclusion=conclusion):
                transport = RecordingMergeTrainGitHubTransport(
                    responses=(
                        _protected_branch_with_checks("ci-gate", app_id=None),
                        [],
                        _combined_status(statuses=({"context": "ci-gate", "state": status_state},)),
                        {"check_runs": [_required_check_run("ci-gate", "completed", conclusion)]},
                    )
                )
                verdict = _required_checks_status(
                    transport=transport,
                    repository_path="example/tool",
                    encoded_head_sha=HEAD,
                    base_branch="main",
                )
                self.assertEqual(verdict, "fail")

    def test_head_ignores_superseded_failure_beside_unrelated_running_workflow(self) -> None:
        checks, runs = _superseded_with_unrelated_running()
        transport = RecordingMergeTrainGitHubTransport(
            responses=(
                _protected_branch_with_checks("ci-gate"),
                [],
                {**_combined_status(statuses=()), "sha": HEAD},
                {"check_runs": checks},
                runs,
            )
        )
        status = _required_checks_status(
            transport=transport,
            repository_path="example/tool",
            encoded_head_sha=HEAD,
            base_branch="main",
        )
        self.assertEqual(status, "pass")

    def test_new_execution_without_required_job_cannot_borrow_old_success(self) -> None:
        checks, runs = _superseded_with_unrelated_running()
        old_success = {**checks[0], "conclusion": "success"}
        inventory = runs["workflow_runs"]
        assert isinstance(inventory, list)
        current = inventory[0]
        assert isinstance(current, dict)
        current.update(status="in_progress", conclusion=None)
        for unavailable in (False, True):
            for consumer in ("head", "admission"):
                with self.subTest(unavailable=unavailable, consumer=consumer):
                    transport = RecordingMergeTrainGitHubTransport(
                        responses=(
                            *(
                                (_protected_branch_with_checks("ci-gate"), [])
                                if consumer == "head"
                                else ()
                            ),
                            {**_combined_status(statuses=()), "sha": HEAD},
                            {"check_runs": [old_success]},
                            MergeTrainGitHubError("workflow evidence unavailable", status_code=403)
                            if unavailable
                            else runs,
                        )
                    )
                    status: str
                    if consumer == "head":
                        status = _required_checks_status(
                            transport=transport,
                            repository_path="example/tool",
                            encoded_head_sha=HEAD,
                            base_branch="main",
                        )
                    else:
                        status = read_technical_checks_for_requirements(
                            transport=transport,
                            merge_client=GitHubMergeTrainClient(transport=transport),
                            repository="example/tool",
                            base_sha="base-sha",
                            head_sha=HEAD,
                            evaluated_at="2026-01-01T10:03:00Z",
                            strict=False,
                            required_checks=(
                                TenantAdmissionRequiredTechnicalCheck(name="ci-gate"),
                            ),
                        ).status
                    expected = (
                        ("unknown" if consumer == "head" else "unavailable")
                        if unavailable
                        else "pending"
                    )
                    self.assertEqual(status, expected)

    def test_candidate_ignores_superseded_failure_beside_unrelated_running_workflow(self) -> None:
        checks, runs = _superseded_with_unrelated_running()
        transport = RecordingMergeTrainGitHubTransport(
            responses=(
                _protected_branch_with_checks("ci-gate"),
                [],
                {**_combined_status(statuses=()), "sha": HEAD},
                {"check_runs": checks},
                runs,
            )
        )
        candidate = _batch_candidate().model_copy(
            update={
                "repository": "example/tool",
                "candidate_sha": HEAD,
                "status": "ready_for_checks",
            }
        )
        observed = GitHubMergeTrainClient(transport=transport).observe_batch_candidate_checks(
            candidate=candidate
        )
        self.assertEqual(observed.required_checks_status, "pass")

    def test_head_ignores_optional_failing_status(self) -> None:
        transport = RecordingMergeTrainGitHubTransport(
            responses=(
                _protected_branch_with_checks("ci-gate"),
                [],
                _combined_status(statuses=({"context": "codecov/patch", "state": "failure"},)),
                {"check_runs": [_required_check_run("ci-gate", "completed", "success")]},
            )
        )
        status = _required_checks_status(
            transport=transport,
            repository_path="example/tool",
            encoded_head_sha=HEAD,
            base_branch="main",
        )
        self.assertEqual(status, "pass")

    def test_head_waits_for_missing_required_check(self) -> None:
        transport = RecordingMergeTrainGitHubTransport(
            responses=(
                _protected_branch_with_checks("ci-gate"),
                [],
                _combined_status(statuses=()),
                {"check_runs": [_required_check_run("lint", "completed", "success")]},
            )
        )
        status = _required_checks_status(
            transport=transport,
            repository_path="example/tool",
            encoded_head_sha=HEAD,
            base_branch="main",
        )
        self.assertEqual(status, "pending")

    def test_candidate_ignores_optional_failing_check(self) -> None:
        transport = RecordingMergeTrainGitHubTransport(
            responses=(
                _protected_branch_with_checks("ci-gate"),
                [],
                _combined_status(statuses=()),
                {
                    "check_runs": [
                        _required_check_run("ci-gate", "completed", "success"),
                        _required_check_run("optional-lint", "completed", "failure", app_id=99),
                    ]
                },
            )
        )
        candidate = _batch_candidate().model_copy(
            update={
                "repository": "example/tool",
                "candidate_sha": HEAD,
                "status": "ready_for_checks",
            }
        )
        observed = GitHubMergeTrainClient(transport=transport).observe_batch_candidate_checks(
            candidate=candidate
        )
        self.assertEqual(observed.required_checks_status, "pass")

    def test_optional_failure_is_not_candidate_blame(self) -> None:
        transport = RecordingMergeTrainGitHubTransport(
            responses=(
                _protected_branch_with_checks("ci-gate"),
                [],
                {
                    "check_runs": [
                        _required_check_run("ci-gate", "completed", "success"),
                        _required_check_run("optional-lint", "completed", "failure", app_id=99),
                    ]
                },
                {**_combined_status(statuses=()), "sha": HEAD},
            )
        )
        failures = GitHubMergeTrainClient(transport=transport).read_candidate_check_failures(
            candidate=_batch_candidate().model_copy(update={"candidate_sha": HEAD})
        )
        self.assertFalse(failures.checks)
        self.assertFalse(failures.conclusive)

    def test_ruleset_checks_and_classic_protection_both_apply(self) -> None:
        transport = RecordingMergeTrainGitHubTransport(
            responses=(
                _protected_branch_with_checks("ci-gate"),
                [
                    {
                        "type": "required_status_checks",
                        "parameters": {
                            "required_status_checks": [
                                {"context": "security-gate", "integration_id": 15368}
                            ]
                        },
                    }
                ],
            )
        )
        required = _required_branch_checks(
            transport=transport, repository_path="example/tool", base_branch="main"
        )
        self.assertIn("security-gate", {name for name, _ in required})


if __name__ == "__main__":
    unittest.main()
