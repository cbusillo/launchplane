from collections.abc import Callable
from datetime import datetime, timezone
from typing import Protocol, cast

from control_plane.contracts.integration_attestation import (
    IntegrationAttestationEvidence,
    IntegrationAttestationRecord,
    IntegrationAttestationRequest,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.product_review import ProductReviewStore, applicable_product_review_decisions
from control_plane.product_review_carry import (
    _IntegrationEvidence,
    _carryable,
    _complete_commits,
    _integrated_change_reviewed,
    _object,
    _on_base_branch,
    _path,
)


class IntegrationAttestationStore(ProductReviewStore, Protocol):
    def list_integration_attestation_records(
        self, *, repository: str, pull_request_number: int
    ) -> tuple[IntegrationAttestationRecord, ...]: ...

    def write_integration_attestation_record_if_absent(
        self, record: IntegrationAttestationRecord
    ) -> IntegrationAttestationRecord: ...


def plan_integration_attestation(
    *,
    request: IntegrationAttestationRequest,
    profile: LaunchplaneProductProfileRecord,
    store: ProductReviewStore,
    read: Callable[[str], object],
) -> IntegrationAttestationEvidence:
    if request.product != profile.product or request.repository != profile.repository:
        raise ValueError("The product does not own this repository.")
    prefix = f"/repos/{_path(profile.repository)}"
    change = _object(read(f"{prefix}/pulls/{request.pull_request_number}"))
    base_branch = _object(change.get("base")).get("ref")
    if (
        change.get("state") != "open"
        or _object(change.get("head")).get("sha") != request.expected_head_sha
        or _object(_object(change.get("base")).get("repo")).get("full_name") != profile.repository
        or not isinstance(base_branch, str)
        or not base_branch.strip()
    ):
        raise ValueError("The open change's head or repository no longer matches.")
    decisions = applicable_product_review_decisions(
        decisions=store.list_product_review_decision_records(
            repository=profile.repository, pull_request_number=request.pull_request_number
        ),
        profile=profile,
        pull_request_number=request.pull_request_number,
    )
    if (
        not decisions
        or decisions[0].record_id != request.source_decision_id
        or not _carryable(decisions[0])
        or decisions[0].base_branch != base_branch
    ):
        raise ValueError("The exact source acceptance is no longer applicable.")
    accepted = decisions[0]
    comparison = _object(read(f"{prefix}/compare/{accepted.head_sha}...{request.merge_sha}"))
    commits = _complete_commits(comparison, accepted.head_sha)
    merge = next((value for value in commits if value.get("sha") == request.merge_sha), {})
    parents = merge.get("parents")
    if (
        comparison.get("status") != "ahead"
        or not isinstance(parents, list)
        or len(parents) != 2
        or _object(parents[0]).get("sha") != accepted.head_sha
    ):
        raise ValueError("The integration must merge directly onto the exact accepted parent.")
    imported_parent = _object(parents[1]).get("sha")
    if not isinstance(imported_parent, str) or not _on_base_branch(
        repository=profile.repository, commit=imported_parent, base_branch=base_branch, read=read
    ):
        raise ValueError("The imported parent is not on the change's base branch.")
    target_comparison = _object(
        read(f"{prefix}/compare/{request.merge_sha}...{request.expected_head_sha}")
    )
    if target_comparison.get("status") not in {"ahead", "identical"}:
        raise ValueError("The exact merge is not in the current change's history.")
    evidence = _IntegrationEvidence()
    for commit in commits:
        sha = str(commit["sha"])
        if sha == request.merge_sha:
            continue
        if not _on_base_branch(
            repository=profile.repository, commit=sha, base_branch=imported_parent, read=read
        ) or not _integrated_change_reviewed(
            sha=sha,
            base_branch=base_branch,
            store=store,
            profile=profile,
            read=read,
            evidence=evidence,
        ):
            raise ValueError("Every imported change requires its qualified acceptance or basis.")
    return IntegrationAttestationEvidence(
        product=profile.product,
        repository=profile.repository,
        pull_request_number=request.pull_request_number,
        base_branch=base_branch,
        owner_github_id=profile.owner.github_id,
        source_decision_id=accepted.record_id,
        merge_sha=request.merge_sha,
        tree_sha=str(_object(_object(merge.get("commit")).get("tree")).get("sha") or ""),
        parent_shas=(accepted.head_sha, imported_parent),
        integrated_changes=tuple(evidence.changes[number] for number in sorted(evidence.changes)),
        batch_record_ids=tuple(sorted(evidence.batch_record_ids)),
    )


def record_integration_attestation(
    *,
    request: IntegrationAttestationRequest,
    evidence: IntegrationAttestationEvidence,
    store: IntegrationAttestationStore,
    actor: str,
) -> IntegrationAttestationRecord:
    if request.reviewed_evidence_sha256 != evidence.digest() or request.review is None:
        raise ValueError("Apply requires the exact reviewed evidence and confirming model review.")
    return store.write_integration_attestation_record_if_absent(
        IntegrationAttestationRecord(
            evidence=evidence,
            review=request.review,
            statement=request.statement,
            recorded_by=actor,
            recorded_at=datetime.now(timezone.utc).isoformat(),
        )
    )


def integration_attestation_store(store: object) -> IntegrationAttestationStore:
    if not all(
        callable(getattr(store, name, None))
        for name in (
            "list_integration_attestation_records",
            "write_integration_attestation_record_if_absent",
            "list_product_review_decision_records",
            "product_review_lock",
        )
    ):
        raise TypeError("Integration attestation storage is unavailable.")
    return cast(IntegrationAttestationStore, store)
