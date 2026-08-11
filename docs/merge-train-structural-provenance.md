---
title: Merge Train Structural Provenance
---

Launchplane records structural candidate provenance for each merge-train batch.
The record binds the repository and base branch, base commit and tree, policy
identity, ordered PR positions, exact PR head commits and trees, affected
product/system subjects, every rolling parent/head/result commit and tree, the
terminal candidate identity, and an optional proven stack-collapse root.

The provenance and candidate fingerprints are canonical SHA-256 digests. A
landing-plan fingerprint separately binds the active plan while excluding
mutable landing progress. These digests are evidence identifiers, not merge or
release authority.

`evaluate_merge_train_structural_candidate` is pure and read-only. It produces
the `exact | recorded_rolling | mismatch | unknown` status consumed by merge
readiness:

- `exact` requires the active candidate and landing plan, unchanged policy and
  queue, exact head/tree evidence, known impact subjects, and the original
  recorded base.
- `recorded_rolling` additionally requires every prior plan entry to be durably
  recorded as landed at its exact head/tree, with an unbroken actual rolling
  base and result-tree chain. A proven stack-collapse root is also recorded
  rolling composition, including at position one.
- `mismatch` means available evidence contradicts the live candidate, queue,
  plan, policy, base, head/tree, impact, stack, or rolling chain.
- `unknown` means required evidence is absent or legacy records predate this
  additive contract. Old records remain readable but never default to exact.

Individually reviewed entries may compose without combined review only when
their product/system subjects are disjoint. Same-subject or impact-expanded
composition requires combined-candidate Owner evidence bound to the exact
candidate, landing plan, policy, queue heads/trees, and current subjects. The
contract does not claim behavioral or artifact equivalence, perform L3
admission, call provider effects, or persist a new record family.

Candidate construction reads immutable GitHub commit objects for the recorded
base and each PR head. It records every merge step, including GitHub `204`
already-contained responses as explicit no-op steps that preserve parent commit
and tree identity.
