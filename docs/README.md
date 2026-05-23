---
title: Launchplane Docs
---

Use these docs as the source of truth for `launchplane`.

- [architecture.md](architecture.md) — ownership boundaries and system shape.
- [config-boundary.md](config-boundary.md) — bootstrap-vs-DB config authority
  and the fallback-removal target.
- [service-boundary.md](service-boundary.md) — Launchplane HTTP ingress, GitHub
  OIDC trust, and first API contracts.
- [dokploy-service-deployments.md](dokploy-service-deployments.md) — contract
  for simple image-backed services deployed through Dokploy applications.
- [new-product-repo.md](new-product-repo.md) — checklist for building a new
  website or service repo operated by Launchplane.
- [product-repo-contract.md](product-repo-contract.md) — thin product repo
  approval gate and new website repo checklist.
- [preview-workflow-contract.md](preview-workflow-contract.md) — reusable thin
  preview workflow event, idempotency, feedback, and migration contract.
- [driver-descriptors.md](driver-descriptors.md) — provider-neutral driver
  descriptor, action safety, registry, and read-model endpoint contract.
- [driver-development.md](driver-development.md) — when and how to add a new
  Launchplane driver type or product driver.
- [compatibility-retirement.md](compatibility-retirement.md) — checkpoints for
  deleting or demoting local CLI/file-backed compatibility surfaces.
- [ui-standards.md](ui-standards.md) — tenant-first Launchplane UI direction and
  review rubric.
- [operator-experience.md](operator-experience.md) — API-first product,
  environment, settings, promotion, cleanup, and UI rebuild contract.
- [work-graph-read-model.md](work-graph-read-model.md) — Code Plans/GitHub work
  graph snapshot and recommendation queue contract.
- [merge-train-policy.md](merge-train-policy.md) — repository/base-branch merge
  train policy contract, enqueue authority, and smoke-target policy.
- [runner-lane-baseline.md](runner-lane-baseline.md) — self-hosted runner lane
  baseline, Docker credential isolation, and readiness contract.
- [runner-host-hygiene.md](runner-host-hygiene.md) — report-only shared runner
  host hygiene evidence, budgets, and future apply boundary.
- [agent-context-boundary.md](agent-context-boundary.md) — public-safe agent
  context, caller profiles, scoped intent, redaction, and provenance boundary.
- [operations.md](operations.md) — operator workflows and runtime boundary rules.
- [records.md](records.md) — persisted record formats and storage policy.
- [public-readiness.md](public-readiness.md) — current blockers and exit criteria
  before making Launchplane public.
- [secrets.md](secrets.md) — Launchplane secret ownership and local contract.
- [style/python.md](style/python.md) — Python conventions.
- [style/testing.md](style/testing.md) — testing conventions.
- [policies/coding-standards.md](policies/coding-standards.md) — naming and
  code-quality guardrails.
