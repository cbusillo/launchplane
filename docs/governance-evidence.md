---
title: Governance Evidence Projection
---

`GET /v1/governance/projection` reads current machine readiness, immutable merge
admission, landing outcomes, and advisory observations for a repository/PR/base
branch. When live repository evidence is available, the requested base must match the
pull request. Repository policy or merge-train policy-target read authority
controls access; the retired Client
acceptance read grant is not required.

The response authorizes no effect. `merge_readiness` is ephemeral and recomputed
with the same live evaluator used for landing. `merge_admission` describes the
latest recorded exact attempt, without granting current effect authority.
`landing_outcome` independently records landed, rejected, reconciliation-required,
or not-observed state. Admission and outcome targets are classified as current or
historical against the live head/tree, or unknown when that evidence is unavailable.
GitHub observations are advisory only.

Retired Client acceptance and change-impact evaluation are absent from this read.
`owner_judgment` is nullable for transitional schema compatibility and current
responses return null. Client decisions use the product-review and release
checklist pages. Historical admission payloads remain unchanged.

When no active landing lineage exists, readiness is `not_active`; missing current
provider, candidate or controller evidence is `unavailable`. Authorized reads
retain stored admission and outcome records for the requested repository, base
branch and PR. When the repository provider fails, `target` is null and
`requested_target` identifies only the lookup scope; missing stored records stay
absent. The endpoint uses the repository policy's configured GitHub token source
and performs no writes. Repository policy and read authorization must be
established before history lookup; missing policy still returns an unavailable
response rather than bypassing that check.

`/ui/engineering/governance-projection` shows four separate regions: current
readiness, recorded admission, landing outcome, and GitHub observations. It keeps
technical-check reasons, cached evidence, unavailable state and access refusal
visible on desktop and narrow viewports. It has no mutation controls.
