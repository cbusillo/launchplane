---
title: Merge Readiness
---

Merge readiness is an ephemeral read of the evidence for one exact merge attempt.
It authorizes no effect. The live adapter uses the same evaluation for guarded
landing and the engineering governance view.

Current readiness checks the required technical checks at the exact candidate
SHA, advisory engineering-review observations, current policy fingerprints, candidate and rolling-base provenance, and the
controller lease plus expected effect SHA. Missing evidence stays unknown;
contradictory evidence blocks the attempt. Each landing re-reads these inputs.

Retired Client acceptance and change-impact policies do not participate. Live
results have no Client facets or impact fingerprint. Optional legacy fields remain
readable in stored admission snapshots so their existing digests and historical
outcomes are preserved; their presence does not cause a fresh Client evaluation.
The site's current Client decision is recorded through product review and the
release checklist, separately from machine merge readiness.

The six current policy dimensions are `technical_checks`, `engineering_review`,
`ruleset`, `merge_train`, `authorization`, and `admission_algorithm`. Advisory
engineering-review evidence and its policy fingerprint remain visible without
blocking. Stored required-review snapshots keep their historical aggregation
and digests; current evaluations always use advisory review. Reserved Launchplane
advisory check projections never count as required technical-check authority.

States aggregate deterministically: unknown, candidate identity, policy,
technical checks, then ready. Every active facet keeps its
reason codes. Legacy Client-blocked states remain readable only in old snapshots.
Policy, evidence, and state collections are canonicalized; observation timestamps
and advisory GitHub observations do not change the readiness digest.

The candidate must match the current repository, base, queue position, PR head and
tree, and expected effect. A recorded rolling base is accepted only with complete
prior landing evidence. The controller must still own its unexpired lease and the
observed effect must equal the admitted SHA. None of these checks can be replaced
by a Client decision or a GitHub approval.

See [merge admission](merge-admission.md), [structural provenance](merge-train-structural-provenance.md),
and [release review](release-review.md) for their distinct authority boundaries.
