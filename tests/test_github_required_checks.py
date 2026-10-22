import unittest

from control_plane.merge_train_github import (
    GitHubMergeTrainClient,
    GitHubMergeTrainSnapshotReader,
    RecordingMergeTrainGitHubTransport,
    MergeTrainGitHubError,
    MergeTrainGitHubStaleHeadError,
    _list_check_runs,
    _required_branch_checks,
    _required_checks_status,
    _required_checks_observation,
)
from control_plane.tenant_admission_controller import (
    TenantAdmissionRequiredTechnicalCheck,
    TenantAdmissionControllerError,
    TenantAdmissionControllerStaleCandidateError,
    TenantAdmissionControllerGitHubClient,
    read_technical_checks_for_requirements,
)
from tests.test_merge_train_github import (
    _batch_candidate,
    _combined_status,
    _protected_branch_with_checks,
    _required_check_run,
    _owner_review_check,
    _github_branch,
    _github_pull_request,
    _label_events,
    _conversation_rule,
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
    def test_base_requires_success_for_every_implicated_check(self) -> None:
        for source in ("check_run", "commit_status"):
            for state, expected in (
                ("absent", "unknown"),
                ("success", "pass"),
                ("failure", "fail"),
                ("pending", "pending"),
                ("skipped", "unknown"),
                ("neutral", "unknown"),
                ("unrecognized", "unknown"),
            ):
                with self.subTest(source=source, state=state):
                    checks = [_required_check_run("ci-gate", "completed", "success")]
                    statuses: list[dict[str, object]] = []
                    if state != "absent":
                        if source == "check_run":
                            checks.append(
                                _required_check_run(
                                    "implicated",
                                    "queued" if state == "pending" else "completed",
                                    None if state == "pending" else state,
                                )
                            )
                        else:
                            statuses.append({"context": "implicated", "state": state})
                    transport = RecordingMergeTrainGitHubTransport(
                        responses=(
                            _protected_branch_with_checks("ci-gate"),
                            [],
                            _combined_status(statuses=tuple(statuses)),
                            {"check_runs": checks},
                        )
                    )
                    self.assertEqual(
                        GitHubMergeTrainClient(transport=transport).read_base_check_status(
                            candidate=_batch_candidate(),
                            failed_check_names=("implicated",),
                        ),
                        expected,
                    )

    def test_base_implicated_subset_and_conflicting_sources_are_not_green(self) -> None:
        cases: tuple[tuple[list[dict[str, object]], list[dict[str, object]], str], ...] = (
            ([], [], "unknown"),
            ([{"context": "second", "state": "success"}], [], "pass"),
            (
                [{"context": "second", "state": "success"}],
                [_required_check_run("second", "completed", "failure")],
                "fail",
            ),
            (
                [{"context": "second", "state": "success"}],
                [_required_check_run("second", "completed", "neutral")],
                "unknown",
            ),
        )
        for extra_statuses, extra_runs, expected in cases:
            with self.subTest(statuses=extra_statuses, runs=extra_runs):
                transport = RecordingMergeTrainGitHubTransport(
                    responses=(
                        _protected_branch_with_checks("ci-gate"),
                        [],
                        _combined_status(statuses=tuple(extra_statuses)),
                        {
                            "check_runs": [
                                _required_check_run("ci-gate", "completed", "success"),
                                _required_check_run("FIRST", "completed", "success"),
                                *extra_runs,
                            ]
                        },
                    )
                )
                self.assertEqual(
                    GitHubMergeTrainClient(transport=transport).read_base_check_status(
                        candidate=_batch_candidate(),
                        failed_check_names=("first", "second"),
                    ),
                    expected,
                )

    def test_base_supersession_needs_implicated_replacement_evidence(self) -> None:
        for replacement_status, replacement_conclusion, reported in (
            ("queued", None, False),
            ("in_progress", None, False),
            ("completed", "success", False),
            ("completed", "success", True),
        ):
            with self.subTest(status=replacement_status, reported=reported):
                checks = [
                    _actions_check("implicated", 11, 101, 10, "completed", "failure"),
                    _actions_check("ci-gate", 13, 103, 30, "completed", "success"),
                ]
                if reported:
                    checks.append(_actions_check("implicated", 12, 102, 20, "completed", "success"))
                runs = [
                    _run(11, 7, 1, 10, "completed", "failure", "2026-01-01T10:00:00Z"),
                    _run(
                        12,
                        7,
                        2,
                        20,
                        replacement_status,
                        replacement_conclusion,
                        "2026-01-01T10:01:00Z",
                    ),
                    _run(13, 8, 1, 30, "completed", "success", "2026-01-01T10:00:00Z"),
                ]
                transport = RecordingMergeTrainGitHubTransport(
                    responses=(
                        _protected_branch_with_checks("ci-gate"),
                        [],
                        {**_combined_status(statuses=()), "sha": HEAD},
                        {"check_runs": checks},
                        {"total_count": len(runs), "workflow_runs": runs},
                    )
                )
                self.assertEqual(
                    GitHubMergeTrainClient(transport=transport).read_base_check_status(
                        candidate=_batch_candidate().model_copy(
                            update={"repository": "example/tool", "base_sha": HEAD}
                        ),
                        failed_check_names=("implicated",),
                    ),
                    "pass" if reported else "unknown",
                )

    def test_head_snapshot_keeps_queue_visible_without_enforced_policy(self) -> None:
        for policy in (
            {"protected": False},
            {
                "protected": True,
                "protection": {
                    "required_status_checks": {"enforcement_level": "off", "contexts": []}
                },
            },
        ):
            with self.subTest(policy=policy):

                class SnapshotTransport:
                    def request(
                        self, *, method: str, path: str, body: dict[str, object] | None = None
                    ) -> object:
                        if path.endswith("/branches/main"):
                            return {**_github_branch(), **policy}
                        if "/pulls?" in path:
                            return [
                                _github_pull_request(
                                    n, repository="example/tool", author_association="OWNER"
                                )
                                for n in (42, 43)
                            ]
                        if path.endswith(("/pulls/42", "/pulls/43")):
                            return _github_pull_request(
                                int(path.rsplit("/", 1)[1]),
                                repository="example/tool",
                                author_association="OWNER",
                            )
                        if path.endswith("/permission"):
                            return {"permission": "admin"}
                        if "/events?" in path:
                            return _label_events()
                        if "/comments?" in path or "/rules/branches/" in path:
                            return []
                        if "/commits/" in path:
                            head_sha = path.split("/commits/", 1)[1].split("/", 1)[0]
                            if "/status?" in path:
                                return {**_combined_status(statuses=()), "sha": head_sha}
                            if "/check-runs?" in path:
                                return {
                                    "check_runs": [
                                        {
                                            **_required_check_run(
                                                "ci-gate", "completed", "success"
                                            ),
                                            "head_sha": head_sha,
                                        }
                                    ]
                                }
                        if path == "/graphql":
                            return _conversation_rule()
                        if "/contents/" in path:
                            return {"encoding": "base64", "content": ""}
                        raise AssertionError(path)

                transport = SnapshotTransport()
                snapshot = GitHubMergeTrainSnapshotReader(
                    transport=transport
                ).read_merge_train_snapshot(repository="example/tool", base_branch="main")
                self.assertEqual([p.number for p in snapshot.pull_requests], [42, 43])
                for pr in snapshot.pull_requests:
                    self.assertEqual(pr.head_sha, f"head-{pr.number}")
                    self.assertEqual(pr.base_ref, "main")
                    self.assertEqual(pr.required_checks_status, "unknown")
                    self.assertTrue(pr.required_checks_observation_error)
                with self.assertRaises(MergeTrainGitHubError):
                    GitHubMergeTrainClient(transport=transport).observe_batch_candidate_checks(
                        candidate=_batch_candidate().model_copy(
                            update={"repository": "example/tool", "candidate_sha": HEAD}
                        )
                    )

    def test_projection_only_policy_has_named_unknown_even_with_owner_pass(self) -> None:
        from control_plane.product_review_status import OWNER_REVIEW_STATUS_CONTEXT

        for owner_required in (False, True):
            transport = RecordingMergeTrainGitHubTransport(
                responses=(
                    _protected_branch_with_checks(OWNER_REVIEW_STATUS_CONTEXT),
                    [],
                    _combined_status(statuses=()),
                    {"check_runs": [_owner_review_check("success", head_sha=HEAD)]},
                )
            )
            status, diagnostic = _required_checks_observation(
                transport=transport,
                repository_path="example/tool",
                base_branch="main",
                encoded_head_sha=HEAD,
                owner_review_required=owner_required,
                owner_review_app_id=77,
            )
            self.assertEqual(status, "unknown")
            self.assertTrue(diagnostic)

    def test_unprotected_tenant_policy_remains_unavailable(self) -> None:
        for strict in (False, True, None):
            with self.subTest(strict=strict):
                transport = RecordingMergeTrainGitHubTransport(
                    responses=(
                        MergeTrainGitHubError("no classic protection", status_code=404)
                        if strict is None
                        else {"strict": strict, "checks": [], "contexts": []},
                        {"protected": False},
                    )
                )
                observed = TenantAdmissionControllerGitHubClient(
                    transport=transport
                ).read_technical_checks(
                    repository="example/tool",
                    base_branch="main",
                    base_sha="base-sha",
                    head_sha=HEAD,
                    evaluated_at="2026-01-01T10:03:00Z",
                )
                self.assertEqual(observed.status, "unavailable")
                self.assertFalse(observed.required_checks)

    def test_check_run_head_binding_agrees_across_readers(self) -> None:
        for consumer in ("head", "candidate", "admission"):
            for present, returned_head in (
                (True, HEAD),
                (True, "other-head"),
                (True, False),
                (True, None),
                (False, None),
            ):
                with self.subTest(consumer=consumer, present=present, head=returned_head):
                    check = _required_check_run("ci-gate", "completed", "success")
                    if present:
                        check["head_sha"] = returned_head
                    transport = RecordingMergeTrainGitHubTransport(
                        responses=(
                            *(
                                (_protected_branch_with_checks("ci-gate"), [])
                                if consumer != "admission"
                                else ()
                            ),
                            {**_combined_status(statuses=()), "sha": HEAD},
                            {"check_runs": [check]},
                        )
                    )

                    def read_status() -> str:
                        if consumer == "head":
                            return _required_checks_status(
                                transport=transport,
                                repository_path="example/tool",
                                encoded_head_sha=HEAD,
                                base_branch="main",
                            )
                        client = GitHubMergeTrainClient(transport=transport)
                        if consumer == "candidate":
                            return client.observe_batch_candidate_checks(
                                candidate=_batch_candidate().model_copy(
                                    update={"repository": "example/tool", "candidate_sha": HEAD}
                                )
                            ).required_checks_status
                        return read_technical_checks_for_requirements(
                            transport=transport,
                            merge_client=client,
                            repository="example/tool",
                            base_sha="base-sha",
                            head_sha=HEAD,
                            evaluated_at="2026-01-01T10:03:00Z",
                            strict=False,
                            required_checks=(
                                TenantAdmissionRequiredTechnicalCheck(name="ci-gate"),
                            ),
                        ).status

                    if returned_head == HEAD or (not present and consumer != "admission"):
                        self.assertEqual(read_status(), "pass")
                    else:
                        error = (
                            TenantAdmissionControllerStaleCandidateError
                            if consumer == "admission" and present
                            else TenantAdmissionControllerError
                            if consumer == "admission"
                            else MergeTrainGitHubStaleHeadError
                        )
                        with self.assertRaises(error):
                            read_status()

    def test_contradictory_head_cannot_hide_in_history_or_later_pages(self) -> None:
        wrong = {
            **_required_check_run("ci-gate", "completed", "success"),
            "head_sha": "other-head",
            "check_suite": {"id": 7},
            "id": 1,
        }
        current = {**wrong, "head_sha": HEAD, "id": 2}
        for pages in (
            ({"check_runs": [wrong, current]},),
            (
                {"check_runs": [{**current, "id": index + 2} for index in range(100)]},
                {"check_runs": [wrong]},
            ),
        ):
            with self.subTest(page_count=len(pages)):
                transport = RecordingMergeTrainGitHubTransport(responses=pages)
                with self.assertRaises(MergeTrainGitHubStaleHeadError):
                    _list_check_runs(
                        transport=transport, repository_path="example/tool", encoded_head_sha=HEAD
                    )

    def test_failure_diagnosis_keeps_only_related_execution_evidence(self) -> None:
        failed_test = "tests.test_widget.WidgetTests.test_renders"
        for case, gate_conclusion, leaf_run, leaf_suite, leaf_status, named_test, expected in (
            ("optional_only", "success", 11, 10, "completed", False, (False, False)),
            ("same_run_loss", "failure", 11, 10, "completed", False, (True, True)),
            ("same_run_test", "failure", 11, 10, "completed", True, (True, False)),
            ("unrelated_loss", "failure", 12, 20, "completed", False, (True, False)),
            ("unrelated_pending", "failure", 12, 20, "in_progress", False, (True, False)),
            ("different_suite", "failure", 11, 20, "completed", False, (True, False)),
            ("different_run", "failure", 12, 10, "completed", False, (True, False)),
        ):
            with self.subTest(case=case):
                checks = [
                    _actions_check("ci-gate", 11, 101, 10, "completed", gate_conclusion),
                    _actions_check(
                        "leaf",
                        leaf_run,
                        102,
                        leaf_suite,
                        leaf_status,
                        "failure" if leaf_status == "completed" else None,
                    ),
                ]
                runs = [_run(11, 7, 1, 10, "completed", gate_conclusion, "2026-01-01T10:00:00Z")]
                if leaf_run != 11:
                    runs.append(
                        _run(
                            leaf_run,
                            8,
                            1,
                            leaf_suite,
                            leaf_status,
                            "failure" if leaf_status == "completed" else None,
                            "2026-01-01T10:00:00Z",
                        )
                    )
                annotations: list[object] = []
                if gate_conclusion == "failure":
                    annotations.append([{"message": "Process completed with exit code 1."}])
                annotations.append([{"message": "The runner has received a shutdown signal."}])
                if named_test:
                    checks.append(_actions_check("leaf2", 11, 103, 10, "completed", "failure"))
                    annotations.append([{"title": "Failed test", "message": failed_test}])
                transport = RecordingMergeTrainGitHubTransport(
                    responses=(
                        _protected_branch_with_checks("ci-gate"),
                        [],
                        {"check_runs": checks},
                        *(
                            ({"total_count": len(runs), "workflow_runs": runs},)
                            if gate_conclusion == "failure"
                            else ()
                        ),
                        {**_combined_status(statuses=()), "sha": HEAD},
                        *annotations,
                    )
                )
                failures = GitHubMergeTrainClient(
                    transport=transport
                ).read_candidate_check_failures(
                    candidate=_batch_candidate().model_copy(
                        update={"repository": "example/tool", "candidate_sha": HEAD}
                    )
                )
                self.assertEqual((failures.conclusive, failures.infrastructure_only), expected)
                self.assertEqual(failures.failed_tests, (failed_test,) if named_test else ())

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

    def test_replaced_failure_with_missing_required_job_stays_pending(self) -> None:
        checks, runs = _superseded_with_unrelated_running()
        old_failure = checks[0]
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
                            {"check_runs": [old_failure]},
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
                    expected = "fail" if unavailable else "pending"
                    self.assertEqual(status, expected)

    def test_passing_auxiliary_evidence_cannot_replace_unknown_required_checks(self) -> None:
        unknown = _required_check_run("ci-gate", "completed", "unrecognized")
        for consumer in ("owner", "base"):
            with self.subTest(consumer=consumer):
                transport = RecordingMergeTrainGitHubTransport(
                    responses=(
                        _protected_branch_with_checks("ci-gate", app_id=None),
                        [],
                        _combined_status(statuses=()),
                        {
                            "check_runs": [
                                unknown,
                                _owner_review_check("success", head_sha=HEAD)
                                if consumer == "owner"
                                else _required_check_run("implicated", "completed", "success"),
                            ]
                        },
                    )
                )
                if consumer == "owner":
                    status = _required_checks_status(
                        transport=transport,
                        repository_path="example/tool",
                        encoded_head_sha=HEAD,
                        base_branch="main",
                        owner_review_required=True,
                        owner_review_app_id=77,
                    )
                else:
                    status = GitHubMergeTrainClient(transport=transport).read_base_check_status(
                        candidate=_batch_candidate().model_copy(
                            update={"repository": "example/tool"}
                        ),
                        failed_check_names=("implicated",),
                    )
                self.assertEqual(status, "unknown")

    def test_app_pinned_commit_status_requires_explicit_matching_app_identity(self) -> None:
        for app_id in (None, 77, 78):
            for consumer in ("head", "candidate", "admission"):
                with self.subTest(app_id=app_id, consumer=consumer):
                    status_payload: dict[str, object] = {
                        "context": "ci-gate",
                        "state": "success",
                        "creator": {"login": "example-app[bot]"},
                        **({"app": {"id": app_id}} if app_id is not None else {}),
                    }
                    transport = RecordingMergeTrainGitHubTransport(
                        responses=(
                            *(
                                (_protected_branch_with_checks("ci-gate", app_id=77), [])
                                if consumer != "admission"
                                else ()
                            ),
                            {**_combined_status(statuses=(status_payload,)), "sha": HEAD},
                            {"check_runs": []},
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
                    elif consumer == "candidate":
                        status = (
                            GitHubMergeTrainClient(transport=transport)
                            .observe_batch_candidate_checks(
                                candidate=_batch_candidate().model_copy(
                                    update={"repository": "example/tool", "candidate_sha": HEAD}
                                )
                            )
                            .required_checks_status
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
                                TenantAdmissionRequiredTechnicalCheck(name="ci-gate", app_id=77),
                            ),
                        ).status
                    self.assertEqual(status, "pass" if app_id == 77 else "pending")

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

    def test_optional_actions_failure_cannot_require_history_for_a_green_head(self) -> None:
        transport = RecordingMergeTrainGitHubTransport(
            responses=(
                _protected_branch_with_checks("ci-gate"),
                [],
                {**_combined_status(statuses=()), "sha": HEAD},
                {
                    "check_runs": [
                        _actions_check("ci-gate", 11, 101, 10, "completed", "success"),
                        _actions_check("optional", 12, 102, 20, "completed", "failure"),
                    ]
                },
                MergeTrainGitHubError("workflow evidence unavailable", status_code=500),
            )
        )
        self.assertEqual(
            _required_checks_status(
                transport=transport,
                repository_path="example/tool",
                encoded_head_sha=HEAD,
                base_branch="main",
            ),
            "pass",
        )
        self.assertFalse(any("/actions/runs" in request.path for request in transport.requests))

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
