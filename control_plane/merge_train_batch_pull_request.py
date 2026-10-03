"""Protected, PR-native landing of a whole tested candidate in one provider effect."""

from collections.abc import Callable
from time import sleep
from urllib.parse import quote

from control_plane.contracts.merge_admission_record import (
    MergeAdmissionRecord,
    MergeBatchNoEffectEvidence,
)
from control_plane.contracts.merge_train_batch import (
    MergeTrainBatchCandidate,
    MergeTrainBatchLandingEntry,
    MergeTrainBatchLandingPlan,
    MergeTrainBatchLandingPlanRecord,
    build_merge_train_batch_candidate_ref,
)
from control_plane.merge_admission import (
    GuardedMergeAdmission,
    MergeAdmissionDeniedError,
    MergeAdmissionReconciliationRequiredError,
)
from control_plane.merge_train import review_conversations_reason
from control_plane.merge_train_github import (
    GitHubMergeTrainClient,
    MERGE_REF_READ_DELAYS_SECONDS,
    MergeTrainGitHubError,
    MergeTrainGitHubStaleHeadError,
    _base_branch_identity,
    _git_commit_identity,
    _json_object,
    _repository_path,
    _repository_full_name,
    _required_text,
    _validated_landing_commit_identity,
    _validated_model_update,
    _wait_for_branch_sha,
)
from control_plane.release_review_github import (
    missing_owner_test_notes,
    nest_owner_test_notes,
    owner_test_notes,
)


def _candidate_branch(
    *, repository: str, base_branch: str, batch_id: str, candidate_ref: str
) -> str:
    expected = build_merge_train_batch_candidate_ref(
        repository=repository, base_branch=base_branch, batch_id=batch_id
    )
    if candidate_ref != expected:
        raise MergeAdmissionDeniedError("Batch PR requires the recorded train candidate ref.")
    return candidate_ref.removeprefix("refs/heads/")


def _validate_batch_pull_request(
    payload: object,
    *,
    repository: str,
    base_branch: str,
    candidate_branch: str,
    candidate_sha: str,
) -> dict[str, object]:
    pull_request = _json_object(payload, "GitHub batch pull request")
    head = _json_object(pull_request.get("head"), "GitHub batch PR head")
    base = _json_object(pull_request.get("base"), "GitHub batch PR base")
    head_repository = _json_object(head.get("repo"), "GitHub batch PR head repository")
    base_repository = _json_object(base.get("repo"), "GitHub batch PR base repository")
    if (
        head.get("sha") != candidate_sha
        or head.get("ref") != candidate_branch
        or _repository_full_name(head_repository, "Batch PR head repository") != repository
        or base.get("ref") != base_branch
        or _repository_full_name(base_repository, "Batch PR base repository") != repository
        or pull_request.get("draft") is not False
    ):
        raise MergeTrainGitHubStaleHeadError(
            "Batch pull request identity changed.", status_code=409
        )
    return pull_request


def _candidate_pull_requests(
    *, client: GitHubMergeTrainClient, candidate: MergeTrainBatchCandidate
) -> list[dict[str, object]]:
    branch = _candidate_branch(
        repository=candidate.repository,
        base_branch=candidate.base_branch,
        batch_id=candidate.batch_id,
        candidate_ref=candidate.candidate_ref,
    )
    owner = candidate.repository.split("/", maxsplit=1)[0]
    pulls = client.transport.request(
        method="GET",
        path=f"/repos/{_repository_path(candidate.repository)}/pulls?state=all&head={quote(f'{owner}:{branch}', safe='')}&per_page=100",
    )
    if not isinstance(pulls, list) or len(pulls) >= 100:
        raise MergeAdmissionDeniedError(
            "Batch pull request discovery is unavailable or incomplete."
        )
    return [_owned_batch_pull_request(payload, candidate) for payload in pulls]


def _owned_batch_pull_request(
    payload: object, candidate: MergeTrainBatchCandidate
) -> dict[str, object]:
    pull_request = _json_object(payload, "Managed batch PR")
    head = _json_object(pull_request.get("head"), "Managed batch PR head")
    base = _json_object(pull_request.get("base"), "Managed batch PR base")
    branch = _candidate_branch(
        repository=candidate.repository,
        base_branch=candidate.base_branch,
        batch_id=candidate.batch_id,
        candidate_ref=candidate.candidate_ref,
    )
    number = pull_request.get("number")
    if (
        type(number) is not int
        or number < 1
        or head.get("ref") != branch
        or _repository_full_name(head.get("repo"), "Managed batch PR head repo")
        != candidate.repository
        or _repository_full_name(base.get("repo"), "Managed batch PR base repo")
        != candidate.repository
    ):
        raise MergeAdmissionDeniedError(
            "PR is not owned by the recorded Launchplane candidate ref."
        )
    return pull_request


def _batch_owner_test_notes(
    *, client: GitHubMergeTrainClient, candidate: MergeTrainBatchCandidate
) -> str:
    """Carry each constituent's own Client test notes into the batch PR body.

    GitHub attributes a batch-landed commit to the batch PR alone, so release
    review reads these notes from the batch PR, never from the constituents.
    """
    sections = []
    for entry in candidate.entries:
        detail = client._pull_request_detail(
            repository=candidate.repository, pull_request_number=entry.pull_request_number
        )
        title = detail.get("title")
        heading = f"### #{entry.pull_request_number}" + (
            f" {title.strip()}" if isinstance(title, str) and title.strip() else ""
        )
        body = detail.get("body")
        notes = owner_test_notes(body if isinstance(body, str) else "")
        sections.append(
            f"{heading}\n\n"
            + (
                nest_owner_test_notes(notes, min_heading_level=4)
                if notes
                else missing_owner_test_notes(entry.pull_request_number)
            )
        )
    # role-words: legacy. Product repositories pin the CI action by SHA, and pins older
    # than the Client heading only accept this one; release review reads both.
    return "## Owner test notes\n\n" + "\n\n".join(sections)


def _find_batch_pull_request(
    *, client: GitHubMergeTrainClient, candidate: MergeTrainBatchCandidate
) -> object | None:
    pulls = _candidate_pull_requests(client=client, candidate=candidate)
    opened = [pull for pull in pulls if pull.get("state") == "open"]
    if len(opened) > 1:
        raise MergeTrainGitHubStaleHeadError(
            "Generated batch ref has multiple open PRs.", status_code=409
        )
    if opened:
        return opened[0]
    marker = f"<!-- launchplane-batch:{candidate.batch_id}:{candidate.candidate_sha} -->"
    for pull in pulls:
        body = pull.get("body")
        if isinstance(body, str) and marker in body:
            return pull
        head = _json_object(pull.get("head"), "Closed batch PR head")
        if head.get("sha") == candidate.candidate_sha:
            number = pull["number"]
            assert isinstance(number, int)
            detail = client._pull_request_detail(
                repository=candidate.repository, pull_request_number=number
            )
            if detail.get("merged") is not False:
                raise MergeAdmissionReconciliationRequiredError(
                    "Closed batch PR effect is not confirmed absent."
                )
    return None


def _bound_batch_pull_request(
    payload: object, candidate: MergeTrainBatchCandidate
) -> dict[str, object]:
    pull_request = _validate_batch_pull_request(
        payload,
        repository=candidate.repository,
        base_branch=candidate.base_branch,
        candidate_branch=_candidate_branch(
            repository=candidate.repository,
            base_branch=candidate.base_branch,
            batch_id=candidate.batch_id,
            candidate_ref=candidate.candidate_ref,
        ),
        candidate_sha=candidate.candidate_sha,
    )
    number = pull_request.get("number")
    body = pull_request.get("body")
    marker = f"<!-- launchplane-batch:{candidate.batch_id}:{candidate.candidate_sha} -->"
    if type(number) is not int or number < 1 or not isinstance(body, str) or marker not in body:
        raise MergeTrainGitHubStaleHeadError(
            "Batch pull request lost its recorded candidate binding.", status_code=409
        )
    return pull_request


def close_batch_pull_request(
    *, client: GitHubMergeTrainClient, candidate: MergeTrainBatchCandidate
) -> None:
    """Close PRs owned by this generated ref even when their mutable fields drift."""
    if not candidate.candidate_sha or len(candidate.entries) < 2:
        return
    for payload in _candidate_pull_requests(client=client, candidate=candidate):
        number = payload["number"]
        assert isinstance(number, int)
        detail = client._pull_request_detail(
            repository=candidate.repository, pull_request_number=number
        )
        pull_request = _owned_batch_pull_request(detail, candidate)
        head = _json_object(pull_request.get("head"), "Retired batch PR head")
        if pull_request.get("merged") is True and head.get("sha") != candidate.candidate_sha:
            continue  # Historical merged PR from an earlier candidate on this ref.
        if pull_request.get("merged") is not False:
            raise MergeAdmissionReconciliationRequiredError(
                "Batch PR has a merged or unavailable effect; it cannot be retired as unused."
            )
        if pull_request.get("state") == "closed":
            continue
        if pull_request.get("state") != "open":
            raise MergeAdmissionReconciliationRequiredError("Batch PR lifecycle is unavailable.")
        response = client.transport.request(
            method="PATCH",
            path=f"/repos/{_repository_path(candidate.repository)}/pulls/{number}",
            body={"state": "closed"},
        )
        closed = _owned_batch_pull_request(response, candidate)
        if closed.get("state") != "closed" or closed.get("merged") is not False:
            raise MergeAdmissionReconciliationRequiredError("Batch PR retirement is not confirmed.")


def batch_pull_request_body(
    *, client: GitHubMergeTrainClient, candidate: MergeTrainBatchCandidate
) -> str:
    marker = f"<!-- launchplane-batch:{candidate.batch_id}:{candidate.candidate_sha} -->"
    members = "\n".join(
        f"- #{entry.pull_request_number} at `{entry.head_sha}`" for entry in candidate.entries
    )
    return (
        f"{marker}\n\nThis PR lands Launchplane's tested candidate `{candidate.candidate_sha}` "
        f"through the protected merge endpoint. Constituent PRs and reviewed heads:\n\n{members}\n\n"
        "Launchplane revalidates every constituent and records the shared landing effect. "
        "Keep source PRs and branches intact. Let the Launchplane controller merge this PR; "
        "do not merge it by hand or update its generated branch. An out-of-controller merge "
        "without a preceding admission remains fenced for explicit reconciliation. "
        "The controller confirms constituent completion after landing.\n\n"
        + _batch_owner_test_notes(client=client, candidate=candidate)
    )


def changed_closed_batch_body(
    *, client: GitHubMergeTrainClient, candidate: MergeTrainBatchCandidate
) -> bool:
    """Require exact closed, unmerged batch binding before comparing generated input."""
    payload = _find_batch_pull_request(client=client, candidate=candidate)
    if payload is None:
        return False
    bound = _bound_batch_pull_request(payload, candidate)
    number = bound["number"]
    assert isinstance(number, int)
    detail = client._pull_request_detail(
        repository=candidate.repository, pull_request_number=number
    )
    closed = _bound_batch_pull_request(detail, candidate)
    if closed.get("state") != "closed" or closed.get("merged") is not False:
        return False
    return closed["body"] != batch_pull_request_body(client=client, candidate=candidate)


def ensure_batch_pull_request(
    *, client: GitHubMergeTrainClient, candidate: MergeTrainBatchCandidate
) -> int:
    if candidate.status not in {"ready_for_checks", "passed"} or len(candidate.entries) < 2:
        raise MergeAdmissionDeniedError(
            "Batch PR creation requires a fully built multi-entry candidate."
        )
    branch = _candidate_branch(
        repository=candidate.repository,
        base_branch=candidate.base_branch,
        batch_id=candidate.batch_id,
        candidate_ref=candidate.candidate_ref,
    )
    repository_path = _repository_path(candidate.repository)
    payload = _find_batch_pull_request(client=client, candidate=candidate)
    if payload is None:
        payload = client.transport.request(
            method="POST",
            path=f"/repos/{repository_path}/pulls",
            body={
                "title": "Launchplane batch: "
                + ", ".join(f"#{entry.pull_request_number}" for entry in candidate.entries),
                "head": branch,
                "base": candidate.base_branch,
                "draft": False,
                "maintainer_can_modify": False,
                "body": batch_pull_request_body(client=client, candidate=candidate),
            },
        )
    pull_request = _bound_batch_pull_request(payload, candidate)
    number = pull_request.get("number")
    assert isinstance(number, int)
    if pull_request.get("merged") is True:
        raise MergeAdmissionReconciliationRequiredError(
            "Batch PR merged before its landing plan; exact effect reconciliation is required."
        )
    if pull_request.get("state") != "open":
        raise MergeTrainGitHubStaleHeadError(
            "Batch PR was closed without merging; retire this candidate and replan changed queue entries.",
            status_code=409,
        )
    return number


def _read_batch_pull_request(
    client: GitHubMergeTrainClient, plan: MergeTrainBatchLandingPlan
) -> dict[str, object]:
    branch = _candidate_branch(
        repository=plan.repository,
        base_branch=plan.base_branch,
        batch_id=plan.batch_id,
        candidate_ref=plan.candidate_ref,
    )
    payload = client.transport.request(
        method="GET",
        path=f"/repos/{_repository_path(plan.repository)}/pulls/{plan.candidate_pull_request_number}",
    )
    pull_request = _validate_batch_pull_request(
        payload,
        repository=plan.repository,
        base_branch=plan.base_branch,
        candidate_branch=branch,
        candidate_sha=plan.candidate_sha,
    )
    if pull_request.get("number") != plan.candidate_pull_request_number:
        raise MergeTrainGitHubError("Batch PR read returned a different pull request.")
    return pull_request


def _confirmed_batch_entries(
    *, client: GitHubMergeTrainClient, plan: MergeTrainBatchLandingPlan, merge_sha: str
) -> tuple[tuple[MergeTrainBatchLandingEntry, ...], str, str]:
    repository_path = _repository_path(plan.repository)
    base_sha = plan.entries[0].expected_base_sha
    base_tree_sha = plan.entries[0].recorded_candidate_parent_tree_sha
    merge_sha, merge_tree_sha = _validated_landing_commit_identity(
        transport=client.transport,
        repository_path=repository_path,
        commit_sha=merge_sha,
        expected_parent_sha=base_sha,
        expected_head_sha=plan.candidate_sha,
        merge_method="merge",
    )
    if merge_tree_sha != plan.candidate_tree_sha:
        raise MergeAdmissionReconciliationRequiredError(
            "Batch merge tree differs from the tested candidate."
        )
    _wait_for_branch_sha(
        transport=client.transport,
        repository_path=repository_path,
        base_branch=plan.base_branch,
        expected_sha=merge_sha,
        previous_sha=base_sha,
    )
    observed_base_sha, observed_base_tree_sha = _base_branch_identity(
        transport=client.transport, repository_path=repository_path, base_branch=plan.base_branch
    )
    if observed_base_sha != merge_sha and not client.branch_contains_commit(
        repository=plan.repository, branch_ref=plan.base_branch, commit_sha=merge_sha
    ):
        raise MergeAdmissionReconciliationRequiredError(
            "Protected base does not contain the batch merge."
        )
    confirmed = []
    for entry in plan.entries:
        original = client._pull_request_detail(
            repository=plan.repository,
            pull_request_number=entry.pull_request_number,
        )
        for delay in MERGE_REF_READ_DELAYS_SECONDS:
            pending_head = _json_object(original.get("head"), "Original PR head")
            if (
                original.get("merged") is not False
                or pending_head.get("sha") != entry.expected_head_sha
            ):
                break
            sleep(delay)
            original = client._pull_request_detail(
                repository=plan.repository, pull_request_number=entry.pull_request_number
            )
        head = _json_object(original.get("head"), "Original PR head")
        base = _json_object(original.get("base"), "Original PR base")
        base_repository = _json_object(base.get("repo"), "Original PR base repository")
        if (
            original.get("merged") is not True
            or original.get("state") != "closed"
            or head.get("sha") != entry.expected_head_sha
            or base.get("ref") != plan.base_branch
            or _repository_full_name(base_repository, "Original PR base repository")
            != plan.repository
        ) or not client.branch_contains_commit(
            repository=plan.repository,
            branch_ref=plan.base_branch,
            commit_sha=entry.expected_head_sha,
        ):
            raise MergeAdmissionReconciliationRequiredError(
                f"Batch merged; completion of original PR #{entry.pull_request_number} is not confirmed."
            )
        confirmed.append(
            _validated_model_update(
                entry,
                status="merged",
                recorded_rolling_base_sha=base_sha,
                recorded_rolling_base_tree_sha=base_tree_sha,
                landed_head_sha=entry.expected_head_sha,
                landed_head_tree_sha=entry.expected_head_tree_sha,
                merge_commit_sha=merge_sha,
                merge_commit_tree_sha=merge_tree_sha,
            )
        )
    return tuple(confirmed), observed_base_sha, observed_base_tree_sha


def land_protected_batch(
    *,
    client: GitHubMergeTrainClient,
    landing_plan: MergeTrainBatchLandingPlan,
    admission_guard: GuardedMergeAdmission,
    recorded_at: str,
    provider_checkpoint: Callable[[MergeTrainBatchLandingPlan, MergeTrainBatchLandingEntry], None]
    | None,
    checkpoint: Callable[
        [MergeTrainBatchLandingPlan, MergeTrainBatchLandingEntry, str],
        MergeTrainBatchLandingPlanRecord | None,
    ]
    | None,
) -> MergeTrainBatchLandingPlan:
    plan = landing_plan
    number = plan.candidate_pull_request_number
    if number is None:
        raise MergeAdmissionDeniedError("Protected batch landing requires its batch PR binding.")
    repository_path = _repository_path(plan.repository)
    batch_pull_request = _read_batch_pull_request(client, plan)
    was_merged = batch_pull_request.get("merged") is True
    admissions: list[MergeAdmissionRecord] = []
    if not was_merged:
        if batch_pull_request.get("merged") is not False:
            raise MergeAdmissionReconciliationRequiredError("Batch PR merge state is unavailable.")
        base_sha, base_tree_sha = _base_branch_identity(
            transport=client.transport,
            repository_path=repository_path,
            base_branch=plan.base_branch,
        )
        if client.branch_contains_commit(
            repository=plan.repository, branch_ref=plan.base_branch, commit_sha=plan.candidate_sha
        ):
            raise MergeAdmissionReconciliationRequiredError(
                "Base contains the candidate but batch PR completion is not confirmed."
            )
        admission_guard.reconcile_batch_no_effect(
            evidence=MergeBatchNoEffectEvidence.model_validate(
                {
                    "pull_request_number": number,
                    "head_sha": plan.candidate_sha,
                    "state": batch_pull_request.get("state"),
                    "merged": False,
                    "base_contains_head": False,
                }
            ),
            observed_base_sha=base_sha,
            observed_base_tree_sha=base_tree_sha,
            observed_at=recorded_at,
        )
        if batch_pull_request.get("state") != "open":
            raise MergeTrainGitHubStaleHeadError(
                "Batch pull request is closed without merging; retire this candidate.",
                status_code=409,
            )
        if any(entry.status != "planned" for entry in plan.entries):
            raise MergeAdmissionReconciliationRequiredError(
                "Batch PR state contradicts recorded constituent completion."
            )
        if (base_sha, base_tree_sha) != (
            plan.entries[0].expected_base_sha,
            plan.entries[0].recorded_candidate_parent_tree_sha,
        ):
            raise MergeTrainGitHubStaleHeadError(
                "Protected base changed before batch landing.", status_code=409
            )
        if batch_pull_request.get("mergeable_state") == "behind":
            raise MergeTrainGitHubStaleHeadError(
                "The protected batch PR is behind its base; rebuild the candidate.",
                status_code=409,
            )
        for entry in plan.entries:
            client._validate_open_landing_pull_request(
                repository_path=repository_path,
                entry=entry,
                expected_base_ref=plan.base_branch,
                expected_base_sha=base_sha,
                expected_base_tree_sha=base_tree_sha,
                require_client_review=True,
            )
        candidate = client.observe_batch_candidate_checks(
            candidate=admission_guard.candidate_record.candidate
        )
        if candidate.status == "failed":
            raise MergeTrainGitHubStaleHeadError(
                "Protected batch required checks failed; retire this candidate.", status_code=409
            )
        if candidate.status != "passed":
            raise MergeAdmissionDeniedError(
                "Batch PR requires successful checks on the exact candidate.",
                reason_code="batch_pull_request_checks_not_ready",
            )
        # Code scanning can open threads on the batch PR itself, which GitHub
        # then refuses to merge. Stop before any admission instead.
        conversations = client.read_review_conversations(
            repository=plan.repository, base_branch=plan.base_branch, pull_request_number=number
        )
        if conversations is not None:
            raise MergeAdmissionDeniedError(
                f"Batch PR #{number} has {review_conversations_reason(conversations)}.",
                reason_code="batch_pull_request_conversations_unresolved",
            )
        try:
            # Evaluate the whole batch before appending its first admission.
            # Admission repeats the fresh evaluation before each persisted write.
            for entry in plan.entries:
                if checkpoint is not None:
                    checkpoint(plan, entry, "merge_entry")
                admission_guard.build_proposal(
                    entry=entry,
                    observed_base_sha=base_sha,
                    observed_base_tree_sha=base_tree_sha,
                    observed_head_sha=entry.expected_head_sha,
                    observed_head_tree_sha=entry.expected_head_tree_sha,
                )
            for entry in plan.entries:
                head_sha, head_tree_sha = _git_commit_identity(
                    transport=client.transport,
                    repository_path=repository_path,
                    commit_sha=entry.expected_head_sha,
                )
                if (head_sha, head_tree_sha) != (
                    entry.expected_head_sha,
                    entry.expected_head_tree_sha,
                ):
                    raise MergeTrainGitHubStaleHeadError(
                        "Batch constituent head tree changed.", status_code=409
                    )
                client._validate_open_landing_pull_request(
                    repository_path=repository_path,
                    entry=entry,
                    expected_base_ref=plan.base_branch,
                    expected_base_sha=base_sha,
                    expected_base_tree_sha=base_tree_sha,
                    require_client_review=True,
                )
                if checkpoint is not None:
                    checkpoint(plan, entry, "merge_entry")
                admissions.append(
                    admission_guard.admit(
                        entry=entry,
                        observed_base_sha=base_sha,
                        observed_base_tree_sha=base_tree_sha,
                        observed_head_sha=head_sha,
                        observed_head_tree_sha=head_tree_sha,
                    )
                )
            # Recheck every immutable member after the final admission, before
            # the single shared provider effect. Later pushes are never reported
            # as the pushed head having landed.
            for entry in plan.entries:
                client._validate_open_landing_pull_request(
                    repository_path=repository_path,
                    entry=entry,
                    expected_base_ref=plan.base_branch,
                    expected_base_sha=base_sha,
                    expected_base_tree_sha=base_tree_sha,
                    require_client_review=True,
                )
            final_batch = _read_batch_pull_request(client, plan)
            if final_batch.get("merged") is True or final_batch.get("state") != "open":
                raise MergeAdmissionReconciliationRequiredError(
                    "Batch PR changed lifecycle before dispatch."
                )
            if _base_branch_identity(
                transport=client.transport,
                repository_path=repository_path,
                base_branch=plan.base_branch,
            ) != (base_sha, base_tree_sha):
                raise MergeTrainGitHubStaleHeadError(
                    "Protected base moved before batch dispatch.", status_code=409
                )
            if provider_checkpoint is not None:
                provider_checkpoint(plan, plan.entries[-1])
        except Exception:
            for admission in admissions:
                admission_guard.record_not_dispatched(admission=admission, observed_at=recorded_at)
            raise
        try:
            merge_sha = client.merge_pull_request(
                repository=plan.repository,
                pull_request_number=number,
                head_sha=plan.candidate_sha,
                merge_method="merge",
            )
        except Exception as error:
            for admission in admissions:
                admission_guard.record_provider_failure(
                    admission=admission, error=error, observed_at=recorded_at
                )
            raise
    else:
        merge_sha = _required_text(
            batch_pull_request.get("merge_commit_sha"), "Merged batch PR requires merge commit SHA."
        )
    try:
        entries, observed_base_sha, observed_base_tree_sha = _confirmed_batch_entries(
            client=client, plan=plan, merge_sha=merge_sha
        )
    except Exception as error:
        message = (
            str(error)
            if isinstance(error, MergeAdmissionReconciliationRequiredError)
            else "Batch provider effect requires exact Git and original-PR completion evidence."
        )
        for admission in admissions:
            admission_guard.record_reconcile_required(
                admission=admission,
                reason="landing_evidence_incomplete",
                message=message,
                observed_at=recorded_at,
            )
        raise MergeAdmissionReconciliationRequiredError(message) from error
    for index, entry in enumerate(entries):
        if was_merged:
            admission_guard.reconcile_existing_landed(
                entry=entry,
                observed_base_sha=observed_base_sha,
                observed_base_tree_sha=observed_base_tree_sha,
                provider_effect_attempted=True,
                observed_at=recorded_at,
            )
        else:
            admission_guard.record_landed(
                admission=admissions[index],
                entry=entry,
                observed_base_sha=observed_base_sha,
                observed_base_tree_sha=observed_base_tree_sha,
                base_contains_merge_commit=True,
                provider_effect_attempted=True,
                observed_at=recorded_at,
            )
        progress = _validated_model_update(
            plan, entries=entries[: index + 1] + plan.entries[index + 1 :]
        )
        record = checkpoint(progress, entry, "entry_merged") if checkpoint is not None else None
        if record is not None:
            admission_guard.update_landing_plan_record(record)
        else:
            admission_guard.update_landing_plan(progress)
    return _validated_model_update(plan, entries=entries)
