---
title: Ephemeral Owner-Aware Merge Readiness
---

# Ephemeral Owner-Aware Merge Readiness

`control_plane.merge_readiness` implements the L2 contract approved in #2051.
It computes a current, non-authoritative view immediately before a later L3
admission attempt. The evaluator never writes a record, authorizes an effect, or
claims that a merge occurred.

## Boundary

The contract is intentionally limited to ephemeral readiness:

- `mode` is always `ephemeral`.
- `authoritative` is always `false`.
- `authorizes` is always empty.
- `evaluated_at` describes the observation time, not durable authority.
- `readiness_digest` is reproducible evidence for one evaluation payload, not
  an admission token.
- persistence, migrations, HTTP/OpenAPI/UI projection, structural candidate
  proof, and durable L3 admission or landing effects remain outside this
  module.

Structural candidate composition is produced by the pure, read-only
`evaluate_merge_train_structural_candidate` boundary documented in
`merge-train-structural-provenance.md` as one of `exact`, `recorded_rolling`,
`mismatch`, or `unknown`. L2 consumes that result;
it does not attempt to reproduce structural provenance.

## Canonical States

Canonical state and detailed reason codes are independent. The only states are:

1. `ready`
2. `blocked_owner_evidence`
3. `blocked_checks`
4. `blocked_engineering_review`
5. `blocked_policy`
6. `blocked_candidate_identity`
7. `unknown`

Aggregation retains every product and global facet, then selects the worst
state with the frozen precedence:

`unknown` → `blocked_candidate_identity` → `blocked_policy` →
`blocked_engineering_review` → `blocked_checks` →
`blocked_owner_evidence` → `ready`.

Input order never breaks a tie. Product facets, evidence references, advisory
observations, and reason codes are sorted canonically. Any `unknown` facet makes
the aggregate non-ready.

## Evidence Facets

The pure evaluator consumes these current inputs:

- the current `OwnerAcceptanceDecision`, projected into independent
  product/system/action/environment facets;
- the current `EngineeringReviewDecisionRecord` plus exact qualifying run
  evidence references;
- required technical-check state for the exact effect SHA;
- all seven scoped policy fingerprints: `impact`, `technical_checks`,
  `engineering_review`, `ruleset`, `merge_train`, `authorization`, and
  `admission_algorithm`;
- the #2084 structural candidate status plus the current candidate record
  identity, queue position, base, and PR head;
- the current merge-controller lease and expected-SHA fence.

The live adapter accepts existing Owner, engineering-run, tenant technical-check,
batch-candidate, and controller-state models and converts them into the safe
pure-evaluator inputs. It performs no storage operation.

## Detailed Reasons

Reason codes are a closed Pydantic literal contract. They preserve both passing
and blocking facet detail without changing the canonical state vocabulary.

- Owner: `owner_not_required`, `owner_acceptance_valid`,
  `owner_acceptance_missing`, `owner_changes_requested`,
  `owner_acceptance_revoked`, `owner_acceptance_stale`,
  `owner_evidence_stale`, `owner_evidence_unavailable`,
  `owner_authority_unavailable`, `owner_authority_denied`,
  `owner_preview_evidence_unavailable`, `owner_preview_evidence_stale`,
  `owner_review_expired`, `owner_preview_isolation_insufficient`,
  `owner_contributing_identity_unknown`, `owner_self_review_denied`,
  `owner_review_context_missing`, `owner_evidence_head_mismatch`, and
  `owner_evidence_tree_mismatch`.
- Checks: `checks_passed`, `checks_pending`, `checks_failed`, `checks_unknown`,
  and `checks_head_mismatch`.
- Engineering review: `engineering_review_approved`,
  `engineering_review_pending`, `engineering_review_changes_requested`,
  `engineering_review_blocked`, `engineering_review_unknown`,
  `engineering_review_stale`, `engineering_review_head_mismatch`,
  `engineering_review_tree_mismatch`, and
  `engineering_review_evidence_missing`.
- Policy: `policy_fingerprints_match`, plus a distinct `_missing` and `_drift`
  reason for each of the seven required dimensions.
- Candidate and fence: `candidate_exact`, `candidate_recorded_rolling`,
  `candidate_identity_mismatch`, `candidate_identity_unknown`,
  `candidate_queue_mismatch`, `candidate_base_mismatch`,
  `candidate_head_mismatch`, `controller_scope_mismatch`,
  `controller_lease_held`, `controller_lease_missing`,
  `controller_lease_lost`, `controller_lease_expired`, `expected_sha_match`,
  and `expected_sha_mismatch`.

Owner `not_required` is a passing product facet, not missing evidence. Current
authority loss, review expiry, self-review denial, and changed human outcomes
remain distinguishable blockers without rewriting historical L1 evidence.
When current impact evidence proves that Owner review is not required and has no
affected-product subjects, the adapter emits one canonical
`__not_applicable__` facet so the passing Owner outcome remains explicit. If
impact evidence is unavailable before subjects can be resolved, that same
unscoped facet is `unknown` and fails closed.

## Advisory Checks

Launchplane Owner, engineering-review, and legacy shadow check contexts are
observations only. The live adapter removes them from required technical-check
policy and signal aggregation. Their observed names and states may be returned
for diagnostics, but they cannot change readiness state or reason codes.

`readiness_digest` excludes both `evaluated_at` and advisory observations. Thus
clock movement and advisory conclusion changes do not alter the authoritative
technical/Owner/engineering/policy/candidate/fence payload digest. Advisory
ruleset drift enforcement remains owned by #2031 rather than this L2 contract.

## Revalidation

Callers must recompute L2 under the current controller lease immediately before
each later L3 attempt. A lost, missing, expired, or wrong-owner lease; controller
scope mismatch; expected-SHA mismatch; current-head drift; queue movement;
policy drift; or evidence loss produces a non-ready result. A previous L2 result
is never reusable authority.

The production guarded landing adapter follows this rule for every constituent
PR and persists the complete result only inside the subsequent immutable L3
admission. See [merge-admission.md](merge-admission.md).
