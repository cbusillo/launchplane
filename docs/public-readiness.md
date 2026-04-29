---
title: Public Readiness
---

## Current Verdict

The repo is acceptable to make public once the public-readiness scrub PR is
merged and the first public CodeQL run is reviewed.

Launchplane is still product-proving-ground code, not a polished standalone
distribution. That is acceptable for public source visibility because live
runtime authority now stays outside git in DB-backed records, managed secrets,
GitHub settings, and private product repos.

## Public Boundary

- Public source contains Launchplane code, docs, tests, workflows, schema code,
  and generic contract examples.
- Product drivers may be visible when they are part of the current implementation,
  but product-private runtime memos, target IDs, and customer-specific policy
  do not belong in this repo.
- Live Dokploy targets, runtime env, target IDs, managed secrets, and authz policy
  are DB-backed Launchplane records or bootstrap env, not checked-in files.
- GHCR package visibility is separate from source visibility. The image can be
  public when image inspection confirms no runtime secret material is baked into
  layers.

## Image And Secret Posture

The Launchplane container image should not contain runtime secrets. The Docker
build copies source, scripts, and public-safe config/docs, while `.dockerignore` excludes
runtime state and local artifacts. Secrets such as database URLs, encryption
keys, Dokploy credentials, product tokens, passwords, and SSH private keys should
remain runtime inputs or Launchplane-managed encrypted records.

The public-readiness concern is therefore not "the image has secrets baked in."
The remaining concern is keeping product-private operations in private product
repos and verifying the first public code-scanning signal after visibility
changes.

## Ready-To-Public Checklist

- Keep the checked-in CodeQL workflow enabled once the repo is public and verify
  initial code-scanning alerts are clean or tracked.
- Confirm live target identifiers and product-specific authorization policy stay
  out of checked-in config and are represented by DB-backed Launchplane records.
- Audit the built image layers before any package visibility change and confirm
  they contain no runtime secret material.
- Keep all runtime secrets, target-id catalogs, runtime-environment files, and
  product-private runtime memos outside git. Treat that as a hard invariant.
- Move product/customer-specific operational detail to private product repos when
  it is useful to preserve but not part of the shared Launchplane contract.

## Safe Public Posture

The safe public posture before Launchplane is fully generalized is:

- public source code
- private runtime secrets and operator catalogs
- public or private GHCR package visibility chosen independently from source
  visibility
- no checked-in live environment identifiers, credentials, or rendered secret
  files

That posture is workable because private operator knowledge is not stored in the
repo.
