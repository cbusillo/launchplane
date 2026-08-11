from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.authz_grant_service import AuthzManagedPolicyReconcileEnvelope
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.service_auth import GitHubActionsPolicyRule, LaunchplaneAuthzPolicy


GENERIC_WEB_PREVIEW_MANAGED_SET_ID = "operator.generic-web-preview"
GENERIC_WEB_PREVIEW_CALLER_WORKFLOW_PATH = ".github/workflows/launchplane-preview.yml"
GENERIC_WEB_PREVIEW_NOTICE_WORKFLOW_PATH = ".github/workflows/launchplane-preview-notice.yml"
_LAUNCHPLANE_REPOSITORY = "cbusillo/launchplane"
_INGRESS_PLAN_WORKFLOW_REF = (
    f"{_LAUNCHPLANE_REPOSITORY}/.github/workflows/ingress-route-dry-run.yml@refs/heads/main"
)
_INGRESS_APPLY_WORKFLOW_REF = (
    f"{_LAUNCHPLANE_REPOSITORY}/.github/workflows/ingress-route-apply.yml@refs/heads/main"
)
_PRODUCT_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_COMMIT_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_DEFAULT_BRANCH_PATTERN = re.compile(r"^[A-Za-z0-9._/-]+$")
GenericWebPreviewAuthzOperation = Literal["onboard", "expand", "contract", "retire"]
GenericWebPreviewRetirementAuthoritySource = Literal["current_product_rules", "product_profile"]


class GenericWebPreviewAuthzPlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    product: Literal["launchplane"] = "launchplane"
    operation: GenericWebPreviewAuthzOperation = "onboard"
    target_product: str
    repository: str = ""
    repository_id: str = ""
    repository_owner_id: str = ""
    default_branch: str = "main"
    preview_context: str = ""
    launchplane_sha: str
    include_ingress_operator: bool = False
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
        if self.repository and _REPOSITORY_PATTERN.fullmatch(self.repository) is None:
            raise ValueError("generic-web preview authz repository must use owner/name form")
        if self.operation != "retire" and not self.repository:
            raise ValueError("generic-web preview authz requires repository")
        if bool(self.repository_id) != bool(self.repository_owner_id):
            raise ValueError(
                "generic-web preview authz repository assertions require both repository_id "
                "and repository_owner_id when either is supplied"
            )
        for label, value in (
            ("repository_id", self.repository_id),
            ("repository_owner_id", self.repository_owner_id),
        ):
            if value and not value.isdecimal():
                raise ValueError(f"generic-web preview authz {label} must be a numeric GitHub ID")
        if self.operation != "retire" and not self.repository_id:
            raise ValueError(
                "generic-web preview authz requires immutable repository identity for non-retire"
            )
        if self.operation == "retire" and self.include_ingress_operator:
            raise ValueError(
                "generic-web preview authz retire rejects include_ingress_operator=true"
            )
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
    retirement_authority: GenericWebPreviewRetirementAuthorityEvidence | None = None


class GenericWebPreviewRetirementAuthorityEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    authority_sources: tuple[GenericWebPreviewRetirementAuthoritySource, ...]
    managed_rule_ids: tuple[str, ...]
    managed_rule_count: int = Field(ge=1)
    repository_identity_sha256: str


@dataclass(frozen=True)
class GenericWebPreviewRetirementAuthority:
    repository: str
    repository_id: str
    repository_owner_id: str
    evidence: GenericWebPreviewRetirementAuthorityEvidence


def build_generic_web_preview_authz_reconcile_request(
    *,
    current_policy: LaunchplaneAuthzPolicy,
    request: GenericWebPreviewAuthzPlanRequest,
    profile: LaunchplaneProductProfileRecord | None = None,
    retirement_authority: GenericWebPreviewRetirementAuthority | None = None,
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
        generated_rules = _desired_generic_web_preview_rules(
            current_policy=current_policy,
            request=request,
        )
        if _rules_by_managed_id(current_target_rules) != _rules_by_managed_id(generated_rules):
            raise ValueError(
                "generic-web preview authz onboarding found different current managed preview "
                "rules; use expand and contract for a reviewed rotation"
            )
    if request.operation in {"expand", "contract", "retire"} and not current_target_rules:
        raise ValueError(
            f"generic-web preview authz {request.operation} requires current product rules"
        )
    if request.operation == "retire":
        resolved_retirement_authority = (
            retirement_authority
            or resolve_generic_web_preview_retirement_authority(
                current_target_rules=current_target_rules,
                request=request,
                profile=profile,
            )
        )
        _validate_retirement_assertions(
            request=request,
            authority=resolved_retirement_authority,
        )
    desired_github_rules = tuple(
        rule
        for rule in retained_github_rules
        if request.operation not in {"contract", "retire"}
        or request.target_product not in rule.products
    )
    if request.operation != "retire":
        generated_rules = _desired_generic_web_preview_rules(
            current_policy=current_policy,
            request=request,
        )
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


def resolve_generic_web_preview_retirement_authority(
    *,
    current_target_rules: tuple[GitHubActionsPolicyRule, ...],
    request: GenericWebPreviewAuthzPlanRequest,
    profile: LaunchplaneProductProfileRecord | None,
) -> GenericWebPreviewRetirementAuthority:
    if not current_target_rules:
        raise ValueError("generic-web preview authz retire requires current product rules")
    product_repository_rules = tuple(
        rule for rule in current_target_rules if not _is_ingress_operator_rule(rule)
    )
    if not product_repository_rules:
        raise ValueError(
            "generic-web preview authz retire requires product repository rules after "
            "excluding ingress-operator rules"
        )
    complete_identities = {
        (rule.repository, rule.repository_id, rule.repository_owner_id)
        for rule in product_repository_rules
        if rule.repository_id and rule.repository_owner_id
    }
    rule_repositories = {rule.repository for rule in product_repository_rules}
    if len(rule_repositories) != 1 or len(complete_identities) > 1:
        raise ValueError("generic-web preview authz retire found ambiguous repository identities")
    repository = next(iter(rule_repositories))
    has_name_only_rules = any(
        not rule.repository_id and not rule.repository_owner_id for rule in product_repository_rules
    )
    if complete_identities:
        _, repository_id, repository_owner_id = next(iter(complete_identities))
    else:
        repository_id = ""
        repository_owner_id = ""
    authority_sources: list[GenericWebPreviewRetirementAuthoritySource] = ["current_product_rules"]
    if profile is not None:
        if profile.repository != repository:
            raise ValueError(
                "generic-web preview authz retire found product profile and rule repository disagreement"
            )
        authority_sources.append("product_profile")
        if profile.repository_id and profile.repository_owner_id:
            profile_identity = (
                profile.repository,
                profile.repository_id,
                profile.repository_owner_id,
            )
            if complete_identities and profile_identity not in complete_identities:
                raise ValueError(
                    "generic-web preview authz retire found product profile and rule identity disagreement"
                )
            repository_id = profile.repository_id
            repository_owner_id = profile.repository_owner_id
    if has_name_only_rules and not (
        profile and profile.repository_id and profile.repository_owner_id
    ):
        raise ValueError(
            "generic-web preview authz retire requires a matching product profile with complete "
            "repository identity for legacy name-only rules"
        )
    if not repository_id or not repository_owner_id:
        raise ValueError("generic-web preview authz retire found incomplete repository identity")
    managed_rule_ids = tuple(
        sorted(rule.managed_rule_id for rule in current_target_rules if rule.managed_rule_id)
    )
    identity_digest = hashlib.sha256(
        "\x00".join((repository, repository_id, repository_owner_id)).encode("utf-8")
    ).hexdigest()
    return GenericWebPreviewRetirementAuthority(
        repository=repository,
        repository_id=repository_id,
        repository_owner_id=repository_owner_id,
        evidence=GenericWebPreviewRetirementAuthorityEvidence(
            authority_sources=tuple(authority_sources),
            managed_rule_ids=managed_rule_ids,
            managed_rule_count=len(current_target_rules),
            repository_identity_sha256=identity_digest,
        ),
    )


def _validate_retirement_assertions(
    *,
    request: GenericWebPreviewAuthzPlanRequest,
    authority: GenericWebPreviewRetirementAuthority,
) -> None:
    if request.repository and request.repository != authority.repository:
        raise ValueError(
            "generic-web preview authz retire repository assertion does not match authority"
        )
    if request.repository_id and (
        request.repository_id != authority.repository_id
        or request.repository_owner_id != authority.repository_owner_id
    ):
        raise ValueError(
            "generic-web preview authz retire repository identity assertion does not match authority"
        )


def _is_ingress_operator_rule(rule: GitHubActionsPolicyRule) -> bool:
    ingress_workflow_refs = {
        "ingress_route.plan": _INGRESS_PLAN_WORKFLOW_REF,
        "ingress_route.apply": _INGRESS_APPLY_WORKFLOW_REF,
    }
    action = rule.actions[0] if len(rule.actions) == 1 else ""
    return (
        rule.repository == _LAUNCHPLANE_REPOSITORY
        and action in ingress_workflow_refs
        and rule.workflow_refs == (ingress_workflow_refs[action],)
    )


def _desired_generic_web_preview_rules(
    *,
    current_policy: LaunchplaneAuthzPolicy,
    request: GenericWebPreviewAuthzPlanRequest,
) -> tuple[GitHubActionsPolicyRule, ...]:
    preview_rules = generic_web_preview_rules(request)
    if not request.include_ingress_operator:
        return preview_rules
    return (
        *preview_rules,
        *generic_web_preview_ingress_operator_rules(
            current_policy=current_policy,
            request=request,
        ),
    )


def generic_web_preview_ingress_operator_rules(
    *,
    current_policy: LaunchplaneAuthzPolicy,
    request: GenericWebPreviewAuthzPlanRequest,
) -> tuple[GitHubActionsPolicyRule, ...]:
    generation = request.launchplane_sha[:7]
    plan_template = _ingress_operator_rule_template(
        current_policy=current_policy,
        action="ingress_route.plan",
        workflow_ref=_INGRESS_PLAN_WORKFLOW_REF,
    )
    apply_template = _ingress_operator_rule_template(
        current_policy=current_policy,
        action="ingress_route.apply",
        workflow_ref=_INGRESS_APPLY_WORKFLOW_REF,
    )

    def rule(
        *, slot: str, action: str, template: GitHubActionsPolicyRule
    ) -> GitHubActionsPolicyRule:
        return template.model_copy(
            update={
                "managed_set_id": GENERIC_WEB_PREVIEW_MANAGED_SET_ID,
                "managed_rule_id": (
                    f"generic-web-preview.{request.target_product}.{generation}.{slot}"
                ),
                "products": (request.target_product,),
                "contexts": (request.preview_context,),
                "instances": (),
                "actions": (action,),
            }
        )

    return (
        rule(slot="ingress-plan", action="ingress_route.plan", template=plan_template),
        rule(slot="ingress-apply", action="ingress_route.apply", template=apply_template),
    )


def _ingress_operator_rule_template(
    *,
    current_policy: LaunchplaneAuthzPolicy,
    action: str,
    workflow_ref: str,
) -> GitHubActionsPolicyRule:
    candidates = tuple(
        rule
        for rule in current_policy.github_actions
        if rule.repository == _LAUNCHPLANE_REPOSITORY
        and rule.workflow_refs == (workflow_ref,)
        and rule.actions == (action,)
        and rule.job_workflow_refs
    )
    templates = {
        (
            rule.repository,
            rule.repository_id,
            rule.repository_owner_id,
            rule.workflow_refs,
            rule.job_workflow_refs,
            rule.event_names,
            rule.refs,
            rule.environments,
        ): rule
        for rule in candidates
    }
    if len(templates) != 1:
        raise ValueError(
            "generic-web preview ingress authorization requires one unambiguous pinned "
            f"{action} workflow template in the active policy"
        )
    return next(iter(templates.values()))


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
            managed_rule_id=f"generic-web-preview.{request.target_product}.{generation}.{slot}",
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
