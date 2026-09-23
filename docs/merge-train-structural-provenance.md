---
title: Merge Train Structural Provenance
---

Launchplane records structural candidate provenance for each merge-train batch.
The record binds the repository and base branch, base commit and tree, policy
identity, ordered PR positions, exact PR head commits and trees, every rolling
parent/head/result commit and tree, the terminal candidate
identity, and an optional proven stack-collapse root. The candidate builder and
live evaluator do not consume retired Owner or change-impact authority.

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

Legacy delta fingerprints, affected subjects and combined Owner bindings remain
optional readable fields during retirement; the evaluator does not use them.
Changes to a batch are qualified by the exact candidate, ordered head/tree
identities, recorded parent/result chain and technical checks. The live adapter
reads repository evidence immediately before each landing, without querying
Owner events or change-impact policies. Candidate no-op entries still require
exact structural containment and landing evidence.

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
