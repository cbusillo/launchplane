---
title: Launchplane Docs
---

Use these docs as the source of truth for `launchplane`.

Start with the Director's [overall direction](https://github.com/cbusillo/direction/blob/main/DIRECTION.md),
then [DIRECTION.md](../DIRECTION.md) for this repository's direction, roles, stop
boundaries, and delivery path. Overall direction takes precedence; these files
win over any issue, milestone, or older page.
Some pages below still describe targets rather than live behavior; a target does
not describe runtime authority until it is implemented, deployed, and activated.

- [DIRECTION.md](../DIRECTION.md) — what Launchplane is for, who may do what, and
  where agents stop.
- [role-words.md](role-words.md) — Director, Client, the admin permission, and
  what legacy `owner` and `operator` identifiers mean.
- [architecture.md](architecture.md) — ownership boundaries and system shape.
- [reviewed-retirement.md](reviewed-retirement.md) — exact stored-setting removal and inert selected workflow-rule retirement plans.
- [config-boundary.md](config-boundary.md) — bootstrap-vs-DB config authority
  and checked-in config authority limits.
- [authorization-authority.md](authorization-authority.md) — who can do what,
  denial handling, transitional GitHub boundary, and DB-native target.
- [service-boundary.md](service-boundary.md) — Launchplane HTTP ingress, GitHub
  OIDC trust, and API contracts.
- [dokploy-inventory.md](dokploy-inventory.md) — complete value-free provider
  project/environment enumeration, pagination and placement limits.
- [dokploy-service-deployments.md](dokploy-service-deployments.md) — contract
  for simple image-backed services deployed through Dokploy applications.
- [prepared-public-serving.md](prepared-public-serving.md) — isolated prepared
  public pages/assets, writer fence/drain and measured pause capability for
  database-changing releases.
- [generic-web-two-slot-releases.md](generic-web-two-slot-releases.md) — compatible application/Compose cutover, common traffic checks and isolated recovery proof.
- [odoo-two-slot-releases.md](odoo-two-slot-releases.md) — exact-artifact Odoo
  cutover, private readiness, atomic traffic authority and restart records.
- [new-product-repo.md](new-product-repo.md) — checklist for building a new
  website or service repo operated by Launchplane.
- [product-repo-contract.md](product-repo-contract.md) — thin product repo
  approval gate and new website repo checklist.
- [artifact-provenance.md](artifact-provenance.md) — how Launchplane verifies
  which repository and commit a product's own build came from.
- [artifact-security.md](artifact-security.md) — verified-image scanning before event deploys and service-side retention dry runs.
- [release-database-compatibility.md](release-database-compatibility.md) — verified full-input release classification and targeted module-plan producer contract.
- [event-driven-deploys.md](event-driven-deploys.md) — GitHub App webhook
  receiver and reconciler that deploy verified builds to testing and previews
  without the product repository calling Launchplane.
- [testing-deploy-retry.md](testing-deploy-retry.md) — reviewed exact-artifact
  recovery of an exhausted Odoo testing retry budget with retained data.
- [preview-memory-admission.md](preview-memory-admission.md) — service-owned host
  memory floors and read-only preview-start admission.
- [service-runtime-settings.md](service-runtime-settings.md) — reviewed writes
  of declared non-secret settings on Launchplane's own service context.
- [preview-workflow-contract.md](preview-workflow-contract.md) — existing preview compatibility, data-source safety, recovery, and Client-review contract.
- [product-retirement.md](product-retirement.md) — reviewed product retirement,
  including owned previews, disposable-lane eligibility, and resumable execution.
- [retained-volume-sweep.md](retained-volume-sweep.md) — private kept-volume custody and reviewed service cleanup after retirement.
- [testing-candidate-release.md](testing-candidate-release.md) — migrated restore storage admission and releasing a failed testing candidate's storage.
- [disposable-lanes.md](disposable-lanes.md) — usage/data/isolation facts, safe
  testing writes and replacement of stranded testing operations.
- [odoo-shopify-identity.md](odoo-shopify-identity.md) — hash-only held-testing Shopify identity and applied-production comparison.
- [odoo-testing-walkthrough.md](odoo-testing-walkthrough.md) — testing task
  accounts, opaque service-owned browser sign-in, receipts and cleanup.
- [preview-parts-import.md](preview-parts-import.md) — fixed exact-preview Fishbowl parts import, grants preflight and original-key recovery.
- [preview-fixtures.md](preview-fixtures.md) — image-owned, repeatable sample data through the existing audited preview maintenance operation.
- [legacy-preview-reconciliation.md](legacy-preview-reconciliation.md) — reviewed
  reconciliation of provider-absent generic-web and Odoo preview history.
- [driver-descriptors.md](driver-descriptors.md) — provider-neutral driver
  descriptor, action safety, registry, and read-model endpoint contract.
- [driver-development.md](driver-development.md) — when and how to add a new
  Launchplane driver type or product driver.
- [ui-standards.md](ui-standards.md) — tenant-first Launchplane UI direction and
  review rubric.
- [`operator-experience.md`](operator-experience.md) — product, environment,
  Client-review, settings, promotion, cleanup, and UI delivery contract.
- [discussion-topics.md](discussion-topics.md) — product-scoped neutral topics,
  supported add/close API, and separate page/reminder delivery slices.
- [request-attention.md](request-attention.md) — original-source waiting requests,
  Supervisor routing and explicit collection coverage.
- [merge-train-policy.md](merge-train-policy.md) — repository/base-branch merge
  train policy contract, enqueue authority, and smoke-target policy.
- [merge-train-events.md](merge-train-events.md) — signed App events, worker wake hints,
  bounded controller progression and timed recovery.
- [merge-admission.md](merge-admission.md) — merge readiness, structural
  provenance, immutable per-attempt admission, truthful landing outcomes,
  append-only reconciliation and the read-only governance projection.
- [merge-train-historical-completion.md](merge-train-historical-completion.md) —
  applicability proof and atomic historical disposition without an admission.
- [runner-lane-baseline.md](runner-lane-baseline.md) — self-hosted runner lane
  baseline, Docker credential isolation, and readiness contract.
- [runner-host-hygiene.md](runner-host-hygiene.md) — shared runner host reports,
  audited maintenance, budgets, and recovery.
- [agent-context-boundary.md](agent-context-boundary.md) — public-safe agent
  context, caller profiles, scoped intent, redaction, and provenance boundary.
- [`agent-operator-contract.md`](agent-operator-contract.md) — generated,
  public-safe agent/admin operation, workflow, lifecycle, and safety contract.
- [engineering-review-runs.md](engineering-review-runs.md) — shadow-only review
  run records, dispatch binding, credential boundary, and worker lifecycle.
- [engineering-review-decisions.md](engineering-review-decisions.md) — exact-head
  classification plus independent-run evaluation and shadow GitHub projection.
- [`owner-acceptance.md`](owner-acceptance.md) — current Client review and the
  compatibility boundary for retired acceptance history.
- [odoo-website-settings.md](odoo-website-settings.md) — typed settings preparation, exact release application, rollback and nonproduction exclusion.
- [release-review.md](release-review.md) — Client checklist decisions for production
  promotion, revision changes, and recorded admin overrides.
- [privileged-operations.md](privileged-operations.md) — typed human-governed
  planning, managed-rule authorization, redaction, and future execution boundary.
- [operations.md](operations.md) — admin workflows and runtime boundary rules.
- [health-alert-drills.md](health-alert-drills.md) — isolated routing reads and
  explicitly marked, rate-limited Discord test alerts for existing lane checks.
- [production-recipient.md](production-recipient.md) — generic-web production
  recipient preparation, purpose-bound read-only sources, isolated MariaDB recovery,
  atomic Client accepted-release cutover, exact inverse and original-key continuation.
- [testing-activation.md](testing-activation.md) — qualified generic-web testing
  allocation consumption, retained custody and supported hold/rebuild continuation.
- [stored-record-preflight.md](stored-record-preflight.md) — scrubbed service export,
  isolated candidate rehearsal and ordered self-deploy gate activation.
- [lane-service-restart.md](lane-service-restart.md) — restart one Odoo or generic-web lane
  service on its current artifact through Product Ops or the bounded helper.
- [production-backup-provider.md](production-backup-provider.md) — typed
  Proxmox/PBS capture, host boundary, evidence and rollout prerequisites.
- [odoo-online-backups.md](odoo-online-backups.md) — coherent online logical
  snapshots, attachment retention and isolated restore proof.
- [records.md](records.md) — persisted record formats and storage policy.
- [public-readiness.md](public-readiness.md) — standing public-source posture,
  image and secret boundary, and ongoing public hygiene.
- [preview-settings.md](preview-settings.md) — managed preview settings, secrets, precedence and migration.
- [copy-aggregates.md](copy-aggregates.md) — read-only counts from frozen sanitized snapshots with source provenance.
- [odoo-inventory-readback.md](odoo-inventory-readback.md) — exact current testing inventory, bounded private witnesses and native purchaser facts without writes.
- [clean-testing-snapshots.md](clean-testing-snapshots.md) — independent sanitized testing snapshots, preview copies and transition prerequisites.
- [secrets.md](secrets.md) — Managed secrets, key rotation, plaintext exposure,
  and local contract.
- [credential-inventory.md](credential-inventory.md) — administrator metadata-only
  inventory, source coverage, freshness and recurring drift reports.
- [testing-local-credentials.md](testing-local-credentials.md) — Bounded external-provider
  observations, service-generated testing passwords, creation/rebuild integration
  and names-only overlap audit.
- [testing-local-consumer.md](testing-local-consumer.md) — Recipient-encrypted
  delivery of one testing bridge credential to a local client.
- [private-testing-transport.md](private-testing-transport.md) — private testing
  HTTPS/WSS planning, reviewed source installation/recovery and scoped CA trust.
- [github-actions-security.md](github-actions-security.md) — GitHub Actions
  supply-chain pinning, source classification, provenance, and update policy.
- [dependency-health-contract.md](dependency-health-contract.md) — causal
  pull-request dependency comparisons and absolute health evidence.
- [runtime-dependency-health.md](runtime-dependency-health.md) — derived lane
  dependencies, public health results, shared web helper and individual adoption.
- [sync-cycle-health.md](sync-cycle-health.md) — typed sync facts retained by
  existing health probes, their readback and qualification limits.
- [advisory-governance-checks.md](advisory-governance-checks.md) — dedicated
  GitHub App identity, stable advisory engineering/Client check runs, replay,
  drift, and authority self-exclusion.
- [style/python.md](style/python.md) — Python conventions.
- [style/testing.md](style/testing.md) — testing conventions.
- [policies/coding-standards.md](policies/coding-standards.md) — naming and
  code-quality guardrails.
- [host-program-package.md](host-program-package.md) — versioned host programs, installation and readback.
