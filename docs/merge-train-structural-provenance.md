---
title: Merge Train Structural Provenance
---

Launchplane records structural candidate provenance for each merge-train batch.
The record binds the repository and base branch, base commit and tree, policy
identity, ordered PR positions, exact PR head commits and trees, every rolling
parent/head/result commit and tree, the terminal candidate
identity, and an optional proven stack-collapse root. Impact and review facts
are intentionally supplied at evaluation time instead of being injected into
the candidate builder.

The provenance and candidate fingerprints are canonical SHA-256 digests. A
landing-plan fingerprint separately binds the active plan while excluding
mutable landing progress. These digests are evidence identifiers, not merge or
release authority.

`evaluate_merge_train_structural_candidate` is pure and read-only. It produces
the `exact | recorded_rolling | mismatch | unknown` status consumed by merge
readiness:

- `exact` requires the active candidate and landing plan, unchanged policy and
  queue, exact head/tree evidence, complete reviewed/current delta
  fingerprints, and the original recorded base.
- `recorded_rolling` additionally requires every prior plan entry to be durably
  recorded as landed at its exact head/tree, with an unbroken actual rolling
  base and result-tree chain. A proven stack-collapse root is also recorded
  rolling composition, including at position one.
- `mismatch` means available evidence contradicts the live candidate, queue,
  plan, policy, base, head/tree, impact, stack, or rolling chain.
- `unknown` means required evidence is absent or legacy records predate this
  additive contract. Old records remain readable but never default to exact.

Evaluation entries carry both reviewed and current fingerprints. Each
fingerprint binds the exact head SHA/tree, normalized changed paths, and
affected product/system subjects. The evaluator detects delta drift, changed
path overlap, same-subject composition, and impact expansion. Missing review or
change-impact evidence is `unknown`; contradictory current evidence is
`mismatch`.

Risky composition requires combined-candidate Owner evidence bound to the exact
candidate digest, landing-plan digest, policy, and evaluation entries. That
evidence must carry non-empty exact L1 Owner event IDs and their immutable
binding digests. It is explicitly non-authoritative, authorizes no merge or
other effect, and cannot be replaced by a free-text evidence ID.

#2085 is the first production caller and adapter for this pure boundary. It must
derive reviewed/current deltas and combined-candidate evidence from the current
Owner and change-impact services. #2084 supplies only the pure evaluator,
durable Git provenance, and additive record payloads. It adds no HTTP or UI
surface, no L3 admission authority, and no table or record family.

Candidate construction reads immutable GitHub commit objects for the recorded
base and each PR head. For every non-no-op merge it resolves the result commit
again by SHA, requires the candidate ref to point at that commit, and requires
its complete parent list to be exactly the prior rolling parent plus the PR
head. A response-embedded tree is never accepted as commit identity evidence.
GitHub `204` already-contained responses are explicit no-op steps whose ref,
commit, and tree preserve the parent identity.

Landing observes and stores the actual rolling-base SHA/tree, landed-head
SHA/tree, and merge-result SHA/tree on normal, retry, and already-merged crash
recovery paths. Candidate no-op entries land as `skipped`, preserve the rolling
parent as their result, and remain usable when later queue positions are
evaluated. Absent legacy landing identities remain `unknown`, not contradiction.
