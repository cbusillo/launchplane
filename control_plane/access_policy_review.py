"""Render the existing policy plan for a signed-in administrator, without values."""

from collections.abc import Callable
from functools import lru_cache
from http.client import HTTPException
import json
import re
from time import monotonic
from typing import Literal, cast
from urllib.error import URLError
from urllib.request import Request, urlopen

from control_plane.authz_grant_service import (
    AuthzPolicyRecordStore,
    AuthzCollectionType,
    AuthzManagedRuleEntry,
    _authz_policy_rule_collections,
    plan_managed_authz_policy_reconcile,
)
from control_plane.contracts.access_policy_review import (
    AccessPolicyReview,
    AccessPolicyRoleEffect,
    AccessPolicyRuleReview,
)
from control_plane.contracts.privileged_operation import (
    ManagedAuthzPolicySetHumanEvidence,
    ManagedAuthzPolicySetProposalInput,
)
from control_plane.contracts.ordinary_agent import OrdinaryAgentPolicyRule
from control_plane.service_auth import GitHubHumanPolicyRule, LaunchplaneAuthzPolicy


def github_account_login(github_id: int) -> str:
    return _public_github_account_login(github_id, int(monotonic() // 300))


@lru_cache(maxsize=256)
def _public_github_account_login(github_id: int, cache_window: int) -> str:
    """Public display metadata only. No credential, membership or policy changes."""
    request = Request(
        f"https://api.github.com/user/{github_id}",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "Launchplane"},
    )
    try:
        with urlopen(request, timeout=3) as response:
            payload = json.loads(response.read(65536))
        login = payload.get("login")
        if (
            payload.get("id") == github_id
            and isinstance(login, str)
            and re.fullmatch(r"[A-Za-z0-9-]{1,39}", login)
        ):
            return login
    except (URLError, OSError, ValueError, AttributeError, HTTPException):
        pass
    return ""


def bounded_account_logins(reader: Callable[[int], str]) -> Callable[[int], str]:
    """Share one small metadata budget across the entire HTTP response."""
    deadline = monotonic() + 1
    logins: dict[int, str] = {}

    def read(github_id: int) -> str:
        if github_id not in logins:
            if len(logins) >= 10 or monotonic() >= deadline:
                return ""
            logins[github_id] = reader(github_id)
        return logins[github_id]

    return read


def _principal(entry: AuthzManagedRuleEntry, login_for_id: Callable[[int], str]) -> str:
    rule = entry.rule
    if isinstance(rule, GitHubHumanPolicyRule):
        selectors = []
        if rule.github_ids:
            selectors.append(
                ", ".join(
                    f"GitHub login {login} (account ID {github_id})"
                    if (login := login_for_id(github_id))
                    else f"GitHub account ID {github_id} (login unavailable)"
                    for github_id in rule.github_ids
                )
            )
        if rule.logins:
            selectors.append("GitHub login " + ", ".join(rule.logins))
        if rule.organizations:
            selectors.append("member of organization " + ", ".join(rule.organizations))
        if rule.teams:
            selectors.append("member of team " + ", ".join(rule.teams))
        return "; and ".join(selectors) or "Any signed-in GitHub user"
    labels = {
        "local_operators": "Local operator",
        "local_admins": "Local administrator",
        "terminal_agents": "Terminal agent",
        "ordinary_agents": "Agent principal",
        "github_actions": "Workflow",
    }
    if entry.principal_type == "github_actions":
        return "GitHub workflow in " + getattr(rule, "repository", "")
    if entry.principal_type == "ordinary_agents":
        return "Agent principal " + getattr(rule, "principal_id", "")
    return (
        labels[entry.principal_type]
        + " "
        + (", ".join(getattr(rule, "subjects", ())) or "(any subject)")
    )


def _scope(entry: AuthzManagedRuleEntry, schema_version: int) -> str:
    rule = entry.rule
    if isinstance(rule, OrdinaryAgentPolicyRule):
        return f"Repository {rule.target.repository} (ID {rule.target.repository_id}), base branch {rule.target.base_branch}."
    instances = ", ".join(rule.instances) or (
        "all" if schema_version == 1 else "context/global only"
    )
    summary = f"Products: {', '.join(rule.products) or 'all'}. Contexts: {', '.join(rule.contexts) or 'all'}. Targets: {instances}."
    if entry.principal_type == "github_actions":
        for label, field in (
            ("Workflows", "workflow_refs"),
            ("Reusable workflows", "job_workflow_refs"),
            ("Events", "event_names"),
            ("Git refs", "refs"),
            ("Environments", "environments"),
        ):
            summary += f" {label}: {', '.join(getattr(rule, field)) or 'any'}."
    return summary


def _role_effects(
    rule: GitHubHumanPolicyRule,
    before: LaunchplaneAuthzPolicy,
    after: LaunchplaneAuthzPolicy,
    login_for_id: Callable[[int], str],
) -> tuple[AccessPolicyRoleEffect, ...]:
    # Membership is not an inventory of people. Never turn an empty synthetic
    # membership into a claim that a person has no other route to sign in.
    if rule.teams or rule.organizations or (rule.github_ids and len(rule.logins) > 1):
        return (
            AccessPolicyRoleEffect(
                principal=_principal(AuthzManagedRuleEntry("github_humans", rule), login_for_id),
                state="unknown",
            ),
        )
    principals = (
        tuple(
            (github_id, login_for_id(github_id) or (rule.logins[0] if rule.logins else ""))
            for github_id in rule.github_ids
        )
        if rule.github_ids
        else tuple((0, login) for login in rule.logins)
    )
    effects = []
    for github_id, login in principals:
        principal = f"GitHub login {login}" if login else f"GitHub account ID {github_id}"
        # Unobserved ID/login and group membership may satisfy a surviving rule.
        # Report uncertainty instead of asserting an entire role is lost.
        uncertain = any(
            (other.github_ids and not github_id)
            or (other.logins and not login)
            or other.teams
            or other.organizations
            for other in after.github_humans
            if other.model_copy(
                update={
                    "github_ids": other.github_ids if github_id else (),
                    "logins": other.logins if login else (),
                    "teams": (),
                    "organizations": (),
                }
            ).matches_principal(
                github_id=github_id,
                login=login,
                organizations=frozenset(),
                teams=frozenset(),
                role="admin",
            )
            or other.model_copy(
                update={
                    "github_ids": other.github_ids if github_id else (),
                    "logins": other.logins if login else (),
                    "teams": (),
                    "organizations": (),
                }
            ).matches_principal(
                github_id=github_id,
                login=login,
                organizations=frozenset(),
                teams=frozenset(),
                role="read_only",
            )
        )
        previous_role = before.human_role_for(
            github_id=github_id, login=login, organizations=frozenset(), teams=frozenset()
        )
        candidate_role = after.human_role_for(
            github_id=github_id, login=login, organizations=frozenset(), teams=frozenset()
        )
        state: Literal["lost", "reduced", "retained", "gained", "unknown", "unaffected"] = (
            "unknown"
            if uncertain and previous_role != candidate_role
            else "unaffected"
            if previous_role is None and candidate_role is None
            else "retained"
            if previous_role == candidate_role
            else "lost"
            if candidate_role is None
            else "gained"
            if previous_role is None or candidate_role == "admin"
            else "reduced"
        )
        effects.append(
            AccessPolicyRoleEffect(
                principal=principal, before=previous_role, after=candidate_role, state=state
            )
        )
    return tuple(effects) or (
        AccessPolicyRoleEffect(principal="Any signed-in GitHub user", state="unknown"),
    )


def build_access_policy_review(
    *,
    record_store: object,
    request: ManagedAuthzPolicySetProposalInput,
    evidence: ManagedAuthzPolicySetHumanEvidence,
    login_for_id: Callable[[int], str] = lambda _: "",
) -> AccessPolicyReview:
    # The planner owns reconciliation, diffing and candidate construction.
    # A stale plan remains readable but cannot be described as current evidence.
    try:
        _, active, candidate, diff = plan_managed_authz_policy_reconcile(
            record_store=cast(AuthzPolicyRecordStore, record_store),
            request=request.reconcile_request(),
        )
    except (LookupError, TypeError, ValueError):
        return AccessPolicyReview(state="unavailable")
    if diff.plan_sha256 != evidence.diff.plan_sha256:
        return AccessPolicyReview(state="unavailable")
    rules: list[AccessPolicyRuleReview] = []
    for change in diff.changes:
        sides: tuple[
            tuple[Literal["removed", "added"], LaunchplaneAuthzPolicy, AuthzCollectionType | None],
            ...,
        ] = (
            ("removed", active.policy, change.previous_principal_type),
            ("added", candidate, change.desired_principal_type),
        )
        for kind, policy, principal_type in sides:
            if principal_type is None:
                continue
            entries = dict(_authz_policy_rule_collections(policy))[principal_type]
            for rule in entries:
                if (
                    rule.managed_set_id != request.managed_set_id
                    or rule.managed_rule_id != change.managed_rule_id
                ):
                    continue
                rules.append(
                    AccessPolicyRuleReview(
                        managed_rule_id=change.managed_rule_id,
                        change=kind,
                        principal=_principal(
                            AuthzManagedRuleEntry(principal_type, rule), login_for_id
                        ),
                        roles=(rule.roles or ("admin", "read_only"))
                        if isinstance(rule, GitHubHumanPolicyRule)
                        else (),
                        permissions=rule.actions,
                        scope_summary=_scope(
                            AuthzManagedRuleEntry(principal_type, rule), policy.schema_version
                        ),
                        role_effects=_role_effects(rule, active.policy, candidate, login_for_id)
                        if isinstance(rule, GitHubHumanPolicyRule)
                        else (),
                    )
                )
    return AccessPolicyReview(state="available", rules=tuple(rules))
