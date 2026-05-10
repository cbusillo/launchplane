from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


PreviewWorkflowEventName = Literal["pull_request", "pull_request_target", "workflow_dispatch"]
PreviewWorkflowPullRequestAction = Literal[
    "opened",
    "reopened",
    "synchronize",
    "edited",
    "labeled",
    "unlabeled",
    "closed",
]
PreviewWorkflowOperation = Literal[
    "refresh",
    "destroy",
    "unsupported_notice",
    "ignore",
]
PreviewWorkflowExecutionTrust = Literal["same_repo", "fork", "dependabot"]


class PreviewWorkflowEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_name: PreviewWorkflowEventName
    action: PreviewWorkflowPullRequestAction | str = ""
    operation: Literal["refresh", "destroy", ""] = ""
    repository: str
    anchor_repo: str
    anchor_pr_number: int = Field(ge=1)
    actor: str = ""
    base_repository: str
    head_repository: str
    head_sha: str = ""
    label_names: tuple[str, ...] = ()
    action_label: str = ""
    preview_label: str = "preview"

    @model_validator(mode="after")
    def _validate_event(self) -> "PreviewWorkflowEvent":
        if not self.repository.strip():
            raise ValueError("preview workflow event requires repository")
        if not self.anchor_repo.strip():
            raise ValueError("preview workflow event requires anchor_repo")
        if not self.base_repository.strip():
            raise ValueError("preview workflow event requires base_repository")
        if not self.head_repository.strip():
            raise ValueError("preview workflow event requires head_repository")
        if not self.preview_label.strip():
            raise ValueError("preview workflow event requires preview_label")
        if self.event_name in {"pull_request", "pull_request_target"} and not self.action.strip():
            raise ValueError("pull request preview workflow events require action")
        if self.event_name == "workflow_dispatch" and not self.operation.strip():
            raise ValueError("workflow_dispatch preview workflow events require operation")
        if self.action == "labeled" and not self.action_label.strip():
            raise ValueError("labeled preview workflow events require action_label")
        if self.event_name == "pull_request" and _is_dependabot_actor(self.actor):
            raise ValueError("dependabot preview workflow events must use pull_request_target")
        return self


class PreviewWorkflowDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: PreviewWorkflowOperation
    reason: str
    execution_trust: PreviewWorkflowExecutionTrust
    label_enabled: bool
    launchplane_route_path: str = ""
    feedback_status: Literal["pending", "destroyed", "unsupported", ""] = ""
    checkout_untrusted_head: bool = False
    product_build_required: bool = False
    launchplane_feedback_required: bool = False


def decide_preview_workflow_operation(event: PreviewWorkflowEvent) -> PreviewWorkflowDecision:
    """Classify a thin product-repo preview trigger into the Launchplane contract."""

    label_enabled = _preview_label_enabled(event)
    execution_trust = _execution_trust(event)

    if event.event_name == "workflow_dispatch":
        if event.operation == "destroy":
            return PreviewWorkflowDecision(
                operation="destroy",
                reason="manual_destroy_requested",
                execution_trust=execution_trust,
                label_enabled=label_enabled,
                launchplane_route_path="/v1/drivers/generic-web/preview-destroy",
                feedback_status="destroyed",
                launchplane_feedback_required=True,
            )
        return PreviewWorkflowDecision(
            operation="refresh",
            reason="manual_refresh_requested",
            execution_trust=execution_trust,
            label_enabled=label_enabled,
            launchplane_route_path="/v1/drivers/generic-web/preview-refresh",
            feedback_status="pending",
            checkout_untrusted_head=True,
            product_build_required=True,
            launchplane_feedback_required=True,
        )

    if event.event_name == "pull_request_target":
        if _needs_unsupported_notice(event=event, label_enabled=label_enabled):
            return PreviewWorkflowDecision(
                operation="unsupported_notice",
                reason=f"preview_not_supported_for_{execution_trust}",
                execution_trust=execution_trust,
                label_enabled=label_enabled,
                launchplane_route_path="/v1/previews/pr-feedback",
                feedback_status="unsupported",
                launchplane_feedback_required=True,
            )
        return PreviewWorkflowDecision(
            operation="ignore",
            reason="pull_request_target_is_only_for_unsupported_notices",
            execution_trust=execution_trust,
            label_enabled=label_enabled,
        )

    if execution_trust != "same_repo":
        return PreviewWorkflowDecision(
            operation="ignore",
            reason=f"pull_request_event_must_not_run_untrusted_{execution_trust}_preview",
            execution_trust=execution_trust,
            label_enabled=label_enabled,
        )

    if event.action == "closed":
        return PreviewWorkflowDecision(
            operation="destroy",
            reason="pull_request_closed",
            execution_trust=execution_trust,
            label_enabled=label_enabled,
            launchplane_route_path="/v1/drivers/generic-web/preview-destroy",
            feedback_status="destroyed",
            launchplane_feedback_required=True,
        )

    if event.action == "unlabeled" and event.action_label == event.preview_label:
        return PreviewWorkflowDecision(
            operation="destroy",
            reason="preview_label_removed",
            execution_trust=execution_trust,
            label_enabled=False,
            launchplane_route_path="/v1/drivers/generic-web/preview-destroy",
            feedback_status="destroyed",
            launchplane_feedback_required=True,
        )

    if not label_enabled:
        return PreviewWorkflowDecision(
            operation="ignore",
            reason="preview_label_not_enabled",
            execution_trust=execution_trust,
            label_enabled=False,
        )

    if event.action in {"opened", "reopened", "synchronize", "edited"}:
        return PreviewWorkflowDecision(
            operation="refresh",
            reason=f"pull_request_{event.action}",
            execution_trust=execution_trust,
            label_enabled=True,
            launchplane_route_path="/v1/drivers/generic-web/preview-refresh",
            feedback_status="pending",
            checkout_untrusted_head=True,
            product_build_required=True,
            launchplane_feedback_required=True,
        )

    if event.action == "labeled" and event.action_label == event.preview_label:
        return PreviewWorkflowDecision(
            operation="refresh",
            reason="preview_label_added",
            execution_trust=execution_trust,
            label_enabled=True,
            launchplane_route_path="/v1/drivers/generic-web/preview-refresh",
            feedback_status="pending",
            checkout_untrusted_head=True,
            product_build_required=True,
            launchplane_feedback_required=True,
        )

    return PreviewWorkflowDecision(
        operation="ignore",
        reason=f"pull_request_{event.action}_does_not_change_preview",
        execution_trust=execution_trust,
        label_enabled=label_enabled,
    )


def preview_workflow_idempotency_key(
    *,
    product: str,
    context: str,
    operation: PreviewWorkflowOperation,
    anchor_pr_number: int,
    run_id: str,
    run_attempt: str,
) -> str:
    if operation == "ignore":
        raise ValueError("ignored preview workflow operations do not need idempotency keys")
    normalized_product = _normalize_key_part(product, "product")
    normalized_context = _normalize_key_part(context, "context")
    normalized_run_id = _normalize_key_part(run_id, "run_id")
    normalized_run_attempt = _normalize_key_part(run_attempt, "run_attempt")
    return (
        f"preview-workflow:{normalized_product}:{normalized_context}:{operation}:"
        f"pr-{anchor_pr_number}:{normalized_run_id}:{normalized_run_attempt}"
    )


def _preview_label_enabled(event: PreviewWorkflowEvent) -> bool:
    return event.preview_label.strip() in {label_name.strip() for label_name in event.label_names}


def _execution_trust(event: PreviewWorkflowEvent) -> PreviewWorkflowExecutionTrust:
    if _is_dependabot_actor(event.actor):
        return "dependabot"
    if event.base_repository.casefold() != event.head_repository.casefold():
        return "fork"
    return "same_repo"


def _needs_unsupported_notice(*, event: PreviewWorkflowEvent, label_enabled: bool) -> bool:
    if not label_enabled:
        return False
    if event.action not in {"opened", "reopened", "synchronize", "edited", "labeled"}:
        return False
    return _execution_trust(event) in {"fork", "dependabot"}


def _is_dependabot_actor(actor: str) -> bool:
    return actor.strip().casefold() in {"dependabot[bot]", "dependabot-preview[bot]"}


def _normalize_key_part(value: str, label: str) -> str:
    normalized_value = value.strip()
    if not normalized_value:
        raise ValueError(f"preview workflow idempotency key requires {label}")
    return normalized_value.replace("/", "-")
