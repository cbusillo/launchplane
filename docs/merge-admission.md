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

New admissions record algorithm `merge-admission-v2`, which removes retired
Client/change-impact merge gates. Existing v1 admissions and landing outcomes
remain immutable and readable. Client review is recorded separately by the
product-review and release-checklist paths. Repository evidence now comes from
the independent Git evidence reader; admission never imports or calls the retired
approval classifiers. Historical payload readers preserve prior record digests.

Each batch entry is re-evaluated under the current repository/base controller
lease immediately before its provider merge. The live adapter resolves current
engineering decision and run evidence,
[required technical checks](merge-train-policy.md#candidate-validation), policy fingerprints, structural candidate
provenance, queue position, rolling base, exact head/tree, candidate identity,
and the expected effect SHA. It re-reads the active merge-train policy record
for every entry and the GitHub queue for every sequential entry, rather than
treating request-start policy or stored candidate order as live evidence.
Protected batch landing shares queue and member reads within one pass, as
described under [Batch And Recovery](#batch-and-recovery).

The native controller route and scheduled controller create a repository-evidence
reader per call. Within that reader they reuse up to 128 Git commit-to-tree
results, keyed by repository ID and exact commit SHA. These immutable results
cannot change with a PR update. Repository identity, PR metadata and confirmation,
changed files, authorship, the live queue, active policy and technical checks are
still read on every evaluation. Failed tree reads are never cached. The cache
ends with the controller call; injected readers and the direct batch-landing
endpoint retain their existing read behavior. This reduces one repeated read per
unchanged member without treating prior admission or provider refusal as reusable
authority.

That live queue is built from the plan's unlanded entries alone, under every
batch rule. Conflict hold-outs and pull requests that joined the queue after
planning, wherever they sort, wait for the next candidate and do not invalidate
the landing. A planned entry whose head moved, that left the queue, or that no
longer fits the batch refuses admission with `landing_lineage_changed`.

Only `ready` Level 2 evidence plus `exact` or `recorded_rolling` structural
evidence may produce an admission. The guarded caller binds the expected lease
holder from its acquired controller authority before each fresh controller-state
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
reason code and the public-safe readiness facets. When structural evaluation
completed, the result also includes its status, reason codes, effective base
commit and tree, and candidate, landing-plan, and provenance digests. This
diagnostic is projected from the same evaluation that refused admission; it
does not re-evaluate, authorize, or persist a rejected admission. The controller
releases its lease cleanly and leaves the landing plan available for a later
pass after the missing or stale evidence is corrected. A pre-effect policy
refusal is not durable effect ambiguity and must not be converted into
controller reconciliation.

Unavailable or malformed authoritative repository evidence refuses admission
with `repository_evidence_unavailable`; evidence that changes during resolution
uses `repository_evidence_stale`. Both are pre-effect denials: the controller
returns a normal block and releases its lease without requiring reconciliation.
The denial exposes a bounded message, not raw provider details or file paths.

## Merge Readiness

Merge readiness is an ephemeral read of the evidence for one exact merge attempt.
It authorizes no effect. The live adapter uses the same evaluation for guarded
landing and the [governance projection](#governance-projection).

Current readiness checks the required technical checks at the exact candidate
SHA, advisory engineering-review observations, current policy fingerprints,
candidate and rolling-base provenance, and the controller lease plus expected
effect SHA. Technical checks use the
[shared required-check evaluation](merge-train-policy.md#candidate-validation):
a missing named check is pending, while unavailable policy or evidence cannot
qualify a pass. Other missing authoritative evidence stays unknown;
contradictory evidence blocks the attempt. Each landing re-reads these inputs.

A single-entry candidate can reuse current passing PR-head checks when the
head, tree, base and candidate identities match its stored reuse binding.
Both exact candidates and structurally proven collapsed-stack roots qualify;
recorded rolling provenance does not discard that binding. Unproven stack
composition, changed identities and lost check evidence still refuse landing.

The six current policy dimensions are `technical_checks`, `engineering_review`,
`ruleset`, `merge_train`, `authorization`, and `admission_algorithm`. Advisory
engineering-review evidence and its policy fingerprint remain visible without
blocking. Stored required-review snapshots keep their historical aggregation
and digests; current evaluations always use advisory review. Reserved Launchplane
advisory check projections never count as required technical-check authority.

States aggregate deterministically: unknown, candidate identity, policy,
technical checks, then ready. Every active facet keeps its
reason codes. Legacy Client facets and Client-blocked states remain readable
only in old snapshots and never trigger a fresh Client evaluation.
Policy, evidence, and state collections are canonicalized; observation timestamps
and advisory GitHub observations do not change the readiness digest.

The candidate must match the current repository, base, queue position, PR head and
tree, and expected effect. A recorded rolling base is accepted only with complete
prior landing evidence. The controller must still own its unexpired lease and the
observed effect must equal the admitted SHA. None of these checks can be replaced
by a Client decision or a GitHub approval.

## Structural Provenance

Launchplane records structural candidate provenance for each merge-train batch.
The record binds the repository and base branch, base commit and tree, policy
identity, ordered PR positions, exact PR head commits and trees, every rolling
parent/head/result commit and tree, the terminal candidate
identity, and an optional proven stack-collapse root.

The provenance and candidate fingerprints are canonical SHA-256 digests. A
landing-plan fingerprint separately binds the active plan while excluding
mutable landing progress. These digests are evidence identifiers, not merge or
release authority.

`evaluate_merge_train_structural_candidate` is pure and read-only. It produces
the `exact | recorded_rolling | mismatch | unknown` status consumed by merge
readiness:

- `exact` requires the active candidate and landing plan, unchanged policy and
  queue, exact head/tree evidence, and the original recorded base.
- `recorded_rolling` additionally requires every prior plan entry to be durably
  recorded as landed at its exact head/tree, with an unbroken actual rolling
  base and result-tree chain. A proven stack-collapse root is also recorded
  rolling composition, including at position one.
- `mismatch` means available evidence contradicts the live candidate, queue,
  plan, policy, base, head/tree, stack, or rolling chain.
- `unknown` means required evidence is absent or legacy records predate this
  additive contract. Old records remain readable but never default to exact.
  Legacy delta fingerprints, affected subjects and combined Client bindings
  stay readable and unused.

Strict technical checks still read current required-check policy and results
for the exact final candidate against the observed base. A proven
`recorded_rolling` result satisfies base freshness when GitHub's actual landing
commit differs from the synthetic candidate commit despite identical trees.
Every prior merge must have the recorded head and result tree, and the actual
base must match that chain. The original technical observation and its digest
remain unchanged; readiness combines it with structural proof. Missing
evidence, tree drift, or failed required checks continue to refuse landing.

Landing observes and stores the actual rolling-base SHA/tree, landed-head
SHA/tree, and merge-result SHA/tree on normal, retry, and already-merged crash
recovery paths. Candidate no-op entries land as `skipped`, preserve the rolling
parent as their result, and remain usable when later queue positions are
evaluated. Absent legacy landing identities remain `unknown`, not contradiction.

Ordinary jobs additionally bind a skipped entry to the persisted candidate
effect's exact containment proof and a joined no-op finalization. A candidate
record alone does not authorize skipping. The finalization preserves the actual
rolling base, records fresh admission and a truthful zero-effect outcome, and
advances progress atomically. Later successors may carry that proven skipped
entry without treating it as a provider merge.

## Outcome Boundary

Outcomes use only three public states:

- `landed`: exact evidence confirms the intended result, either a provider merge
  or an ordinary job's proven already-contained change.
- `rejected`: provider evidence, or later exact observation, conclusively proves
  that the attempt produced no landing.
- `reconcile_required`: transport, process, lease, or observation evidence
  cannot distinguish landed from not landed.

An admission without an outcome is effect-unknown. A `reconcile_required`
outcome is also effect-unknown. Neither state permits another provider attempt.
Do not rerun the merge by hand or infer success from checks, candidate status,
landing-plan progress, or a sent GitHub request; run the controller again so it
observes GitHub and appends a successor outcome. Reconciliation never rewrites
history or repeats an ambiguous mutation; terminal `landed` and `rejected`
outcomes cannot be superseded. Contradictory or incomplete evidence stays
`reconcile_required` for admin investigation.

A GitHub merge-endpoint HTTP 405 remains a conclusive rejected outcome. The
adapter makes one read of the same PR to diagnose the refusal from its
unchanged, open head's `mergeable_state`. The admin response retains the
attempt's trace, PR number, provider status, and that state as
`github_mergeable_state`. `behind` gives a branch-refresh instruction
(`head_behind_base`); `blocked` names a base-branch requirement such as an
unresolved conversation or a missing review (`merge_blocked`). A failed,
malformed, closed, or changed-head read leaves the diagnosis unconfirmed and
asks the admin to reread merge requirements. Raw provider response bodies are not copied into this diagnosis.
For a blocked head, the adapter also reads the active branch's required
code-scanning tools and the exact head's native `github-advanced-security`
results through the existing rule and check readers. If a required tool is
absent after all observed checks have been complete for the configured grace, the
refusal diagnosis is `required_code_scanning_missing`. That grace allows GitHub
to process a just-uploaded result. Running checks, missing completion times,
unreadable evidence and changed heads do not prove an absent scan; a similarly
named check from another app cannot supply the native result. This recovery
neither requests scans nor changes GitHub's rules or findings thresholds.
The refusal returns HTTP 409 `github_merge_rejected`, not an upstream-outage
retry instruction; no second merge is attempted by the diagnostic read.
Before each subsequent merge, the existing PR read also blocks an observed
`behind` head before new admission or provider mutation, after reconciling an
earlier unresolved attempt from that same unchanged open-head/base proof. This includes resumed
partial batches: the first landed entry stays recorded, and the behind entry
does not accumulate repeated admissions and rejected merge calls while GitHub
continues to report that condition. Unknown mergeability alone does not prove
the refusal's cause.

The `already_contained_no_provider_effect` landed reason is a successful no-op,
not evidence of a merge request. It records `provider_effect_attempted=false`,
the observed PR lifecycle and exact unchanged base/head identities, with no
provider status, request ID or rejection. Under the existing skipped-entry
contract, the merge-commit fields carry the unchanged rolling base SHA/tree;
they do not describe a newly created commit. Projections must use the reason to
label this result as already contained and display the unchanged base.

A conclusive provider refusal and a conclusive observed no-effect result are
different evidence. No-effect reconciliation records the exact open PR state,
head/tree, and unchanged base SHA/tree without claiming a provider rejection.
If an admission had no outcome, reconciliation first appends the truthful
`process_interrupted` observation and then appends the proven no-effect
successor.

## Batch And Recovery

The controller and direct batch-landing endpoint share the same guarded
boundary. Each constituent PR receives independent evidence and fresh
revalidation. Original-PR landing uses the actual rolling base. Protected batch
PR landing admits every member against the same initial base, persists all
admissions before its one provider effect, and binds the provider PR in the
landing plan's digest. Queue, head, policy,
technical-check, structural, lease, or expected-SHA drift refuses the next
admission before mutation.

Within one protected batch pass, the live adapter reads the open-PR queue
snapshot and each member's repository evidence once and reuses them for every
evaluation except the final admission, which rereads both before the
member, batch-PR and base rechecks that precede the provider merge. Each
member's head and open state, the active policy, technical checks and the
controller fence are still read for every admission. Without this, a pass
cost two evaluations per member, each reading every open PR in the
repository, so an eight-member batch took about half an hour while holding
the controller lease (#3069). The shared reads end with the pass; a queue
change in between is caught by the final admission and records the earlier
admissions as not dispatched.

If a batch stops after admitting only a prefix but before dispatch, those
admissions receive `rejected / batch_not_dispatched` outcomes with
`provider_effect_attempted=false`. After an interrupted provider attempt,
recovery reads the batch PR and candidate containment first. An unmerged batch
PR whose exact candidate head is absent from the protected base proves
`batch_reconciliation_confirmed_no_effect`; typed `batch_no_effect` evidence
records that provider target without claiming that changed constituent heads
were observed unchanged. This resolves prior uncertainty before stale-member,
closed-batch, or pending-check handling. A merged batch is reconciled from its
Git identity and every original PR's completion, with no new provider attempt.

These are additive record fields. Historical plans and outcomes without batch
fields retain their original digests. A rollback after batch records have been
written requires a reader that understands those fields; an older strict reader
must not be assumed compatible.

Landing progress records retain one stable `landing_plan_id` while each
persisted checkpoint receives its own record ID. Admissions bind both values,
and recovery searches the stable lineage so a restart after a progress
checkpoint can still find the preceding attempt.

Controller phase evidence distinguishes admission work from the provider effect.
`admit_pull_request` means Launchplane is re-observing prior outcomes and
computing or persisting fresh merge admission; GitHub's merge endpoint has not
yet been called. `merge_pull_request` is checkpointed only after an immutable
admission exists and immediately before the guarded GitHub merge request. An
admin must not infer that GitHub rejected a merge from a 409 recorded under
the admission phase: an HTTP 409 alone does not distinguish controller fencing,
admission refusal, admission reconciliation, or a GitHub state conflict. Keep
the response JSON and trace ID from a failed call.

On restart, an already-merged exact PR is reconciled against its preceding
admission and receives a truthful `landed` outcome only after Launchplane reads
the actual base ref SHA/tree and proves that base contains the merge commit. If
the exact PR remains open
and the expected base did not advance, Launchplane appends conclusive no-effect
evidence and may create a fresh admission only after complete L2 recomputation.
Candidate-ref cleanup happens after landing evidence, so cleanup failure cannot
erase or downgrade a truthful outcome.

## Governance Projection

`GET /v1/governance/projection` reads current machine readiness, immutable merge
admission, landing outcomes, and advisory observations for a repository/PR/base
branch. It adds no record stream. When live repository evidence is available,
the requested base must match the pull request. Repository policy or
merge-train policy-target read authority controls access.

The response authorizes no effect. `merge_readiness` is ephemeral and recomputed
with the same live evaluator used for landing. `merge_admission` describes the
latest recorded exact attempt, without granting current effect authority.
`landing_outcome` independently records landed, rejected, reconciliation-required,
or not-observed state. Admission and outcome targets are classified as current or
historical against the live head/tree, or unknown when that evidence is unavailable.
GitHub observations are advisory only. `owner_judgment` is nullable for schema
compatibility and current responses return null.

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
closed. Historical landing plans are not backfilled into admissions.

New landing outcomes capture `observed_at` after the provider result or exact
reconciliation has been processed, using a clock separate from the controller
pass start. Existing immutable outcomes retain their recorded time. In the
governance view, a legacy time that predates its matching admission is labeled
"Recorded time (unqualified)"; the outcome and merge identity remain visible.

## Rolling branch identity and PR projections

Sequential landing uses the authoritative target branch SHA and tree as its
rolling base. PR detail must still report an open PR, the exact expected head,
and the expected target branch. If its base SHA projection differs, Launchplane
performs one bounded branch-identity confirmation and requires the same rolling
SHA and tree before admission. The existing identity reader may additionally
read the commit when the branch response omits its tree. This is not a polling
loop; unknown or unavailable branch evidence cannot authorize a merge.

A later stale error preserves the latest persisted landing checkpoint, including
already merged entries. Provider transport or malformed-response errors retain
their existing error types. Confirmation does not make GitHub observation and
merge atomic; response commit proof and final containment remain required.
