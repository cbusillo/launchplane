from __future__ import annotations

from fnmatch import fnmatchcase
import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.authz_scope import (
    exact_instance_workflow_authz_actions,
    exclusively_instance_scoped_authz_actions,
)
from control_plane.service_auth import (
    AuthorizationTarget,
    GitHubActionsIdentity,
    GitHubActionsPolicyRule,
    LaunchplaneAuthzPolicy,
)


AuthzDiagnosticFailureCategory = Literal[
    "repository",
    "repository_id",
    "repository_owner_id",
    "workflow_ref",
    "job_workflow_ref",
    "event_name",
    "ref",
    "environment",
    "product",
    "context",
    "target",
    "action",
    "policy_schema",
]


class AuthzDiagnosticEvaluateEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    action: str
    product: str
    context: str
    target: AuthorizationTarget = Field(
        default_factory=lambda: AuthorizationTarget(scope="context")
    )

    @model_validator(mode="after")
    def _validate_request(self) -> "AuthzDiagnosticEvaluateEnvelope":
        self.action = self.action.strip()
        self.product = self.product.strip()
        self.context = self.context.strip()
        if not self.action or not self.product or not self.context:
            raise ValueError("Authz diagnostics require action, product, and context.")
        return self


class AuthzDiagnosticRuleEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["allowed", "denied"]
    failure_categories: tuple[AuthzDiagnosticFailureCategory, ...]
    rule_sha256: str


class AuthzDiagnosticEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["allowed", "denied"]
    failure_categories: tuple[AuthzDiagnosticFailureCategory, ...]
    identity_sha256: str
    rule_evaluations: tuple[AuthzDiagnosticRuleEvaluation, ...]
    rule_evaluations_truncated: bool


class AuthzDiagnosticEvaluateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    policy_record_id: str
    policy_revision: int
    policy_sha256: str
    evaluation: AuthzDiagnosticEvaluation


def evaluate_github_actions_authz(
    *,
    policy: LaunchplaneAuthzPolicy,
    identity: GitHubActionsIdentity,
    request: AuthzDiagnosticEvaluateEnvelope,
) -> AuthzDiagnosticEvaluation:
    repository_rules = tuple(
        rule for rule in policy.github_actions if rule.repository == identity.repository
    )
    if not repository_rules:
        return AuthzDiagnosticEvaluation(
            decision="denied",
            failure_categories=("repository",),
            identity_sha256=_identity_sha256(identity),
            rule_evaluations=(),
            rule_evaluations_truncated=False,
        )
    evaluations = tuple(
        _evaluate_rule(policy=policy, rule=rule, identity=identity, request=request)
        for rule in repository_rules
    )
    ordered_evaluations = tuple(
        sorted(
            evaluations,
            key=lambda evaluation: (len(evaluation.failure_categories), evaluation.rule_sha256),
        )
    )
    retained_evaluations = ordered_evaluations[:3]
    allowed = any(evaluation.decision == "allowed" for evaluation in evaluations)
    failure_categories = tuple(
        sorted(
            {
                category
                for evaluation in retained_evaluations
                for category in evaluation.failure_categories
            }
        )
    )
    return AuthzDiagnosticEvaluation(
        decision="allowed" if allowed else "denied",
        failure_categories=() if allowed else failure_categories,
        identity_sha256=_identity_sha256(identity),
        rule_evaluations=retained_evaluations,
        rule_evaluations_truncated=len(ordered_evaluations) > len(retained_evaluations),
    )


def _evaluate_rule(
    *,
    policy: LaunchplaneAuthzPolicy,
    rule: GitHubActionsPolicyRule,
    identity: GitHubActionsIdentity,
    request: AuthzDiagnosticEvaluateEnvelope,
) -> AuthzDiagnosticRuleEvaluation:
    failures: list[AuthzDiagnosticFailureCategory] = []
    if rule.repository_id and rule.repository_id != identity.repository_id:
        failures.append("repository_id")
    if rule.repository_owner_id and rule.repository_owner_id != identity.repository_owner_id:
        failures.append("repository_owner_id")
    if rule.workflow_refs and not _matches(identity.workflow_ref, rule.workflow_refs):
        failures.append("workflow_ref")
    if rule.job_workflow_refs and not _matches(identity.job_workflow_ref, rule.job_workflow_refs):
        failures.append("job_workflow_ref")
    if rule.event_names and identity.event_name not in rule.event_names:
        failures.append("event_name")
    if rule.refs and identity.ref not in rule.refs:
        failures.append("ref")
    if rule.environments and identity.environment not in rule.environments:
        failures.append("environment")
    if rule.products and not _matches(request.product, rule.products):
        failures.append("product")
    if rule.contexts and not _matches(request.context, rule.contexts):
        failures.append("context")
    if not _target_allowed(policy=policy, rule=rule, request=request):
        failures.append("target")
    if rule.actions and request.action not in rule.actions:
        failures.append("action")
    if policy.schema_version != 2 and request.action in exact_instance_workflow_authz_actions():
        failures.append("policy_schema")
    return AuthzDiagnosticRuleEvaluation(
        decision="allowed" if not failures else "denied",
        failure_categories=tuple(failures),
        rule_sha256=_rule_sha256(rule),
    )


def _identity_sha256(identity: GitHubActionsIdentity) -> str:
    payload = {
        "environment": identity.environment,
        "event_name": identity.event_name,
        "job_workflow_ref": identity.job_workflow_ref,
        "ref": identity.ref,
        "repository": identity.repository,
        "repository_id": identity.repository_id,
        "repository_owner_id": identity.repository_owner_id,
        "workflow_ref": identity.workflow_ref,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _matches(value: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatchcase(value.strip(), pattern) for pattern in patterns)


def _rule_sha256(rule: GitHubActionsPolicyRule) -> str:
    return hashlib.sha256(
        json.dumps(
            rule.model_dump(mode="json", exclude_none=True),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _target_allowed(
    *,
    policy: LaunchplaneAuthzPolicy,
    rule: GitHubActionsPolicyRule,
    request: AuthzDiagnosticEvaluateEnvelope,
) -> bool:
    target = request.target
    if (
        policy.schema_version == 2
        and request.action in exclusively_instance_scoped_authz_actions()
        and target.scope != "instance"
    ):
        return False
    if target.scope != "instance":
        return not rule.instances
    if not rule.instances:
        return policy.schema_version == 1
    if rule.instances == ("*",):
        return True
    return set(target.instances).issubset(rule.instances)
