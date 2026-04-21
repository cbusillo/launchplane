---
title: Public Readiness
---

## Current Verdict

Do not flip `cbusillo/launchplane` public yet.

The immediate deploy incident is fixed, but the repo and its runtime contract
still carry private-operations assumptions that should be cleaned up first.

## Current Blockers

- The repo still documents Launchplane as the Odoo-specific implementation rather
  than a generic public Launchplane surface. That is accurate today, but it means a
  public move would expose product-specific internals before the public story is
  coherent.
- The live Dokploy target depends on a private git compose source plus a
  Dokploy-managed SSH key. Public visibility for the repo does not remove that
  contract automatically, and operators still need an intentional Dokploy key
  story.
- Launchplane image pulls still depend on a Dokploy-side saved GHCR credential. A
  public repo does not help if the image package remains private or if Dokploy
  is configured with stale registry credentials.
- Launchplane runtime secrets and operator catalogs remain intentionally external to
  git. That is the correct contract, but the repo must make it obvious that
  public source visibility does not imply public runtime configuration.
- Product and tenant-specific examples remain throughout the docs and config
  examples. They are acceptable for a private Odoo control-plane repo today,
  but they should be pruned or generalized before treating the repo as a public
  Launchplane reference implementation.

## Ready-To-Public Checklist

- Replace or generalize tenant-specific examples that do not need to be public
  product documentation.
- Decide whether the Launchplane GHCR package should also become public, or keep the
  repo public while documenting the private package contract explicitly.
- Document the Dokploy SSH and registry prerequisites in one operator-facing
  place, with a short failure-mode checklist.
- Keep all runtime secrets, target-id catalogs, and runtime-environment files
  outside git. Treat that as a hard invariant, not a best effort.
- Confirm the repo README tells a public reader what Launchplane is today, what is
  Odoo-specific, and what is still intentionally private operational state.

## Safe Public Posture

If the repo needs to go public before Launchplane is fully generalized, the safe
interim posture is:

- public source code
- private runtime secrets and operator catalogs
- explicit docs for private GHCR and Dokploy prerequisites
- no checked-in live environment identifiers, credentials, or rendered secret
  files

That posture is workable, but only once the docs and examples stop implying
that private operator knowledge is stored in the repo.
