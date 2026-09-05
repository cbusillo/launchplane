"""Compare current semantic impact identity without rewriting recorded provenance."""

import hashlib

from control_plane.contracts.change_impact import ChangeImpactEvaluation
from control_plane.contracts.owner_acceptance import OwnerAcceptanceDecision
from control_plane.contracts.engineering_review_decision import EngineeringReviewDecisionRecord


_MISSING_POLICY_SHA256 = hashlib.sha256(b"launchplane-policy-unavailable").hexdigest()


def impact_binding_fingerprints(
    *,
    impact: ChangeImpactEvaluation,
    owner_decision: OwnerAcceptanceDecision,
    engineering_decision: object | None,
) -> tuple[str, str | None]:
    records: list[object] = [
        product.binding for product in owner_decision.products if product.binding is not None
    ]
    if engineering_decision is not None:
        records.append(engineering_decision)
    versions = {getattr(record, "binding_hash_version", None) for record in records}
    if versions and versions != {impact.binding_hash_version}:
        return _MISSING_POLICY_SHA256, None
    if impact.binding_hash_version == 2:
        digests = {getattr(record, "change_impact_decision_digest", None) for record in records}
        if not records and owner_decision.status == "not_required" and impact.status == "success":
            return (
                impact.change_impact_decision_digest or _MISSING_POLICY_SHA256,
                impact.change_impact_decision_digest,
            )
        if len(digests) != 1 or None in digests or "" in digests:
            return _MISSING_POLICY_SHA256, None
        expected = next(iter(digests))
        return str(expected), impact.change_impact_decision_digest

    # Preserve legacy full-policy comparison exactly, including missing engineering evidence.
    expected_digests = {
        product.binding.change_impact_policy_digest
        for product in owner_decision.products
        if product.binding is not None
    }
    engineering_digest = str(
        getattr(engineering_decision, "change_impact_policy_digest", "")
    ).strip()
    if engineering_digest:
        expected_digests.add(engineering_digest)
    if len(expected_digests) == 1:
        expected = next(iter(expected_digests))
    elif (
        not expected_digests
        and owner_decision.status == "not_required"
        and impact.status == "success"
        and impact.policy_digest
    ):
        expected = impact.policy_digest
    else:
        expected = _MISSING_POLICY_SHA256
    return expected, impact.policy_digest or None


def select_current_engineering_decision(
    *,
    impact: ChangeImpactEvaluation,
    decisions: tuple[EngineeringReviewDecisionRecord, ...],
) -> EngineeringReviewDecisionRecord | None:
    """Never resurrect an older decision when the latest uses a different version."""
    if not decisions or decisions[0].binding_hash_version != impact.binding_hash_version:
        return None
    return decisions[0]
