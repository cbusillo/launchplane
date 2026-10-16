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
  database-changing releases; production integration remains separately tracked.
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
- [preview-workflow-contract.md](preview-workflow-contract.md) — reusable thin
  preview workflow event, idempotency, feedback, and lifecycle contract.
- [product-retirement.md](product-retirement.md) — reviewed product retirement,
  including owned previews, disposable-lane eligibility, and resumable execution.
- [disposable-lanes.md](disposable-lanes.md) — usage/data/isolation facts, safe
  testing writes and replacement of stranded testing operations.
- [odoo-testing-walkthrough.md](odoo-testing-walkthrough.md) — testing task
  accounts, opaque service-owned browser sign-in, receipts and cleanup.
- [legacy-preview-reconciliation.md](legacy-preview-reconciliation.md) — reviewed
  reconciliation of provider-absent generic-web preview history.
- [driver-descriptors.md](driver-descriptors.md) — provider-neutral driver
  descriptor, action safety, registry, and read-model endpoint contract.
- [driver-development.md](driver-development.md) — when and how to add a new
  Launchplane driver type or product driver.
- [ui-standards.md](ui-standards.md) — tenant-first Launchplane UI direction and
  review rubric.
- [`operator-experience.md`](operator-experience.md) — product, environment,
  Client-review, settings, promotion, cleanup, and UI delivery contract.
- [post-v2-audit.md](post-v2-audit.md) — post-v2 product, security,
  persistence, contract, test, and modularity audit baseline and execution
  graph.
- [work-graph-read-model.md](work-graph-read-model.md) — RETIRED (work graphs
  inside Launchplane), scheduled for deletion; see DIRECTION.md.
- [merge-train-policy.md](merge-train-policy.md) — repository/base-branch merge
  train policy contract, enqueue authority, and smoke-target policy.
- [merge-train-events.md](merge-train-events.md) — signed App events, worker wake hints,
  bounded controller progression and timed recovery.
- [merge-readiness.md](merge-readiness.md) — ephemeral machine-derived merge
  readiness facets, fail-closed aggregation, and live-evidence adapter boundary.
- [governance-evidence.md](governance-evidence.md) — one read-only API and
  workbench projection that keeps current readiness, recorded admission,
  landing outcomes, and advisory observations independent.
- [merge-train-structural-provenance.md](merge-train-structural-provenance.md) —
  deterministic candidate, rolling-base, and landing-plan
  provenance consumed by merge readiness.
- [merge-admission.md](merge-admission.md) — immutable per-attempt Level 3
  admission, truthful landing outcomes, and append-only reconciliation.
- [merge-train-historical-completion.md](merge-train-historical-completion.md) —
  applicability proof and atomic historical disposition without an admission.
- [runner-lane-baseline.md](runner-lane-baseline.md) — self-hosted runner lane
  baseline, Docker credential isolation, and readiness contract.
- [runner-host-hygiene.md](runner-host-hygiene.md) — report-only shared runner
  host hygiene evidence, budgets, and future apply boundary.
- [agent-context-boundary.md](agent-context-boundary.md) — public-safe agent
  context, caller profiles, scoped intent, redaction, and provenance boundary.
- [`agent-operator-contract.md`](agent-operator-contract.md) — generated,
  public-safe agent/admin operation, workflow, lifecycle, and safety contract.
- [engineering-review-runs.md](engineering-review-runs.md) — shadow-only review
  run records, dispatch binding, credential boundary, and worker lifecycle.
- [engineering-review-decisions.md](engineering-review-decisions.md) — exact-head
  classification plus independent-run evaluation and shadow GitHub projection.
- [`product-owner-policy.md`](product-owner-policy.md) — retired Client policy
  history and the current product-profile Client boundary.
- [`owner-acceptance.md`](owner-acceptance.md) — current Client review and the
  compatibility boundary for retired acceptance history.
- [odoo-website-settings.md](odoo-website-settings.md) — typed settings preparation, exact release application, rollback and nonproduction exclusion.
- [release-review.md](release-review.md) — Client checklist decisions for production
  promotion, revision changes, and recorded admin overrides.
- [mediaforce-releases.md](mediaforce-releases.md) — source-side macOS release
  adapter, host fencing/evidence contract, and separate activation prerequisites.
- [`owner-control-channel.md`](owner-control-channel.md) — public canonical admin
  challenge payloads, cross-host conformance artifact, and deferred runtime boundary.
- [privileged-operations.md](privileged-operations.md) — typed human-governed
  planning, managed-rule authorization, redaction, and future execution boundary.
- [change-impact-policy.md](change-impact-policy.md) — retired classifier history and the
  independent repository-evidence boundary.
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
- [lane-service-restart.md](lane-service-restart.md) — restart one Odoo lane
  service on its current artifact through Product Ops or the bounded helper.
- [production-backup-provider.md](production-backup-provider.md) — typed
  Proxmox/PBS capture, host boundary, evidence and rollout prerequisites.
- [odoo-online-backups.md](odoo-online-backups.md) — coherent online logical
  snapshots, attachment retention and isolated restore proof.
- [records.md](records.md) — persisted record formats and storage policy.
- [public-readiness.md](public-readiness.md) — standing public-source posture,
  image and secret boundary, and ongoing public hygiene.
- [secrets.md](secrets.md) — Managed secrets, key rotation, plaintext exposure,
  and local contract.
- [testing-local-consumer.md](testing-local-consumer.md) — Recipient-encrypted
  delivery of one testing bridge credential to a local client.
- [private-testing-transport.md](private-testing-transport.md) — inert planning and
  configuration readback for isolated no-website testing HTTPS/WSS listeners.
- [github-actions-security.md](github-actions-security.md) — GitHub Actions
  supply-chain pinning, source classification, provenance, and update policy.
- [dependency-health-contract.md](dependency-health-contract.md) — causal
  pull-request dependency comparisons and absolute health evidence.
- [advisory-governance-checks.md](advisory-governance-checks.md) — dedicated
  GitHub App identity, stable advisory engineering/Client check runs, replay,
  drift, and authority self-exclusion.
- [style/python.md](style/python.md) — Python conventions.
- [style/testing.md](style/testing.md) — testing conventions.
- [policies/coding-standards.md](policies/coding-standards.md) — naming and
  code-quality guardrails.
- [codex-lab-workers.md](codex-lab-workers.md) — executable selection, session
  provenance, and retirement verification.
- [every-code-feedback-resume.md](every-code-feedback-resume.md) — feedback
  continuation contracts and evidence foundation; not connected to production,
  and its plan (#2328) is closed.
