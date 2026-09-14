---
title: Historical Merge Completion
---

# Historical Merge Completion

A provider can report a completed merge for which Launchplane has no immutable
admission. That observation cannot be converted into an admission or a successful
Launchplane landing outcome. The existing controller keeps the affected plan
fenced until a supported disposition exists.

## Read-only applicability preflight

The existing `POST /v1/work-graph/merge-train/controller/run-once` route accepts
an optional `historical_completion` selector with `mutate=false`. The repository
policy's existing `service_authz` still controls access. This adds no grant,
ordinary-agent authority, or Owner approval requirement.

The selector binds the inspected active record, stable plan, candidate effect,
policy, and ordered entry identities:

```json
{
  "repository": "example/widget",
  "base_branch": "main",
  "mutate": false,
  "historical_completion": {
    "expected_active_record_id": "landing-record-42",
    "expected_landing_plan_id": "landing-plan-42",
    "expected_effect_sha": "candidate-commit",
    "expected_policy_sha256": "inspected-policy-digest",
    "expected_entries": [
      {
        "position": 1,
        "pull_request_number": 42,
        "expected_head_sha": "inspected-head-commit",
        "expected_head_tree_sha": "inspected-head-tree"
      }
    ]
  }
}
```

An agent can derive these fields from the scoped controller status and diagnostic
read. They are comparisons against persisted state, never caller-supplied proof.
The preflight requires an inactive legacy `reconcile_required` landing fence and
complete, unchanged plan/candidate/policy evidence. It refuses ordinary-bound
work, stack batches, no-op entries, existing admissions, and incomplete batches.
It reads the exact landing record within the authorized repository and base,
then checks that the record is active. Candidate history is scoped to that
record's batch before applying the 100-record limit. An oversized batch reports
`candidate_history_limit_exceeded`; unrelated historical batches do not consume
its evidence window. An initial candidate without a commit SHA may precede the
materialized candidate, but conflicting materialized candidates are ambiguous.

For each of at most 25 selected PRs, the portable observational reader checks
for an active stack whose root is that PR. The native PostgreSQL service path
also validates every active stack in the target and excludes overlap as either
a root or a member. Its queries have no capped history post-filter. It refuses
any admission for a selected repository/base/PR, across all heads and lineages,
and any ordinary effect for the target, including terminal effects. An ordinary
controller or active progress binding also prevents recovery. Malformed or
ambiguous target evidence fails closed. Repository names are normalized before
every scoped read; branch case is preserved.

Provider verification uses only GETs. It checks each PR's actual merged state,
exact head and tree, merge parents, recorded candidate result tree, and
containment in a pinned current base commit. Later batch entries use the previous
actual merge commit as their rolling parent. The candidate commit and provider
merge commit may differ while their expected trees agree. A changed target tip
or changed stored snapshot makes the preflight indeterminate. Unrelated history
changes do not invalidate the selected snapshot.

The typed `result.historical_completion_preflight` distinguishes `eligible`,
`unsupported`, and `indeterminate`, with closed overall and per-entry reason
codes. Only positive proof for every entry yields `evidence_eligible=true`.
Provider failures and unreadable history never prove absence. Responses contain
bounded evidence rather than provider exception text or Owner/review payloads.

The preflight creates no controller lease, admission, outcome, run, disposition,
or idempotency record; an `Idempotency-Key` header does not cache or replay it.
The native path checks exactly one current DB merge-policy catalog and authz
policy under their existing locks, before provider access, and rechecks the
selected snapshot and exclusions after the provider proof. The catalog contains
multiple repository policies; it remains one active record. Native recovery
uses the public GitHub API adapter endpoint; a caller-selected endpoint cannot
provide historical truth or receive the service credential.

Normal requests without the selector retain their existing behavior and incur
no new reads. Ordinary-agent controller execution rejects the selector.

## Atomic historical disposition

Native PostgreSQL supports the same explicit selector with `mutate=true` and a
new `Idempotency-Key`. Filesystem and SQLite cannot apply this operation; there
is no local-write fallback. A positive native dry-run reports
`disposition_supported=true` and `mutation_enabled=true`, while
`fence_released=false` and `admission_created=false`. These capability flags do
not replace current authorization, positive evidence or operator intent.

Apply repeats fresh GET-only proof, then performs one transaction:

1. Acquire the target controller advisory lock and row, then the authz
   and merge-policy locks. Every supported admission, controller, progress,
   stack and ordinary-effect writer takes the same controller lock, so their
   writes cannot appear between the absence checks and commit.
2. Revalidate the exact inactive reconcile-required controller, source landing
   and candidate, current authority identities, and all absence conditions.
3. Insert one deterministic historical successor, supersede only its exact
   predecessor, release only that controller fence, and persist the completed
   idempotency response in the same commit.

The successor classifies the observation as `observed_merged_without_admission`
with `authority_state=observation_only`. Its landing entries are `stale` and have
no successful landing fields. Its typed evidence records the actual observed
provider merge. Its disposition authorization identifies the caller who
recorded the recovery, the key, DB recording time, and the exact authz and merge
policy identities checked at commit. It makes no claim about who performed the
historical merge or whether Launchplane authorized that merge.

The transaction contains no provider I/O and uses short lock and statement
timeouts. Contention or changed state returns a bounded conflict and leaves the
fence intact. A failure before commit rolls back history, retirement, controller
release and idempotency together. There is no separate pending reservation or
lease to recover. A lost response after commit can be replayed with the same
key: current authz and merge-policy locks protect that lookup, without requiring
the original controller state. Revoked callers receive no cached evidence. A
different key for an exact completed disposition reports `already_recorded`
with its record ID and creates no additional record.

Generic writers continue rejecting creation or overwrite of historical
records. The record-level landing schema remains version 2. Its inner evidence
version 2 requires disposition authorization; the reader still accepts older
inner version 1 observations. **After the first version 2 evidence is written,
the release containing this atomic disposition reader is the minimum compatible
rollback release.** Deploy that reader before the first live apply.

The failed historical merge request must not be blindly replayed. After fresh
status proves the exact fence release, the controller treats this stale legacy
landing as completed for its candidate and performs fresh selection. Later PRs
still require current checks, readiness and admission. Candidate-ref cleanup,
new merges, policy changes and runtime activation are separate actions.
