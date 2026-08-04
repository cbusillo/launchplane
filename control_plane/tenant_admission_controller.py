from __future__ import annotations

from collections.abc import Callable
import hashlib
import json
from typing import Literal, Protocol, cast
from urllib.parse import quote, urlencode

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.manager_preview_approval_projection import (
    MANAGER_PREVIEW_APPROVAL_CHECK_NAME,
)
from control_plane.contracts.merge_train_policy import MergeTrainMergeMethod
from control_plane.contracts.tenant_merge_eligibility import (
    TenantMergeCandidate,
    _normalize_utc_timestamp,
)
from control_plane.github_payload import json_object, required_positive_int, required_string_text
from control_plane.merge_train_controller_run_once import (
    DEFAULT_MERGE_TRAIN_CONTROLLER_LEASE_SECONDS,
    MergeTrainControllerLeaseContext,
    MergeTrainControllerStateRecordStore,
    merge_train_controller_lease_owner,
)
from control_plane.merge_train_github import (
    GitHubMergeTrainClient,
    MergeTrainGitHubError,
    MergeTrainGitHubStaleHeadError,
    MergeTrainGitHubTransport,
    UrllibMergeTrainGitHubTransport,
)
from control_plane.tenant_admission_status import (
    TENANT_ADMISSION_STATUS_CONTEXT,
    TenantAdmissionStatusReadModel,
    TenantAdmissionStatusStore,
    get_tenant_admission_status,
)
from control_plane.workflows.ship import utc_now_timestamp


TENANT_ADMISSION_CONTROLLER_RUN_ONCE_ACTION = "tenant_admission.controller.run_once"
TENANT_ADMISSION_CONTROLLER_ACTIVE_ACTION = "tenant_admission_merge"

TenantAdmissionControllerOutcome = Literal[
    "ready",
    "merged",
    "adopted",
    "already_merged",
    "blocked",
    "not_applicable",
]
TenantAdmissionTechnicalCheckSource = Literal["commit_status", "check_run"]
TenantAdmissionTechnicalCheckState = Literal["pass", "pending", "fail", "unavailable"]

_ADMITTED_CATEGORIES = frozenset({"manager-approved", "technical-waived", "maintenance-admitted"})
_EXCLUDED_TECHNICAL_CONTEXTS = frozenset(
    {
        TENANT_ADMISSION_STATUS_CONTEXT.casefold(),
        MANAGER_PREVIEW_APPROVAL_CHECK_NAME.casefold(),
    }
)
_PROVIDER_EFFECT_PHASES = frozenset({"merge_pull_request", "confirm_merge"})


class TenantAdmissionControllerError(ValueError):
    pass


class TenantAdmissionControllerStaleCandidateError(TenantAdmissionControllerError):
    pass


class TenantAdmissionControllerReconciliationError(TenantAdmissionControllerError):
    pass


class TenantAdmissionControllerRunOnceEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    candidate: TenantMergeCandidate
    base_branch: str
    merge_method: MergeTrainMergeMethod = "merge"
    mutate: bool = False

    @model_validator(mode="after")
    def _validate_envelope(self) -> "TenantAdmissionControllerRunOnceEnvelope":
        if self.schema_version != 1:
            raise ValueError("Unsupported tenant admission controller schema version.")
        self.base_branch = self.base_branch.strip()
        if not self.base_branch:
            raise ValueError("Tenant admission controller requires base_branch.")
        return self


class TenantAdmissionPullRequestFacts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: str
    pull_request_number: int = Field(ge=1)
    pull_request_url: str
    state: Literal["open", "closed"]
    merged: bool
    draft: bool = False
    mergeable: bool | None = None
    head_sha: str
    base_branch: str
    base_sha: str
    merge_commit_sha: str = ""

    @model_validator(mode="after")
    def _validate_facts(self) -> "TenantAdmissionPullRequestFacts":
        self.repository = self.repository.strip().lower()
        self.pull_request_url = self.pull_request_url.strip()
        self.head_sha = self.head_sha.strip().lower()
        self.base_branch = self.base_branch.strip()
        self.base_sha = self.base_sha.strip().lower()
        self.merge_commit_sha = self.merge_commit_sha.strip().lower()
        if self.merged and (self.state != "closed" or not self.merge_commit_sha):
            raise ValueError("Merged tenant admission PR facts require closed state and merge SHA.")
        if not self.merged and self.state != "open":
            raise ValueError("Unmerged tenant admission PR facts must remain open.")
        return self


class TenantAdmissionTechnicalCheckSignal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: TenantAdmissionTechnicalCheckSource
    name: str
    app_id: int | None = Field(default=None, ge=1)
    state: TenantAdmissionTechnicalCheckState

    @model_validator(mode="after")
    def _validate_signal(self) -> "TenantAdmissionTechnicalCheckSignal":
        self.name = self.name.strip()
        if not self.name:
            raise ValueError("Tenant admission technical check signal requires name.")
        return self


class TenantAdmissionRequiredTechnicalCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    app_id: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _validate_required_check(self) -> "TenantAdmissionRequiredTechnicalCheck":
        self.name = self.name.strip()
        if not self.name:
            raise ValueError("Required tenant admission technical check requires name.")
        return self


class TenantAdmissionTechnicalChecks(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    head_sha: str
    base_sha: str
    strict: bool
    base_up_to_date: bool | None = None
    status: TenantAdmissionTechnicalCheckState
    required_checks: tuple[TenantAdmissionRequiredTechnicalCheck, ...] = ()
    signals: tuple[TenantAdmissionTechnicalCheckSignal, ...] = ()
    excluded_contexts: tuple[str, ...] = (
        MANAGER_PREVIEW_APPROVAL_CHECK_NAME,
        TENANT_ADMISSION_STATUS_CONTEXT,
    )
    evaluated_at: str
    binding_sha256: str = ""

    @model_validator(mode="after")
    def _validate_checks(self) -> "TenantAdmissionTechnicalChecks":
        if self.schema_version != 1:
            raise ValueError("Unsupported tenant admission technical checks schema version.")
        self.head_sha = self.head_sha.strip().lower()
        self.base_sha = self.base_sha.strip().lower()
        self.evaluated_at = _normalize_utc_timestamp(self.evaluated_at, "evaluated_at")
        if self.strict and self.base_up_to_date is None:
            raise ValueError("Strict tenant admission checks require base freshness evidence.")
        expected_digest = _model_sha256(self, exclude={"binding_sha256"})
        if self.binding_sha256:
            self.binding_sha256 = self.binding_sha256.strip().lower()
            if self.binding_sha256 != expected_digest:
                raise ValueError("Tenant admission technical checks digest does not match payload.")
        else:
            self.binding_sha256 = expected_digest
        if self.status == "pass" and (not self.required_checks or not self.signals):
            raise ValueError(
                "Passing tenant admission technical checks require policy and signals."
            )
        if self.status == "pass" and self.strict and not self.base_up_to_date:
            raise ValueError("Passing strict tenant admission checks require an up-to-date base.")
        return self


class TenantAdmissionControllerRunOnceResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    outcome: TenantAdmissionControllerOutcome
    mutated: bool = False
    candidate: TenantMergeCandidate
    base_branch: str
    merge_method: MergeTrainMergeMethod
    pull_request_facts: TenantAdmissionPullRequestFacts
    admission: TenantAdmissionStatusReadModel | None = None
    technical_checks: TenantAdmissionTechnicalChecks | None = None
    merge_commit_sha: str = ""
    detail: str

    @model_validator(mode="after")
    def _validate_result(self) -> "TenantAdmissionControllerRunOnceResult":
        if self.schema_version != 1:
            raise ValueError("Unsupported tenant admission controller result schema version.")
        self.base_branch = self.base_branch.strip()
        self.merge_commit_sha = self.merge_commit_sha.strip().lower()
        self.detail = self.detail.strip()
        if not self.detail:
            raise ValueError("Tenant admission controller result requires detail.")
        if self.outcome in {"merged", "adopted", "already_merged"}:
            if not self.pull_request_facts.merged or not self.merge_commit_sha:
                raise ValueError(
                    "Completed tenant admission merge result requires merged PR facts."
                )
        if self.outcome == "merged" and not self.mutated:
            raise ValueError("Merged tenant admission result must report mutation.")
        return self


class TenantAdmissionControllerCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    operation_id: str
    request: TenantAdmissionControllerRunOnceEnvelope
    pull_request_facts: TenantAdmissionPullRequestFacts | None = None
    admission: TenantAdmissionStatusReadModel | None = None
    technical_checks: TenantAdmissionTechnicalChecks | None = None
    merge_commit_sha: str = ""

    @model_validator(mode="after")
    def _validate_checkpoint(self) -> "TenantAdmissionControllerCheckpoint":
        if self.schema_version != 1:
            raise ValueError("Unsupported tenant admission controller checkpoint schema version.")
        self.operation_id = self.operation_id.strip()
        self.merge_commit_sha = self.merge_commit_sha.strip().lower()
        if self.operation_id != tenant_admission_controller_operation_id(self.request):
            raise ValueError("Tenant admission controller checkpoint operation ID does not match.")
        return self


class TenantAdmissionControllerStore(
    TenantAdmissionStatusStore,
    MergeTrainControllerStateRecordStore,
    Protocol,
):
    pass


TransportFactory = Callable[[str], MergeTrainGitHubTransport]


class TenantAdmissionControllerGitHubClient:
    def __init__(self, *, transport: MergeTrainGitHubTransport) -> None:
        self.transport = transport
        self.merge_client = GitHubMergeTrainClient(transport=transport)

    def read_pull_request(
        self,
        *,
        expected: TenantMergeCandidate,
        base_branch: str,
    ) -> TenantAdmissionPullRequestFacts:
        owner, repo = expected.repository.split("/", 1)
        payload = json_object(
            self.transport.request(
                method="GET",
                path=(
                    f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/pulls/"
                    f"{expected.pull_request_number}"
                ),
            ),
            "GitHub pull request response",
            error_type=TenantAdmissionControllerError,
        )
        if (
            required_positive_int(
                payload.get("number"),
                "GitHub pull request response requires a positive number.",
                error_type=TenantAdmissionControllerError,
            )
            != expected.pull_request_number
        ):
            raise TenantAdmissionControllerStaleCandidateError(
                "GitHub pull request number changed from the expected candidate."
            )
        state = required_string_text(
            payload.get("state"),
            "GitHub pull request response requires state.",
            error_type=TenantAdmissionControllerError,
        ).lower()
        raw_merged = payload.get("merged")
        if not isinstance(raw_merged, bool):
            raise TenantAdmissionControllerError(
                "GitHub pull request response merged must be boolean."
            )
        merged = raw_merged
        raw_draft = payload.get("draft")
        if not isinstance(raw_draft, bool):
            raise TenantAdmissionControllerError(
                "GitHub pull request response draft must be boolean."
            )
        draft = raw_draft
        raw_mergeable = payload.get("mergeable")
        if raw_mergeable is not None and not isinstance(raw_mergeable, bool):
            raise TenantAdmissionControllerError(
                "GitHub pull request response mergeable must be boolean or null."
            )
        mergeable = raw_mergeable
        if state not in {"open", "closed"} or (state == "closed" and not merged):
            raise TenantAdmissionControllerStaleCandidateError(
                "GitHub pull request is neither open nor an exact merged result."
            )
        base = json_object(
            payload.get("base"),
            "GitHub pull request base",
            error_type=TenantAdmissionControllerError,
        )
        base_repo = json_object(
            base.get("repo"),
            "GitHub pull request base repository",
            error_type=TenantAdmissionControllerError,
        )
        base_owner = json_object(
            base_repo.get("owner"),
            "GitHub pull request base repository owner",
            error_type=TenantAdmissionControllerError,
        )
        head = json_object(
            payload.get("head"),
            "GitHub pull request head",
            error_type=TenantAdmissionControllerError,
        )
        head_repo = json_object(
            head.get("repo"),
            "GitHub pull request head repository",
            error_type=TenantAdmissionControllerError,
        )
        head_owner = json_object(
            head_repo.get("owner"),
            "GitHub pull request head repository owner",
            error_type=TenantAdmissionControllerError,
        )
        current_repository_id = str(
            required_positive_int(
                base_repo.get("id"),
                "GitHub base repository requires a positive id.",
                error_type=TenantAdmissionControllerError,
            )
        )
        current_owner_id = str(
            required_positive_int(
                base_owner.get("id"),
                "GitHub base repository owner requires a positive id.",
                error_type=TenantAdmissionControllerError,
            )
        )
        current_repository = required_string_text(
            base_repo.get("full_name"),
            "GitHub base repository requires full_name.",
            error_type=TenantAdmissionControllerError,
        ).lower()
        current_head_sha = required_string_text(
            head.get("sha"),
            "GitHub pull request head requires sha.",
            error_type=TenantAdmissionControllerError,
        ).lower()
        current_base_branch = required_string_text(
            base.get("ref"),
            "GitHub pull request base requires ref.",
            error_type=TenantAdmissionControllerError,
        )
        current_base_sha = required_string_text(
            base.get("sha"),
            "GitHub pull request base requires sha.",
            error_type=TenantAdmissionControllerError,
        ).lower()
        head_repository_id = str(
            required_positive_int(
                head_repo.get("id"),
                "GitHub head repository requires a positive id.",
                error_type=TenantAdmissionControllerError,
            )
        )
        head_owner_id = str(
            required_positive_int(
                head_owner.get("id"),
                "GitHub head repository owner requires a positive id.",
                error_type=TenantAdmissionControllerError,
            )
        )
        head_repository = required_string_text(
            head_repo.get("full_name"),
            "GitHub head repository requires full_name.",
            error_type=TenantAdmissionControllerError,
        ).lower()
        if (
            current_repository_id != expected.repository_id
            or current_owner_id != expected.repository_owner_id
            or current_repository != expected.repository
            or current_head_sha != expected.head_sha
            or current_base_branch != base_branch
            or head_repository_id != expected.repository_id
            or head_owner_id != expected.repository_owner_id
            or head_repository != expected.repository
        ):
            raise TenantAdmissionControllerStaleCandidateError(
                "GitHub repository, branch, or head identity changed from the expected candidate."
            )
        merge_commit_sha = (
            str(payload.get("merge_commit_sha") or "").strip().lower() if merged else ""
        )
        return TenantAdmissionPullRequestFacts(
            repository=current_repository,
            pull_request_number=expected.pull_request_number,
            pull_request_url=required_string_text(
                payload.get("html_url"),
                "GitHub pull request response requires html_url.",
                error_type=TenantAdmissionControllerError,
            ),
            state=cast(Literal["open", "closed"], state),
            merged=merged,
            draft=draft,
            mergeable=mergeable,
            head_sha=current_head_sha,
            base_branch=current_base_branch,
            base_sha=current_base_sha,
            merge_commit_sha=merge_commit_sha,
        )

    def read_technical_checks(
        self,
        *,
        repository: str,
        base_branch: str,
        base_sha: str,
        head_sha: str,
        evaluated_at: str,
    ) -> TenantAdmissionTechnicalChecks:
        owner, repo = repository.split("/", 1)
        repository_path = f"{quote(owner, safe='')}/{quote(repo, safe='')}"
        encoded_head_sha = quote(head_sha, safe="")
        encoded_base_branch = quote(base_branch, safe="")
        try:
            required_checks_payload = json_object(
                self.transport.request(
                    method="GET",
                    path=(
                        f"/repos/{repository_path}/branches/{encoded_base_branch}/protection/"
                        "required_status_checks"
                    ),
                ),
                "GitHub required status checks response",
                error_type=TenantAdmissionControllerError,
            )
        except MergeTrainGitHubError as error:
            if error.status_code != 404:
                raise
            required_checks_payload = {"strict": False, "checks": [], "contexts": []}
        strict, required_checks = _required_technical_checks(required_checks_payload)
        base_up_to_date = (
            self.merge_client.branch_contains_commit(
                repository=repository,
                branch_ref=head_sha,
                commit_sha=base_sha,
            )
            if strict
            else None
        )
        status_payload = json_object(
            self.transport.request(
                method="GET",
                path=f"/repos/{repository_path}/commits/{encoded_head_sha}/status",
            ),
            "GitHub combined status response",
            error_type=TenantAdmissionControllerError,
        )
        current_status_sha = required_string_text(
            status_payload.get("sha"),
            "GitHub combined status response requires sha.",
            error_type=TenantAdmissionControllerError,
        ).lower()
        if current_status_sha != head_sha:
            raise TenantAdmissionControllerStaleCandidateError(
                "GitHub commit status response does not match the expected head."
            )
        signals = list(_technical_status_signals(status_payload))
        page = 1
        while True:
            query = urlencode({"filter": "latest", "per_page": "100", "page": str(page)})
            check_runs_payload = json_object(
                self.transport.request(
                    method="GET",
                    path=(
                        f"/repos/{repository_path}/commits/{encoded_head_sha}/check-runs?{query}"
                    ),
                ),
                "GitHub check runs response",
                error_type=TenantAdmissionControllerError,
            )
            raw_check_runs = check_runs_payload.get("check_runs")
            if not isinstance(raw_check_runs, list):
                raise TenantAdmissionControllerError(
                    "GitHub check runs response must include check_runs."
                )
            signals.extend(
                _technical_check_run_signals(
                    raw_check_runs,
                    expected_head_sha=head_sha,
                )
            )
            if len(raw_check_runs) < 100:
                break
            page += 1
        normalized_signals = tuple(
            sorted(
                signals,
                key=lambda signal: (
                    signal.source,
                    signal.name.casefold(),
                    signal.app_id or 0,
                ),
            )
        )
        return TenantAdmissionTechnicalChecks(
            head_sha=head_sha,
            base_sha=base_sha,
            strict=strict,
            base_up_to_date=base_up_to_date,
            status=_required_technical_check_state(
                required_checks=required_checks,
                signals=normalized_signals,
                strict=strict,
                base_up_to_date=base_up_to_date,
            ),
            required_checks=required_checks,
            signals=normalized_signals,
            evaluated_at=evaluated_at,
        )

    def merge_pull_request(
        self,
        *,
        request: TenantAdmissionControllerRunOnceEnvelope,
    ) -> str:
        return self.merge_client.merge_pull_request(
            repository=request.candidate.repository,
            pull_request_number=request.candidate.pull_request_number,
            head_sha=request.candidate.head_sha,
            merge_method=request.merge_method,
        )

    def branch_contains_commit(
        self,
        *,
        repository: str,
        base_branch: str,
        commit_sha: str,
    ) -> bool:
        return self.merge_client.branch_contains_commit(
            repository=repository,
            branch_ref=base_branch,
            commit_sha=commit_sha,
        )


def require_tenant_admission_controller_store(
    record_store: object,
) -> TenantAdmissionControllerStore:
    required_methods = (
        "list_tenant_repository_classification_records",
        "list_repository_human_role_policy_records",
        "list_tenant_technical_human_waiver_event_records",
        "list_trusted_maintenance_policy_records",
        "list_trusted_maintenance_evidence_records",
        "list_authz_policy_records",
        "list_preview_records",
        "read_preview_generation_record",
        "list_manager_preview_approval_event_records",
        "list_merge_train_controller_state_records",
        "acquire_merge_train_controller_state_record",
        "compare_and_set_merge_train_controller_state_record",
    )
    missing_methods = tuple(
        name for name in required_methods if not callable(getattr(record_store, name, None))
    )
    if missing_methods:
        raise TypeError(
            "Launchplane record store does not support tenant admission controller runs: "
            + ", ".join(missing_methods)
        )
    return cast(TenantAdmissionControllerStore, record_store)


def tenant_admission_controller_operation_id(
    request: TenantAdmissionControllerRunOnceEnvelope,
) -> str:
    digest = hashlib.sha256(
        json.dumps(
            request.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return f"tenant-admission-merge-{digest}"


def evaluate_tenant_admission_candidate(
    *,
    request: TenantAdmissionControllerRunOnceEnvelope,
    store: TenantAdmissionStatusStore,
    token: str,
    transport_factory: TransportFactory = lambda value: UrllibMergeTrainGitHubTransport(
        token=value
    ),
) -> TenantAdmissionControllerRunOnceResult:
    if request.mutate:
        raise ValueError("Tenant admission evaluation requires mutate=false.")
    client = TenantAdmissionControllerGitHubClient(transport=transport_factory(token))
    return _evaluate_tenant_admission_candidate(
        request=request,
        store=store,
        client=client,
    )


def execute_tenant_admission_controller_run_once(
    *,
    request: TenantAdmissionControllerRunOnceEnvelope,
    store: TenantAdmissionControllerStore,
    token: str,
    trace_id: str,
    transport_factory: TransportFactory = lambda value: UrllibMergeTrainGitHubTransport(
        token=value
    ),
) -> TenantAdmissionControllerRunOnceResult:
    if not request.mutate:
        return evaluate_tenant_admission_candidate(
            request=request,
            store=store,
            token=token,
            transport_factory=transport_factory,
        )

    client = TenantAdmissionControllerGitHubClient(transport=transport_factory(token))
    operation_id = tenant_admission_controller_operation_id(request)
    lease_owner = merge_train_controller_lease_owner(trace_id=trace_id)
    controller_state = store.acquire_merge_train_controller_state_record(
        repository=request.candidate.repository,
        base_branch=request.base_branch,
        policy_key=(
            f"tenant-admission:{request.candidate.repository_id}:"
            f"{request.candidate.product}:{request.candidate.context}"
        ),
        policy_sha256=_controller_scope_sha256(request),
        lease_owner=lease_owner,
        lease_seconds=DEFAULT_MERGE_TRAIN_CONTROLLER_LEASE_SECONDS,
        initial_active_action=TENANT_ADMISSION_CONTROLLER_ACTIVE_ACTION,
        initial_active_phase="evaluate_candidate",
        adoptable_active_actions=(TENANT_ADMISSION_CONTROLLER_ACTIVE_ACTION,),
    )
    lease = MergeTrainControllerLeaseContext(record=controller_state, record_store=store)
    try:
        if controller_state.reconciliation_status == "adopted":
            return _resume_tenant_admission_controller(
                request=request,
                operation_id=operation_id,
                store=store,
                client=client,
                lease=lease,
            )
        lease.checkpoint(
            active_action=TENANT_ADMISSION_CONTROLLER_ACTIVE_ACTION,
            active_phase="evaluate_candidate",
            active_record_id=operation_id,
            active_pull_request_number=request.candidate.pull_request_number,
            step_payload=TenantAdmissionControllerCheckpoint(
                operation_id=operation_id,
                request=request,
            ).model_dump(mode="json"),
        )
        return _execute_tenant_admission_merge(
            request=request,
            operation_id=operation_id,
            store=store,
            client=client,
            lease=lease,
        )
    except Exception as error:
        _release_tenant_controller_after_error(lease=lease, error=error)
        raise


def _resume_tenant_admission_controller(
    *,
    request: TenantAdmissionControllerRunOnceEnvelope,
    operation_id: str,
    store: TenantAdmissionControllerStore,
    client: TenantAdmissionControllerGitHubClient,
    lease: MergeTrainControllerLeaseContext,
) -> TenantAdmissionControllerRunOnceResult:
    if (
        lease.record.active_action == TENANT_ADMISSION_CONTROLLER_ACTIVE_ACTION
        and not lease.record.active_record_id
    ):
        lease.checkpoint(
            active_action=TENANT_ADMISSION_CONTROLLER_ACTIVE_ACTION,
            active_phase="evaluate_candidate",
            active_record_id=operation_id,
            active_pull_request_number=request.candidate.pull_request_number,
            step_payload=TenantAdmissionControllerCheckpoint(
                operation_id=operation_id,
                request=request,
            ).model_dump(mode="json"),
        )
        return _execute_tenant_admission_merge(
            request=request,
            operation_id=operation_id,
            store=store,
            client=client,
            lease=lease,
        )
    if (
        lease.record.active_action != TENANT_ADMISSION_CONTROLLER_ACTIVE_ACTION
        or lease.record.active_record_id != operation_id
    ):
        raise TenantAdmissionControllerReconciliationError(
            "A different durable controller effect already owns this repository and base branch."
        )
    checkpoint = TenantAdmissionControllerCheckpoint.model_validate(lease.record.step_payload)
    if checkpoint.request != request:
        raise TenantAdmissionControllerReconciliationError(
            "Tenant admission controller retry does not match the durable request."
        )
    facts = client.read_pull_request(
        expected=request.candidate,
        base_branch=request.base_branch,
    )
    if facts.merged:
        if not client.branch_contains_commit(
            repository=request.candidate.repository,
            base_branch=request.base_branch,
            commit_sha=facts.merge_commit_sha,
        ):
            raise TenantAdmissionControllerReconciliationError(
                "Merged pull request commit is not contained by the target branch."
            )
        if checkpoint.merge_commit_sha and checkpoint.merge_commit_sha != facts.merge_commit_sha:
            raise TenantAdmissionControllerReconciliationError(
                "Merged pull request commit differs from the durable controller checkpoint."
            )
        lease.release()
        return TenantAdmissionControllerRunOnceResult(
            outcome="adopted",
            mutated=False,
            candidate=request.candidate,
            base_branch=request.base_branch,
            merge_method=request.merge_method,
            pull_request_facts=facts,
            admission=checkpoint.admission,
            technical_checks=checkpoint.technical_checks,
            merge_commit_sha=facts.merge_commit_sha,
            detail="Adopted the exact merged pull request after durable reconciliation.",
        )
    return _execute_tenant_admission_merge(
        request=request,
        operation_id=operation_id,
        store=store,
        client=client,
        lease=lease,
    )


def _execute_tenant_admission_merge(
    *,
    request: TenantAdmissionControllerRunOnceEnvelope,
    operation_id: str,
    store: TenantAdmissionControllerStore,
    client: TenantAdmissionControllerGitHubClient,
    lease: MergeTrainControllerLeaseContext,
) -> TenantAdmissionControllerRunOnceResult:
    first_result = _evaluate_tenant_admission_candidate(
        request=request,
        store=store,
        client=client,
    )
    if first_result.outcome != "ready":
        lease.release()
        return first_result
    lease.checkpoint(active_phase="pre_merge_recheck")
    fresh_result = _evaluate_tenant_admission_candidate(
        request=request,
        store=store,
        client=client,
    )
    if fresh_result.outcome != "ready":
        lease.release()
        return fresh_result
    if first_result.pull_request_facts.base_sha != fresh_result.pull_request_facts.base_sha:
        raise TenantAdmissionControllerStaleCandidateError(
            "Pull request base moved between controller evaluation and merge."
        )
    checkpoint = TenantAdmissionControllerCheckpoint(
        operation_id=operation_id,
        request=request,
        pull_request_facts=fresh_result.pull_request_facts,
        admission=fresh_result.admission,
        technical_checks=fresh_result.technical_checks,
    )
    lease.checkpoint(
        active_phase="merge_pull_request",
        step_payload=checkpoint.model_dump(mode="json"),
    )
    try:
        merge_commit_sha = client.merge_pull_request(request=request)
    except MergeTrainGitHubStaleHeadError as error:
        lease.release()
        raise TenantAdmissionControllerStaleCandidateError(
            "GitHub rejected the merge because the exact pull request head moved."
        ) from error
    checkpoint = checkpoint.model_copy(update={"merge_commit_sha": merge_commit_sha})
    lease.checkpoint(
        active_phase="confirm_merge",
        step_payload=checkpoint.model_dump(mode="json"),
    )
    merged_facts = client.read_pull_request(
        expected=request.candidate,
        base_branch=request.base_branch,
    )
    if not merged_facts.merged or merged_facts.merge_commit_sha != merge_commit_sha:
        raise TenantAdmissionControllerReconciliationError(
            "GitHub did not confirm the requested exact pull request merge."
        )
    if not client.branch_contains_commit(
        repository=request.candidate.repository,
        base_branch=request.base_branch,
        commit_sha=merge_commit_sha,
    ):
        raise TenantAdmissionControllerReconciliationError(
            "GitHub target branch does not contain the confirmed merge commit."
        )
    lease.release()
    return TenantAdmissionControllerRunOnceResult(
        outcome="merged",
        mutated=True,
        candidate=request.candidate,
        base_branch=request.base_branch,
        merge_method=request.merge_method,
        pull_request_facts=merged_facts,
        admission=fresh_result.admission,
        technical_checks=fresh_result.technical_checks,
        merge_commit_sha=merge_commit_sha,
        detail="Merged the exact admitted tenant pull request after fresh technical checks.",
    )


def _evaluate_tenant_admission_candidate(
    *,
    request: TenantAdmissionControllerRunOnceEnvelope,
    store: TenantAdmissionStatusStore,
    client: TenantAdmissionControllerGitHubClient,
) -> TenantAdmissionControllerRunOnceResult:
    facts = client.read_pull_request(
        expected=request.candidate,
        base_branch=request.base_branch,
    )
    if facts.merged:
        if not client.branch_contains_commit(
            repository=request.candidate.repository,
            base_branch=request.base_branch,
            commit_sha=facts.merge_commit_sha,
        ):
            raise TenantAdmissionControllerReconciliationError(
                "Merged pull request commit is not contained by the target branch."
            )
        return TenantAdmissionControllerRunOnceResult(
            outcome="already_merged",
            mutated=False,
            candidate=request.candidate,
            base_branch=request.base_branch,
            merge_method=request.merge_method,
            pull_request_facts=facts,
            merge_commit_sha=facts.merge_commit_sha,
            detail="The exact pull request was already merged into the target branch.",
        )
    evaluated_at = utc_now_timestamp()
    admission = get_tenant_admission_status(
        store=store,
        candidate=request.candidate,
        evaluated_at=evaluated_at,
    )
    if admission.category == "engineering":
        return TenantAdmissionControllerRunOnceResult(
            outcome="not_applicable",
            candidate=request.candidate,
            base_branch=request.base_branch,
            merge_method=request.merge_method,
            pull_request_facts=facts,
            admission=admission,
            detail="Engineering repositories retain their existing merge flow.",
        )
    if admission.classification_kind != "tenant_ui":
        return TenantAdmissionControllerRunOnceResult(
            outcome="blocked",
            candidate=request.candidate,
            base_branch=request.base_branch,
            merge_method=request.merge_method,
            pull_request_facts=facts,
            admission=admission,
            detail=f"Tenant admission is {admission.category} for the exact current head.",
        )
    if facts.draft or facts.mergeable is not True:
        mergeability = "draft" if facts.draft else "unavailable or conflicting"
        return TenantAdmissionControllerRunOnceResult(
            outcome="blocked",
            candidate=request.candidate,
            base_branch=request.base_branch,
            merge_method=request.merge_method,
            pull_request_facts=facts,
            admission=admission,
            detail=f"GitHub mergeability is {mergeability} for the exact current head.",
        )
    checks = client.read_technical_checks(
        repository=request.candidate.repository,
        base_branch=request.base_branch,
        base_sha=facts.base_sha,
        head_sha=request.candidate.head_sha,
        evaluated_at=utc_now_timestamp(),
    )
    if admission.category not in _ADMITTED_CATEGORIES or not admission.decision.admitted:
        return TenantAdmissionControllerRunOnceResult(
            outcome="blocked",
            candidate=request.candidate,
            base_branch=request.base_branch,
            merge_method=request.merge_method,
            pull_request_facts=facts,
            admission=admission,
            technical_checks=checks,
            detail=(
                f"Tenant admission is {admission.category} and technical checks are "
                f"{checks.status} for the exact current head."
            ),
        )
    if checks.status != "pass":
        return TenantAdmissionControllerRunOnceResult(
            outcome="blocked",
            candidate=request.candidate,
            base_branch=request.base_branch,
            merge_method=request.merge_method,
            pull_request_facts=facts,
            admission=admission,
            technical_checks=checks,
            detail=f"Technical checks are {checks.status} for the exact current head.",
        )
    return TenantAdmissionControllerRunOnceResult(
        outcome="ready",
        candidate=request.candidate,
        base_branch=request.base_branch,
        merge_method=request.merge_method,
        pull_request_facts=facts,
        admission=admission,
        technical_checks=checks,
        detail="Tenant admission and technical checks are current for the exact head.",
    )


def _release_tenant_controller_after_error(
    *,
    lease: MergeTrainControllerLeaseContext,
    error: Exception,
) -> None:
    if not lease.owner:
        return
    phase = lease.record.active_phase
    active_action = lease.record.active_action or TENANT_ADMISSION_CONTROLLER_ACTIVE_ACTION
    try:
        if isinstance(error, TenantAdmissionControllerReconciliationError):
            lease.release(
                reconciliation_status="required",
                reconciliation_detail=(
                    f"operator_required:{active_action}:{phase}:{type(error).__name__}"
                ),
                clear_active_state=False,
            )
        elif phase in _PROVIDER_EFFECT_PHASES:
            lease.release(
                reconciliation_status="required",
                reconciliation_detail=(f"retryable:{active_action}:{phase}:{type(error).__name__}"),
                clear_active_state=False,
            )
        else:
            lease.release()
    except Exception:
        return


def _technical_status_signals(
    payload: dict[str, object],
) -> tuple[TenantAdmissionTechnicalCheckSignal, ...]:
    raw_statuses = payload.get("statuses")
    if not isinstance(raw_statuses, list):
        raise TenantAdmissionControllerError(
            "GitHub combined status response must include statuses."
        )
    signals: list[TenantAdmissionTechnicalCheckSignal] = []
    seen_contexts: set[str] = set()
    for raw_status in raw_statuses:
        status = json_object(
            raw_status,
            "GitHub commit status",
            error_type=TenantAdmissionControllerError,
        )
        context = required_string_text(
            status.get("context"),
            "GitHub commit status requires context.",
            error_type=TenantAdmissionControllerError,
        )
        normalized_context = context.casefold()
        if (
            normalized_context in _EXCLUDED_TECHNICAL_CONTEXTS
            or normalized_context in seen_contexts
        ):
            continue
        seen_contexts.add(normalized_context)
        signals.append(
            TenantAdmissionTechnicalCheckSignal(
                source="commit_status",
                name=context,
                app_id=_optional_github_app_id(status),
                state=_commit_status_state(str(status.get("state") or "")),
            )
        )
    return tuple(signals)


def _technical_check_run_signals(
    raw_check_runs: list[object],
    *,
    expected_head_sha: str,
) -> tuple[TenantAdmissionTechnicalCheckSignal, ...]:
    signals: list[TenantAdmissionTechnicalCheckSignal] = []
    for raw_check_run in raw_check_runs:
        check_run = json_object(
            raw_check_run,
            "GitHub check run",
            error_type=TenantAdmissionControllerError,
        )
        name = required_string_text(
            check_run.get("name"),
            "GitHub check run requires name.",
            error_type=TenantAdmissionControllerError,
        )
        current_head_sha = required_string_text(
            check_run.get("head_sha"),
            "GitHub check run requires head_sha.",
            error_type=TenantAdmissionControllerError,
        ).lower()
        if current_head_sha != expected_head_sha:
            raise TenantAdmissionControllerStaleCandidateError(
                "GitHub check run does not match the expected head."
            )
        app = json_object(
            check_run.get("app"),
            "GitHub check run app",
            error_type=TenantAdmissionControllerError,
        )
        app_id = required_positive_int(
            app.get("id"),
            "GitHub check run app requires a positive id.",
            error_type=TenantAdmissionControllerError,
        )
        if name.casefold() in _EXCLUDED_TECHNICAL_CONTEXTS:
            continue
        signals.append(
            TenantAdmissionTechnicalCheckSignal(
                source="check_run",
                name=name,
                app_id=app_id,
                state=_check_run_state(check_run),
            )
        )
    return tuple(signals)


def _required_technical_checks(
    payload: dict[str, object],
) -> tuple[bool, tuple[TenantAdmissionRequiredTechnicalCheck, ...]]:
    strict = payload.get("strict")
    if not isinstance(strict, bool):
        raise TenantAdmissionControllerError(
            "GitHub required status checks response strict must be boolean."
        )
    raw_checks = payload.get("checks")
    raw_contexts = payload.get("contexts")
    if not isinstance(raw_checks, list) or not isinstance(raw_contexts, list):
        raise TenantAdmissionControllerError(
            "GitHub required status checks response must include checks and contexts."
        )
    required_checks: dict[tuple[str, int | None], TenantAdmissionRequiredTechnicalCheck] = {}
    detailed_contexts: set[str] = set()
    for raw_check in raw_checks:
        check = json_object(
            raw_check,
            "GitHub required status check",
            error_type=TenantAdmissionControllerError,
        )
        context = required_string_text(
            check.get("context"),
            "GitHub required status check requires context.",
            error_type=TenantAdmissionControllerError,
        )
        normalized_context = context.casefold()
        if normalized_context in _EXCLUDED_TECHNICAL_CONTEXTS:
            continue
        raw_app_id = check.get("app_id")
        if raw_app_id is None:
            app_id = None
        else:
            app_id = required_positive_int(
                raw_app_id,
                "GitHub required status check app_id must be positive or null.",
                error_type=TenantAdmissionControllerError,
            )
        detailed_contexts.add(normalized_context)
        required_checks[(normalized_context, app_id)] = TenantAdmissionRequiredTechnicalCheck(
            name=context,
            app_id=app_id,
        )
    for raw_context in raw_contexts:
        context = required_string_text(
            raw_context,
            "GitHub required status check context must be text.",
            error_type=TenantAdmissionControllerError,
        )
        normalized_context = context.casefold()
        if (
            normalized_context in _EXCLUDED_TECHNICAL_CONTEXTS
            or normalized_context in detailed_contexts
        ):
            continue
        required_checks[(normalized_context, None)] = TenantAdmissionRequiredTechnicalCheck(
            name=context,
        )
    return strict, tuple(
        sorted(
            required_checks.values(),
            key=lambda required: (required.name.casefold(), required.app_id or 0),
        )
    )


def _optional_github_app_id(payload: dict[str, object]) -> int | None:
    raw_app = payload.get("app")
    if raw_app is None:
        return None
    app = json_object(
        raw_app,
        "GitHub commit status app",
        error_type=TenantAdmissionControllerError,
    )
    return required_positive_int(
        app.get("id"),
        "GitHub commit status app requires a positive id.",
        error_type=TenantAdmissionControllerError,
    )


def _commit_status_state(value: str) -> TenantAdmissionTechnicalCheckState:
    normalized = value.strip().lower()
    if normalized == "success":
        return "pass"
    if normalized == "pending":
        return "pending"
    if normalized in {"failure", "error"}:
        return "fail"
    return "unavailable"


def _check_run_state(check_run: dict[str, object]) -> TenantAdmissionTechnicalCheckState:
    status = str(check_run.get("status") or "").strip().lower()
    if status != "completed":
        return "pending" if status in {"queued", "in_progress", "pending"} else "unavailable"
    conclusion = str(check_run.get("conclusion") or "").strip().lower()
    if conclusion in {"success", "neutral", "skipped"}:
        return "pass"
    if conclusion in {
        "failure",
        "timed_out",
        "cancelled",
        "action_required",
        "startup_failure",
        "stale",
    }:
        return "fail"
    return "unavailable"


def _combine_technical_check_states(
    signals: tuple[TenantAdmissionTechnicalCheckSignal, ...],
) -> TenantAdmissionTechnicalCheckState:
    if not signals:
        return "unavailable"
    states = {signal.state for signal in signals}
    if "fail" in states:
        return "fail"
    if "pending" in states:
        return "pending"
    if "unavailable" in states:
        return "unavailable"
    return "pass"


def _required_technical_check_state(
    *,
    required_checks: tuple[TenantAdmissionRequiredTechnicalCheck, ...],
    signals: tuple[TenantAdmissionTechnicalCheckSignal, ...],
    strict: bool,
    base_up_to_date: bool | None,
) -> TenantAdmissionTechnicalCheckState:
    if not required_checks:
        return "unavailable"
    if strict and not base_up_to_date:
        return "fail"
    required_states: list[TenantAdmissionTechnicalCheckState] = []
    for required_check in required_checks:
        matches = tuple(
            signal
            for signal in signals
            if signal.name.casefold() == required_check.name.casefold()
            and (required_check.app_id is None or signal.app_id == required_check.app_id)
        )
        required_states.append(_combine_technical_check_states(matches))
    if "fail" in required_states:
        return "fail"
    if "pending" in required_states:
        return "pending"
    if "unavailable" in required_states:
        return "unavailable"
    return "pass"


def _controller_scope_sha256(request: TenantAdmissionControllerRunOnceEnvelope) -> str:
    payload = {
        "repository_id": request.candidate.repository_id,
        "repository_owner_id": request.candidate.repository_owner_id,
        "repository": request.candidate.repository,
        "product": request.candidate.product,
        "context": request.candidate.context,
        "base_branch": request.base_branch,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _model_sha256(model: BaseModel, *, exclude: set[str]) -> str:
    return hashlib.sha256(
        json.dumps(
            model.model_dump(mode="json", exclude=exclude, exclude_none=True),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
