"""Protected, PR-native landing of a whole tested candidate in one provider effect."""

from collections.abc import Callable
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
from control_plane.merge_train_github import (
    GitHubMergeTrainClient,
    MergeTrainGitHubError,
    MergeTrainGitHubStaleHeadError,
    _base_branch_identity,
    _git_commit_identity,
    _json_object,
    _repository_path,
    _required_text,
    _validated_landing_commit_identity,
    _validated_model_update,
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
        or head_repository.get("full_name") != repository
        or base.get("ref") != base_branch
        or base_repository.get("full_name") != repository
        or pull_request.get("draft") is not False
    ):
        raise MergeTrainGitHubStaleHeadError(
            "Batch pull request identity changed.", status_code=409
        )
    return pull_request


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
    owner = candidate.repository.split("/", maxsplit=1)[0]
    marker = f"<!-- launchplane-batch:{candidate.batch_id}:{candidate.candidate_sha} -->"
    pulls = client.transport.request(
        method="GET",
        path=f"/repos/{repository_path}/pulls?state=all&head={quote(f'{owner}:{branch}', safe='')}&base={quote(candidate.base_branch, safe='')}&per_page=100",
    )
    if not isinstance(pulls, list) or len(pulls) > 1:
        raise MergeAdmissionDeniedError("Batch pull request discovery is unavailable or ambiguous.")
    if pulls:
        payload = pulls[0]
    else:
        members = "\n".join(
            f"- #{entry.pull_request_number} at `{entry.head_sha}`" for entry in candidate.entries
        )
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
                "body": (
                    f"{marker}\n\nThis PR lands Launchplane's tested candidate `{candidate.candidate_sha}` "
                    f"through the protected merge endpoint. Constituent PRs and reviewed heads:\n\n{members}\n\n"
                    "Launchplane revalidates every constituent and records the shared landing effect. "
                    "Keep source PRs and branches intact. The controller confirms their completion after landing."
                ),
            },
        )
    pull_request = _validate_batch_pull_request(
        payload,
        repository=candidate.repository,
        base_branch=candidate.base_branch,
        candidate_branch=branch,
        candidate_sha=candidate.candidate_sha,
    )
    number = pull_request.get("number")
    body = pull_request.get("body")
    if type(number) is not int or number < 1 or not isinstance(body, str) or marker not in body:
        raise MergeAdmissionDeniedError("Batch pull request lacks its recorded candidate binding.")
    if pull_request.get("state") != "open" or pull_request.get("merged") is True:
        raise MergeAdmissionDeniedError(
            "The recorded batch pull request is already closed; reconcile it before replanning."
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
        head = _json_object(original.get("head"), "Original PR head")
        base = _json_object(original.get("base"), "Original PR base")
        base_repository = _json_object(base.get("repo"), "Original PR base repository")
        if (
            original.get("merged") is not True
            or original.get("state") != "closed"
            or head.get("sha") != entry.expected_head_sha
            or base.get("ref") != plan.base_branch
            or base_repository.get("full_name") != plan.repository
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
            raise MergeAdmissionDeniedError(
                "Batch pull request is closed without merging.",
                reason_code="batch_pull_request_closed",
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
            raise MergeAdmissionDeniedError(
                "The protected batch PR is behind its base; rebuild the candidate.",
                reason_code="batch_pull_request_behind",
            )
        candidate = client.observe_batch_candidate_checks(
            candidate=admission_guard.candidate_record.candidate
        )
        if candidate.status != "passed":
            raise MergeAdmissionDeniedError(
                "Batch PR requires successful checks on the exact candidate.",
                reason_code="batch_pull_request_checks_not_ready",
            )
        try:
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
