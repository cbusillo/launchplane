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
  boundary. The production legacy WSGI bridge is removed.
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
- Durable workflows: accepted first path. Long-running work moves to a
  Launchplane-owned DB-backed worker queue with leases and heartbeats. Temporal
  remains a deferred candidate for workflows that outgrow that model.
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
- A Launchplane-owned DB-backed worker queue, lease, and heartbeat model for the
  current long-running operation families.
- Temporal or another workflow engine only after named workflows prove they need
  durability beyond the DB-backed worker model.
- Python 3.14 only after dependency, CI, deployment-image, and any future
  workflow SDK compatibility is proven. Until then, the runtime may remain on
  the latest compatible Python version documented by the repo gates.

FastAPI is no longer speculative. The serving-boundary slice has landed and the
production service is served directly by Uvicorn/FastAPI without mounting the
legacy WSGI service. New service routes should use FastAPI route modules unless
a PR documents a bounded compatibility exception.

The remaining HTTP work is request hardening, OpenAPI contract coverage, final
dead-code removal, and transition-doc cleanup. The production legacy WSGI mount,
test-only WSGI fallback stubs, and tests that recreate removed WSGI production
behavior are removed once native FastAPI coverage preserves the relevant
contract.

Each HTTP migration slice moves one route family at a time. The route family is
not considered migrated until the native FastAPI path owns the production route,
Pydantic models define the request and response contracts, focused OpenAPI
assertions cover the path and schema references, route tests preserve the
relevant behavior, and the PR names the fallback impact: deleted, demoted,
unchanged with a removal condition, or retained with an owning issue.
Request hardening expectations belong in the same route-family slice: JSON
content-type behavior, maximum body-size behavior, validation errors,
authentication errors, authorization errors, and the consistent `400`, `413`,
`401`, and `403` error-envelope shape where those statuses can apply.

OpenAPI examples are contract examples, not operator inventory. They may use
fake public-safe identifiers only; real product, tenant, repository, branch,
lane, domain, provider target, operator, authz grant, route, health-check, and
runtime-environment values remain service records, provider state, managed
secrets, identity or authorization provider state, or explicit scoped operator
input.

Candidate route-family order:

1. health, auth/session, and request/error envelope infrastructure
2. evidence ingress routes
3. merge-train, work-graph, and preview lifecycle routes
4. operator/read-model routes
5. product-config and authz-policy routes
6. driver execution and long-running mutation routes

This order is a starting point, not a second plan. A slice may move a lower-risk
route family earlier when the owning issue explains why and preserves the same
removal discipline.

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
- request-process daemon threads that previously handled durable work
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

Implementation slices should add new service behavior through FastAPI route
modules and delete obsolete compatibility code as the owning route family proves
its native path. Native tests, OpenAPI coverage, caller evidence, and live or
rehearsed evidence are the proof points for removing the old path; obsolete code
should not stay as a second implementation after those checkpoints pass.

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

#### #1326 Boundary Record

Issue #1326 records Keycloak as an identity-boundary candidate, not an accepted
implementation dependency. The current identity classes remain GitHub Actions
workflow subjects, GitHub browser humans, terminal-agent read subjects,
local-operator write subjects, and local-admin write subjects. A future
Keycloak slice may add OIDC human subjects and OIDC service-client subjects when
it proves a concrete Launchplane need such as non-GitHub users, federation,
service-client token exchange, or independent operator group management.

Keycloak would act as an identity provider and session/token issuer only. It
must not become product, tenant, lane, domain, provider-target, runtime,
authorization-grant, or operator-assignment authority in checked-in files. The
only repo/process-level configuration shape allowed for that future slice is
minimal Launchplane OIDC bootstrap wiring: issuer or discovery endpoint,
expected audience, Launchplane client id, client secret or managed platform
secret reference, and session-signing root. Live users, groups, realm/client
assignments, service-client grants, sessions, and operator membership remain
identity-provider state, Launchplane records, OpenFGA tuples if adopted, managed
secrets, or explicit scoped operator input.

Any future OpenFGA integration should receive normalized caller facts from the
authenticated identity, not raw provider authority. The minimum normalized
identity facts are subject type, stable subject id, issuer, audience or client
id, token expiry/freshness, optional display claims for audit, and normalized
group or role identifiers when the provider supplies them. Resource facts such
as action, product, context, instance, and resource id remain Launchplane
request and record facts, not identity-provider facts.

If Keycloak or OIDC validation is unavailable, missing, mismatched, expired, or
ambiguous, Launchplane fails closed for Keycloak-authenticated subjects. A
Keycloak outage must not silently fall back to a weaker local file, workflow
default, ambient GitHub CLI identity, or broad bootstrap policy.

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

#### #1327 Boundary Record

Issue #1327 records OpenFGA as a relationship-authorization candidate, not an
accepted runtime dependency. The current DB-backed policy records remain the
active authorization path until a later implementation slice proves tuple
parity, failure behavior, and rollback posture.

The checked-in OpenFGA surface, if adopted, may own only generic relation model
code and validators. It must not contain live tuples or real product, tenant,
repository, branch, domain, lane, provider-target, operator, client, group, or
grant values. Live tuples, grants, and assignments belong in authorization
provider state, Launchplane records, managed secrets when secret-shaped, or
explicit scoped operator input.

The candidate relation vocabulary is deliberately small:

- `subject`: normalized caller identity from the service auth boundary.
- `product`: durable Launchplane product workspace.
- `context`: product-scoped lane or runtime context.
- `provider_target`: provider-neutral target object under a context.
- `private_health_endpoint`: private monitoring endpoint under a context.
- `deployment` and `promotion`: evidence-backed operation resources.
- `agent_intent`: delegated agent intent record.
- `authz_policy`: policy administration resource.

Representative checks for the model are generic and public-safe:

- provider inspect: can `subject:example-workflow` inspect
  `provider_target:example-product/example-context/example-target`?
- private health apply: can `subject:example-operator` apply
  `private_health_endpoint:example-product/example-context/example-check`?
- private health read: can `subject:example-agent` read
  `private_health_endpoint:example-product/example-context/example-check`?
- deploy: can `subject:example-workflow` deploy
  `context:example-product/example-context`?
- promotion: can `subject:example-workflow` promote
  `promotion:example-product/example-context/example-promotion`?
- agent delegated action: can `subject:example-agent` delegate or preflight
  `agent_intent:example-product/example-context/example-intent`?
- policy/admin action: can `subject:example-admin` administer
  `authz_policy:launchplane/example-policy`?

Tuple writes must be service-owned. The native FastAPI
`POST /v1/authz-policies/*` grant and removal routes are the migration write
boundary: they can plan tuple proposals from active DB-backed grants, run
dry-run parity checks against the current DB policy result, and later write
provider tuples only after the implementation slice proves reconciliation and
rollback. Product repos, workflows, local files, and docs examples must not
write tuple state directly.

Consistency must be fail-closed. Missing, stale, ambiguous, or unreachable tuple
state denies authorization rather than falling back to broader DB grants,
checked-in defaults, local files, ambient CLI identity, or workflow defaults.
Any mutation that depends on a newly written tuple must name its read-after-write
behavior before apply. During migration, DB policy records may remain the source
for parity comparison and repair evidence; after cutover, they must not stay as
a second live authority by drift.

OpenFGA audit output should be redacted and decision-oriented. Route responses
or audit records may include subject type, resource kind, relation/action,
decision, trace id, model version, tuple source digest, and policy record id.
They must not dump raw tuple sets, provider payloads, secret values, real
operator identities, domains, or target topology.

OpenFGA is only safer than compact DB-backed policy records if it makes
delegation paths, inherited product/context relationships, provider-target
access, private-health access, and policy administration review more explicit
than per-route string matching. If the representative checks do not prove that,
the DB-backed policy model remains the simpler accepted path.

### Durable Workflows

Issue #1328 resolves the first durable workflow decision: use a minimal
Launchplane-owned DB-backed worker queue now, and defer Temporal or a comparable
workflow engine until a named workflow proves the smaller model is not enough.

The current problem is concrete and narrower than general workflow orchestration.
Odoo stable bootstrap and Odoo stable target replacement write typed Launchplane
operation records, expose poll/read boundaries, and are executed by the
supervised DB-backed worker. VeriReel async backup gate work uses the same
model: the HTTP route writes or replays a typed operation record that references
the backup evidence record, while the supervised worker owns claim, lease,
heartbeat, execution, and terminal operation state. The backup evidence record
remains the promotion/replay authority; queue records are execution mechanics.
New durable paths should use a dedicated Launchplane worker process that claims
DB-backed work, updates a lease/heartbeat, executes typed handlers, and writes
terminal records.

The DB-backed worker model owns execution mechanics only. Launchplane records
remain the control-plane audit and read-model boundary; worker queue state is not
runtime configuration, product authority, or a second source of product truth.

The first implementation target is intentionally small:

- enqueue or replay a typed operation record from the HTTP route
- claim pending work through storage-owned leases
- record `running`, phase, attempt, lease owner, lease expiry, and heartbeat
- write terminal `pass` or `fail` results; operator review is a failure
  classification, reason, read-model field, or notification until a future
  schema/API slice explicitly adds another persisted status
- recover expired leases by retrying only explicitly safe phases and otherwise
  failing closed for operator review
- preserve existing idempotency and product/context/instance single-flight
  behavior with DB constraints

Before a production worker handles leases, each typed handler must declare its
phase contract: phase name, idempotency and retry-safety, external side-effect
boundary, resume behavior after an expired lease, terminal status mapping, max
attempts, backoff, and whether an unsafe expired lease retries or fails closed.
The worker deployment must be safe for more than one process: lease claims,
heartbeats, and terminal writes are storage-owned concurrency boundaries, not
in-process locks.

Do not reintroduce request-process daemon-thread fallback after worker proof.
Proof requires the worker process to be deployed and supervised, migrations
applied, claim, heartbeat, expiry, retry, and terminal paths covered by tests,
stale active records reconciled, queue depth/stalled lease/worker health
observability in place, rollback behavior defined for worker outage, and at least
one rehearsed or real operation family completed through the worker path. Old
code is recoverable from git; production code should converge on one durable
execution path.

Temporal remains a candidate, not a dependency. Re-open that decision when a
specific Launchplane workflow needs several of these at once:

- fan-out across many provider targets with a durable join
- external waits or human approval signals inside the workflow lifecycle
- multi-system compensating rollback that must resume after worker death
- cross-worker pause, resume, cancel, or signal handling that is awkward in DB
  state
- evidence that the DB worker is accumulating bespoke timer, scheduler, or saga
  machinery that Temporal would simplify

Any future Temporal adoption must update this ADR and document operational owner,
deployment topology, Python SDK compatibility, local/dev bootstrap, failure mode
when Temporal is unavailable, rollback posture, and how Temporal history maps
back to Launchplane records without becoming product/runtime authority.

### Managed Secrets And Key Rotation

Managed secrets are accepted Launchplane authority for secret-shaped runtime and
provider values. The repository owns schemas, validators, redaction rules,
rotation contracts, and fake tests. Live secret values, bindings, provider
credentials, operator assignments, product runtime values, and key material must
stay in Launchplane records, managed secret providers, platform secrets, or
explicit scoped operator input.

The bootstrap/root-of-trust exception is intentionally narrow: Launchplane may
receive the minimal decryption root or platform-secret reference required to
decrypt DB-backed managed-secret versions before the secret store is usable.
That bootstrap surface must not become a product/runtime secret catalog,
provider credential fallback, operator assignment list, or checked-in key ring.

#### #1331 Boundary Record

Issue #1331 records the v2 managed-secret and rotation model. Launchplane
managed secrets remain the accepted implementation boundary; external Vault,
HSM, KMS, or cloud-secret-manager providers are deferred candidates until a
future slice proves a named Launchplane need, local/dev bootstrap plan,
operational owner, failure mode, and rollback posture.

Each encrypted secret version must carry stable non-secret identifiers:

- `secret_id`: the durable Launchplane secret identity.
- `version_id`: the immutable secret-value version identity.
- `encryption_key_id`: the non-secret id of the decryption root that encrypted
  the version.

`current_version_id` points at the active secret-value version. It is not the
encryption key id and must not be used as master-key rotation state. Key ids are
opaque labels, not key material, provider tokens, product values, domains,
operator identities, or topology.

Rotation introduces a new active `encryption_key_id`, keeps older key ids only
for explicit historical decrypt windows, re-encrypts active secret versions
through a Launchplane-owned rotation path, verifies readability under the new
key id, and retires the old key id. Missing, ambiguous, stale, disabled, or
unreadable secret-version/key metadata fails closed rather than trying service
env, local files, previous ciphertext, provider env dumps, workflow defaults, or
ambient operator credentials.

Plaintext exposure is exceptional and service-side. Routine APIs, CLI output,
workflows, UI, logs, and agent context may show metadata such as binding ids,
secret ids, version ids, key ids, scopes, counts, reason codes, and findings.
They must not show plaintext, ciphertext, token prefixes, provider env dumps,
or request bodies that contain secrets. Any plaintext resolution or reveal
attempt must write redacted audit evidence before or with the operation it
supports.

The legacy-removal checkpoint for this slice is clear: production-capable secret
paths with fixed or ambiguous key ids, unlabeled ciphertext, or no rotation
metadata are not compliant v2 paths. They must be re-keyed, migrated, or removed
by the implementation slice that makes managed-secret rotation production
critical.

### ORM And Migrations

New shared-service persistence should use SQLAlchemy ORM models and Alembic
migrations. Raw SQL belongs in migrations or narrow, reviewed storage helpers
only when the ORM cannot express the operation clearly.

Authority-critical invariants should move into DB constraints when the field is
used for uniqueness, active policy selection, idempotency, leases, authorization,
or cross-worker coordination. Application and Pydantic validation remain useful,
but they do not replace database-enforced integrity for shared production state.

Concrete invariant examples for new migrations:

- Record ids and natural operation ids stay unique at the database boundary.
- Active policy/config selections use DB constraints or transactional state
  changes; application checks alone are not enough.
- Idempotency records are keyed by caller scope, route path, and idempotency key.
- Worker leases store owner and expiry fields that claim/update paths check
  atomically before executing side effects.
- Single-flight operation families use DB-backed uniqueness or equivalent
  transactional guards, not in-process locks.
- Authorization grant, relationship, and assignment records keep live tuples as
  runtime authority while code owns only schemas, validators, and relation
  contracts.

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

The first contract proof targets the native FastAPI `GET /v1/health` route. It
uses an explicit Pydantic `response_model`, a stable operation id, public-safe
schema examples, and focused OpenAPI assertions that fail if the route loses its
typed response contract. This is the pattern for follow-on read-model route
families: backend Pydantic models own the contract, FastAPI emits OpenAPI, and
tests assert the small contract details that generated clients depend on without
snapshotting the whole document.

Frontend TypeScript generation remains deferred until a UI-facing route family
needs it. That future slice should consume FastAPI OpenAPI output instead of
hand-maintaining frontend mirrors. Generated examples and schemas must stay
public-safe and must not include real product, domain, provider, authz, or
operator values.

Production health traffic uses the native FastAPI route directly.

### Python 3.14

Python 3.14 is a compatibility target, not an immediate foundation requirement.
Current verdict: no-go for the required baseline; Python 3.13 remains the active
repo and deployment baseline until the gates below prove compatibility.
Before changing the repo baseline, prove support for the runtime, CI images,
deployment image, `uv`, Ruff, mypy, FastAPI, Uvicorn, Pydantic, SQLAlchemy,
Alembic, psycopg, cryptography, auth libraries, and any workflow SDK added after
a named workflow outgrows the DB-backed worker model. A non-blocking CI matrix is
acceptable before 3.14 becomes required.

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

Completed legacy categories:

- WSGI fallback routes mounted behind the FastAPI app are removed and must not be
  reintroduced.

The remaining legacy categories are:

- file-backed production mutation paths
- local CLI live-target mutations that bypass service routes
- request-process daemon threads for durable operations
- manual frontend/backend contract mirrors
- service-host env or workflow defaults used as live runtime authority
- secrets with fixed or ambiguous key ids and no rotation metadata

The v2 closeout pass must run an explicit audit and search for dead code,
including obsolete imports, helpers, tests, docs, dependencies, compatibility
stubs, and transition-only wording. Cleanup must be represented in issue-backed
plans rather than standalone repo-local plan files.

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
- removed compatibility code is not kept as an inactive rollback path

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
- #1325 tracks HTTP/FastAPI route migration and legacy fallback removal.
- #1326 tracks Keycloak identity boundary proof.
- #1327 tracks OpenFGA authorization model proof.
- #1328 tracks the DB-backed worker queue decision and Temporal deferral gates.
- #1329 tracks ORM/Alembic invariants and Python runtime compatibility.
- #1330 tracks Pydantic/OpenAPI contract strategy.
- #1331 tracks managed secrets and key rotation.
- #1332 tracks GPT, Claude, and Gemini-family challenge review before broad
  implementation proceeds.
