from __future__ import annotations

from collections.abc import Callable
from typing import Literal
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.tenant_merge_eligibility import TenantMergeCandidate
from control_plane.github_payload import json_object, required_positive_int, required_string_text
from control_plane.tenant_admission_status import (
    TENANT_ADMISSION_STATUS_CONTEXT,
    TenantAdmissionStatusCategory,
    TenantAdmissionStatusReadModel,
)
from control_plane.workflows.launchplane import github_api_request


TENANT_ADMISSION_STATUS_RECONCILE_ACTION = "tenant_admission.reconcile"

TenantAdmissionGitHubState = Literal["pending", "success", "failure", "error"]
TenantAdmissionProjectionWriteStatus = Literal["projected", "replayed", "not_required"]
GitHubApiRequest = Callable[..., object]


class TenantAdmissionProjectionError(ValueError):
    pass


class TenantAdmissionProjectionStaleCandidateError(TenantAdmissionProjectionError):
    pass


class TenantAdmissionStatusReconcileEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    candidate: TenantMergeCandidate

    @model_validator(mode="after")
    def _validate_envelope(self) -> "TenantAdmissionStatusReconcileEnvelope":
        if self.schema_version != 1:
            raise ValueError("Unsupported tenant admission reconcile schema version.")
        return self


class TenantAdmissionGitHubProjection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    required: bool
    repository: str
    pull_request_number: int = Field(ge=1)
    head_sha: str
    category: TenantAdmissionStatusCategory
    state: TenantAdmissionGitHubState
    description: str
    target_url: str
    decision_binding_sha256: str

    @model_validator(mode="after")
    def _validate_projection(self) -> "TenantAdmissionGitHubProjection":
        if self.schema_version != 1:
            raise ValueError("Unsupported tenant admission projection schema version.")
        self.repository = self.repository.strip().lower()
        owner, separator, name = self.repository.partition("/")
        if not separator or not owner or not name or "/" in name:
            raise ValueError("Tenant admission projection repository must be OWNER/REPO.")
        self.head_sha = self.head_sha.strip().lower()
        self.description = self.description.strip()
        self.target_url = self.target_url.strip()
        self.decision_binding_sha256 = self.decision_binding_sha256.strip().lower()
        if not self.description or len(self.description) > 140:
            raise ValueError("Tenant admission projection description must be 1-140 characters.")
        if not self.target_url.startswith("https://"):
            raise ValueError("Tenant admission projection target_url must use HTTPS.")
        if len(self.decision_binding_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.decision_binding_sha256
        ):
            raise ValueError("Tenant admission projection requires decision SHA-256.")
        if not self.required and self.category != "engineering":
            raise ValueError("Only engineering admission projections may be not required.")
        return self


class TenantAdmissionProjectionWriteResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    status: TenantAdmissionProjectionWriteStatus
    check_context: str = TENANT_ADMISSION_STATUS_CONTEXT
    check_state: TenantAdmissionGitHubState
    head_sha: str


def build_tenant_admission_projection(
    *,
    read_model: TenantAdmissionStatusReadModel,
    candidate: TenantMergeCandidate,
    pull_request_url: str,
) -> TenantAdmissionGitHubProjection:
    category = read_model.category
    state, description = _projection_state_and_description(category)
    return TenantAdmissionGitHubProjection(
        required=category != "engineering",
        repository=candidate.repository,
        pull_request_number=candidate.pull_request_number,
        head_sha=candidate.head_sha,
        category=category,
        state=state,
        description=description,
        target_url=pull_request_url,
        decision_binding_sha256=read_model.decision.decision_binding_sha256,
    )


def read_current_tenant_admission_candidate(
    *,
    expected: TenantMergeCandidate,
    token: str,
    api_request: GitHubApiRequest = github_api_request,
) -> str:
    owner, repo = expected.repository.split("/", 1)
    payload = json_object(
        api_request(
            path=(
                f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/pulls/"
                f"{expected.pull_request_number}"
            ),
            token=token,
        ),
        "GitHub pull request response",
        error_type=TenantAdmissionProjectionError,
    )
    if (
        required_positive_int(
            payload.get("number"),
            "GitHub pull request response requires a positive number.",
            error_type=TenantAdmissionProjectionError,
        )
        != expected.pull_request_number
    ):
        raise TenantAdmissionProjectionStaleCandidateError(
            "GitHub pull request number does not match the expected candidate."
        )
    if (
        required_string_text(
            payload.get("state"),
            "GitHub pull request response requires state.",
            error_type=TenantAdmissionProjectionError,
        ).lower()
        != "open"
    ):
        raise TenantAdmissionProjectionStaleCandidateError("GitHub pull request is not open.")
    base = json_object(
        payload.get("base"),
        "GitHub pull request base",
        error_type=TenantAdmissionProjectionError,
    )
    base_repository = json_object(
        base.get("repo"),
        "GitHub pull request base repository",
        error_type=TenantAdmissionProjectionError,
    )
    base_owner = json_object(
        base_repository.get("owner"),
        "GitHub repository owner",
        error_type=TenantAdmissionProjectionError,
    )
    head = json_object(
        payload.get("head"),
        "GitHub pull request head",
        error_type=TenantAdmissionProjectionError,
    )
    current_repository_id = str(
        required_positive_int(
            base_repository.get("id"),
            "GitHub base repository requires a positive id.",
            error_type=TenantAdmissionProjectionError,
        )
    )
    current_repository_owner_id = str(
        required_positive_int(
            base_owner.get("id"),
            "GitHub base repository owner requires a positive id.",
            error_type=TenantAdmissionProjectionError,
        )
    )
    current_repository = required_string_text(
        base_repository.get("full_name"),
        "GitHub base repository requires full_name.",
        error_type=TenantAdmissionProjectionError,
    ).lower()
    current_head_sha = required_string_text(
        head.get("sha"),
        "GitHub pull request head requires sha.",
        error_type=TenantAdmissionProjectionError,
    ).lower()
    if (
        current_repository_id != expected.repository_id
        or current_repository_owner_id != expected.repository_owner_id
        or current_repository != expected.repository
        or current_head_sha != expected.head_sha
    ):
        raise TenantAdmissionProjectionStaleCandidateError(
            "GitHub repository identity or pull request head changed from the expected candidate."
        )
    return required_string_text(
        payload.get("html_url"),
        "GitHub pull request response requires html_url.",
        error_type=TenantAdmissionProjectionError,
    )


def write_tenant_admission_projection(
    *,
    projection: TenantAdmissionGitHubProjection,
    token: str,
    api_request: GitHubApiRequest = github_api_request,
) -> TenantAdmissionProjectionWriteResult:
    if not projection.required:
        return TenantAdmissionProjectionWriteResult(
            status="not_required",
            check_state=projection.state,
            head_sha=projection.head_sha,
        )
    owner, repo = projection.repository.split("/", 1)
    encoded_owner = quote(owner, safe="")
    encoded_repo = quote(repo, safe="")
    encoded_head_sha = quote(projection.head_sha, safe="")
    current_payload = json_object(
        api_request(
            path=(f"/repos/{encoded_owner}/{encoded_repo}/commits/{encoded_head_sha}/status"),
            token=token,
        ),
        "GitHub combined status response",
        error_type=TenantAdmissionProjectionError,
    )
    statuses = current_payload.get("statuses")
    if not isinstance(statuses, list):
        raise TenantAdmissionProjectionError(
            "GitHub combined status response must include statuses."
        )
    current = next(
        (
            status
            for status in statuses
            if isinstance(status, dict)
            and str(status.get("context") or "") == TENANT_ADMISSION_STATUS_CONTEXT
        ),
        None,
    )
    if current is not None and (
        str(current.get("state") or "") == projection.state
        and str(current.get("description") or "") == projection.description
        and str(current.get("target_url") or "") == projection.target_url
    ):
        return TenantAdmissionProjectionWriteResult(
            status="replayed",
            check_state=projection.state,
            head_sha=projection.head_sha,
        )
    response = json_object(
        api_request(
            path=(f"/repos/{encoded_owner}/{encoded_repo}/statuses/{encoded_head_sha}"),
            token=token,
            method="POST",
            body={
                "state": projection.state,
                "target_url": projection.target_url,
                "description": projection.description,
                "context": TENANT_ADMISSION_STATUS_CONTEXT,
            },
        ),
        "GitHub tenant admission status response",
        error_type=TenantAdmissionProjectionError,
    )
    response_context = required_string_text(
        response.get("context"),
        "GitHub tenant admission status response requires context.",
        error_type=TenantAdmissionProjectionError,
    )
    response_state = required_string_text(
        response.get("state"),
        "GitHub tenant admission status response requires state.",
        error_type=TenantAdmissionProjectionError,
    )
    response_sha = required_string_text(
        response.get("sha"),
        "GitHub tenant admission status response requires sha.",
        error_type=TenantAdmissionProjectionError,
    ).lower()
    if (
        response_context != TENANT_ADMISSION_STATUS_CONTEXT
        or response_state != projection.state
        or response_sha != projection.head_sha
    ):
        raise TenantAdmissionProjectionError(
            "GitHub tenant admission status response did not match the requested projection."
        )
    return TenantAdmissionProjectionWriteResult(
        status="projected",
        check_state=projection.state,
        head_sha=projection.head_sha,
    )


def _projection_state_and_description(
    category: TenantAdmissionStatusCategory,
) -> tuple[TenantAdmissionGitHubState, str]:
    if category == "engineering":
        return "success", "Tenant admission is not required for this repository."
    if category == "pending":
        return "pending", "Tenant admission is waiting for one authorized path."
    if category == "manager-approved":
        return "success", "Manager approved the exact current preview."
    if category == "technical-waived":
        return "success", "Repository owner waived the exact technical head."
    if category == "maintenance-admitted":
        return "success", "Trusted maintenance policy admits the exact current head."
    if category == "stale":
        return "failure", "Tenant admission evidence is stale for the current head."
    if category == "denied":
        return "failure", "Tenant admission is currently denied."
    return "error", "Tenant admission authority is unavailable."
