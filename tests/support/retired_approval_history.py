from __future__ import annotations

from datetime import datetime, timezone
from control_plane.contracts.retired_change_impact_audit import ChangeImpactPolicyAuditRecord


from control_plane.contracts.repository_evidence import (
    RepositoryChangedFileEvidence,
    RepositoryEvidence,
    RepositoryTarget,
)
from control_plane.contracts.retired_change_impact import (
    ChangeImpactComponentRule,
    ChangeImpactPolicyRecord,
    ChangeImpactProductScope,
    ChangeImpactStoredEvidence,
)


REPOSITORY_ID = "1001"
REPOSITORY_OWNER_ID = "2001"
REPOSITORY = "example/shared-addons"
HEAD_SHA = "a" * 40
TREE_SHA = "b" * 40
MERGE_SHA = "d" * 40


def _product(product: str) -> ChangeImpactProductScope:
    return ChangeImpactProductScope(product=product, system="web")


def _policy(
    *,
    revision: int = 1,
    supersedes_record_id: str | None = None,
    effective_at: str = "2026-08-06T00:00:00Z",
) -> ChangeImpactPolicyRecord:
    return ChangeImpactPolicyRecord(
        repository_id=REPOSITORY_ID,
        repository_owner_id=REPOSITORY_OWNER_ID,
        repository=REPOSITORY,
        policy_revision=revision,
        component_rules=(
            ChangeImpactComponentRule(
                component="generic-web-runtime",
                path_prefixes=("src/runtime",),
                affected_products=(_product("generic-web-a"),),
                review_tier="routine",
                reason="Runtime code reaches one generic web product.",
            ),
            ChangeImpactComponentRule(
                component="odoo-shared-addon",
                path_prefixes=("addons/shared",),
                affected_products=(_product("cm-odoo"), _product("opw-odoo")),
                review_tier="routine",
                reason="Shared addon reaches multiple Odoo products.",
            ),
            ChangeImpactComponentRule(
                component="billing-policy",
                path_prefixes=("control_plane/billing",),
                affected_products=(),
                review_tier="sensitive",
                reason="Billing/governance policy is sensitive engineering work.",
            ),
            ChangeImpactComponentRule(
                component="engineering-ci",
                path_prefixes=(".github/workflows",),
                affected_products=(),
                review_tier="routine",
                reason="CI-only engineering change has no product runtime effect.",
            ),
        ),
        effective_at=effective_at,
        source="test",
        reason="Exercise change impact policy.",
        supersedes_record_id=supersedes_record_id,
    )


def _repository_evidence(
    *paths: str,
    head_sha: str = HEAD_SHA,
) -> RepositoryEvidence:
    return RepositoryEvidence(
        target=RepositoryTarget(
            repository_id=REPOSITORY_ID,
            repository_owner_id=REPOSITORY_OWNER_ID,
            repository=REPOSITORY,
            pull_request_number=20,
            head_sha=head_sha,
            tree_sha=TREE_SHA,
        ),
        merge_commit_sha=MERGE_SHA,
        changed_files=tuple(RepositoryChangedFileEvidence(path=path) for path in paths),
    )


def _stored_evidence(
    component: str,
    *,
    kind: str = "dependency",
    products: tuple[ChangeImpactProductScope, ...] = (),
    confidence: str = "known",
) -> ChangeImpactStoredEvidence:
    return ChangeImpactStoredEvidence.model_validate(
        {
            "record_id": f"stored-{kind}-{component}",
            "component": component,
            "kind": kind,
            "affected_products": products,
            "confidence": confidence,
            "reason": "Launchplane storage binds this evidence to the exact target.",
        }
    )


def _audit(subject: str = "operator:first", revision: int = 1) -> ChangeImpactPolicyAuditRecord:
    policy = _policy(
        revision=revision,
        supersedes_record_id=_policy().record_id if revision > 1 else None,
    )
    return ChangeImpactPolicyAuditRecord(
        record_id=policy.record_id,
        policy_digest=policy.policy_digest,
        actor_kind="local_operator",
        actor_subject=subject,
        trace_id="trace-original",
        recorded_at=datetime(2026, 8, 7, tzinfo=timezone.utc),
    )
