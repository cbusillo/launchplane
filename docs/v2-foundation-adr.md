---
title: V2 Foundation ADR
---

## Status

Accepted foundation direction, with conditional platform candidates.

Launchplane v2 proceeds as small slices merged into `main`, not as a long-lived
rewrite branch. Each slice must be coherent on its own, pass the repo gates, and
either remove obsolete v1/compatibility code or name the issue/checkpoint that
owns its removal.

## Decision Summary

- HTTP service boundary: accepted. FastAPI and Uvicorn are the v2 service
  boundary. Legacy WSGI routes remain only as a migration bridge.
- API contracts: accepted. Pydantic models and FastAPI OpenAPI output are the
  contract source for HTTP payloads and future client generation.
- Storage: accepted. SQLAlchemy ORM models plus Alembic migrations are the
  shared-service persistence path.
- Runtime authority: accepted. Postgres-backed Launchplane records, managed
  secrets, provider state, and explicit operator input own live mutable
  authority.
- Machine ingress: accepted current path. GitHub Actions OIDC remains the
  default product-workflow trust boundary.
- Human identity: conditional candidate. Keycloak is a candidate when it proves
  enough value over the current human identity/session path.
- Relationship authorization: conditional candidate. OpenFGA is a candidate when
  the relationship model proves clearer and safer than compact DB-backed policy
  records.
- Durable workflows: conditional candidate. Temporal, or a smaller comparable
  engine, is a candidate when specific workflows need durability beyond the
  current route/record pattern.
- Python runtime: compatibility target. Python 3.14 is desired only after
  dependencies, CI, deployment images, and workflow SDKs prove support.

## Foundation Direction

Launchplane v2 uses these foundation boundaries:

- FastAPI and Uvicorn for the HTTP/service boundary.
- Pydantic response and request models as the API contract source, with OpenAPI
  generated from the service app.
- SQLAlchemy ORM models plus Alembic migrations for shared-service storage.
- Postgres-backed Launchplane records and managed secrets for live mutable
  control-plane authority.
- Keycloak or a justified identity alternative only after the identity spike
  proves it solves a Launchplane need better than the current path.
- OpenFGA or a justified relationship-authorization alternative only after the
  authz spike proves it improves safety, explainability, or reviewability beyond
  the current DB-backed policy records.
- Temporal or a justified durable-workflow alternative only after named
  workflows prove they need durability beyond a smaller DB-backed worker or
  state-machine.
- Python 3.14 only after dependency, CI, deployment-image, and workflow SDK
  compatibility is proven. Until then, the runtime may remain on the latest
  compatible Python version documented by the repo gates.

FastAPI is no longer speculative. The serving-boundary slice has landed and the
legacy WSGI service is mounted only as a migration bridge. New service routes
should use FastAPI route modules unless a PR documents a bounded compatibility
exception.

The remaining HTTP work is route migration, request hardening, OpenAPI contract
coverage, and deletion of fallback routes once their replacements are proven.
The legacy WSGI app is retired when all production route families have native
FastAPI ownership, route tests cover the native paths, and no production request
needs the mounted fallback.

Candidate route-family order:

1. health, auth/session, and request/error envelope infrastructure
2. evidence ingress routes
3. merge-train, work-graph, and preview lifecycle routes
4. operator/read-model routes
5. product-config and authz-policy routes
6. driver execution and long-running mutation routes

This order is a starting point, not a second plan. A slice may move a lower-risk
route family earlier when the owning issue explains why and preserves the same
retirement discipline.

## Authority Model

The Launchplane repository is generic product code. It must not become a checked
in copy of an operator's Launchplane instance.

Code may own schemas, validators, relation models, typed request/response
contracts, migrations, fake examples, tests, and fail-closed defaults. Real
product, tenant, repository, branch, lane, domain, provider target, runtime
environment, authz grant, operator identity, route, health-check, and mutable
runtime values belong in Launchplane service records, managed secrets, identity
provider state, authorization provider state, provider runtime state, or explicit
scoped operator input.

Moving real values from Python into checked-in TOML, JSON, YAML, workflow
defaults, repo metadata, or docs examples is still a boundary violation. The
steady-state service fails closed when DB-backed or provider-backed authority is
missing.

## Why Now

The current service proved the Launchplane boundary, but it also accumulated
friction in places that should be platform primitives:

- custom WSGI routing and error shaping
- bespoke auth/session/policy wiring
- request-process daemon threads for work that needs durable recovery
- manual API/frontend contract mirroring
- mixed compatibility surfaces that are easy for future agents to revive

The v2 direction keeps the parts that worked, such as Postgres-backed records,
managed secrets, GitHub OIDC for product workflows, product drivers, and thin
repo wrappers, while moving the generic platform pieces onto clearer boundaries.

## Service Decomposition

FastAPI adoption does not by itself solve the `control_plane/service.py`
monolith. Each HTTP migration slice should extract or clarify one bounded
context instead of re-mounting the monolith in a new shape.

Use the candidate route-family order above as the default extraction order.
Within a route family, prefer extracting shared request/auth/error handling
before moving mutation-heavy driver or workflow execution code.

Implementation slices should avoid adding new v2 behavior directly to legacy
WSGI routing. If a slice must touch the legacy surface, it must explain why and
name the retirement checkpoint that removes or demotes that path.

## Prove Or Defer Gates

The preferred stack is not permission to add infrastructure by vibes. A slice
that introduces a new platform dependency must update the owning issue and this
ADR with the problem it solves, the simpler alternative considered, and the
rollback/removal posture.

### Keycloak

Before Keycloak becomes production-critical, prove which Launchplane need it
serves better than the current GitHub-based identity paths:

- non-GitHub human users
- SSO federation
- service clients and token exchange
- admin/session lifecycle
- independent operator identity and group management

The implementation must keep live users, groups, clients, grants, and session
state out of checked-in files. Bootstrap/root-of-trust wiring may be process or
provider managed; live assignments belong in provider state or Launchplane
records.

Adoption trigger: Keycloak becomes an implementation slice only after #1326
documents the concrete user/session/service-client gaps, local/dev bootstrap
ergonomics, operational owner, failure mode, and rollback posture.

### OpenFGA

Before OpenFGA replaces or augments the current DB-backed authz model, prove the
relationship shape that requires it:

- cross-resource authorization queries
- delegated product/context ownership
- explainable relationship paths
- policy reviews separate from live tuples
- authorization checks that should not be hand-coded per route

Code may own the relation schema and generated validators. Live relationship
tuples, grants, and assignments are runtime authority and must not be checked in.
If consistency windows matter for a mutation, the design must name the read-after
write behavior and fail-closed posture.

Adoption trigger: OpenFGA becomes an implementation slice only after #1327
documents the relation schema, tuple ownership, consistency expectations,
redaction/audit behavior, and why those relationships are safer outside the
current compact DB-backed policy model.

### Durable Workflows

Before Temporal or another workflow engine becomes production-critical, name the
workflows that need durability beyond a request/response route or a small
DB-backed state machine:

- long-running provider operations
- external waits and polling
- retryable fan-out
- human approval gates
- compensating rollback paths
- restart recovery after worker/process failure

The workflow engine must not become a second source of product/runtime truth.
Launchplane records remain the control-plane audit and read-model boundary;
workflow state is execution evidence and recovery machinery.

Adoption trigger: Temporal or another engine becomes an implementation slice
only after #1328 identifies concrete workflows, compares a minimal DB-backed
queue/lease/heartbeat design, and documents operational footprint, observability,
failure modes, and rollback posture.

### ORM And Migrations

New shared-service persistence should use SQLAlchemy ORM models and Alembic
migrations. Raw SQL belongs in migrations or narrow, reviewed storage helpers
only when the ORM cannot express the operation clearly.

Authority-critical invariants should move into DB constraints when the field is
used for uniqueness, active policy selection, idempotency, leases, authorization,
or cross-worker coordination. Application and Pydantic validation remain useful,
but they do not replace database-enforced integrity for shared production state.

Compatibility `ensure_schema()` behavior may exist for tests, local bootstrap,
or bounded repair, but production schema evolution is migration-led. Schema
changes should preserve rollback posture for the previous Launchplane image when
possible.

`ensure_schema()` must not become the production migration path for shared
Postgres databases.

### Pydantic And OpenAPI

Pydantic models and FastAPI OpenAPI output are part of the service boundary, not
only frontend convenience. UI-facing read models should move toward generated
TypeScript types from OpenAPI instead of manual mirrors.

The first generation slice should target a read-model route family because it is
low-risk and exposes contract drift quickly. Generated examples and schemas must
stay public-safe and must not include real product, domain, provider, authz, or
operator values.

### Python 3.14

Python 3.14 is a compatibility target, not an immediate foundation requirement.
Before changing the repo baseline, prove support for the runtime, CI images,
deployment image, `uv`, Ruff, mypy, FastAPI, Uvicorn, Pydantic, SQLAlchemy,
Alembic, psycopg, cryptography, auth libraries, and any workflow SDK selected by
issue #1328. A non-blocking CI matrix is acceptable before 3.14 becomes
required.

## Migration Rules

- Branch from current `main` for each slice.
- Keep PRs small enough for ordinary review, agent review, and auto-review.
- Link each PR to the v2 epic and the specific child issue it advances.
- Do not stack broad implementation on an already-merged slice branch.
- Update docs when behavior, APIs, config authority, operations, or ownership
  boundaries change.
- Run or update `audit-config-authority` when a slice touches checked-in docs,
  workflows, examples, repo metadata, or config-like files that could be
  mistaken for live authority.
- Treat stale v1 issues and docs as superseded until revalidated against this
  ADR and the v2 epic.
- Prefer forward fixes or replacement slices after later work depends on a
  foundation slice; use direct revert only while the slice is still isolated.

## Legacy Removal Ledger

Every v2 slice must classify its legacy impact:

- `removed`: obsolete path was deleted in the slice
- `demoted`: path remains only for local development, tests, diagnostics, or
  emergency inspection
- `unchanged`: slice does not touch a legacy surface
- `retained`: path remains production-capable and has an owning removal issue,
  condition, and dated reason

The main legacy categories are:

- WSGI fallback routes mounted behind the FastAPI app
- file-backed production mutation paths
- local CLI live-target mutations that bypass service routes
- request-process daemon threads for durable operations
- manual frontend/backend contract mirrors
- service-host env or workflow defaults used as live runtime authority
- secrets with fixed or ambiguous key ids and no rotation metadata

See [compatibility-retirement.md](compatibility-retirement.md) for the detailed
checkpoint rules.

## Rollback Posture

Small slices should preserve rollback to the previous Launchplane image whenever
possible. In practice this means:

- migrations are additive or backward-compatible unless a PR explicitly declares
  a cutover
- old route behavior is removed only after the native replacement has tests and
  live or rehearsal evidence
- heavy provider dependencies are introduced behind explicit service boundaries,
  not scattered through drivers or storage code
- the legacy fallback remains available only until route-family migration proves
  removal is safe

After later slices depend on a foundation change, prefer forward fixes or a
replacement slice over large retroactive reverts. Direct revert is best while a
slice is still isolated.

## Rejected Approaches

- A long-lived v2 mega-branch that merges at the end.
- A rewrite that breaks `main` for long periods.
- Permanent hybrid compatibility by drift.
- Checked-in product catalogs, tenant maps, domains, authz grants, provider
  topology, or operator identities.
- Replacing code hard-coding with checked-in config hard-coding.
- Adding Keycloak, OpenFGA, Temporal, or any comparable service without a named
  Launchplane problem, local/dev plan, operational owner, and rollback posture.

## Follow-Up Issues

- #1322 tracks the v2 foundation epic and issue rerouting.
- #1323 tracks this ADR and accepted decision updates.
- #1325 tracks HTTP/FastAPI route migration and legacy fallback retirement.
- #1326 tracks Keycloak identity boundary proof.
- #1327 tracks OpenFGA authorization model proof.
- #1328 tracks Temporal or comparable durable workflow proof.
- #1329 tracks ORM/Alembic invariants and Python runtime compatibility.
- #1330 tracks Pydantic/OpenAPI contract strategy.
- #1331 tracks managed secrets and key rotation.
- #1332 tracks GPT, Claude, and Gemini-family challenge review before broad
  implementation proceeds.
