"""Normalize protection and combined-candidate checks for a landing read."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Literal
from urllib.parse import quote

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.ordinary_agent_snapshot import (
    OrdinaryAgentProtectionEvidence,
    OrdinaryAgentRequiredCheck,
)
from control_plane.ordinary_agent_github_transport import (
    DeadlineMergeTrainGitHubTransport,
    OrdinaryAgentProviderEvidenceError,
    require_complete_connection,
)
from control_plane.ordinary_agent_landing_graphql import (
    OrdinaryLandingGraphQLObservation,
    LANDING_ENTRY_READ_RESERVE_SECONDS,
)
from control_plane.tenant_admission_controller import (
    TenantAdmissionControllerError,
    TenantAdmissionTechnicalChecks,
    TenantAdmissionTechnicalCheckSignal,
    _check_run_state,
    _commit_status_state,
    _is_excluded_technical_context,
    _required_technical_checks,
    _required_technical_check_state,
)


def read_landing_checks(
    *,
    transport: DeadlineMergeTrainGitHubTransport,
    observation: OrdinaryLandingGraphQLObservation,
    repository: str,
    base_branch: str,
    base_sha: str,
    candidate_sha: str,
) -> tuple[TenantAdmissionTechnicalChecks, OrdinaryAgentProtectionEvidence]:
    """Use the existing evaluator's check semantics, never rollup.state alone.

    The caller must have obtained this observation with the verified landing
    custody profile, which includes administration, checks and status reads.
    This function neither acquires credentials nor proves custody itself.
    """
    owner, name = repository.split("/", 1)
    rules = transport.request(
        method="GET",
        path=f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}/rules/branches/"
        f"{quote(base_branch, safe='')}?per_page=100",
        minimum_remaining_seconds=LANDING_ENTRY_READ_RESERVE_SECONDS,
    )
    return evaluate_observed_commit_checks(
        observation=observation,
        rules=rules,
        base_branch=base_branch,
        base_sha=base_sha,
        candidate_sha=candidate_sha,
    )


def evaluate_observed_commit_checks(
    *,
    observation: OrdinaryLandingGraphQLObservation,
    rules: object,
    base_branch: str,
    base_sha: str,
    candidate_sha: str,
) -> tuple[TenantAdmissionTechnicalChecks, OrdinaryAgentProtectionEvidence]:
    """Evaluate captured provider policy and commit checks without another read.

    Initial snapshots can apply one base-policy observation to each source head;
    landing still evaluates its combined candidate independently.
    """
    try:
        data = _object(json.loads(observation.repository_json))
        if not isinstance(rules, list) or len(rules) >= 100:
            raise OrdinaryAgentProviderEvidenceError("landing_rules_incomplete")
        ref = _object(data.get("ref"))
        if ref.get("name") != base_branch:
            raise OrdinaryAgentProviderEvidenceError("landing_protection_missing")
        if "branchProtectionRule" not in ref:
            raise OrdinaryAgentProviderEvidenceError("landing_protection_missing")
        classic = ref["branchProtectionRule"]
        strict = False
        required: list[dict[str, object]] = []
        if classic is not None:
            protection = _object(classic)
            enabled = _boolean(protection.get("requiresStatusChecks"))
            strict = _boolean(protection.get("requiresStrictStatusChecks"))
            if enabled:
                checks = protection.get("requiredStatusChecks")
                if not isinstance(checks, list):
                    raise OrdinaryAgentProviderEvidenceError("landing_protection_missing")
                for raw in checks:
                    check = _object(raw)
                    if "app" not in check:
                        raise OrdinaryAgentProviderEvidenceError("landing_protection_missing")
                    app = check.get("app")
                    required.append(
                        {
                            "context": _text(check.get("context")),
                            "app_id": None
                            if app is None
                            else _positive_id(_object(app).get("databaseId")),
                        }
                    )
        for raw in rules:
            rule = _object(raw)
            rule_type = _text(rule.get("type"))
            if rule_type != "required_status_checks":
                continue
            parameters = _object(rule.get("parameters"))
            strict = _boolean(parameters.get("strict_required_status_checks_policy")) or strict
            checks = parameters.get("required_status_checks")
            if not isinstance(checks, list):
                raise OrdinaryAgentProviderEvidenceError("landing_protection_missing")
            for raw_check in checks:
                check = _object(raw_check)
                required.append(
                    {
                        "context": _text(check.get("context")),
                        "app_id": check.get("integration_id"),
                    }
                )
        strict, required_checks = _required_technical_checks(
            {
                "strict": strict,
                "checks": required,
                "contexts": [],
            }
        )
        comparison = _object(ref.get("compare"))
        if (
            _object(comparison.get("baseTarget")).get("oid") != base_sha
            or _object(comparison.get("headTarget")).get("oid") != candidate_sha
        ):
            raise OrdinaryAgentProviderEvidenceError("landing_comparison_mismatch")
        comparison_status = comparison.get("status")
        if comparison_status not in {"AHEAD", "IDENTICAL", "BEHIND", "DIVERGED"}:
            raise OrdinaryAgentProviderEvidenceError("landing_comparison_missing")
        base_up_to_date = comparison_status in {"AHEAD", "IDENTICAL"} if strict else None
        commit = _object(data.get("candidate"))
        if commit.get("oid") != candidate_sha:
            raise OrdinaryAgentProviderEvidenceError("landing_candidate_mismatch")
        if "statusCheckRollup" not in commit:
            raise OrdinaryAgentProviderEvidenceError("landing_checks_malformed")
        rollup = commit["statusCheckRollup"]
        contexts = (
            ()
            if rollup is None
            else require_complete_connection(
                _object(rollup).get("contexts"), label="landing_checks"
            )
        )
        signals: list[TenantAdmissionTechnicalCheckSignal] = []
        for context in contexts:
            source: Literal["check_run", "commit_status"]
            kind = context.get("__typename")
            if kind == "CheckRun":
                check_name = _text(context.get("name"))
                app = _object(context.get("checkSuite")).get("app")
                app_id = None if app is None else _positive_id(_object(app).get("databaseId"))
                state = _check_run_state(context)
                source = "check_run"
            elif kind == "StatusContext":
                check_name = _text(context.get("context"))
                app_id = None
                state = _commit_status_state(_text(context.get("state")))
                source = "commit_status"
            else:
                raise OrdinaryAgentProviderEvidenceError("landing_check_kind_unsupported")
            if not _is_excluded_technical_context(check_name):
                signals.append(
                    TenantAdmissionTechnicalCheckSignal(
                        source=source,
                        name=check_name,
                        app_id=app_id,
                        state=state,
                    )
                )
        technical = TenantAdmissionTechnicalChecks(
            head_sha=candidate_sha,
            base_sha=base_sha,
            strict=strict,
            base_up_to_date=base_up_to_date,
            required_checks=required_checks,
            signals=tuple(signals),
            status=_required_technical_check_state(
                required_checks=required_checks,
                signals=tuple(signals),
                strict=strict,
                base_up_to_date=base_up_to_date,
            ),
            evaluated_at=datetime.fromtimestamp(observation.observed_at, timezone.utc).isoformat(),
        )
        evidence = OrdinaryAgentProtectionEvidence(
            source="both" if classic is not None else "evaluated_rules",
            classic_sha256=canonical_json_sha256(classic) if classic is not None else None,
            evaluated_rules_sha256=canonical_json_sha256(rules),
            required_checks=tuple(
                OrdinaryAgentRequiredCheck(
                    context=check.name,
                    integration_id=check.app_id,
                )
                for check in required_checks
            ),
        )
        return technical, evidence
    except (TenantAdmissionControllerError, ValueError, TypeError, KeyError) as error:
        raise OrdinaryAgentProviderEvidenceError("landing_checks_malformed") from error


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise OrdinaryAgentProviderEvidenceError("landing_checks_malformed")
    return value


def _text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OrdinaryAgentProviderEvidenceError("landing_checks_malformed")
    return value


def _boolean(value: object) -> bool:
    if not isinstance(value, bool):
        raise OrdinaryAgentProviderEvidenceError("landing_checks_malformed")
    return value


def _positive_id(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise OrdinaryAgentProviderEvidenceError("landing_checks_malformed")
    return value
