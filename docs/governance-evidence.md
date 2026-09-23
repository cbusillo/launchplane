---
title: Governance Evidence Projection
---

`GET /v1/governance/projection` reads current machine readiness, immutable merge
admission, landing outcomes, and advisory observations for a repository/PR/base
branch. The requested base must match the live pull request. Repository policy
or merge-train policy-target read authority controls access; the retired Owner
acceptance read grant is not required.

The response authorizes no effect. `merge_readiness` is ephemeral and recomputed
with the same live evaluator used for landing. `merge_admission` describes the
latest recorded exact attempt, without granting current effect authority.
`landing_outcome` independently records landed, rejected, reconciliation-required,
or not-observed state. Admission and outcome targets are classified as current or
historical against the live head/tree. GitHub observations are advisory only.

Retired Owner acceptance and change-impact evaluation are absent from this read.
`owner_judgment` is nullable for transitional schema compatibility and current
responses return null. Site Owner decisions use the product-review and release
checklist pages. Historical admission payloads remain unchanged.

When no active landing lineage exists, readiness is `not_active`; missing current
provider, policy, candidate or controller evidence is `unavailable`. Existing
admission and outcome records remain visible in both cases. The endpoint uses the
repository policy's configured GitHub token source and performs no writes.

`/ui/engineering/governance-projection` shows four separate regions: current
readiness, recorded admission, landing outcome, and GitHub observations. It keeps
technical-check reasons, cached evidence, unavailable state and access refusal
visible on desktop and narrow viewports. It has no mutation controls.
