---
title: Guarded Merge Admission And Landing Outcomes
---

# Guarded Merge Admission And Landing Outcomes

Launchplane treats merge readiness, merge authorization, and landing truth as
separate layers. Level 2 readiness remains ephemeral and non-authoritative. A
provider merge effect is allowed only after Launchplane persists one immutable
Level 3 `MergeAdmissionRecord` for the exact attempt. Launchplane then appends a
separate `MergeLandingOutcomeRecord` that reports what can truthfully be proven
after the effect boundary.

## Admission Boundary

Each batch entry is re-evaluated under the current repository/base controller
lease immediately before its provider merge. The live adapter resolves current
Owner acceptance, change impact, engineering decision and run evidence,
required technical checks, policy fingerprints, structural candidate
provenance, queue position, rolling base, exact head/tree, candidate identity,
and the expected effect SHA. It re-reads the active merge-train policy record
and GitHub queue for every entry rather than treating request-start policy or
stored candidate order as live evidence.

Only `ready` Level 2 evidence plus `exact` or `recorded_rolling` structural
evidence may produce an admission. The guarded caller binds the expected lease
owner from its acquired controller authority before each fresh controller-state
observation, so a cleared or replaced lease becomes a normal fail-closed L2
result rather than new authority. The admission binds the complete L2 and
structural results, candidate and landing-plan digests, current lease identity,
algorithm version, attempt sequence, and exact Git identities. Storage creation
is insert-only. The storage transaction re-reads the persisted controller lease,
acquisition token, expiry, policy digest, active PR, stable landing-plan ID, and
expected effect SHA using the actual admission timestamp. Finding the same
admission again never authorizes replay of the provider mutation.

If fresh readiness or structural evidence refuses admission before the provider
checkpoint, the controller returns an accepted `block` result with a stable
reason code and the public-safe readiness facets. It releases the controller
lease cleanly and leaves the landing plan available for a later pass after the
missing or stale evidence is corrected. A pre-effect policy refusal is not
durable effect ambiguity and must not be converted into controller
reconciliation.

Unavailable or malformed authoritative repository evidence refuses admission
with `repository_evidence_unavailable`; evidence that changes during resolution
uses `repository_evidence_stale`. Both are pre-effect denials: the controller
returns a normal block and releases its lease without requiring reconciliation.
The denial exposes a bounded message, not raw provider details or file paths.

## Outcome Boundary

Outcomes use only three public states:

- `landed`: provider response and exact Git commit/tree evidence confirm the
  intended landing.
- `rejected`: provider evidence, or later exact observation, conclusively proves
  that the attempt produced no landing.
- `reconcile_required`: transport, process, lease, or observation evidence
  cannot distinguish landed from not landed.

An admission without an outcome is effect-unknown. A `reconcile_required`
outcome is also effect-unknown. Neither state permits another provider attempt.
Reconciliation observes GitHub first and appends a successor outcome; it never
rewrites history or repeats an ambiguous mutation.

A conclusive provider refusal and a conclusive observed no-effect result are
different evidence. No-effect reconciliation records the exact open PR state,
head/tree, and unchanged base SHA/tree without claiming a provider rejection.
If an admission had no outcome, reconciliation first appends the truthful
`process_interrupted` observation and then appends the proven no-effect
successor.

## Batch And Recovery

The controller and direct batch-landing endpoint share the same guarded
boundary. Each constituent PR receives independent evidence and fresh
revalidation against the actual rolling base. Queue, head, policy, Owner,
technical-check, structural, lease, or expected-SHA drift refuses the next
admission before mutation.

Landing progress records retain one stable `landing_plan_id` while each
persisted checkpoint receives its own record ID. Admissions bind both values,
and recovery searches the stable lineage so a restart after a progress
checkpoint can still find the preceding attempt.

Controller phase evidence distinguishes admission work from the provider effect.
`admit_pull_request` means Launchplane is re-observing prior outcomes and
computing or persisting fresh merge admission; GitHub's merge endpoint has not
yet been called. `merge_pull_request` is checkpointed only after an immutable
admission exists and immediately before the guarded GitHub merge request. An
operator must not infer that GitHub rejected a merge from a 409 recorded under
the admission phase.

On restart, an already-merged exact PR is reconciled against its preceding
admission and receives a truthful `landed` outcome only after Launchplane reads
the actual base ref SHA/tree and proves that base contains the merge commit. If
the exact PR remains open
and the expected base did not advance, Launchplane appends conclusive no-effect
evidence and may create a fresh admission only after complete L2 recomputation.
Candidate-ref cleanup happens after landing evidence, so cleanup failure cannot
erase or downgrade a truthful outcome.

## Persistence

Filesystem rehearsal stores records under:

- `state/launchplane_merge_admissions/`
- `state/launchplane_merge_landing_outcomes/`

Shared-service PostgreSQL stores them under:

- `launchplane_merge_admissions`
- `launchplane_merge_landing_outcomes`

Attempt identity is deterministically validated from stable landing-plan
lineage, lease acquisition, PR, queue position, sequence, and expected effect;
attempt and admission bindings are unique. Landing observations are unique and
ordered authoritatively by admission plus observation sequence, with timestamps
retained only as metadata. Exact replay is idempotent; conflicting replay fails
closed.
