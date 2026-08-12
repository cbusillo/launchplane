---
title: Launchplane Docs
---

Use these docs as the source of truth for `launchplane`.

- [architecture.md](architecture.md) — ownership boundaries and system shape.
- [config-boundary.md](config-boundary.md) — bootstrap-vs-DB config authority
  and checked-in config authority limits.
- [service-boundary.md](service-boundary.md) — Launchplane HTTP ingress, GitHub
  OIDC trust, and API contracts.
- [dokploy-service-deployments.md](dokploy-service-deployments.md) — contract
  for simple image-backed services deployed through Dokploy applications.
- [new-product-repo.md](new-product-repo.md) — checklist for building a new
  website or service repo operated by Launchplane.
- [product-repo-contract.md](product-repo-contract.md) — thin product repo
  approval gate and new website repo checklist.
- [preview-workflow-contract.md](preview-workflow-contract.md) — reusable thin
  preview workflow event, idempotency, feedback, and lifecycle contract.
- [driver-descriptors.md](driver-descriptors.md) — provider-neutral driver
  descriptor, action safety, registry, and read-model endpoint contract.
- [driver-development.md](driver-development.md) — when and how to add a new
  Launchplane driver type or product driver.
- [ui-standards.md](ui-standards.md) — tenant-first Launchplane UI direction and
  review rubric.
- [operator-experience.md](operator-experience.md) — API-first product,
  environment, settings, promotion, cleanup, and UI rebuild contract.
- [post-v2-audit.md](post-v2-audit.md) — post-v2 product, security,
  persistence, contract, test, and modularity audit baseline and execution
  graph.
- [work-graph-read-model.md](work-graph-read-model.md) — Code Plans/GitHub work
  graph snapshot and recommendation queue contract.
- [merge-train-policy.md](merge-train-policy.md) — repository/base-branch merge
  train policy contract, enqueue authority, and smoke-target policy.
- [merge-readiness.md](merge-readiness.md) — ephemeral Owner-aware L2 merge
  readiness facets, fail-closed aggregation, and live-evidence adapter boundary.
- [governance-evidence.md](governance-evidence.md) — one read-only API and
  workbench projection that keeps L1 judgment, L2 readiness, L3 admission,
  landing outcomes, and advisory observations independent.
- [merge-train-structural-provenance.md](merge-train-structural-provenance.md) —
  deterministic candidate, rolling-base, impact-subject, and landing-plan
  provenance consumed by merge readiness.
- [merge-admission.md](merge-admission.md) — immutable per-attempt Level 3
  admission, truthful landing outcomes, and append-only reconciliation.
- [runner-lane-baseline.md](runner-lane-baseline.md) — self-hosted runner lane
  baseline, Docker credential isolation, and readiness contract.
- [runner-host-hygiene.md](runner-host-hygiene.md) — report-only shared runner
  host hygiene evidence, budgets, and future apply boundary.
- [agent-context-boundary.md](agent-context-boundary.md) — public-safe agent
  context, caller profiles, scoped intent, redaction, and provenance boundary.
- [engineering-review-runs.md](engineering-review-runs.md) — shadow-only review
  run records, dispatch binding, credential boundary, and worker lifecycle.
- [engineering-review-decisions.md](engineering-review-decisions.md) — exact-head
  classification plus independent-run evaluation and shadow GitHub projection.
- [product-owner-policy.md](product-owner-policy.md) — additive shadow-mode
  product/system Owner membership, requirement, routing, and evaluation contract.
- [owner-acceptance.md](owner-acceptance.md) — shadow-only exact-change Owner
  acceptance binding, event ledger, and human-only API boundary.
- [change-impact-policy.md](change-impact-policy.md) — additive shadow-mode
  affected-product, Owner-impact, and engineering-review classification
  contract.
- [operations.md](operations.md) — operator workflows and runtime boundary rules.
- [records.md](records.md) — persisted record formats and storage policy.
- [public-readiness.md](public-readiness.md) — current blockers and exit criteria
  before making Launchplane public.
- [secrets.md](secrets.md) — Managed secrets, key rotation, plaintext exposure,
  and local contract.
- [github-actions-security.md](github-actions-security.md) — GitHub Actions
  supply-chain pinning, source classification, provenance, and update policy.
- [dependency-health-contract.md](dependency-health-contract.md) — causal
  pull-request dependency comparisons and absolute health evidence.
- [advisory-governance-checks.md](advisory-governance-checks.md) — dedicated
  GitHub App identity, stable advisory engineering/Owner check runs, replay,
  drift, and authority self-exclusion.
- [style/python.md](style/python.md) — Python conventions.
- [style/testing.md](style/testing.md) — testing conventions.
- [policies/coding-standards.md](policies/coding-standards.md) — naming and
  code-quality guardrails.
