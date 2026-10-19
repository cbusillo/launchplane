"""Render the existing policy plan for a signed-in administrator, without values."""

from collections.abc import Callable
from functools import lru_cache
from http.client import HTTPException
import json
import re
from time import monotonic
from typing import Literal, cast
from urllib.error import URLError
from urllib.parse import quote
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


Account = tuple[int, str]
AccountReader = Callable[[str], Account | None]


def github_account_login(github_id: int) -> str:
    return _public_github_account_login(github_id, int(monotonic() // 300))


def github_account_for_login(login: str) -> Account | None:
    return _public_github_login_account(login, int(monotonic() // 300))


@lru_cache(maxsize=256)
def _public_github_account_login(github_id: int, cache_window: int) -> str:
    account = _read_public_github_account(f"/user/{github_id}", expected_id=github_id)
    return account[1] if account else ""


@lru_cache(maxsize=256)
def _public_github_login_account(login: str, cache_window: int) -> Account | None:
    if re.fullmatch(r"[A-Za-z0-9-]{1,39}", login) is None:
        return None
    return _read_public_github_account(f"/users/{quote(login, safe='')}")


def _read_public_github_account(path: str, *, expected_id: int = 0) -> Account | None:
    """Keep verified public account metadata only; no credentials or membership."""
    request = Request(
        "https://api.github.com" + path,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "Launchplane"},
    )
    try:
        with urlopen(request, timeout=3) as response:
            payload = json.loads(response.read(65536))
        login, github_id = payload.get("login"), payload.get("id")
        if (
            isinstance(github_id, int)
            and not isinstance(github_id, bool)
            and github_id > 0
            and (not expected_id or github_id == expected_id)
            and isinstance(login, str)
            and re.fullmatch(r"[A-Za-z0-9-]{1,39}", login)
        ):
            return github_id, login
    except (URLError, OSError, ValueError, AttributeError, HTTPException):
        pass
    return None


def bounded_account_logins(
    reader: Callable[[int], str],
    account_reader: AccountReader = lambda _: None,
) -> tuple[Callable[[int], str], AccountReader]:
    """Share admission/count limits across both directions of account lookup."""
    deadline = monotonic() + 1
    logins: dict[int, str] = {}
    accounts: dict[str, Account | None] = {}

    def admit() -> bool:
        return len(logins) + len(accounts) < 10 and monotonic() < deadline

    def read(github_id: int) -> str:
        if github_id not in logins:
            if not admit():
                return ""
            logins[github_id] = reader(github_id)
        return logins[github_id]

    def read_login(login: str) -> Account | None:
        if login not in accounts:
            if not admit():
                return None
            accounts[login] = account_reader(login)
        return accounts[login]

    return read, read_login


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
        return (
            "GitHub workflow in "
            + getattr(rule, "repository", "")
            + "; repository ID: "
            + (getattr(rule, "repository_id", "") or "any")
            + "; owner ID: "
            + (getattr(rule, "repository_owner_id", "") or "any")
        )
    if entry.principal_type == "ordinary_agents":
        return "Agent principal " + getattr(rule, "principal_id", "")
    return (
        labels[entry.principal_type]
        + " "
        + (", ".join(getattr(rule, "subjects", ())) or "(any subject)")
        + "; token labels: "
        + (", ".join(getattr(rule, "token_labels", ())) or "any")
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


def _role_bounds(
    policy: LaunchplaneAuthzPolicy,
    *,
    github_id: int,
    login: str,
) -> tuple[Literal["admin", "read_only"] | None, Literal["admin", "read_only"] | None]:
    """Use the existing resolver for known facts and an upper bound on missing facts."""
    known = policy.human_role_for(
        github_id=github_id, login=login, organizations=frozenset(), teams=frozenset()
    )
    # This display-only copy removes unobserved selectors. It is never persisted
    # or used for authorization. A different result means the role is unknown.
    possible = policy.model_copy(
        update={
            "github_humans": tuple(
                rule.model_copy(
                    update={
                        "github_ids": rule.github_ids if github_id else (),
                        "logins": rule.logins if login else (),
                        "organizations": (),
                        "teams": (),
                    }
                )
                for rule in policy.github_humans
            )
        }
    ).human_role_for(github_id=github_id, login=login, organizations=frozenset(), teams=frozenset())
    return known, possible


def _role_effects(
    rule: GitHubHumanPolicyRule,
    before: LaunchplaneAuthzPolicy,
    after: LaunchplaneAuthzPolicy,
    login_for_id: Callable[[int], str],
    account_for_login: AccountReader = lambda _: None,
) -> tuple[AccessPolicyRoleEffect, ...]:
    # Selectors describe who a rule could match. They are never evidence that
    # an account ID belongs to a login. Only an ID-bound lookup establishes that.
    principals = (
        tuple((github_id, login_for_id(github_id)) for github_id in rule.github_ids)
        if rule.github_ids
        else tuple(account_for_login(login) or (0, login) for login in rule.logins)
    )
    effects = []
    for github_id, login in principals:
        principal = (
            f"GitHub login {login} (account ID {github_id})"
            if login and github_id
            else f"GitHub login {login}"
            if login
            else f"GitHub account ID {github_id} (login unavailable)"
        )
        previous, previous_possible = _role_bounds(before, github_id=github_id, login=login)
        candidate, candidate_possible = _role_bounds(after, github_id=github_id, login=login)
        if previous != previous_possible or candidate != candidate_possible:
            effects.append(AccessPolicyRoleEffect(principal=principal, state="unknown"))
            continue
        state: Literal["lost", "reduced", "retained", "gained", "unaffected"] = (
            "unaffected"
            if previous is None and candidate is None
            else "retained"
            if previous == candidate
            else "lost"
            if candidate is None
            else "gained"
            if previous is None or candidate == "admin"
            else "reduced"
        )
        effects.append(
            AccessPolicyRoleEffect(
                principal=principal, before=previous, after=candidate, state=state
            )
        )
    return tuple(effects) or (
        AccessPolicyRoleEffect(
            principal=_principal(AuthzManagedRuleEntry("github_humans", rule), login_for_id),
            state="unknown",
        ),
    )


def build_access_policy_review(
    *,
    record_store: object,
    request: ManagedAuthzPolicySetProposalInput,
    evidence: ManagedAuthzPolicySetHumanEvidence,
    login_for_id: Callable[[int], str] = lambda _: "",
    account_for_login: AccountReader = lambda _: None,
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
                        role_effects=_role_effects(
                            rule, active.policy, candidate, login_for_id, account_for_login
                        )
                        if isinstance(rule, GitHubHumanPolicyRule)
                        else (),
                    )
                )
    return AccessPolicyReview(state="available", rules=tuple(rules))
