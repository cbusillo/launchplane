from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.authz_grant_service import AuthzManagedPolicyReconcileEnvelope
from control_plane.service_auth import GitHubActionsPolicyRule, LaunchplaneAuthzPolicy


GENERIC_WEB_PREVIEW_MANAGED_SET_ID = "operator.generic-web-preview"
GENERIC_WEB_PREVIEW_CALLER_WORKFLOW_PATH = ".github/workflows/launchplane-preview.yml"
GENERIC_WEB_PREVIEW_NOTICE_WORKFLOW_PATH = ".github/workflows/launchplane-preview-notice.yml"
_PRODUCT_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_COMMIT_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_DEFAULT_BRANCH_PATTERN = re.compile(r"^[A-Za-z0-9._/-]+$")
GenericWebPreviewAuthzOperation = Literal["onboard", "expand", "contract", "retire"]


class GenericWebPreviewAuthzPlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    product: Literal["launchplane"] = "launchplane"
    operation: GenericWebPreviewAuthzOperation = "onboard"
    target_product: str
    repository: str
    repository_id: str
    repository_owner_id: str
    default_branch: str = "main"
    preview_context: str = ""
    launchplane_sha: str
    reason: str
    related_issue: str

    @model_validator(mode="after")
    def _validate_request(self) -> "GenericWebPreviewAuthzPlanRequest":
        self.target_product = self.target_product.strip().lower()
        self.repository = self.repository.strip()
        self.repository_id = self.repository_id.strip()
        self.repository_owner_id = self.repository_owner_id.strip()
        self.default_branch = self.default_branch.strip() or "main"
        self.preview_context = self.preview_context.strip() or f"{self.target_product}-preview"
        self.launchplane_sha = self.launchplane_sha.strip().lower()
        self.reason = self.reason.strip()
        self.related_issue = self.related_issue.strip()
        if _PRODUCT_PATTERN.fullmatch(self.target_product) is None:
            raise ValueError(
                "generic-web preview authz target_product must use lowercase letters, "
                "numbers, and hyphens"
            )
        if _REPOSITORY_PATTERN.fullmatch(self.repository) is None:
            raise ValueError("generic-web preview authz repository must use owner/name form")
        for label, value in (
            ("repository_id", self.repository_id),
            ("repository_owner_id", self.repository_owner_id),
        ):
            if not value.isdecimal():
                raise ValueError(f"generic-web preview authz {label} must be a numeric GitHub ID")
        if _DEFAULT_BRANCH_PATTERN.fullmatch(self.default_branch) is None:
            raise ValueError("generic-web preview authz default_branch is invalid")
        if not self.preview_context:
            raise ValueError("generic-web preview authz requires preview_context")
        if _COMMIT_SHA_PATTERN.fullmatch(self.launchplane_sha) is None:
            raise ValueError("generic-web preview authz requires a full Launchplane commit SHA")
        if not self.reason:
            raise ValueError("generic-web preview authz planning requires reason")
        if not self.related_issue:
            raise ValueError("generic-web preview authz planning requires related_issue")
        return self


class GenericWebPreviewAuthzPlanResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: GenericWebPreviewAuthzOperation
    target_product: str
    target_rule_count: int = Field(ge=0)
    desired_rule_count: int = Field(ge=0)
    plan_sha256: str
    configuration: dict[str, object]
    diff: dict[str, object]


def build_generic_web_preview_authz_reconcile_request(
    *,
    current_policy: LaunchplaneAuthzPolicy,
    request: GenericWebPreviewAuthzPlanRequest,
) -> AuthzManagedPolicyReconcileEnvelope:
    retained_github_rules = tuple(
        rule
        for rule in current_policy.github_actions
        if rule.managed_set_id == GENERIC_WEB_PREVIEW_MANAGED_SET_ID
    )
    retained_human_rules = tuple(
        rule
        for rule in current_policy.github_humans
        if rule.managed_set_id == GENERIC_WEB_PREVIEW_MANAGED_SET_ID
    )
    retained_terminal_rules = tuple(
        rule
        for rule in current_policy.terminal_agents
        if rule.managed_set_id == GENERIC_WEB_PREVIEW_MANAGED_SET_ID
    )
    retained_operator_rules = tuple(
        rule
        for rule in current_policy.local_operators
        if rule.managed_set_id == GENERIC_WEB_PREVIEW_MANAGED_SET_ID
    )
    retained_admin_rules = tuple(
        rule
        for rule in current_policy.local_admins
        if rule.managed_set_id == GENERIC_WEB_PREVIEW_MANAGED_SET_ID
    )
    current_target_rules = tuple(
        rule for rule in retained_github_rules if request.target_product in rule.products
    )
    if request.operation == "onboard" and current_target_rules:
        generated_rules = generic_web_preview_rules(request)
        if _rules_by_managed_id(current_target_rules) != _rules_by_managed_id(generated_rules):
            raise ValueError(
                "generic-web preview authz onboarding found different current managed preview "
                "rules; use expand and contract for a reviewed rotation"
            )
    if request.operation in {"expand", "contract", "retire"} and not current_target_rules:
        raise ValueError(
            f"generic-web preview authz {request.operation} requires current product rules"
        )
    desired_github_rules = tuple(
        rule
        for rule in retained_github_rules
        if request.operation not in {"contract", "retire"}
        or request.target_product not in rule.products
    )
    if request.operation != "retire":
        generated_rules = generic_web_preview_rules(request)
        existing_ids = {
            rule.managed_rule_id for rule in desired_github_rules if rule.managed_rule_id
        }
        desired_github_rules = (
            *desired_github_rules,
            *(rule for rule in generated_rules if rule.managed_rule_id not in existing_ids),
        )
    desired_policy = LaunchplaneAuthzPolicy(
        schema_version=2,
        github_actions=desired_github_rules,
        github_humans=retained_human_rules,
        terminal_agents=retained_terminal_rules,
        local_operators=retained_operator_rules,
        local_admins=retained_admin_rules,
    )
    return AuthzManagedPolicyReconcileEnvelope(
        product="launchplane",
        mode="dry_run",
        managed_set_id=GENERIC_WEB_PREVIEW_MANAGED_SET_ID,
        reason=request.reason,
        related_issue=request.related_issue,
        desired_policy=desired_policy,
    )


def generic_web_preview_rules(
    request: GenericWebPreviewAuthzPlanRequest,
) -> tuple[GitHubActionsPolicyRule, ...]:
    generation = request.launchplane_sha[:7]
    preview_workflow_ref = (
        f"{request.repository}/{GENERIC_WEB_PREVIEW_CALLER_WORKFLOW_PATH}@refs/pull/*/merge"
    )
    notice_workflow_ref = (
        f"{request.repository}/{GENERIC_WEB_PREVIEW_NOTICE_WORKFLOW_PATH}@"
        f"refs/heads/{request.default_branch}"
    )
    launchplane_prefix = "cbusillo/launchplane/.github/workflows"
    lifecycle_ref = (
        f"{launchplane_prefix}/reusable-generic-web-preview-lifecycle.yml@{request.launchplane_sha}"
    )
    verification_ref = (
        f"{launchplane_prefix}/reusable-generic-web-preview-verification.yml@"
        f"{request.launchplane_sha}"
    )
    feedback_ref = (
        f"{launchplane_prefix}/reusable-preview-pr-feedback.yml@{request.launchplane_sha}"
    )

    def rule(
        *,
        slot: str,
        action: str,
        workflow_ref: str,
        job_workflow_refs: tuple[str, ...],
        event_name: Literal["pull_request", "pull_request_target"],
    ) -> GitHubActionsPolicyRule:
        return GitHubActionsPolicyRule(
            managed_set_id=GENERIC_WEB_PREVIEW_MANAGED_SET_ID,
            managed_rule_id=(f"generic-web-preview.{request.target_product}.{generation}.{slot}"),
            repository=request.repository,
            repository_id=request.repository_id,
            repository_owner_id=request.repository_owner_id,
            workflow_refs=(workflow_ref,),
            job_workflow_refs=job_workflow_refs,
            event_names=(event_name,),
            products=(request.target_product,),
            contexts=(request.preview_context,),
            actions=(action,),
        )

    return (
        rule(
            slot="refresh",
            action="preview_refresh.execute",
            workflow_ref=preview_workflow_ref,
            job_workflow_refs=(lifecycle_ref,),
            event_name="pull_request",
        ),
        rule(
            slot="refresh-diagnostic",
            action="authz_diagnostic.evaluate",
            workflow_ref=preview_workflow_ref,
            job_workflow_refs=(lifecycle_ref,),
            event_name="pull_request",
        ),
        rule(
            slot="verification",
            action="preview_generation.write",
            workflow_ref=preview_workflow_ref,
            job_workflow_refs=(verification_ref,),
            event_name="pull_request",
        ),
        rule(
            slot="feedback",
            action="preview_pr_feedback.write",
            workflow_ref=preview_workflow_ref,
            job_workflow_refs=(feedback_ref, lifecycle_ref),
            event_name="pull_request",
        ),
        rule(
            slot="destroy",
            action="preview_destroy.execute",
            workflow_ref=notice_workflow_ref,
            job_workflow_refs=(lifecycle_ref,),
            event_name="pull_request_target",
        ),
        rule(
            slot="notice-feedback",
            action="preview_pr_feedback.write",
            workflow_ref=notice_workflow_ref,
            job_workflow_refs=(feedback_ref, lifecycle_ref),
            event_name="pull_request_target",
        ),
    )


def _rules_by_managed_id(
    rules: tuple[GitHubActionsPolicyRule, ...],
) -> dict[str, dict[str, object]]:
    normalized: dict[str, dict[str, object]] = {}
    for rule in rules:
        managed_rule_id = rule.managed_rule_id or ""
        if not managed_rule_id or managed_rule_id in normalized:
            return {}
        normalized[managed_rule_id] = rule.model_dump(mode="json")
    return normalized
