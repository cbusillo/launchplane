---
title: Post-v2 Product And Engineering Audit
---

## Purpose

This document records the baseline, findings, decisions, and execution order for
the post-v2 Launchplane reset tracked by GitHub issue `#1672`. It is a recovery
point for the workstream, not a second issue tracker. GitHub issues own current
status, blockers, and completion evidence.

## Audited Baseline

The audit baseline is exact and reproducible:

- repository revision: `80022f75b3a45fb38800445f96d9c1070a1a6663`
- merged at: July 12, 2026, 20:51 UTC
- successful CI run: `29208541146`
- successful Security run: `29208541154`
- successful CodeQL run: `29208541193`
- successful deploy run: `29208862672`, July 12, 2026, 21:02–21:06 UTC
- deployed image:
  `ghcr.io/cbusillo/launchplane@sha256:b11599cf06937ca5a1d1729602f0b67da5e6ca08cde98632de5dd03ce6c5b9b2`
- subsequent evidence on the same SHA included successful public-ingress,
  preview-lifecycle, preview-TLS, target-inspection, and merge-train runs through
  July 12, 2026, 23:35 UTC

Local baseline validation on the isolated audit worktree:

- `uv run launchplane ci unittest-shard local`: 1,866 targets, 12/12 shards
  passed in 46.6 seconds
- `pnpm --dir frontend validate`: passed after installing the locked frontend
  dependencies
- `uv run --with pip-audit pip-audit --ignore-vuln PYSEC-2025-183`: no known
  third-party vulnerability found in the resolved environment

## Scale And Change Context

V2 landed through a high-change window. Between July 2 and the audited baseline,
`main` received 267 commits including 135 merges.

Current source shape:

- production Python: 138,269 lines
- test Python: 146,489 lines
- frontend TypeScript/TSX/CSS: 10,782 lines
- `control_plane/http_app.py`: 21,353 lines and 161 registered routes, including
  101 POST/action routes
- `control_plane/storage/postgres.py`: 5,669 lines
- `tests/test_service.py`: 20,474 lines
- `frontend/src/App.tsx`: 2,522 lines
- `frontend/src/types.ts`: 1,086 lines and about 100 exported manual contracts

The size is evidence of ownership pressure, not permission for mechanical file
splitting or test deletion.

## Product Decision

Launchplane is a product operations control plane. The primary operator job is
to understand one product's testing, production, previews, configuration,
health, and next safe action without understanding provider plumbing.

Product Ops and Engineering Ops remain valuable but separate surfaces. The
current React application is placeholder scaffolding and may be discarded.
Authentication, API transport, formatting, trust-state primitives, and theme
tokens may survive only where they fit the product model. Handwritten API
contracts do not survive as authority.

See [operator-experience.md](operator-experience.md) for the accepted journeys,
information architecture, action taxonomy, diagnostics boundary, empty states,
and responsive requirements.

## Confirmed Findings

### Security And Trust

- privileged break-glass inputs are interpolated directly into Bash while
  provider credentials are present (`#1681`)
- the unauthenticated GitHub webhook buffers an unbounded body, and public
  outbound health requests do not protect resolution/redirect hops from private
  destinations (`#1682`)
- arbitrary text is silently derived into a managed-secret root key, while
  per-version key selection and root rotation are not implemented (`#1683`)
- raw child-process output can become durable worker or API detail (`#1684`)
- cookie-authenticated mutation support and the documented CSRF/origin boundary
  are inconsistent (`#1685`)
- security-sensitive action/workflow references are mutable rather than pinned
  to reviewed immutable revisions (`#1686`)

Authlib `1.7.0` is affected by advisories in authorization-server grant paths
Launchplane does not instantiate. Upgrading remains required hygiene and is
owned by `#1681`. Cookie `Secure` defaults correctly; human sessions are
PostgreSQL-backed in production; no direct OIDC/authz or webhook HMAC bypass was
found in source review.

### Mutation And Persistence

- completed-response idempotency does not reserve execution before effects;
  concurrent same-key requests can both execute (`#1688`)
- managed-secret, product-config, onboarding, cutover, and cleanup writes can
  publish partial authority graphs (`#1689`)
- several provider mutations and workflow dispatches lack durable intent,
  multi-instance fencing, and post-crash reconciliation (`#1690`)
- external notifications and dispatches have send-versus-record crash windows
  that need a transactional outbox (`#1691`)
- production schema adoption does not verify every critical PostgreSQL
  constraint, while most automated store tests use SQLite (`#1687`)
- merge-train mutation lacks a repository/base controller lease and complete
  per-step recovery state (`#1692`)
- Every Code work requests lack lease expiry, fencing, same-key worker replay,
  and stale recovery (`#1693`)

Existing patterns to reuse include transactional preview-TLS CAS/idempotency,
leased Odoo/VeriReel operations, bundled promotion/preview evidence writes, and
pending notification attempts.

### Runtime Topology And TLS

Current product reads expose a provider target and reachability symptoms but do
not join an environment to actual domains, runtime site/server, edge path, TLS
terminator, or certificate state.

- provider-neutral environment route authority: `#1694` implemented as
  DB-backed route-binding contract, storage, redacted read routes, and
  fail-closed backfill apply foundation
- TLS certificate observations: `#1695`
- product read-model projection and incident explanation: `#1696`

Desired/recorded/observed facts must remain separate. Provider-specific IDs and
addresses remain evidence and are redacted from surfaces that do not require
them. Missing neutral authority must fail closed rather than being synthesized
from provider records as reassuring truth.

### Frontend Contracts

FastAPI already emits useful typed OpenAPI, but the frontend bypasses it with a
manual endpoint catalog and type mirror.

- deterministic OpenAPI export, generated read contracts, and drift checking:
  `#1697`
- precise UI write-route response contracts and generated clients: `#1698`

Generated contracts are backend authority. A thin handwritten adapter retains
session behavior, cancellation, and UI normalization. View models remain
frontend-owned.

### Test Architecture

The suite's main problem is coupling and production fidelity, not its current
47-second local wall time.

- decouple shared support and move ordinary HTTP tests to a lifespan-aware
  supported client: `#1699`
- replace brittle workflow text mirrors with semantic security/contract
  invariants: `#1700`
- add deterministic browser smoke after the clean-slate UI exists: `#1701`

The 12-shard architecture, immutable timing snapshot, fork-hosted isolation,
fail-closed security tests, filesystem coverage, and SQLite portability tests
remain valuable. Real PostgreSQL proof is owned by `#1687`.

### Modularity

`#1048` remains the focused modularity owner. It is intentionally blocked by the
mutation and persistence audits. The first safe extraction should follow stable
contract boundaries: generated OpenAPI read contracts, read-only route-family
registration, shared response/schema helpers, then one audited domain/provider
seam. Storage and write-route extraction follows correctness proof, not file
size.

## UI Delivery Graph

The clean-slate UI remains under `#1675` and is divided into reviewable slices:

- `#1702`: product-first shell and workspace
- `#1703`: environment topology, configuration, and activity
- `#1704`: honest product actions and Engineering Ops relocation
- `#1705`: responsive, accessible, browser-reviewed cleanup

Browser smoke is tracked by `#1701`. The UI does not begin until the product
brief, topology projection, and generated read contracts are ready.

## Execution Order

1. Land immediate exposed-boundary fixes (`#1681`, `#1682`, `#1684`) and the
   real-PostgreSQL/schema proof (`#1687`).
2. Establish generated read contracts (`#1697`) and provider-neutral route
   authority (`#1694`).
3. Implement mutation reservations, transactional authority bundles, durable
   provider operations, outbox delivery, merge-train fencing, and Every Code
   recovery (`#1688`–`#1693`).
4. Add TLS observations and topology projection (`#1695`, `#1696`), then precise
   generated write contracts (`#1698`).
5. Decouple the test architecture (`#1699`, `#1700`) and perform audited
   modularity extraction (`#1048`).
6. Build and browser-review the clean-slate UI (`#1702`–`#1705`, `#1701`).
7. Complete root-key rotation and immutable action pinning (`#1683`, `#1686`),
   re-run the security, production-parity, and operator-journey reviews, and
   close `#1672` only when every confirmed finding is fixed or explicitly
   accepted with durable rationale.

## Guardrails

- Use focused PRs; do not create a post-v2 mega-branch.
- Do not combine behavior changes with mechanical module moves.
- Do not remove tests before mapping the behavior/security proof that replaces
  them.
- Do not infer runtime topology or configuration from checked-in values.
- Prefer durable operation records, transactions, leases, reconciliation, and
  outbox delivery over process-local locks or best-effort rollback.
- Update the owning issue and graph whenever evidence changes sequencing.
