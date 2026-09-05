"""Canonical v2 authority projection; diagnostic provenance never licenses replay."""

from collections.abc import Mapping
from dataclasses import asdict
import hashlib
import json

from control_plane.change_impact_generated import (
    GeneratedBoundary,
    product_authority_mode,
    rule_floors,
)
from control_plane.change_impact_matching import select_change_impact_path_rule
from control_plane.contracts.change_impact import (
    ChangeImpactEvaluation,
    ChangeImpactPolicyRecord,
    ChangeImpactProductScope,
    ChangeImpactRepositoryEvidence,
    ChangeImpactStoredEvidence,
)


CHANGE_IMPACT_DECISION_FIELDS = {
    "schema_version",
    "status",
    "reason_code",
    "classification_model",
    "engineering_review_tier",
    "required_engineering_review_count",
    "owner_impact",
    "affected_products",
    "production_affecting_products",
    "coverage",
    "governance_impact",
}


def validate_v2_file_evidence(evidence: ChangeImpactRepositoryEvidence) -> None:
    paths = {file.path for file in evidence.changed_files}
    for file in evidence.changed_files:
        if file.change_kind == "unknown":
            raise ValueError("v2 requires known file change kinds")
        if file.change_kind == "renamed" and file.previous_path not in paths:
            raise ValueError("v2 requires an explicit rename origin in the changed paths")


def _scopes(scopes: tuple[ChangeImpactProductScope, ...]) -> list[tuple[str, str, str, str]]:
    return sorted(
        {
            (scope.product, scope.system, scope.owner_action, scope.owner_environment)
            for scope in scopes
        }
    )


def _generated(boundary: GeneratedBoundary | None) -> object:
    if boundary is None:
        return None
    return {
        "component": boundary.component,
        "affected_products": _scopes(boundary.affected_products),
        "declared_none": boundary.declared_none,
        "floors": asdict(boundary.floors),
        "generators": [
            {
                "component": leaf.component,
                "affected_products": _scopes(leaf.affected_products),
                "declared_none": leaf.declared_none,
                "own_floors": asdict(leaf.own_floors),
                "ancestor_floors": [asdict(ancestor) for ancestor in leaf.ancestor_floors],
            }
            for leaf in sorted(boundary.generators, key=lambda leaf: leaf.component)
        ],
    }


def build_change_impact_decision_digest(
    *,
    repository_evidence: ChangeImpactRepositoryEvidence,
    policy: ChangeImpactPolicyRecord,
    stored_evidence: tuple[ChangeImpactStoredEvidence, ...],
    generated_boundaries: Mapping[str, GeneratedBoundary],
    evaluation: ChangeImpactEvaluation,
) -> str:
    """Hash complete successful authority, excluding policy/evidence IDs and prose.

    The covered output surface is the effective decision. Matched-evidence
    decorations, order and multiplicity remain independently visible provenance.
    """
    if policy.classification_model != "v2" or evaluation.status != "success":
        raise ValueError("scoped identity requires successful v2 classification")
    validate_v2_file_evidence(repository_evidence)
    paths: list[dict[str, object]] = []
    for file in sorted(repository_evidence.changed_files, key=lambda changed: changed.path):
        selection = select_change_impact_path_rule(path=file.path, rules=policy.component_rules)
        if selection is None:
            raise ValueError("scoped identity requires complete path authority")
        rule = selection.winner.rule
        paths.append(
            {
                "path": file.path,
                "change_kind": file.change_kind,
                "previous_path": file.previous_path,
                "winner": {
                    "schema_version": rule.schema_version,
                    "component": rule.component,
                    "prefix": selection.winner.prefix,
                    "authority": product_authority_mode(rule).value,
                    "affected_products": _scopes(rule.affected_products),
                    "floors": asdict(rule_floors(rule)),
                },
                "ancestors": [
                    {
                        "component": match.rule.component,
                        "prefix": match.prefix,
                        "floors": asdict(rule_floors(match.rule)),
                    }
                    for match in selection.ancestors
                ],
                "generated": _generated(generated_boundaries.get(rule.component)),
            }
        )
    payload = {
        "domain": "launchplane.change-impact-decision.v2",
        "policy_schema_version": policy.schema_version,
        "target": repository_evidence.target.model_dump(mode="json"),
        "base": repository_evidence.base.model_dump(mode="json")
        if repository_evidence.base is not None
        else None,
        "paths": paths,
        "stored_evidence": sorted(
            {
                (
                    evidence.kind,
                    evidence.confidence,
                    evidence.component,
                    tuple(_scopes(evidence.affected_products)),
                )
                for evidence in stored_evidence
            }
        ),
        "decision": evaluation.model_dump(
            mode="json",
            include=CHANGE_IMPACT_DECISION_FIELDS,
        ),
    }
    # JSON defaults to ASCII escapes; str.encode defaults to UTF-8.
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
