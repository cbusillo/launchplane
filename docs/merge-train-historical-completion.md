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
It bounds entry reads to 25 and record windows to 100, reporting unavailable
rather than silently overlooking evidence beyond those windows.

Provider verification uses only GETs. It checks each PR's actual merged state,
exact head and tree, merge parents, recorded candidate result tree, and
containment in a pinned current base commit. Later batch entries use the previous
actual merge commit as their rolling parent. The candidate commit and provider
merge commit may differ while their expected trees agree. A changed target tip
or changed stored snapshot makes the preflight indeterminate.

The typed `result.historical_completion_preflight` distinguishes `eligible`,
`unsupported`, and `indeterminate`, with closed overall and per-entry reason
codes. Only positive proof for every entry yields `evidence_eligible=true`.
Provider failures and unreadable history never prove absence. Responses contain
bounded evidence rather than provider exception text or Owner/review payloads.

The preflight creates no controller lease, admission, outcome, run, disposition,
or idempotency record; an `Idempotency-Key` header does not cache or replay it.
Normal requests without the selector retain their existing behavior and incur
no new reads. Ordinary controller execution rejects the selector.

## Mutation remains unavailable

`historical_completion` with `mutate=true` returns non-retryable HTTP 409 with
`historical_completion_recovery_not_enabled` before controller execution.
Every preflight reports `mutation_enabled=false`, `disposition_supported=false`,
and no admission, provider effect, or fence release. Eligibility is observational
preparation, not approval of a write. Do not loop against the disabled capability.

A future recovery must re-run the proof, record a truthful append-only
observation, and release only the bound fence. Its reader must be deployed before
the first new record is written. The optional record-level schema-v2
`historical_completion` field provides that reader compatibility; existing
generic writers reject creating or overwriting such records. No supported
retirement writer is provided by this preflight slice. Pre-reader source is not
a valid rollback target after a future schema-v2 historical record is written.

The failed historical request must not be blindly replayed. Once a supported
recovery records a disposition and fresh status proves fence release, later PRs
still require fresh selection, checks, readiness, and admission. Candidate-ref
cleanup, new merges, policy changes, and runtime activation are separate actions.
