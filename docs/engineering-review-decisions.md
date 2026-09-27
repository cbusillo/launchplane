---
title: Engineering Review Decisions
---

Engineering review decisions combine server-resolved Git identities with
Launchplane-created review runs for one exact repository, pull request, head,
tree, and stored work-request lifecycle. The caller supplies only the PR
reference and work-request ID. The active authority binds reviewer slots,
model families, and policy revision.

New schema-version-2 decisions require two completed, approved runs from distinct
slots and model families. Creation uses the first two configured authority
slots, without classifying paths or reading retired approval policies. Missing
authority, stale targets or authority, blocked/requested changes, and incomplete
review evidence cannot produce approval.

Decision records are immutable and append-only. Their deterministic binding
excludes evaluation time, so identical evidence replays the original record.
Schema-version-1 history retains its original fields and exact digests, including
the optional legacy versioned impact identity. New decisions reject those retired
classification fields. No historical record is rewritten or deleted.

The GitHub engineering-review check is a projection, never authority. Projection
retry reloads the persisted decision and verifies the current Git target.
Repository merge-train policy retains its existing `advisory`/`required` choice;
this change does not enable enforcement or change runtime policy.

Routes remain:

- `POST /v1/engineering-review-decisions/evaluate`
- `GET /v1/engineering-review-decisions/{decision_id}`
- `POST /v1/engineering-review-decisions/project`
