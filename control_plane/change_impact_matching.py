"""Deterministic v2 path selection without changing legacy policy matching."""

from dataclasses import dataclass

from control_plane.contracts.change_impact import (
    ChangeImpactComponentRule,
    ChangeImpactPolicyRecord,
)


@dataclass(frozen=True, slots=True)
class ChangeImpactRuleMatch:
    rule: ChangeImpactComponentRule
    prefix: str


@dataclass(frozen=True, slots=True)
class ChangeImpactPathSelection:
    winner: ChangeImpactRuleMatch
    ancestors: tuple[ChangeImpactRuleMatch, ...]


def validate_change_impact_v2_policy(policy: ChangeImpactPolicyRecord) -> None:
    """Reject ambiguous or implicit v2 authority at apply and evaluation boundaries."""
    if policy.classification_model != "v2":
        return
    prefixes: set[str] = set()
    for rule in policy.component_rules:
        if not rule.affected_products and rule.product_impact != "declared_none":
            raise ValueError("v2 rules require explicit products or declared_none")
        for prefix in rule.path_prefixes:
            if not prefix or "." in prefix.split("/") or "" in prefix.split("/"):
                raise ValueError("v2 prefixes must be canonical non-root paths")
            if prefix in prefixes:
                raise ValueError("v2 policy has equal-specificity component ambiguity")
            prefixes.add(prefix)


def select_change_impact_path_rule(
    *, path: str, rules: tuple[ChangeImpactComponentRule, ...]
) -> ChangeImpactPathSelection | None:
    """Select one product authority and retain every distinct ancestor rule as a floor."""
    matches: list[ChangeImpactRuleMatch] = []
    for rule in rules:
        prefixes = tuple(
            prefix
            for prefix in rule.path_prefixes
            if path == prefix or path.startswith(f"{prefix}/")
        )
        if prefixes:
            matches.append(ChangeImpactRuleMatch(rule, max(prefixes, key=len)))
    if not matches:
        return None
    matches.sort(key=lambda match: (len(match.prefix), match.prefix, match.rule.component))
    if len(matches) > 1 and len(matches[-1].prefix) == len(matches[-2].prefix):
        raise ValueError("v2 path has equal-specificity component ambiguity")
    return ChangeImpactPathSelection(winner=matches[-1], ancestors=tuple(matches[:-1]))
