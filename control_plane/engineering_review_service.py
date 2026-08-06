from __future__ import annotations

from collections.abc import Callable
from typing import Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field

from control_plane import secrets as control_plane_secrets
from control_plane.contracts.engineering_review_run import (
    EngineeringReviewAuthorityRecord,
    EngineeringReviewConflictError,
    EngineeringReviewModelSlot,
    EngineeringReviewPullRequestTarget,
    EngineeringReviewRunRecord,
    EngineeringReviewSequenceError,
    EngineeringReviewUnavailableError,
    build_engineering_review_assignment_fingerprint,
    build_engineering_review_run_id,
    generate_engineering_review_run_credential,
)
from control_plane.contracts.every_code_work_request import EveryCodeWorkRequestRecord
from control_plane.workflows.launchplane import github_api_request, github_pull_request_reference
from control_plane.workflows.ship import utc_now_timestamp


EngineeringReviewApplyMode = Literal["dry_run", "apply"]
EngineeringReviewApplyStatus = Literal["would_apply", "would_replay", "applied", "replayed"]


class EngineeringReviewAuthorityStore(Protocol):
    def list_engineering_review_authority_records(
        self,
        *,
        repository: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[EngineeringReviewAuthorityRecord, ...]: ...

    def compare_and_write_engineering_review_authority_record(
        self,
        record: EngineeringReviewAuthorityRecord,
        *,
        expected_current_authority_id: str,
        expected_current_authority_digest: str,
    ) -> Literal["written", "replayed"]: ...


class EngineeringReviewRunCreateStore(Protocol):
    def read_every_code_work_request_record(
        self, request_id: str
    ) -> EveryCodeWorkRequestRecord: ...

    def list_engineering_review_authority_records(
        self,
        *,
        repository: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[EngineeringReviewAuthorityRecord, ...]: ...

    def create_engineering_review_run_records_if_absent(
        self,
        records: tuple[EngineeringReviewRunRecord, ...],
        *,
        expected_authority_id: str,
        expected_authority_digest: str,
        expected_work_request_lifecycle_id: str,
    ) -> tuple[tuple[EngineeringReviewRunRecord, ...], bool]: ...


class EngineeringReviewAuthorityApplyResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    status: EngineeringReviewApplyStatus
    record: EngineeringReviewAuthorityRecord


class EngineeringReviewRunCreateResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    status: Literal["created", "replayed"]
    work_request_id: str
    runs: tuple[EngineeringReviewRunRecord, ...]


EngineeringReviewTargetResolver = Callable[
    [EveryCodeWorkRequestRecord], EngineeringReviewPullRequestTarget
]
GitHubApiRequest = Callable[..., object]


def require_engineering_review_authority_store(
    record_store: object,
) -> EngineeringReviewAuthorityStore:
    required = (
        "list_engineering_review_authority_records",
        "compare_and_write_engineering_review_authority_record",
    )
    missing = [name for name in required if not callable(getattr(record_store, name, None))]
    if missing:
        raise TypeError(
            "record store does not support engineering review authority writes: "
            + ", ".join(missing)
        )
    return cast(EngineeringReviewAuthorityStore, record_store)


def require_engineering_review_run_create_store(
    record_store: object,
) -> EngineeringReviewRunCreateStore:
    required = (
        "read_every_code_work_request_record",
        "list_engineering_review_authority_records",
        "create_engineering_review_run_records_if_absent",
    )
    missing = [name for name in required if not callable(getattr(record_store, name, None))]
    if missing:
        raise TypeError(
            "record store does not support engineering review scheduling: " + ", ".join(missing)
        )
    return cast(EngineeringReviewRunCreateStore, record_store)


def apply_engineering_review_authority(
    *,
    store: EngineeringReviewAuthorityStore,
    record: EngineeringReviewAuthorityRecord,
    expected_current_authority_id: str = "",
    expected_current_authority_digest: str = "",
    mode: EngineeringReviewApplyMode = "apply",
) -> EngineeringReviewAuthorityApplyResult:
    records = store.list_engineering_review_authority_records(repository=record.repository)
    replay = _validate_authority_append(
        records=records,
        record=record,
        expected_current_authority_id=expected_current_authority_id,
        expected_current_authority_digest=expected_current_authority_digest,
    )
    if mode == "dry_run":
        return EngineeringReviewAuthorityApplyResult(
            status="would_replay" if replay else "would_apply",
            record=record,
        )
    result = store.compare_and_write_engineering_review_authority_record(
        record,
        expected_current_authority_id=expected_current_authority_id,
        expected_current_authority_digest=expected_current_authority_digest,
    )
    return EngineeringReviewAuthorityApplyResult(
        status="replayed" if result == "replayed" else "applied",
        record=record,
    )


def create_engineering_review_runs(
    *,
    store: EngineeringReviewRunCreateStore,
    work_request_id: str,
    target_resolver: EngineeringReviewTargetResolver,
    created_at: str = "",
) -> EngineeringReviewRunCreateResult:
    work_request = store.read_every_code_work_request_record(work_request_id)
    if work_request.state != "done" or not work_request.result_pr_url.strip():
        raise EngineeringReviewConflictError(
            "Engineering review scheduling requires a completed work request with a linked PR."
        )
    authorities = store.list_engineering_review_authority_records(
        repository=work_request.repository,
        status="active",
        limit=2,
    )
    if len(authorities) != 1:
        raise EngineeringReviewUnavailableError(
            "Engineering review scheduling requires exactly one active repository authority."
        )
    authority = authorities[0]
    target = target_resolver(work_request)
    if target.repository != work_request.repository:
        raise EngineeringReviewConflictError(
            "Linked pull request repository does not match the stored work request."
        )
    review_slots = _default_review_slots(authority)
    timestamp = created_at.strip() or utc_now_timestamp()
    records: list[EngineeringReviewRunRecord] = []
    for model_slot in review_slots:
        assignment_fingerprint = build_engineering_review_assignment_fingerprint(
            target=target,
            authority=authority,
            work_request_id=work_request.request_id,
            work_request_lifecycle_id=work_request.lifecycle_id,
            model_slot=model_slot,
        )
        plaintext_credential, credential_hash = generate_engineering_review_run_credential()
        try:
            credential_ciphertext, credential_key_id = (
                control_plane_secrets._encrypt_secret_value(plaintext_credential)
            )
        except Exception as error:
            raise EngineeringReviewUnavailableError(
                "Engineering review credential encryption authority is unavailable."
            ) from error
        records.append(
            EngineeringReviewRunRecord(
                run_id=build_engineering_review_run_id(
                    assignment_fingerprint=assignment_fingerprint
                ),
                assignment_fingerprint=assignment_fingerprint,
                review_slot=model_slot.review_slot,
                state="pending",
                repository=target.repository,
                pr_number=target.pr_number,
                pr_url=target.pr_url,
                head_sha=target.head_sha,
                tree_sha=target.tree_sha,
                authority_id=authority.authority_id,
                authority_digest=authority.authority_digest,
                policy_revision=authority.policy_revision,
                work_request_id=work_request.request_id,
                work_request_lifecycle_id=work_request.lifecycle_id,
                model_id=model_slot.model_id,
                model_family=model_slot.model_family,
                worker_runtime_id=authority.worker_runtime_id,
                worker_host=authority.worker_host,
                executable_path=authority.executable_path,
                binary_sha256=authority.binary_sha256,
                lease_seconds=authority.lease_seconds,
                created_at=timestamp,
                updated_at=timestamp,
                credential_hash=credential_hash,
                credential_ciphertext=credential_ciphertext,
                credential_key_id=credential_key_id,
            )
        )
    stored, created = store.create_engineering_review_run_records_if_absent(
        tuple(records),
        expected_authority_id=authority.authority_id,
        expected_authority_digest=authority.authority_digest,
        expected_work_request_lifecycle_id=work_request.lifecycle_id,
    )
    return EngineeringReviewRunCreateResult(
        status="created" if created else "replayed",
        work_request_id=work_request.request_id,
        runs=stored,
    )


def resolve_engineering_review_pull_request_target(
    work_request: EveryCodeWorkRequestRecord,
    *,
    github_token: str,
    api_request: GitHubApiRequest = github_api_request,
) -> EngineeringReviewPullRequestTarget:
    if not github_token.strip():
        raise EngineeringReviewUnavailableError(
            "Authenticated GitHub evidence is unavailable for engineering review scheduling."
        )
    reference = github_pull_request_reference(pr_url=work_request.result_pr_url)
    if reference is None:
        raise EngineeringReviewConflictError("Stored work request result_pr_url is not a GitHub PR.")
    owner = str(reference["owner"])
    repo = str(reference["repo"])
    pr_number = int(reference["pr_number"])
    repository = f"{owner}/{repo}"
    if repository.casefold() != work_request.repository.casefold():
        raise EngineeringReviewConflictError(
            "Stored work request result PR belongs to a different repository."
        )
    pull_request = api_request(
        path=f"/repos/{owner}/{repo}/pulls/{pr_number}",
        token=github_token,
    )
    if not isinstance(pull_request, dict) or pull_request.get("state") != "open":
        raise EngineeringReviewConflictError("Engineering review requires an open linked PR.")
    head = pull_request.get("head")
    head_sha = head.get("sha") if isinstance(head, dict) else None
    if not isinstance(head_sha, str):
        raise EngineeringReviewUnavailableError("GitHub PR evidence is missing head.sha.")
    commit = api_request(
        path=f"/repos/{owner}/{repo}/git/commits/{head_sha}",
        token=github_token,
    )
    tree = commit.get("tree") if isinstance(commit, dict) else None
    tree_sha = tree.get("sha") if isinstance(tree, dict) else None
    if not isinstance(tree_sha, str):
        raise EngineeringReviewUnavailableError("GitHub commit evidence is missing tree.sha.")
    html_url = pull_request.get("html_url")
    pr_url = (
        html_url.strip()
        if isinstance(html_url, str) and html_url.strip()
        else f"https://github.com/{owner}/{repo}/pull/{pr_number}"
    )
    return EngineeringReviewPullRequestTarget(
        repository=work_request.repository,
        pr_number=pr_number,
        pr_url=pr_url,
        head_sha=head_sha.strip().lower(),
        tree_sha=tree_sha.strip().lower(),
    )


def _default_review_slots(
    authority: EngineeringReviewAuthorityRecord,
) -> tuple[EngineeringReviewModelSlot, ...]:
    if len(authority.model_slots) < 2:
        raise EngineeringReviewUnavailableError(
            "Server-derived review classification is unavailable; two model-diverse slots are required."
        )
    return authority.model_slots[:2]


def _validate_authority_append(
    *,
    records: tuple[EngineeringReviewAuthorityRecord, ...],
    record: EngineeringReviewAuthorityRecord,
    expected_current_authority_id: str,
    expected_current_authority_digest: str,
) -> bool:
    active = next((candidate for candidate in records if candidate.status == "active"), None)
    same_revision = next(
        (candidate for candidate in records if candidate.policy_revision == record.policy_revision),
        None,
    )
    if same_revision is not None:
        if (
            same_revision.authority_id == record.authority_id
            and same_revision.authority_digest == record.authority_digest
            and same_revision.status == record.status == "active"
            and active is not None
            and active.authority_id == same_revision.authority_id
        ):
            return True
        raise EngineeringReviewConflictError(
            "Engineering review authority revision is stale or has different content."
        )
    if active is None:
        if record.policy_revision != 1 or record.supersedes_authority_id is not None:
            raise EngineeringReviewSequenceError(
                "Initial engineering review authority must use revision 1 without a predecessor."
            )
        if expected_current_authority_id or expected_current_authority_digest:
            raise EngineeringReviewConflictError(
                "Engineering review authority expected-current values are stale."
            )
        return False
    if expected_current_authority_id != active.authority_id:
        raise EngineeringReviewConflictError(
            "Engineering review authority expected_current_authority_id is stale."
        )
    if expected_current_authority_digest != active.authority_digest:
        raise EngineeringReviewConflictError(
            "Engineering review authority expected_current_authority_digest is stale."
        )
    if record.policy_revision != active.policy_revision + 1:
        raise EngineeringReviewSequenceError(
            "Engineering review authority revisions must be contiguous."
        )
    if record.supersedes_authority_id != active.authority_id:
        raise EngineeringReviewSequenceError(
            "Engineering review authority successor must name the active authority."
        )
    return False
