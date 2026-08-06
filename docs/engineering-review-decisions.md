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
