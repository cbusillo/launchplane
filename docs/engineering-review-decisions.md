---
title: Engineering Review Decisions
---

Engineering review decisions combine Launchplane's server-derived change-impact
classification with Launchplane-created engineering-review runs for one exact
repository, pull request, head, tree, and Every Code work-request lifecycle.

The caller supplies only the repository/pull-request reference and stored work
request id. Launchplane resolves current GitHub evidence, the active
change-impact policy, and persisted review runs. Repository identity, head,
tree, review tier, required count, authority, reviewer slot, model family, and
decision binding are not accepted from the caller.

Routine classifications require one approved completed run. Sensitive and
unknown classifications require two distinct slots; approval additionally
requires two model families. Stale targets, conflicting authorities,
nonterminal runs, failed/expired runs, insufficient reviews, and missing policy
evidence cannot produce approval.

Decision records are immutable and append-only. Their deterministic binding
excludes evaluation time so retrying identical evidence replays the same record.
The `launchplane/engineering-review-shadow` GitHub status is a projection only:
it is never read as authority and is not a required merge check. Projection
retry loads a persisted decision, re-verifies the current GitHub target, and
does not mutate review evidence.

Routes:

- `POST /v1/engineering-review-decisions/evaluate`
- `GET /v1/engineering-review-decisions/{decision_id}`
- `POST /v1/engineering-review-decisions/project`

The rollout remains shadow-only until routine and sensitive canaries prove the
exact-head behavior and a later policy change deliberately enables enforcement.

## Versioned Impact Identity

Optional `binding_hash_version` and `change_impact_decision_digest` fields
preserve the legacy decision ID and digest when omitted. Version `2` requires a
scoped decision digest and a distinct hash domain. It excludes only the
change-impact policy record ID, revision, and full digest from semantic
identity; those fields remain stored as original provenance. Re-evaluating
identical v2 semantics returns the original immutable decision even when the
current full-policy provenance differs. Exact target, lifecycle, authority,
review outcome, required count, and qualifying runs remain bound.

Admission requires the newest decision to match the current impact hash version and
compares versioned scoped impact identity for v2. Mixed or missing identities
fail closed. Legacy admission continues comparing full-policy digests, and all
other authority dimensions and exact-head checks remain unchanged. Successful
v2 evaluations derive scoped identity from current server-owned classification
inputs; unknown and stale results cannot publish it. V2 policy activation
remains unavailable pending rollout qualification.
