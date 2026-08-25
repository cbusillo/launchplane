---
title: Launchplane Service Boundary
---

## Purpose

This document defines the first explicit Launchplane service boundary: the initial
HTTP ingress, the GitHub Actions OIDC trust model, the claim-to-permission
mapping, and the first stable API payloads Launchplane should accept.

It exists to keep new cross-product work aligned with Launchplane's target form:

- long-running service
- authenticated machine ingress
- Launchplane-owned drivers
- thin repo extensions

The repo-local CLI is an operator/client surface around this boundary. Local
file-backed state is allowed only for development, tests, explicitly scoped
record backfill, local rehearsal, and emergency inspection; it is not a
production persistence fallback or product/runtime config authority.

Agent-facing context and scoped write-intent rules are summarized in
[agent-context-boundary.md](agent-context-boundary.md). Keep that page aligned
with this endpoint inventory whenever agent-visible behavior changes.

Product repos should build, test, and publish product artifacts, then call this
boundary with minimal trigger facts. They should not carry Launchplane lifecycle
truth, provider mutation logic, rendered evidence payloads, or copied driver
behavior. See [product-repo-contract.md](product-repo-contract.md) for the
approval gate.

## Current Implementation Status

The service boundary is implemented and deployed for the current Odoo and
VeriReel product paths:

Service implementation ownership is split by runtime responsibility:

- `control_plane/service.py` is the stable public `serve_launchplane_service`
  entrypoint only.
- `control_plane/service_bootstrap.py` owns environment-derived identity
  configuration, policy and provider composition, OAuth/session wiring,
  FastAPI application construction, startup validation, Uvicorn execution, and
  store cleanup.
- `control_plane/every_code_github_webhook.py` owns the Every Code GitHub
  webhook store protocol, signature verification boundary, payload parsing,
  actor trust, deduplication, issue and pull-request closure, preview
  validation, and PR-feedback handling.
- `control_plane/http_app.py` owns FastAPI composition, dependency injection,
  remaining core and mutation route registration, and the webhook callable used
  by the unauthenticated GitHub route. Domain modules under
  `control_plane/http_routes/` own extracted read handlers and registrations,
  plus the dependency-explicit evidence-ingress and Generic Web write
  registrars.
- `control_plane/drivers/native_routes.py` owns native descriptor metadata,
  handler authorization binding, and FastAPI driver-route validation.

Startup validates native descriptor paths, methods, authorization actions, and
route uniqueness before opening shared storage or seeding durable authorization
policy. Native FastAPI registration binds the descriptor metadata to each driver
handler, and the post-construction validation rejects missing or duplicate
routes, method drift, and handler/descriptor authorization drift before Uvicorn
starts. After storage opens, policy resolution, application construction,
FastAPI route validation, Uvicorn startup, and all failure exits share one
cleanup scope so the store is always closed.

- CLI: `uv run launchplane service serve`
- server runtime: FastAPI served directly by Uvicorn
- native FastAPI health route: `GET /v1/health`, backed by a Pydantic response
  model and included in OpenAPI as a service contract proof
- authorization recovery: browser-human lifecycle under
  `/v1/authorization-recovery/*` and credential-free total-lockout
  `/v1/authorization-recovery/public/{prepare,challenges,apply}` operations.
  Every response and error for this family is `Cache-Control: no-store`.
  Public apply is authorized exclusively by the exact bounded hardware SSHSIG;
  it does not accept a bearer, session cookie, policy payload, or generic
  execution request.
- native FastAPI Launchplane service runtime reads:
  - `GET /v1/service/runtime`, requiring `launchplane_service.read` for the
    Launchplane service context and returning runtime metadata only
  - `GET /v1/service/odoo-workers/status`, requiring
    `launchplane_service.read` for the Launchplane service context and returning
    Odoo operation worker queue counters without request payloads
  - `GET /v1/service/verireel-workers/status`, requiring
    `launchplane_service.read` for the Launchplane service context and returning
    VeriReel backup-gate operation worker queue counters without request
    payloads
- native FastAPI Odoo operation status reads:
  - `GET /v1/drivers/odoo/stable-bootstrap/operations/{operation_id}`,
    requiring `odoo_stable_bootstrap.execute` for the stored operation product
    and context
  - `GET /v1/drivers/odoo/target-replacement/operations/{operation_id}`,
    requiring `odoo_target_replacement_apply.execute` for the stored operation
    product and context
- native FastAPI protected artifact inventory route:
  - `GET /v1/artifacts/protected`, requiring `artifact_protection.read` for
    the requested product and either the requested context or whole-product
    wildcard context
- native FastAPI driver descriptor discovery routes:
  - `GET /v1/drivers`, requiring `driver.read` for the Launchplane discovery
    context
  - `GET /v1/drivers/{driver_id}`, requiring `driver.read` for the Launchplane
    discovery context
- native FastAPI edge endpoint record reads:
  - `GET /v1/edge-endpoints/records`, requiring `edge_endpoint.read` for the
    Launchplane service context and supporting `provider`, `status`, and
    bounded `limit` filters
  - `GET /v1/edge-endpoints/records/{endpoint_key}`, requiring
    `edge_endpoint.read` for the Launchplane service context
- native FastAPI private health endpoint record reads:
  - `GET /v1/private-health-endpoints/records`, requiring
    `private_health_endpoint.read` for the requested product/context and
    supporting `instance`, `status`, and bounded `limit` filters
  - `GET /v1/private-health-endpoints/records/{endpoint_key}`, requiring
    `private_health_endpoint.read` for the requested product/context and
    returning 404 when the stored record is outside that query scope
- native FastAPI ingress canary route record reads:
  - `GET /v1/ingress/canary-routes/records`, requiring
    `ingress_canary_route.read` for the Launchplane service context and
    supporting `product`, `context`, `status`, and bounded `limit` filters
  - `GET /v1/ingress/canary-routes/records/{canary_key}`, requiring
    `ingress_canary_route.read` for the Launchplane service context
- native FastAPI ingress canary route apply writes:
  - `POST /v1/ingress/canary-routes/records/apply`, requiring
    `ingress_canary_route.apply` for the Launchplane service context and
    accepting `dry-run` without an `Idempotency-Key`; `apply` requires an
    `Idempotency-Key`
  - `POST /v1/ingress/canary-routes/apply`, requiring `ingress_route.apply` for
    the requested product/context, resolving the stored canary route and edge
    endpoint before provider apply, and requiring an `Idempotency-Key`
- native FastAPI environment route-binding reads and reconciliation:
  - `GET /v1/route-bindings/records`, requiring context-scoped
    `route_binding.read` for an unfiltered product/context list or instance-
    scoped authority when an `instance` filter is supplied, and supporting
    `status` and bounded `limit` filters
  - `GET /v1/route-bindings/records/current`, requiring `route_binding.read` for
    the requested product/context/instance tuple
  - `POST /v1/route-bindings/reconcile`, requiring instance-scoped
    `route_binding.read` for `dry-run` and `route_binding.apply` for `apply`.
    The request must explicitly
    expect either an absent binding or the opaque SHA-256 returned by the current
    record read. The service derives topology only from DB-backed provider-target,
    tracked Dokploy target and target-id, edge-endpoint, and terminal ingress
    audit records. The terminal audit's exact `edge_endpoint_key` joins the
    route to its active edge-endpoint record; provider project/display names do
    not substitute for that persisted relationship. It returns create,
    unchanged, evidence-refresh, blocked, or
    authority-conflict findings without accepting caller-supplied domains or
    provider identifiers. `dry-run` does not require an `Idempotency-Key`;
    `apply` does. Apply compare-and-writes the full expected record and commits
    create, refresh, or unchanged no-op plus completed replay evidence in one
    PostgreSQL transaction. Per-binding transaction locks serialize absent-row
    creates and current-row refreshes. Filesystem-backed service apply fails
    closed because it cannot provide that atomic boundary; filesystem storage
    remains available for explicit local rehearsal.
  - `POST /v1/route-bindings/odoo-testing/controller/run-once`, requiring the
    service-scoped `route_binding.odoo_testing_refresh.plan` or
    `route_binding.odoo_testing_refresh.apply` action and then exact-instance
    `route_binding.read` or `route_binding.apply` authority for every discovered
    target. The request contains no product/context/instance selectors. The
    service selects at most 25 DB-backed Odoo profile lanes whose instance is
    exactly `testing` and whose matching binding is active and service-owned.
    It never creates bindings, never selects external authority, and cannot
    select production. All target authorization completes before writes; apply
    first reserves the parent controller key, then serializes due evidence
    refreshes through the existing per-binding PostgreSQL CAS/idempotency
    boundary, and compare-and-completes the parent response after the child loop.
    Dry-run is stateless. Apply requires an `Idempotency-Key` and exact
    confirmation text.
  - `POST /v1/route-bindings/external/reconcile`, requiring exclusively
    instance-scoped `route_binding.external.plan` for `dry-run` and
    `route_binding.external.apply` for `apply`. The request supplies only the
    product/context/instance tuple, desired active or disabled authority state,
    expected-current digest, provenance label, reason, and confirmation. The
    service derives public HTTPS domains and provider placement from DB-backed
    product-profile and provider-target records, requires a strict public
    runtime-identity check, and records an operator-owned external edge with
    external TLS ownership. It does not call or claim internal evidence from an
    external proxy. Apply reuses the same atomic PostgreSQL CAS/idempotency
    boundary as managed reconciliation. Setting `desired_status = "disabled"`
    explicitly relinquishes external authority; only then may managed reconcile
    replace it with service-owned provider evidence.
- native FastAPI ingress route apply write:
  - `POST /v1/drivers/ingress/route-apply`, requiring `ingress_route.plan` for
    `dry-run` and `ingress_route.apply` for `apply`, resolving optional edge
    endpoint records before provider execution, writing ingress route audit
    records, using the bearer/OIDC write identity path, and requiring an
    `Idempotency-Key` only for `apply`. Requests may name an exact instance;
    those calls require instance-scoped authority and DB-backed lane-domain
    validation. Exact-instance apply is limited to a reviewed existing-host
    no-op with an edge-endpoint record and cannot create or mutate provider
    routes. A lane domain may be a subset of a shared provider host: the service
    compares the full live host internally for no-op and drift checks while the
    persisted audit keeps `requested_domains` scoped to the authorized lane.
    Audit operations, response operations, and proxy-host evidence are redacted
    to that lane. Response metadata records full-host comparison and the
    provider-domain count without disclosing sibling domain names or granting
    route authority over them.
- native FastAPI Dokploy target inspect read:
  - `GET /v1/dokploy-targets/inspect`, requiring `dokploy_target.inspect` for
    the Launchplane service context and returning redacted provider identity
    evidence only
- native FastAPI deployment, promotion, preview, inventory, operations, and
  managed-secret status reads:
  - `GET /v1/deployments/{record_id}`, requiring `deployment.read` for the
    stored record context
  - `GET /v1/promotions/{record_id}`, requiring `promotion.read` for the stored
    record context
  - `GET /v1/previews/{preview_id}`, requiring `preview.read` for the stored
    preview context
  - `GET /v1/previews/{preview_id}/history`, requiring `preview.read` for the
    stored preview context
  - `GET /v1/inventory/{context}/{instance}`, requiring `inventory.read` for
    the stored inventory context
  - `GET /v1/contexts/{context}/operations/recent`, requiring
    `operations.read` for the path context
  - `GET /v1/contexts/{context}/secrets`, requiring `secret.list` for the path
    context
  - `GET /v1/contexts/{context}/instances/{instance}/secrets`, requiring
    `secret.list` for the path context
  - `GET /v1/secrets/{secret_id}`, requiring `secret.read` for the stored
    secret context
  - `GET /v1/products/{product}/environments/{environment}/public-ingress/incidents`,
    requiring `product_environment.read` for the profile-resolved stable lane
    and returning a bounded list of lane-owned incident occurrences
  - `GET /v1/products/{product}/environments/{environment}/public-ingress/incidents/{incident_id}`,
    requiring `product_environment.read` for the profile-resolved stable lane
    and returning typed observation, event, reminder, notification-attempt, and
    outbox-delivery evidence without raw notification or provider internals
- authenticated evidence routes:
  - `POST /v1/products/public-ingress-monitor/run-once` (native FastAPI for
    bearer-token callers, with Pydantic/OpenAPI contract coverage,
    idempotency replay preservation, and no legacy `GET` route). The accepted
    result returns every observation plus changed incident records, durable
    incident events, per-policy reminder state, and direct delivery attempts.
    Equivalent failed observations remain in `records` but do not appear as a
    new event; `reminder` events identify their exact bounded policy window.
  - Native `POST /v1/evidence/*` ingress routes reject non-JSON media types with
    the Launchplane `400 invalid_request` envelope; they require bounded,
    non-chunked `Content-Length` headers and enforce the same 2 MiB byte ceiling
    while reading the request stream, returning the Launchplane
    `413 request_entity_too_large` envelope before route-specific storage
    mutation.
  - `POST /v1/evidence/backup-gates` (native FastAPI for bearer-token callers,
    with Pydantic/OpenAPI contract coverage and idempotency replay preservation)
  - `POST /v1/evidence/deployments` (native FastAPI for bearer-token callers,
    with Pydantic/OpenAPI contract coverage and idempotency replay preservation)
  - `POST /v1/evidence/promotions` (native FastAPI for bearer-token callers,
    with Pydantic/OpenAPI contract coverage and idempotency replay preservation)
  - `POST /v1/evidence/previews/generations` (native FastAPI for bearer-token
    callers, with Pydantic/OpenAPI contract coverage, idempotency replay
    preservation, and bundled preview/generation evidence storage)
  - `POST /v1/evidence/previews/destroyed` (native FastAPI for bearer-token
    callers, with Pydantic/OpenAPI contract coverage, idempotency replay
    preservation, and preview destroyed storage)
  - `POST /v1/evidence/runner-host-hygiene/audits` (native FastAPI for
    bearer-token callers, with Pydantic/OpenAPI contract coverage, idempotency
    replay preservation, and runner-host hygiene audit storage)
  - `GET /v1/evidence/runner-host-hygiene/audits` (bounded runner-host hygiene
    audit summaries for authorized bearer-token callers)
  - `GET /v1/evidence/runner-host-hygiene/audits/record` (one sanitized audit
    projection selected by `audit_record_key`)
  - `GET /v1/evidence/runner-host-hygiene/history` (bounded timestamped
    pre/post cache telemetry history for one runner host)
  - `POST /v1/evidence/runner-lane-registration/audits` (native FastAPI for
    bearer-token callers, with Pydantic/OpenAPI contract coverage, idempotency
    replay preservation, and runner-lane registration audit storage)
- product profile routes:
  - `GET /v1/product-profiles` (native FastAPI for bearer-token,
    human-session, and Every Code worker-token callers)
  - `GET /v1/product-profiles/{product}` (native FastAPI for bearer-token and
    human-session callers)
  - `POST /v1/product-profiles` (native FastAPI for bearer-token callers,
    product-profile write-contract validation, record storage, and optional
    `Idempotency-Key` replay/conflict handling)
  - `POST /v1/product-profiles/health-monitoring/apply` (native FastAPI for
    exact-lane reviewed public-check planning, profile compare-and-write, and
    apply-only atomic idempotency enforcement)
  - `POST /v1/product-profiles/prelaunch-rebuild/apply` (native FastAPI for
    exact-lane reviewed Odoo prelaunch-rebuild policy planning, profile
    compare-and-write, and apply-only atomic idempotency enforcement)
  - `POST /v1/product-profiles/preview-tls/apply` (native FastAPI for
    Launchplane-operator workflow callers, DB-backed dry-run/apply planning,
    reviewed-plan continuity, and apply-only idempotency enforcement)
- product config write route:
  - `POST /v1/product-config/apply` (native FastAPI for GitHub Actions OIDC,
    signed-in GitHub human sessions, and local-operator bearer callers, with
    DB-backed storage, redacted planning/apply behavior, local-operator dry-run
    continuity, and optional `Idempotency-Key` replay/conflict handling)
- runtime key-safety policy route:
  - `POST /v1/runtime-key-safety/policies/apply` (native FastAPI for
    bearer-token callers, DB-backed storage, metadata-only policy writes, and
    optional `Idempotency-Key` replay/conflict handling)
- product onboarding route:
  - `POST /v1/product-onboarding/apply` (native FastAPI for bearer-token
    callers, DB-backed onboarding records, optional `Idempotency-Key`
    replay/conflict handling, and sanitized onboarding evidence)
- Dokploy target setup route:
  - `POST /v1/dokploy-targets/setup` (native FastAPI for bearer-token
    callers, DB-backed setup records, apply-only `Idempotency-Key`
    replay/conflict handling, and repeatable dry-runs)
- provider-target operation route:
  - `POST /v1/provider-targets/operations` (native FastAPI for bearer-token
    callers, DB-backed audit/backfill records, apply-only `Idempotency-Key`
    replay/conflict handling, and repeatable audits/dry-runs)
- public ingress notification policy route:
  - `POST /v1/public-ingress/notification-policies/apply` (native FastAPI for
    bearer-token callers, DB-backed storage, local-operator reason enforcement,
    and optional `Idempotency-Key` replay/conflict handling)
- authz policy administration routes:
  - `GET /v1/authz-policies/active`
  - `GET /v1/authz-diagnostics/active-policy/health`
  - `POST /v1/authz-diagnostics/candidate-policy/preview`
  - `POST /v1/authz-policies/managed-rule-sets/reconcile`
    (native FastAPI for bearer-token and signed-in GitHub human-session
    callers and DB-backed policy records; managed-rule-set dry-runs return a
    reviewed plan digest, while apply atomically couples policy CAS and
    `Idempotency-Key` completion. Managed reconciliation is the sole policy
    write contract and requires immutable numeric `repository_id` and
    `repository_owner_id` selectors for GitHub Actions rules.)

- Every Code local automation work-request routes:
  - `GET /v1/every-code/summary` (native FastAPI for bearer-token,
    human-session, and Every Code worker-token callers)
  - `GET /v1/previews/readiness` (native FastAPI for bearer-token,
    human-session, and Every Code worker-token callers)
  - `GET /v1/every-code/work-requests` (native FastAPI for bearer-token,
    human-session, and Every Code worker-token callers)
  - `GET /v1/every-code/work-requests/{request_id}` (native FastAPI for
    bearer-token, human-session, and Every Code worker-token callers)
  - `GET /v1/every-code/pr-feedback` (native FastAPI for bearer-token,
    human-session, and Every Code worker-token callers)
  - `GET /v1/every-code/preview-gates` (native FastAPI for bearer-token,
    human-session, and Every Code worker-token callers)
  - `GET /v1/every-code/notification-attempts` (native FastAPI for
    bearer-token, human-session, and Every Code worker-token callers)
  - `GET /v1/previews/pr-feedback/notification-attempts` (native FastAPI for
    bearer-token and human-session callers)
  - `POST /v1/every-code/notification-policies/apply` (native FastAPI for
    bearer-token callers, DB-backed storage, local-operator reason enforcement,
    and optional `Idempotency-Key` replay/conflict handling)
  - `POST /v1/every-code/github-webhook` (native FastAPI,
    unauthenticated GitHub HMAC verification, signed-event skip semantics,
    Every Code work-request creation/dedupe, issue and pull-request close
    handling, preview validation comments, and PR-feedback ingestion)
  - `POST /v1/every-code/work-requests/create` (native FastAPI for
    bearer-token callers, `every_code_work_request.write` authorization on
    `launchplane`/`launchplane`, record-store write capability checks, and
    optional `Idempotency-Key` replay/conflict handling)
  - `POST /v1/every-code/work-requests/claim` (native FastAPI for Every Code
    worker-token callers and bearer-token callers with
    `every_code_work_request.claim`, record-store claim capability checks,
    `404 not_found` for missing requests, `409 work_request_already_claimed`
    for non-queued requests, and bearer or worker-token `Idempotency-Key`
    replay/conflict handling; PostgreSQL commits the claim and completed replay
    evidence atomically)
  - `POST /v1/every-code/work-requests/heartbeat` (native FastAPI for Every Code
    worker-token callers and authorized bearer callers, extending the lease only
    when host and fencing token still match the active record)
  - `POST /v1/every-code/work-requests/recover-stale` (native FastAPI for Every
    Code worker-token callers and bearer-token callers with
    `every_code_work_request.update`, using a locked stale-snapshot compare to
    requeue bounded attempts or block for manual review)
  - `POST /v1/every-code/work-requests/status` (native FastAPI for Every Code
    worker-token callers and bearer-token callers with
    `every_code_work_request.update`, replay-before-write idempotency handling,
    record-store status capability checks, exact fencing-token enforcement for
    leased requests, `404 not_found` for missing requests, and
    blocked-notification delivery)
  - `POST /v1/every-code/work-requests/rerun` (native FastAPI for Every Code
    worker-token callers and bearer-token callers with
    `every_code_work_request.rerun`, approved `every_code_rerun` write-intent
    evidence, workflow replay-before-write idempotency handling, record-store
    rerun capability checks, `404 not_found` for missing requests, terminal-only
    compare-and-write requeue semantics, and atomic replay evidence for bearer
    and worker-token callers)
  - `POST /v1/every-code/pr-feedback` (native FastAPI for Every Code
    worker-token callers, direct PR-feedback record writes, and DB-backed
    storage capability enforcement without idempotency state)
  - `POST /v1/every-code/pr-feedback/status` (native FastAPI for Every Code
    worker-token callers, PR-feedback status transitions, `404 not_found` for
    missing feedback, and `409 feedback_already_final` for already-final
    feedback)
  - `POST /v1/every-code/preview-gates` (native FastAPI for Every Code
    worker-token callers, direct preview-gate record writes, and DB-backed
    storage capability enforcement without idempotency state)
- preview PR feedback notification policy route:
  - `POST /v1/previews/pr-feedback` (native FastAPI for bearer-token callers,
    `preview_pr_feedback.write` or matching lifecycle authorization,
    preview PR feedback write-capable storage, optional `Idempotency-Key` replay/conflict
    handling, and preview PR feedback notification delivery attempts)
  - `POST /v1/previews/pr-feedback/remediation` (local-operator/admin bearer
    identities only, distinct plan/apply authorization, exact product/context/
    repository/PR binding, durable dry-run evidence, reviewed-state continuity,
    idempotent apply, and Launchplane-owned marker plus author verification)
  - `POST /v1/previews/pr-feedback/notification-policies/apply` (native FastAPI
    for bearer-token callers, DB-backed storage, explicit product/context scope,
    local-operator reason enforcement, and optional `Idempotency-Key`
    replay/conflict handling)

No GitHub Actions identity is authorized through the remediation route. An
apply requires the same `Idempotency-Key` as its matching dry-run, an exact
confirmation phrase, a non-empty reason and related issue, and an unchanged
managed-comment observation. If the managed comment is already absent, apply
records `already_absent` without claiming a GitHub mutation.

- work graph chooser route:
  - `GET /v1/agent/context`
  - `GET /v1/repo-product-mapping`
  - `GET /v1/work-graph/snapshot` (native FastAPI)
  - `GET /v1/work-graph/github/issues` (native FastAPI)
  - `GET /v1/work-graph/merge-train/policy-targets` (native FastAPI)
  - `GET /v1/work-graph/merge-train/admission` (native FastAPI)
  - `GET /v1/work-graph/merge-train/controller/status` (native FastAPI)
  - `POST /v1/work-graph/rank` (native FastAPI)
  - `POST /v1/work-graph/github/issues/reconcile` (native FastAPI)
  - `POST /v1/work-graph/merge-train/run-once` (native FastAPI)
  - `POST /v1/work-graph/merge-train/batch-candidate/run-once` (native FastAPI)
  - `POST /v1/work-graph/merge-train/batch-landing/run-once` (native FastAPI)
  - `POST /v1/work-graph/merge-train/stack-collapse/run-once` (native FastAPI)
  - `POST /v1/work-graph/merge-train/pr-feedback` (native FastAPI)
  - `POST /v1/work-graph/merge-train/controller/run-once` (native FastAPI)
- product driver routes:
  - `POST /v1/drivers/launchplane/self-deploy` (native FastAPI)
  - `POST /v1/drivers/generic-web/deploy` (native FastAPI)
  - `POST /v1/drivers/generic-web/prod-promotion` (native FastAPI)
  - `POST /v1/drivers/generic-web/prod-promotion-workflow` (native FastAPI)
  - `POST /v1/drivers/generic-web/prod-rollback-plan` (native FastAPI)
  - `POST /v1/drivers/generic-web/prod-rollback` (native FastAPI)
  - `POST /v1/drivers/generic-web/stable-verification` (native FastAPI)
  - `POST /v1/drivers/generic-web/preview-desired-state` (native FastAPI)
  - `POST /v1/drivers/generic-web/preview-refresh` (native FastAPI)
  - `POST /v1/drivers/generic-web/preview-inventory` (native FastAPI)
  - `POST /v1/drivers/generic-web/preview-readiness` (native FastAPI)
  - `POST /v1/drivers/generic-web/preview-verification` (native FastAPI)
  - `POST /v1/drivers/generic-web/preview-destroy` (native FastAPI)
  - `POST /v1/drivers/odoo/artifact-publish-inputs` (native FastAPI)
  - `POST /v1/drivers/odoo/artifact-publish` (native FastAPI)
  - `POST /v1/drivers/odoo/stable-bootstrap` (native FastAPI)
  - `GET /v1/drivers/odoo/stable-bootstrap/operations/{operation_id}`
    (native FastAPI)
  - `GET /v1/drivers/odoo/target-replacement/operations/{operation_id}`
    (native FastAPI)
  - `POST /v1/drivers/odoo/post-deploy` (native FastAPI)
  - `POST /v1/drivers/odoo/app-maintenance` (native FastAPI)
  - `POST /v1/drivers/odoo/config-parameter-override` (native FastAPI)
  - `POST /v1/drivers/odoo/website-bootstrap-override` (native FastAPI)
  - `POST /v1/drivers/odoo/target-replacement-plan` (native FastAPI)
  - `POST /v1/drivers/odoo/target-replacement-apply` (native FastAPI)
  - `POST /v1/drivers/odoo/preview-apply-inputs` (native FastAPI)
  - `POST /v1/drivers/odoo/preview-apply` (native FastAPI)
  - `POST /v1/drivers/odoo/prod-backup-gate` (native FastAPI)
  - `POST /v1/drivers/odoo/prod-promotion-inputs` (native FastAPI)
  - `POST /v1/drivers/odoo/prod-promotion-run` (native FastAPI)
  - `POST /v1/drivers/odoo/prod-promotion` (native FastAPI retained operator route)
  - `POST /v1/drivers/odoo/prod-rollback` (native FastAPI)
  - `POST /v1/drivers/verireel/testing-deploy` (native FastAPI)
  - `POST /v1/drivers/verireel/testing-verification` (native FastAPI)
  - `POST /v1/drivers/verireel/stable-environment` (native FastAPI)
  - `POST /v1/drivers/verireel/app-maintenance` (native FastAPI)
  - `POST /v1/drivers/verireel/prod-deploy` (native FastAPI)
  - `POST /v1/drivers/verireel/prod-backup-gate` (native FastAPI)
  - `POST /v1/drivers/verireel/prod-promotion` (native FastAPI)
  - `POST /v1/drivers/verireel/prod-rollback` (native FastAPI)
  - `POST /v1/drivers/verireel/preview-refresh` (native FastAPI)
  - `POST /v1/drivers/verireel/preview-inventory` (native FastAPI)
  - `POST /v1/drivers/verireel/preview-destroy` (native FastAPI)

## Service Route Checklist

New or changed service route families must preserve the completed HTTP boundary:

- FastAPI owns the production path.
- Pydantic request and response models define the HTTP contract; use
  `extra="forbid"` for boundary models unless the route documents a narrower
  reason not to.
- OpenAPI coverage asserts the path, method, stable `operation_id`, primary
  response schema reference, and declared error-envelope responses. Keep tests
  focused; do not snapshot the whole OpenAPI document.
- Public-safe examples are fake and generic. They must not contain real product,
  tenant, repository, branch, lane, domain, provider target, operator, authz,
  route, health-check, or runtime-environment authority.
- Request hardening is named for the route family: JSON content-type behavior,
  maximum body-size behavior, validation failures, authentication failures,
  authorization failures, and expected `400`, `413`, `401`, and `403` envelope
  shape where those statuses can apply.
- Route tests preserve relevant behavior directly through service helpers.
- The PR deletes obsolete compatibility code when it replaces an old surface, or
  names the issue-backed removal condition when deletion is not in scope.

`GET /v1/health` is the first proven pattern: native FastAPI route ownership,
typed Pydantic response, and focused OpenAPI assertions. Use it as the small
contract shape for future route-family slices.

Read-only ingress, topology, operational-record, managed-secret, product,
driver, preview, work-graph, merge-train, and Every Code route families, plus
the seven evidence-ingress writes and twelve Generic Web provider writes, are
registered from dependency-explicit modules under `control_plane.http_routes`.
`http_app.py` remains the composition root: it passes frozen dependency objects
and callables into the registrars, and each registrar calls the Launchplane
FastAPI app's custom `add_api_route` directly so shared error-schema handling and
route order remain centralized without an `APIRouter` compatibility layer.
`control_plane.http_routes.mutation_support` owns accepted-response
serialization, caller idempotency scope, canonical request fingerprints, store
capability detection, and the existing general replay serializer. Notification
replay remains separate until a later behavior-focused change can prove a safe
unification.
Route families with intervening write or UI registrations expose multiple
registrar entrypoints rather than being reordered. Generic Web uses one frozen
dependency object and handler set for its preview/deploy/promotion/verification
block and its later rollback pair, preserving the audited global route order.
Product environment config-status remains the final API route registered
immediately before the exception handlers. Core auth, session, service-status,
and mutation handlers not covered by a proven registrar remain in the
composition root until their own domain boundary is proven.

Frontend contract generation uses the same boundary. Run
`uv run launchplane service export-openapi --output frontend/generated/openapi-canonical.json`
to write the canonical OpenAPI document from `create_launchplane_fastapi_app`
without live credentials, managed-secret values, or runtime-authority examples.
The frontend then derives the checked `frontend/generated/openapi-ui.json` slice
and checked `frontend/src/generated/openapi.ts/` types from that canonical
export. `pnpm --dir frontend check:openapi-drift` regenerates those artifacts in
temporary paths and fails when the checked schema or generated types drift from
the backend contract. The canonical `x-launchplane-ui-read-operations` and
`x-launchplane-ui-write-operations` manifests own each selected GET or accepted
browser-safe POST path and stable operation id; slicing fails closed when a
route, method, operation id, success response, or referenced schema drifts. The
write slice currently covers work-graph ranking, product-config dry-run/apply,
generic-web promotion dry-runs, and generic-web promotion workflow dispatches.
It also covers browser-human privileged-operation approval and revocation; the
service-internal execution path is intentionally absent.
Issue reconciliation remains a bearer-only service operation rather than a
browser client binding. Generated request, success, validation, and error
bindings are the API boundary consumed by the UI. Handwritten frontend types
remain only for UI view models and explicit normalization.

The same canonical export also feeds the narrower checked
`contracts/agent-operator-contract.json` artifact. Run
`uv run launchplane service export-agent-contract --output contracts/agent-operator-contract.json`
to project only the explicit agent/operator allow-list plus the semantic
lifecycle, deploy, reconciliation, workflow, and governance overlay. The
artifact carries a semantic digest and non-gating source revision. The existing
`pnpm --dir frontend check:openapi-drift` gate regenerates it in a temporary
path and compares only its normalization version and semantic digest, so
unrelated OpenAPI or provenance-only changes do not create consumer drift.

The browser client keeps every accepted browser write path in one generated-type-checked
allowlist. The shared mutation transport serializes cookie-backed writes,
refreshes the single-use CSRF token immediately before every attempt, and copies
only the generated `Idempotency-Key` field into the request. Descriptor
`route_path` strings and arbitrary generated authorization/cookie headers are
not transport inputs. The browser operation state preserves a request
fingerprint and idempotency key across definitive retries and uncertain network
results, records `trace_id`, `original_trace_id`, and `replayed`, and refuses to
replace an uncertain operation with a new key. The direct generic-web promotion
binding always rewrites the generated request to `dry_run=true`; live promotion
remains the separate product-owned workflow-dispatch path.

The Engineering Ops browser routes consume the generated work-graph snapshot,
issue-inbox, Every Code summary, merge-train policy-target, and merge-train
controller-status read models directly. Work-graph ranking is the only
Engineering Ops POST in the generated browser write contract; it is stateless
and accepts the current generated snapshot. Issue reconciliation remains
read-only in the browser: the UI explains the native GitHub Actions OIDC or
trusted owner-agent write identity boundary and renders no Dry Run or Apply
button. Merge-train UI is also status-only: target selection comes exclusively
from `GET /v1/work-graph/merge-train/policy-targets`, while controller and
legacy worker POST routes are never derived from data or invoked dynamically.
Each route aborts superseded reads, distinguishes denied and empty responses,
and marks retained data as cached when a refresh fails or is cancelled.

Launchplane converts FastAPI request-validation failures into the standard
`400` Launchplane error envelope, so canonical and generated contracts omit the
framework's unreachable `422` response. Product-config request generation keeps
the structured `runtime_env` model while accepting the legacy flat environment
map and unknown nested fields that earlier external callers could send; precise
response contracts must not turn contract generation into an unannounced input
compatibility break.

The human auth/session family uses FastAPI routes in the production service:
`GET /auth/github/login`, `GET /auth/github/callback`, `GET /v1/auth/session`,
and `POST /auth/logout`. GitHub OAuth login preserves PKCE state, same-origin
`return_to` sanitization, GitHub authorization redirect, callback error
envelopes, and signed session cookie issuance with the existing
`HumanSessionManager`. Session read preserves the Launchplane human-session
response shape, renews expiring signed session cookies, and returns
`authentication_required` with the `configured` flag when no valid human session
exists. On hosted PostgreSQL requests, Launchplane refreshes the active DB-backed
policy before authorization and re-evaluates the session's human role against
that policy. Removed humans are rejected immediately, role changes take effect
on the request, and an email that still matches the configured bootstrap-admin
root of trust remains admin. Logout deletes the cookie-backed session when auth
is configured and always emits the Launchplane session clearing cookie.

### Browser Mutation Boundary

`SameSite=Lax` remains defense-in-depth; it is not the authorization or CSRF
contract. A request that resolves to a GitHub human session may use a mutation
route only when all of these browser facts validate before route authorization
or mutation logic runs:

- exactly one `Origin` matches the normalized origin of
  `LAUNCHPLANE_PUBLIC_URL`; forwarded host headers are not origin authority
- exactly one `Sec-Fetch-Site: same-origin`, one fetch mode of `cors` or
  `same-origin`, and one `Sec-Fetch-Dest: empty` are present
- exactly one `X-CSRF-Token` matches the current HMAC-bound session generation

`GET /v1/auth/session` returns the current `csrf_token` with
`Cache-Control: no-store`. Each accepted token is consumed atomically before the
route handler runs and advances the generation stored with the human session.
The old token is then stale and cannot be replayed, including when later route
authorization or request handling rejects the operation. Browser clients must
serialize writes and acquire the current token before each attempt. Existing
signed sessions remain compatible: records written before this boundary begin
at generation zero and receive a token through the normal session read instead
of forcing a logout.

Cancellation before the mutation request is dispatched ends that local attempt.
Cancellation or a network failure after dispatch has an uncertain result: the
operator UI may retry only with the same route, request fingerprint, and
idempotency key so the service can replay or reconcile the original operation.
It must not generate a replacement key merely because the browser stopped
waiting.

The cookie-capable mutation inventory is intentionally limited to:

- `POST /auth/logout`
- `POST /v1/work-graph/rank`
- `POST /v1/agent/write-intents/evaluate`
- `POST /v1/drivers/generic-web/prod-promotion`
- `POST /v1/drivers/generic-web/prod-promotion-workflow`
- `POST /v1/product-config/apply`
- `POST /v1/merge-train/policies/import`
- `POST /v1/authz-policies/managed-rule-sets/reconcile`
- `POST /v1/tenant-admission/technical-human-waivers/apply`
- `POST /v1/tenant-admission/trusted-maintenance-policies/apply`

Every other authenticated mutation route intentionally rejects session-cookie
authentication and continues to require its existing GitHub Actions OIDC,
local-operator/admin bearer, Every Code worker, or webhook boundary. A valid
`Authorization: Bearer` identity on the existing mixed-identity routes above
also bypasses browser origin, fetch-metadata, and CSRF checks exactly as before;
a cookie does not weaken or replace bearer verification. The tenant technical
human waiver apply route is narrower: after the browser mutation boundary it
requires a `GitHubHumanIdentity` with positive numeric `github_id` and rejects
bearer-only/non-human identities. The operator UI exposes only the separately
generated UI write slice; this inventory is a server-side cookie-capable surface
list, not a promise of UI controls for every route. In particular, GitHub issue
inbox reconciliation is displayed as unavailable because it remains a GitHub
Actions OIDC service operation. Managed-secret root re-encryption is explicitly
bearer-only even when a valid human session cookie is present; rotating the
service root is not a browser mutation surface.

Trusted Launchplane CLI clients that are explicitly given `--session-cookie`
preserve compatibility by reading `/v1/auth/session` immediately before the
write and sending the same strict origin/fetch-metadata headers plus the returned
single-use token. Bearer-token CLI requests do not perform that preflight and
retain their existing request shape.

Launchplane verifies GitHub OIDC, authorizes workflow identity claims, accepts
deployment/promotion/preview lifecycle evidence over HTTP, and executes the
current Odoo/VeriReel artifact, deploy, backup, promotion, rollback, maintenance,
and preview mutations as authenticated Launchplane routes. Authz administration
accepts GitHub Actions OIDC callers and authenticated admin human sessions and
requires `authz_policy_grant.write`. Managed-rule-set reconciliation is the
only durable write/reload boundary for every principal type. Responses return
record metadata, rule counts, compact diffs, and redacted audit metadata rather
than echoing workflow refs, human logins, owner-agent subjects, or the full
policy body.

Managed rule-set reconciliation is the durable authz write contract. A stable
`(managed_set_id, managed_rule_id)` owns each managed rule independent of its
content hash or principal type. Dry-run reads the one active DB-backed policy,
normalizes selector order, and returns a redacted add/adopt/update/remove diff,
unmanaged compatibility candidate and retirement evidence, plus `plan_sha256`.
Apply must repeat the same desired set, migration/adoption intent, reason, and
related issue with that reviewed digest and an `Idempotency-Key`. The service
then reserves idempotency, locks the singleton active policy, compares record
ID/revision/digest, supersedes and inserts only when changed, completes replay
evidence, and commits the transaction as one unit. No-op applies complete replay
evidence without creating policy history.

Manager approval of rendered previews is a separate Launchplane domain from
Every Code preview-gate validation. `control_plane/manager_preview_approval.py`
builds and evaluates exact preview bindings from the current preview record,
serving generation, immutable artifact image digest, checked runtime identity,
and active authorization policy. Manager-authored events require exactly one
schema-v2 managed GitHub-human rule granting
`manager_preview_approval.write`; that rule must include the actor's stable
numeric GitHub id. The login is display evidence and may change without changing
the authorized identity.

The resulting `launchplane_manager_preview_approval_events` ledger is
append-only in both filesystem rehearsal storage and PostgreSQL. Approval reads
use `manager_preview_approval.read`, and the decision projection fails closed to
pending, stale, or unavailable whenever current head, serving generation,
artifact, manifest, runtime identity, verification, preview state, or policy
does not exactly match the recorded event. This contract does not add a browser
or GitHub mutation route by itself; GitHub comment handling, check projection,
and promotion admission belong to the downstream interaction layer.

People-based manager lookup remains private Every Code communication and
planning context. It cannot populate, authorize, or override Launchplane runtime
approval records. Tenant repositories own site code and thin workflow inputs;
Launchplane owns authorization, durable approval evidence, and lifecycle
invalidation. Preview destroy, PR close, label removal, and cleanup never call
the approval decision as an admission gate.

Schema-v1 migration and unmanaged-rule adoption are never implicit. The caller
must request `schema_migration = migrate_v1_to_v2` and/or
`unmanaged_adoption = adopt_matching` during both review and apply. Once a
desired managed GitHub Actions rule already exists unchanged, `adopt_matching`
also converges one matching name-only compatibility rule by retiring it from the
same candidate policy. Retirement requires an exact repository and action set,
no immutable IDs on the compatibility rule, and proof that every other managed
selector is equal or narrower than the compatibility authorization. Ambiguous,
cross-principal, ID-bound, broader-action, or unmatched rules are not retired.
Ambiguity is evaluated against the complete desired managed set before unchanged
rules become retirement candidates, so a broad compatibility rule cannot be
removed during another matching rule's transition. The service also evaluates
the candidate policy with the applying identity and rejects retirement if that
identity would lose policy-administration authority. The diff separates stale
managed-rule removals from unmanaged compatibility retirements and exposes only
managed IDs and rule hashes. `GET /v1/authz-policies/active` exposes only active
record metadata, counts, managed IDs, principal types, and rule hashes; it never
returns full workflow refs or principal selectors. Its removal readiness fields
include managed and unmanaged rule totals, unmanaged counts by principal type,
and the count of privileged GitHub Actions rules that still lack an immutable
reusable-workflow identity.

`GET /v1/authz-diagnostics/active-policy/health` is a separate read-only
administrator contract. It requires a GitHub administrator or local
administrator with `authz_policy_health.read`, reloads the exact active DB
policy after preflight authorization, and reauthorizes against that record. The
response contains active-record identity, revision, digest, schema version,
bounded health reason codes, at most 100 lexically ordered managed-set summaries
with rule and principal-type counts, and policy-administrator rule counts. It
does not expose managed rule IDs, rule hashes, selectors, actions, repositories,
workflows, or principal identifiers. Missing active state returns `503`, and
multiple active records return `409`; the service never falls back to cached
policy state for the response.

`GET /v1/authz-diagnostics/activation-preflight/self` is the signed-in
browser-human self-check for policy-administration authority. It rejects every
Authorization header and accepts no body, query parameter, caller-selected
identity, role, organization, team, session identifier, or cookie value. The
service verifies the signed session cookie without renewal, requires current
stored claims, re-derives the human role from the single active DB policy, and
evaluates the fixed global `authz_policy_grant.write` request. The response is
limited to allowed/denied, an hour-bounded evaluation time, an opaque keyed policy
generation, and a trace ID. The route and all errors are
`Cache-Control: no-store`; the path performs no durable write and requires no
separate diagnostic authorization grant.

`POST /v1/authz-diagnostics/candidate-policy/preview` is a separate
administrator-read contract protected by `authz_policy_candidate_preview.read`.
It accepts one exact schema-v2 candidate authorization policy and at most 25
explicit probes, reloads and reauthorizes against the single active DB policy,
and evaluates both policies through the ordinary effective-access evaluator.
The response contains immutable active-policy provenance, submitted and
canonical evaluated-candidate digests, a normalization flag, bounded health and
administrator counts, count/category-only structural changes,
operational-readiness reason categories, and old/new probe decisions. It never
returns rule IDs or hashes, managed-set IDs, raw rules, selectors, actions,
principal identifiers, tokens, secrets, or topology. Same-origin browser calls
verify CSRF without renewing the session or rotating its token, and the route
performs no policy, session, denial-evidence, idempotency, outbox, provider,
runtime, secret, durable-operation, or other persistence write.

`POST /v1/authz-diagnostics/repository-scope/read` is the independently
grantable DB-backed repository-scope audit read. It requires
`authz_repository_scope.read` and accepts at most 100 exact caller-known
repository candidates. GitHub humans, local operators, and local administrators
may use the permission; GitHub Actions and terminal-agent identities are
ineligible. The route authorizes against runtime policy before reading route
state, reloads exactly one active DB policy, and authorizes again against that
record.

The response reconciles active product profiles, current repository human-role
and tenant-classification records, nonterminal Every Code work requests, and
exact GitHub Actions repository membership from the active policy. It never
returns a repository name or numeric identifier. Matched candidates are
referenced by request ordinal and receive only an opaque purpose-separated
handle plus `product`, `repository_record`, `work_graph`, and/or
`authorization_chain` membership categories. Unmatched DB repositories produce
aggregate counts only. The response also includes generation time, bounded
source counts/timestamps, active-policy record/revision/time provenance, an
opaque handle-generation marker, and count/reason coverage gaps.
Each source query is capped at 1,000 retained records. A source that exceeds its
cap reports `source_truncated` and cannot produce complete coverage.
Public handle derivation requires the canonical high-entropy managed-secret key
ring; legacy passphrase-only compatibility configuration fails closed rather
than exposing a known-plaintext verifier for the managed-secret root.

`coverage.state=complete` requires exact set equality: every DB repository must
match one submitted candidate, every candidate must exist in DB scope, and no
conflicting, malformed, stale-only, case-variant, or missing-identity evidence
may remain, and claimed/running work requests must retain a current lease.
Otherwise the route returns `partial`; consumers must treat that as fail-closed
and cannot use it as #2177 or #2062 completion evidence.
The endpoint is a bounded repository-existence oracle, sets `Cache-Control:
no-store`, and must remain narrowly granted. Successful and partial reads write
nothing. A standard redacted denial record remains the only permitted
best-effort write on HTTP 403 and contains no repository identity.

New managed GitHub Actions rules require immutable GitHub `repository_id` and
`repository_owner_id` selectors. Production-capable, destructive,
secret-backed, or policy-admin reusable-workflow rules require exact caller
workflow refs and a reusable `job_workflow_ref` pinned to a full commit SHA. A
mutable reusable ref may appear only in a reviewed overlap plan when the active
policy already authorizes that exact ref; narrowing removes it from the same
stable managed rule after canary evidence.

The standalone policy-admin worker owns its exact immutable grants in the
separate `operator.authz-policy-reconcile` managed set. Rotate that authority by
expanding through the currently authorized worker, advancing the wrapper pin,
verifying the new identity, and then contracting the old rule. The deploy
workflow no longer carries any authorization inputs, secret, or reconciliation
job. Do not reintroduce a deploy-time policy bridge; missing policy-admin
authority is a DB-native recovery design blocker.

Production diagnostic and repair workers do not accept a service URL or OIDC
audience from their callers. Thin dispatch workflows may forward only the typed
operation inputs declared by the reusable worker; the worker resolves its
Launchplane destination and audience from protected repository configuration.
Tracked-target log reads and Odoo website-bootstrap writes therefore require an
exact default-branch caller ref plus the reviewed full-SHA reusable-worker ref
before the service authorizes the request.

Reviewed exact-instance external-route and product-health-monitoring workflow
rules require the same immutable reusable-workflow identity even for non-prod
instances, and those actions cannot be authorized by schema-v1 policy.

The service also serves the built operator UI shell at `/`, with `/ui` retained
as a route alias. This route family is native FastAPI. Built assets live under
`/ui/assets/...`, while `/ui/*` falls back to the app shell so the frontend can
own client-side routes. Versioned API ingress remains under `/v1`.

Validate the operator UI shell with browser navigation or `GET /ui`. Do not use
`HEAD /ui` as the only availability check, because static app-shell fallback
behavior can differ between request methods.

`POST /v1/every-code/github-webhook` and
`POST /v1/manager-preview-approval/github-webhook` are the only unauthenticated
write routes. They trust request bodies through route-specific GitHub webhook
HMAC verification instead of OIDC. Before buffering or HMAC processing, the
ASGI boundary requires exactly
one unsigned-decimal `Content-Length`, rejects transfer-encoded or missing-length
requests, caps both declared and observed body bytes at 2 MiB, and rejects a
declared/observed length mismatch. Contract failures return `400` or `413`
without invoking the webhook handler. The route requires
`X-Hub-Signature-256`, `X-GitHub-Delivery`, and `X-GitHub-Event`, supports
`issues.labeled` events for the `every-code` label, and accepts pull-request
`closed` events to terminalize linked Every Code work requests. Other signed
events, actions, or labels return `202` with `skipped: true`. Matching
issue-label deliveries create or return the durable Every Code work request and
include `deduped` plus the delivery id in the response. Matching pull-request
close deliveries can close every linked request referenced by the PR, including
still-queued requests that never stored a result PR URL. The route is native
FastAPI.

Both existing signed webhook surfaces also invoke the common
trusted-maintenance capture handler after signature verification and payload
decoding for `pull_request` deliveries. This reuse intentionally adds no new
route, secret, durable raw webhook receipt, or runtime configuration. Invalid
signatures, missing delivery IDs, and malformed payloads stop at the existing
ingress boundary and never reach the trusted-maintenance handler. Unsupported
or nonmatching deliveries remain accepted/skipped so unrelated Every Code and
manager-preview behavior stays compatible.

Trusted-maintenance capture treats the signed payload as a structural pre-filter
only. It can use the signed numeric repository tuple, PR number, sender
ID/type/login, PR author ID/type/login, and head SHA to decide whether a current
repository-wide `tenant_ui` classification and exact trusted-maintenance rule
candidate exist. Repositories without active authority, or deliveries without an
exact signed numeric actor/sender/event/action candidate, are accepted/skipped
before any GitHub API call. Relevant deliveries resolve the GitHub token from the
DB-authoritative classification product/context, re-fetch the current PR, and
write evidence only when the re-fetched base repository tuple, open state,
author identity, head SHA, and same-repository head tuple exactly match the
signed delivery and policy rule. Transient token, GitHub API, or PostgreSQL
uncertainty on such a relevant delivery returns retryable `503` with no
evidence; exact GitHub redelivery or the existing signed replay-envelope tooling
is the reconcile path. Evidence conflicts return `409`. Responses expose only
capture status, not policy actor IDs or logins.

The manager-preview webhook uses
`LAUNCHPLANE_MANAGER_PREVIEW_GITHUB_WEBHOOK_SECRET`, accepts signed
`issue_comment.created` and selected pull-request lifecycle deliveries, and
re-fetches comments, actor numeric identity, current PR head, current serving
preview, and active managed policy before writing evidence. Its GitHub comment
and `manager-preview-approval` status writes use the Launchplane-managed token
resolved for the product context; tenant workflow or PR code cannot supply that
credential. GitHub projection failure is degraded output, not approval loss and
not a reason to block destroy or cleanup.

`POST /v1/manager-preview-approval/reconcile` is the authenticated retry path.
It requires `manager_preview_approval.read` authorization for the resolved
product/context, re-fetches current GitHub and Launchplane evidence, and rewrites
the credential-owned comment and current-head status. Managed authz policy apply
also attempts reconciliation for existing previews. Removing the managed
approval rule is the rollback switch; records remain append-only.

The Every Code worker read, native claim, and status routes also accept a
dedicated local-worker bearer token. Configure
`LAUNCHPLANE_EVERY_CODE_WORKER_TOKEN` on the Launchplane service and on the Mac
worker host, then run the worker with
`uv run launchplane every-code start --service-url https://...`. That token is
scoped in code to Every Code work-request, PR-feedback, preview-gate, preview
readiness routes, plus read-only `GET /v1/product-profiles` for preview
readiness projection. The worker can create service-owned PR feedback records
only for Launchplane-derived worker signals such as failed-check routing, while
human PR feedback still enters through the signed webhook path and trusted-actor
gate. It cannot create webhook requests, write product records, or access other
Launchplane service routes. This keeps remote DB credentials on the Launchplane
host while still allowing visible local Code/tmux work sessions to claim, rerun
terminal requests, reconcile preview state, route failed checks, and report
progress.

Engineering-review worker routes reuse that bearer secret but do not trust
caller-supplied host or runtime strings. The service derives the worker identity
from `LAUNCHPLANE_ENGINEERING_REVIEW_WORKER_RUNTIME_ID` and
`LAUNCHPLANE_ENGINEERING_REVIEW_WORKER_HOST`, then requires the active
DB-backed review authority and each claimed run to match both values before a
scoped review credential is decrypted or returned.

The local worker uses a separate GitHub token for public claim comments. Provide
`LAUNCHPLANE_EVERY_CODE_GITHUB_TOKEN` on the worker host, and set
`LAUNCHPLANE_EVERY_CODE_GITHUB_ACTOR` when the operator expects a specific
automation account. Before creating the `<!-- every-code-claim -->` issue
comment, the worker resolves `gh api user --jq .login` with that token and
blocks the work request if the actor does not match. Claim comments never fall
back to the host's ambient `gh` login.

Local terminal agents that need Launchplane context use a separate read-only
bearer credential, not the browser OAuth session cookie and not
`LAUNCHPLANE_EVERY_CODE_WORKER_TOKEN`. Configure
`LAUNCHPLANE_TERMINAL_AGENT_READ_TOKEN` on the service and provide the same
secret to the trusted local terminal agent out of band. Configure
`LAUNCHPLANE_TERMINAL_AGENT_SUBJECT` and
`LAUNCHPLANE_TERMINAL_AGENT_TOKEN_LABEL` to identify the local owner subject and
token label used by `terminal_agents` authz policy rules; both identity values
are required whenever the bearer token is configured. The service only accepts
this identity on `GET` routes, so even an overly broad terminal-agent policy
rule cannot dispatch product config writes, prod promotion, destructive cleanup,
authz policy mutation, read-model POSTs, or plaintext secret reveal routes.
Policy still scopes which redacted read actions and product/context pairs the
agent can access, such as `product_environment.read` for product environment and
config-status diagnostics.

Trusted owner terminals that need to make Launchplane-owned operator mutations
without a browser session can use separate owner-agent bearer credentials.
Configure `LAUNCHPLANE_LOCAL_OPERATOR_TOKEN` on the service and provide the same
secret to trusted local agents through
`~/.config/launchplane/local-operator.env`. Configure
`LAUNCHPLANE_LOCAL_OPERATOR_SUBJECT` and
`LAUNCHPLANE_LOCAL_OPERATOR_TOKEN_LABEL` to identify the actor in audit and
idempotency records; both identity values are required whenever the bearer token
is configured. Routine owner-operator authority is DB-backed by `local_operators`
authz policy rules, scoped by subject, token label, product, context, and action.

Rare owner-admin operations use `LAUNCHPLANE_LOCAL_ADMIN_TOKEN` with configured
`LAUNCHPLANE_LOCAL_ADMIN_SUBJECT` and `LAUNCHPLANE_LOCAL_ADMIN_TOKEN_LABEL`.
Both identity values are required whenever the bearer token is configured. Those
credentials are also DB-backed by exact `local_admins` authz policy rules; the
token alone does not grant blanket access. Owner-agent write requests must
include a non-empty `reason`; product-config apply is also rejected until the
service has recorded a matching owner-agent dry-run for the same payload. These
requests still use Launchplane records, redacted responses, runtime key-safety
policy, and managed secret storage. Terminal-agent credentials remain read-only.

`GET /v1/every-code/summary` returns a compact agent-safe projection of Every
Code work requests. It requires `every_code_work_request.read` for
product/context `launchplane` and supports `repository`, `issue_number`,
`state`, `limit`, and `offset` query parameters. Summary entries include source
links, state, summary status, claim metadata, timestamps, result PR URL, and
safe rerun guidance. They intentionally omit raw webhook delivery ids, error
messages, issue bodies, prompt text, local checkout paths, and local worker
hostnames. Entries include compact agent-context provenance and evidence so
callers can tell recorded request state apart from source-of-truth links.

`GET /v1/previews/readiness` returns a compact agent-safe projection of Every
Code preview gate records. It requires `every_code_preview_gate.read` for
product/context `launchplane` and supports `repository`, `pr_number`, `status`,
`limit`, and `offset` query parameters. Readiness items map gate records to
agent-facing states such as waiting on checks, ready, needs attention, or
cancelled, include source links and freshness/provenance, and avoid provider-only
internals, local paths, or secrets. Detail fields and check summaries are bounded
and redacted before they leave the read-model projection.

The direct Every Code record reads are also native FastAPI routes. Work-request
reads use `every_code_work_request.read`; PR-feedback reads use
`every_code_pr_feedback.read`; preview-gate reads use
`every_code_preview_gate.read`; Every Code notification-attempt reads use
`every_code_notification_attempt.read`; preview PR-feedback notification-attempt
reads use `preview_pr_feedback_notification_attempt.read`. The dedicated Every
Code worker token is accepted only for the worker-facing Every Code read routes,
not for the preview PR-feedback notification-attempt route. Every Code
work-request status is also a native FastAPI route for the dedicated Every Code
worker token and workflows authorized for `every_code_work_request.update`. It
checks idempotency replay before requiring status-write store capabilities,
writes successful idempotency records, preserves missing-request `404 not_found`,
and delivers configured blocked notifications when a request transitions to
`blocked`. Every Code PR-feedback write, PR-feedback status, and preview-gate
write routes are also native FastAPI routes for the dedicated Every Code worker
token. They require only the matching PR-feedback or preview-gate record-store
capabilities, preserve the direct worker signal payloads, and do not create
idempotency records. PR-feedback status preserves
the worker transition semantics: missing feedback returns `404 not_found`, and
already applied or ignored feedback returns `409 feedback_already_final`.

`POST /v1/work-graph/rank` ranks a caller-supplied work graph snapshot and
returns the queue payload under `result.queue`. The route requires the
`work_graph.rank` action for product/context `launchplane`, accepts GitHub
Actions OIDC and GitHub human-session callers, rejects owner-agent bearer tokens
at the identity boundary even if a policy grant is too broad, performs no
storage writes, and does not make Launchplane authoritative for copied GitHub or
Code Plans state.

`POST /v1/work-graph/github/issues/reconcile` reconciles the configured GitHub
issue inbox into Code Plans Project state. `dry_run` mode requires
`work_graph.rank`; `apply` requires `work_graph.issue_inbox.reconcile`. The
route uses the native FastAPI write identity boundary for GitHub Actions OIDC
and trusted owner-agent write credentials, then returns reconcile evidence under
`result.reconcile`.

`POST /v1/work-graph/merge-train/run-once` executes one policy-backed Level 1
ordered-queue pass for a requested repository/base branch. It requires the
`service_authz` action/product/context declared by the matching merge-train
repository policy, resolves its GitHub token from that policy's
`github_token.env_var`, and fails closed before GitHub calls when no matching
policy or token is available. The route is dry-run by default; `mutate: true`
applies at most one worker transition from one fresh snapshot. This route is the
deployed sequential baseline, not the full batch train target. It is native
FastAPI, and accepted calls persist `launchplane_merge_train_runs` evidence with
optional `Idempotency-Key` replay/conflict handling.

`POST /v1/work-graph/merge-train/pr-feedback` is a native FastAPI route that
writes the public pull-request feedback surface for train progress. It uses the
same repository/base policy and `service_authz` scope as `run-once`, resolves the
same GitHub token, and creates or updates one Launchplane-managed issue comment
per PR using a hidden marker. Accepted calls persist a
`launchplane_merge_train_pr_feedback` record with the rendered markdown, event,
controller action metadata, delivery status, and GitHub comment id/url. The route
fails closed when authorization, storage, or token configuration is missing;
callers should use it for queued, waiting, blocked, stale-policy, and completed
transition summaries instead of writing ad hoc comments from scheduler scripts.

`GET /v1/work-graph/merge-train/policy-targets` is a native FastAPI route that
returns the authorized repository/base-branch targets from the active DB-backed
merge-train policy. It performs no GitHub reads or mutations and is the source
of truth for operator UI target selection and scheduled runner intent; callers
should not infer merge-train targets from product inventory, work-graph
awareness items, or repository variables.

`GET /v1/work-graph/merge-train/admission` is a native FastAPI route that returns
the stored-history scheduler admission decision for a requested `repository` and
`base_branch`. It uses the same merge-train repository policy and `service_authz`
scope as `run-once`, but performs no GitHub reads and no storage writes.
Schedulers use this route to pace calls into `run-once`; execution still re-reads
GitHub before any dry-run or mutation.

`GET /v1/work-graph/merge-train/controller/status` is a native FastAPI route that
returns the operator read model for the same repository/base branch. It uses the
same authorization as the policy route, performs no GitHub reads, and composes
stored scheduler admission, latest Level 1 run history, active batch candidates,
landing plans, and stack-collapse plans. Only records that match the active
repository policy key and digest can drive the advertised controller action;
stale records stay visible with a stale reason. Operators can use this route to
see the current controller action, durable record ids, PR numbers, candidate
SHA/check state, compact entry counts, lease owner, active phase, lease and
heartbeat age, and reconciliation state without invoking a worker mutation.

`POST /v1/work-graph/merge-train/controller/run-once` is the operator-facing
one-action controller for the full batch train. Request payloads name
`repository`, `base_branch`, and optional `mutate`; the route uses the same
policy, authorization, and GitHub token boundary as the lower-level merge-train
routes. The native FastAPI route supports optional `Idempotency-Key`
replay/conflict handling.
Each call advances at most one safe phase from DB-backed records and
fresh GitHub evidence: plan stack collapse, execute stack collapse, admit the
collapsed root PR, plan/build/observe a batch candidate, plan landing, or land
the original PRs. Dry-run calls return the next controller action without
writing records or mutating GitHub. Mutation calls reuse the same persisted
candidate, stack-collapse, and landing-plan records as the phase-specific
routes, acquire one storage-clocked repository/base lease, checkpoint before
every GitHub mutation, and reject stale policy digests before advancing stored
records. Expired owners cannot checkpoint or release after a successor acquires
the fence. Restarted owners adopt exact candidate-ref, stack-merge, PR-merge,
cleanup, and stack-child disposition evidence or remain
`reconcile_required`; active/expired lease conflicts return explicit HTTP 409
errors rather than becoming generic worker failures. The
response `result.controller_action` is the helper contract for retry/stop
behavior; see [merge-train-policy.md](merge-train-policy.md) for the action
matrix and public-safe reporting fields.

Mutating landing phases are guarded by immutable per-attempt Level 3 admission.
The service recomputes current Level 2 and structural evidence under the live
controller lease immediately before each constituent PR effect, persists the
admission before GitHub mutation, and appends `landed`, `rejected`, or
`reconcile_required` evidence afterward. A blocked admission returns
`merge_train_landing_not_admitted`; unresolved effect evidence returns
`merge_train_landing_reconcile_required`. Neither response permits provider
replay.

Every retained write-capable merge-train route acquires that same
repository/base fence for its full mutation window. Phase-specific and legacy
mutation calls therefore return the same lease-held or reconciliation-required
HTTP 409 errors rather than racing the controller. Long-running legacy phases
renew the lease at provider-effect boundaries, and successful evidence plus
idempotency completion is persisted before the lease is released. Read-only
controller and legacy dry-run calls do not acquire or mutate the fence.

`POST /v1/work-graph/merge-train/batch-candidate/run-once` executes one
policy-backed batch-candidate phase for a requested repository/base branch. The
native FastAPI route accepts `mode: plan`, `mode: build`, or `mode: observe`.
Plan mode reads a fresh GitHub snapshot, derives one deterministic batch
candidate from the currently eligible queued PRs, and writes a
`launchplane_merge_train_batch_candidates` record. When the selected PR is the
root of a supported stack, plan mode writes a stack-collapse plan record instead
of a batch candidate; unsupported stack topologies return accepted evidence with
no record write. Build mode requires a prior candidate record id, creates or
resets the Launchplane train ref, merges queued PR heads into that ref in order,
and records the resulting candidate SHA. Observe mode requires a prior candidate
record id, reads required checks for that exact candidate SHA, and records
whether the candidate is still pending, passed, or failed. The route
never lands original PRs; PR-native landing remains a later phase with separate
records and pre-merge invariants.

`POST /v1/work-graph/merge-train/stack-collapse/run-once` executes one
policy-backed stack-collapse phase for a requested repository/base branch. The
native FastAPI route accepts `mode: execute` with an existing stack-collapse plan
record id, merges supported stack children into the root PR, and persists a
waiting-for-root-checks stack-collapse plan record. `mode: admit` requires that
executed record, verifies the active policy digest and the root PR head against
fresh GitHub evidence, then writes a root-only batch-candidate record for the
normal candidate build/observe/landing phases and accepted calls support
optional `Idempotency-Key` replay/conflict handling.

`POST /v1/work-graph/merge-train/batch-landing/run-once` executes one
policy-backed batch-landing phase for a requested repository/base branch. The
native FastAPI route accepts `mode: plan` with a passed batch-candidate record id
or `mode: land` with a landing-plan record id. Plan mode writes a
`launchplane_merge_train_batch_landing_plans` record with the original PR order,
expected head SHAs, expected base SHA, and policy merge method. Land mode merges
the original PRs in that order through GitHub's PR merge endpoint. Before each
merge it requires the PR to remain open at the recorded head SHA and target the
recorded base ref and rolling base SHA, then also uses GitHub's head-SHA guard.
Recovery adopts a merged PR only when the recorded target branch contains its
reported merge commit. A later commit after the final merge is accepted only
when the final merge commit remains in target-branch history; divergence and
force-push removal stay stale conflicts. When landing a collapsed stack root,
the route validates the linked
stack-collapse record before the root merge and then writes stack-child
disposition evidence after the landing record is persisted. Accepted calls
support optional `Idempotency-Key` replay/conflict handling.

Land mode is not a second authority path. It requires the same guarded
admission store, live evidence adapter, controller lease, and append-only
landing outcome behavior as controller mode. If those dependencies are
unavailable, the route fails closed before GitHub mutation.

`.github/workflows/merge-train-runner.yml` is the first external scheduler for
this route. It mints a GitHub Actions OIDC token for the Launchplane service,
reads DB-backed policy targets for scheduled runs, reads admission, and calls
one worker entrypoint only when the decision is `admitted`. Scheduled
repository, base-branch, runner-mode, and mutation selection come from the
active merge-train policy record through `policy-targets`; manual dispatch uses
explicit workflow inputs. Scheduled runs with no enabled policy scheduler target
complete as no-ops before admission, and scheduled runs with multiple enabled
targets fail closed. The default manual runner mode calls the Level 1 `run-once`
route; setting manual `runner_mode: controller` switches an admitted pass to one
full-controller `run-once` call instead.
Controller-mode dry-runs do not deliver PR feedback comments; feedback delivery
is reserved for mutate runs and explicit manual phase workflows.
Workflow dispatches may select at most one non-`none` phase input across
batch-candidate, batch-landing, and stack-collapse modes; the runner validates
that exclusivity before any phase step mutates state.

`GET /v1/repo-product-mapping` is a native FastAPI route that returns the
repository ownership/awareness read model used by work graph and agent context.
The route requires
`product_environment.read` for product/context `launchplane`, performs no
writes, and distinguishes Launchplane-managed runtime repos from awareness-only
Every Code work-request repos. Managed runtime entries come from product profile
records and include product, contexts, stable environments, driver id, and
preview context; awareness entries do not imply Launchplane runtime ownership.

`GET /v1/work-graph/snapshot` is a native FastAPI route that returns the
current Launchplane-assembled work graph snapshot for the same authorization
boundary. It composes product overviews and Every Code work-request records into
the typed snapshot contract.
When a caller-owned planning ingestion provider is configured, the route can
overlay compact GitHub/Code Plans facts. The first provider is opt-in via
`LAUNCHPLANE_WORK_GRAPH_PROJECT_OWNER` and
`LAUNCHPLANE_WORK_GRAPH_PROJECT_NUMBER`, reads `gh project item-list --format
json`, and supplies Project Focus, Manager, Finish Line, labels, status, updated
time, PR-vs-issue type, dependency counts, subissue counts, and PR check state.
`LAUNCHPLANE_WORK_GRAPH_PROJECT_SIGNAL_LIMIT` bounds the per-snapshot fan-out for
dependency, subissue, and check reads after Project items are loaded. The route
reports product, work-request, and planning-fact source counts, does not fetch or
store GitHub issue bodies, and writes no state. A configured Project or signal
read failure returns an error instead of a silently incomplete snapshot.
The production image includes `gh` for this provider, and deploy automation maps
the `LAUNCHPLANE_WORK_GRAPH_GH_TOKEN` repository secret into service `GH_TOKEN`
when present. Deploy automation also withholds the Project provider env bundle
until that secret exists, so prepared repo variables do not enable
unauthenticated runtime reads. That token is not a Launchplane record and must
remain outside the repo.

`GET /v1/agent/context` is a native FastAPI thin read-only aggregation endpoint
for public-safe skill preflight. It requires `product_environment.read` for
product/context `launchplane`, accepts an optional `repository` filter, and
composes the existing repo-product mapping, work graph snapshot, Every Code
summary, and preview readiness projections under named sections. Each section
reports `available`, `unauthorized`, or `unavailable`; optional work-graph
planning provider failures mark only that section unavailable instead of dropping
the whole context or silently omitting the failure. The endpoint writes no
records, fetches no issue bodies, and must preserve the lower-level
redaction/provenance rules.

`POST /v1/work-graph/merge-train/run-once` is the authenticated service ingress
for one ordered-queue read or mutation pass. It resolves repository/base policy
from the active DB-backed `launchplane_merge_train_policies` record before
authorization, token lookup, or GitHub reads. If no active record exists, the
service fails closed with `merge_train_policy_not_configured`; policy creation
and updates are explicit record writes, not checked-in config files or
service-host env overrides. It authorizes against the policy's `service_authz`,
reads a fresh GitHub snapshot, and writes a `launchplane_merge_train_runs` record
for accepted dry-run and mutate calls. Mutation mode still applies at most one
worker transition from that snapshot.

The full batch train will use additional service contracts and records rather
than overloading `run-once`. Its target behavior is to build one combined
candidate from the base branch plus multiple queued PRs, run required checks on
that candidate commit, and then land the original PRs in queue order through
GitHub's normal PR merge path after pre-merge invariants are rechecked.
The first batch service contract is
`POST /v1/work-graph/merge-train/batch-candidate/run-once`; it owns only the
plan/build/observe candidate phases and writes
`launchplane_merge_train_batch_candidates` records.
The second batch service contract is
`POST /v1/work-graph/merge-train/batch-landing/run-once`; it owns landing-plan
creation and guarded PR-native landing, and writes
`launchplane_merge_train_batch_landing_plans` records.

`POST /v1/merge-train/policies/import` is the native FastAPI service-owned write
path for merge train policy records. It requires database storage and
`merge_train.policy_import` on product/context `launchplane`, accepts `dry_run`
and `apply`, and writes the supplied typed record only in apply mode. GitHub
Actions OIDC callers, signed-in GitHub human sessions, and local operator/admin
bearer callers may use the route when policy grants the action; terminal-agent
credentials remain read-only. Apply requests preserve `Idempotency-Key`
replay/conflict handling when callers provide a key; dry-runs remain stateless
and repeatable. Shared and production policy changes should use this route rather
than direct DB CLI writes from an arbitrary checkout.

## Host Assumption

- Launchplane runs behind an operator-owned HTTPS host.
- Launchplane exposes versioned API ingress under `/v1`.
- Launchplane returns JSON for both success and failure cases.

## Boundary Layers

```text
GitHub Actions workflow
  -> OIDC token from GitHub
  -> Launchplane HTTP ingress
  -> Launchplane authn/authz
  -> Launchplane core record/write logic
  -> Launchplane read models and driver hooks
```

## Authentication

Machine callers should authenticate with GitHub Actions OIDC.

Human browser callers authenticate with GitHub OAuth. Launchplane owns the
browser session after OAuth callback and sets an `HttpOnly`, `SameSite=Lax`
session cookie signed with `LAUNCHPLANE_SESSION_SECRET`. Sessions are backed by
the Launchplane database when `LAUNCHPLANE_DATABASE_URL` is configured. GitHub
access tokens stay server-side and are not exposed to the React operator UI.
Hosted requests reload the current DB policy before authorization and re-evaluate
the session role. OAuth organization/team claims expire for authorization after
24 hours; the session is revoked and the operator must sign in again to refresh
those mutable GitHub claims.

Every non-health `/v1` operation documents the refresh boundary: `503` means the
active DB policy could not be loaded, and `409` means a fenced schema exposed an
ambiguous active-policy state. These responses occur before route-specific
authorization or handler execution.

Local terminal agents should use the dedicated terminal-agent read bearer token
when they only need redacted Launchplane context from a trusted operator shell.
This avoids copying browser session cookies into terminal processes and keeps
agent credentials independent from GitHub Actions OIDC and Every Code worker
automation.

### Identity Types

Launchplane normalizes authenticated callers into compact subject types before
authorization checks and audit records consume them:

- `github_actions`: GitHub Actions workflow subjects from verified GitHub OIDC
  claims, including the immutable numeric repository and repository-owner IDs.
- `github_human`: browser-session humans from GitHub OAuth and Launchplane
  session cookies.
- `terminal_agent`: read-only trusted terminal agents authenticated by the
  dedicated terminal-agent bearer token.
- `local_operator`: reason-bearing owner automation authenticated by the
  dedicated local-operator bearer token.
- `local_admin`: rare privileged owner automation authenticated by the dedicated
  local-admin bearer token.

A future Keycloak slice may add OIDC human and service-client subject types.
Those subjects would be trusted only after issuer, audience, signature, expiry,
and client expectations validate. Keycloak would provide identity and session or
token facts only; product, context, lane, provider, authz, and runtime authority
would still come from Launchplane records, OpenFGA tuples if adopted, managed
secrets, provider state, or explicit scoped operator input.

For future OpenFGA checks, Launchplane should pass normalized facts rather than
raw tokens. Caller facts include `subject_type`, stable `subject_id`, `issuer`,
`audience` or client id, token expiry/freshness, optional audit display claims,
and normalized group or role ids when present. Request/resource facts include
the Launchplane action, product, context, optional instance, and resource id.
GitHub workflow subjects additionally keep workflow identity claims such as
repository, workflow ref, reusable workflow ref, ref, event name, environment,
subject, and SHA. Keycloak service-client subjects should use the validated
client id plus issuer as the stable caller identity, not a checked-in grant or
ambient process identity.

Launchplane should verify:

- `iss` is GitHub's OIDC issuer
- `aud` matches Launchplane's expected audience
- signature validates against GitHub's published keys
- token is not expired and is valid for current time

Recommended audience:

- the Launchplane service host name

That keeps the audience tied to the Launchplane service identity instead of to a
temporary repo or local CLI name.

## Authorization Model

Launchplane should authorize machine callers from workflow identity claims,
not from human repo-admin status and not from copied long-lived service
tokens.

The first policy model should be allow-list based and fail closed.

Native driver routes must evaluate authorization through the mutable active-policy
runtime on every request. They must not capture a bound method from the bootstrap
policy object: DB-backed policy revisions, including revocations, take effect
without a service restart.

### Durable operation reauthorization

Authorization at enqueue time does not grant permanent provider-mutation
authority. Odoo stable bootstrap, Odoo target-replacement apply, and VeriReel
prod backup-gate operation records persist the exact action, product, context,
instances, managed rule identity, policy evidence, and normalized caller or
reusable-workflow identity used at enqueue. Workers load the current active
DB-backed policy and require that same managed rule to still authorize the
recorded caller and target after claim and immediately before the first provider
mutation.

If the rule was removed, narrowed, no longer matches the caller, or the active
policy cannot be read unambiguously, execution fails closed with durable error
evidence and no provider mutation. Legacy queued records without provenance also
fail closed; Launchplane never invents historical authority from current policy.
Policy revision or digest changes alone do not block execution when the same
managed rule still grants the exact target.

Pending operations can be cancelled through authenticated service endpoints
using the operation's existing exact-instance action. The storage transition is
atomic and pending-only. Already-cancelled requests replay the terminal record;
running work returns a conflict because Launchplane cannot promise that no
provider effect has begun after a worker claim.

### Claims Launchplane should rely on first

- `repository`
- `repository_owner`
- `repository_id`
- `repository_owner_id`
- `workflow_ref`
- `job_workflow_ref` when reusable workflows are involved
- `ref`
- `ref_type`
- `event_name`
- `environment` when present
- `sub`
- `sha`

### Claims Launchplane should not treat as the primary authorization boundary

- `actor`
- `actor_id`
- human repo role such as admin/maintainer

Those are still useful for audit display, but the authorization decision should
primarily trust the workflow identity GitHub issued.

### First policy shape

Launchplane should map verified claims to a small policy rule set:

```text
rule
  - subject type: github-actions
  - repository name plus immutable repository and owner ID match
  - workflow_ref or job_workflow_ref match
  - event_name match
  - environment/ref constraints
  - allowed product
  - allowed contexts
  - allowed actions
```

Example policy intent:

```text
repository: example-org/verireel
workflow_ref: example-org/verireel/.github/workflows/preview-control-plane.yml@*
event_name: pull_request
allowed product: verireel
allowed contexts: verireel-testing
allowed actions:
  - verireel_preview_refresh.execute
  - preview_generation.write
```

Another example:

```text
repository: example-org/verireel
workflow_ref: example-org/verireel/.github/workflows/preview-cleanup.yml@*
event_name: pull_request
allowed product: verireel
allowed contexts: verireel-testing
allowed actions:
  - preview_lifecycle.plan
  - preview_lifecycle.cleanup
  - verireel_preview_destroy.execute
  - preview_destroyed.write
```

Launchplane preview lifecycle sweep example:

```text
repository: example-org/launchplane
workflow_ref: example-org/launchplane/.github/workflows/preview-lifecycle.yml@refs/heads/main
event_name: schedule or workflow_dispatch
allowed products: all preview-enabled products
allowed contexts: each preview-enabled product preview context
allowed actions:
  - preview_lifecycle.plan
  - preview_lifecycle.cleanup
```

Stable-lane examples:

```text
repository: example-org/verireel
workflow_ref: example-org/verireel/.github/workflows/publish-image.yml@refs/heads/main
event_name: push or workflow_dispatch
allowed product: verireel
allowed contexts: verireel
allowed actions:
  - verireel_testing_deploy.execute
  - verireel_stable_environment.read
  - deployment.write
```

```text
repository: example-org/verireel
workflow_ref: example-org/verireel/.github/workflows/promote-image.yml@refs/heads/main
event_name: workflow_dispatch
allowed product: verireel
allowed contexts: verireel
allowed actions:
  - verireel_stable_environment.read
  - backup_gate.write
  - verireel_prod_deploy.execute
  - verireel_prod_promotion.execute
  - verireel_prod_rollback.execute
  - deployment.write
  - promotion.write
```

Odoo stable-lane example:

```text
repository: example-org/product-repo
workflow_ref: example-org/product-repo/.github/workflows/deploy-product.yml@refs/heads/main
event_name: workflow_dispatch
allowed product: example-odoo-product
allowed contexts: example-odoo
allowed actions:
  - odoo_post_deploy.execute
  - odoo_prod_backup_gate.execute
  - odoo_prod_promotion.execute
  - odoo_prod_rollback.execute
```

The initial policy engine can be config-backed and static. It does not need a
full RBAC system yet.

Human policy rules use the same reviewed policy file under `github_humans`.
The first supported roles are `read_only` and `admin`. Browser sessions can
authorize read endpoints, but POST mutation routes remain GitHub Actions OIDC
only until browser-initiated mutation workflows get a dedicated CSRF and audit
design.

Agent consumers use the same allow-list policy but are classified into a compact
subject model before diagnostics or downstream intent contracts consume them:

- `github_actions`: workflow subjects from verified OIDC claims. Read actions
  are context-only; write, prod, destructive, secret-backed, and policy-admin
  actions require explicit matching policy grants for the workflow identity.
- `terminal_agent`: trusted local terminal agents authenticated by the dedicated
  read bearer token. These subjects are always read-only context consumers and
  cannot use POST routes, product mutations, authz policy changes, destructive
  cleanup, or secret-backed actions even if a policy rule is too broad.
- `local_operator`: trusted owner terminal agents authenticated by the dedicated
  write bearer token. These subjects can use only product-config plan/apply from
  a trusted shell with a required reason and matching dry-run before apply. They
  cannot call other mutation, destructive, production, secret-backed
  non-product-config, or authz policy routes.
- `github_human`: browser-session humans with `read_only` or `admin` role from
  GitHub human policy rules or bootstrap admin email matching. Read-only humans
  are `limited_remote_user` consumers: even if a rule is accidentally broad,
  Launchplane only allows read and safe-write action families for them, scoped by
  the exact repo/product/context/action rule. Admin humans are `human_admin`
  consumers and are approval-capable for future operator-mediated intent flows,
  but direct mutation routes still require their own CSRF/audit design before
  broad browser writes are allowed.

Action names are classified for agent context as `read`, `safe_write`,
`mutation`, `prod`, `destructive`, `secret_backed`, or `policy_admin`. This
classification is diagnostic and contractual; authorization still fails closed
through the exact action/product/context policy rule.

Agent-facing authorization diagnostics include an `agent_audit` envelope with
the decision, safe reason code, agent subject, action, product, context, policy
source, policy digest, and `authz_policy` source kind. Write-intent evaluations
persist that same provenance as `launchplane_agent_write_intents` records and
return the record id so later action routes can link to durable evidence.

`POST /v1/agent/write-intents/evaluate` is a native FastAPI scoped intent
surface. It does not execute product/runtime mutations. It validates a requested
intent, maps it to the exact existing policy action, evaluates the caller's
policy grant, persists the evaluation record, and returns status, safe next
action, source URL, record id, and `agent_audit` metadata. Denied intents are
successful preflight results with `202 accepted`; route errors are reserved for
authentication, validation, and fail-closed storage-capability failures. The
terminal-agent bearer callers keep the route-specific preflight exception only
through this native path. When callers send `Idempotency-Key`, Launchplane
replays matching evaluations or rejects conflicting payloads before requiring the
write-intent record store.
Agents can use it to preflight safe rerun, preview, config, cleanup, and
promotion-dispatch candidates without receiving a generic write token or reusable
credentials. Some intents, such as product config apply and promotion dry-run,
remain dry-run-first even when the caller has the underlying policy action.
Secret-backed intents may name managed secret binding keys plus a runtime
destination. Launchplane evaluates the existing runtime key-safety policy and an
additional `.secret` policy action such as `product_config.apply.secret`, then
returns sanitized binding keys, policy ids, and finding codes only. It never
returns plaintext secret values, ciphertext, provider env dumps, or token
prefixes to the agent.

### Managed Secret Boundary

Secret writes, rotation, metadata reads, and plaintext resolution are
Launchplane service/storage responsibilities. Drivers, workers, and product
workflows must request managed-secret bundles through service-owned interfaces;
they must not query secret tables, inspect ciphertext, select encryption keys,
or fall back to provider env dumps or local files.

Routine reads return metadata only: binding keys, ids, statuses, scopes,
version ids, encryption key ids, counts, and finding codes. Plaintext resolution
is allowed only for an authorized service-side operation that immediately uses
the value, such as rendering a provider payload or preparing a worker runtime
environment after authz and runtime key-safety checks pass. A future trusted
operator reveal path must be separate, reasoned, scoped, audited, and denied by
default.

Every plaintext resolution or reveal attempt writes redacted audit evidence.
Service responses, logs, workflow artifacts, UI payloads, and agent context must
not include plaintext, ciphertext, token prefixes, provider env dumps, or secret
request bodies.

### OpenFGA Candidate Mapping

The current DB-backed policy records remain the active authorization path.
OpenFGA may replace or augment those checks only after #1327's relation model,
tuple ownership, consistency, and audit rules are implemented and proven.

Future OpenFGA checks consume the normalized subject facts described above plus
Launchplane resource facts. They should map existing service actions to generic
relations without storing real tuple assignments in this repo:

- provider inspect: `dokploy_target.inspect` and provider-target audit/read
  actions check inspect permission on a provider-neutral target resource.
- private health apply/read: `private_health_endpoint.apply` and
  `private_health_endpoint.read` check apply or read permission on a private
  health endpoint resource under the requested product context.
- deploy: driver deploy actions check deploy permission on the target context
  or deployment resource.
- promotion: prod promotion actions check promote permission on the promotion
  resource and its destination context.
- agent delegated action: `agent/write-intents/evaluate` checks delegation or
  preflight permission on an agent-intent resource before any later route can
  execute the requested action.
- policy/admin action: `authz_policy_grant.write` and related policy routes
  check policy administration permission on a Launchplane policy resource.

Managed rule-set reconciliation remains the only supported policy-write
boundary during migration. It may derive tuple proposals from active DB policy
records, compare OpenFGA decisions with existing DB decisions, and later write
provider tuples after parity is proven. Missing, ambiguous, stale, or
unreachable tuple state denies access; it must not fall back to checked-in
tuples, local files, workflow defaults, ambient GitHub CLI identity, or broader
DB grants after cutover.

For first access, `LAUNCHPLANE_BOOTSTRAP_ADMIN_EMAILS` may name comma-separated
verified GitHub email addresses that receive the `admin` role even before a
matching `github_humans` rule exists. The GitHub OAuth client requests
`user:email` so that this bootstrap path works for private profile emails.

## First API Surface

The first Launchplane service surface should focus on evidence ingress and record
writes, not on every possible operator action.

### Evidence ingress endpoints

These evidence-ingress paths are native FastAPI routes.

- `POST /v1/evidence/deployments`
- `POST /v1/evidence/backup-gates`
- `POST /v1/evidence/promotions`
- `POST /v1/evidence/previews/generations`
- `POST /v1/evidence/previews/destroyed`
- `POST /v1/evidence/runner-host-hygiene/audits`
- `POST /v1/evidence/runner-lane-registration/audits`

`POST /v1/evidence/deployments` only requires the deployment-write record-store
capability and any available idempotency store. Replay handling runs before the
deployment-write capability check so a stored response can still be returned if
the backing store is temporarily write-restricted for deployment evidence.

`POST /v1/evidence/backup-gates` only requires the backup-gate record-write
capability and any available idempotency store. Replay handling runs before the
backup-gate capability check so a stored response can still be returned if the
backing store is temporarily write-restricted for backup-gate evidence.

`POST /v1/evidence/promotions` requires any available idempotency store. Unlinked
promotion evidence requires the promotion record-write capability. When a
promotion links a deployment record, the route requires the linked deployment
read capability and the bundled promotion-evidence write capability used to
commit the promotion record and promoted inventory projection together after
validation.
Replay handling runs before those capability checks so a stored response can
still be returned if the backing store is temporarily write-restricted for
promotion evidence.

`POST /v1/evidence/previews/generations` requires any available idempotency
store plus preview record list, preview generation list, preview write,
generation write, and the bundled preview-generation evidence write capability.
The bundled write commits the generation record and transitioned preview record
together after request validation, avoiding a partial initial-preview write on
native FastAPI ingestion. Replay handling runs before those capability checks so
a stored response can still be returned if the backing store is temporarily
write-restricted for preview generation evidence.

`POST /v1/evidence/previews/destroyed` requires any available idempotency store
plus preview record list and preview write capability. It transitions an
existing Launchplane preview record to `destroyed`; it does not create missing
previews. Replay handling runs before those capability checks so a stored
response can still be returned if the backing store is temporarily
write-restricted for preview destroyed evidence.

`POST /v1/evidence/runner-host-hygiene/audits` requires any available
idempotency store plus runner-host hygiene audit write capability. It accepts
only product/context `launchplane/launchplane`, writes the typed audit record,
and returns the accepted record key plus result details for the stored audit.
Replay handling runs before the audit-write capability check so a stored
response can still be returned if the backing store is temporarily
write-restricted for runner-host hygiene audit evidence.

`POST /v1/evidence/runner-lane-registration/audits` requires any available
idempotency store plus runner-lane registration audit write capability. It
accepts only product/context `launchplane/launchplane`, writes the typed audit
record, and returns the accepted record key plus result details for the stored
audit. Replay handling runs before the audit-write capability check so a stored
response can still be returned if the backing store is temporarily
write-restricted for runner-lane registration audit evidence.

### Preview lifecycle endpoints

- `POST /v1/previews/lifecycle-plan`
- `POST /v1/previews/lifecycle-cleanup`
- `POST /v1/previews/lifecycle-sweep`
- `POST /v1/previews/desired-state`
- `POST /v1/previews/pr-feedback`

The first preview lifecycle endpoint remains the source of the durable decision:
Launchplane can discover desired preview anchors from GitHub PR label state,
record that desired-state scan, compare the anchors with the latest recorded
provider inventory scan, write a durable lifecycle plan, and return
keep/orphaned/missing sets. Cleanup execution uses a second endpoint that
requires an existing lifecycle `plan_id`; it defaults to `apply=false`
report-only behavior and records the cleanup request/result next to the plan.
Destructive provider cleanup is only attempted when `apply=true` is explicitly
supplied by an authorized GitHub Actions workflow.

`POST /v1/previews/desired-state`, `POST /v1/previews/lifecycle-plan`,
`POST /v1/previews/lifecycle-cleanup`, and
`POST /v1/previews/lifecycle-sweep` are native FastAPI routes.
Desired-state discovery requires
`preview_desired_state.discover` authorization for the requested product/context,
requires storage that can persist `PreviewDesiredStateRecord`, preserves optional
`Idempotency-Key` replay/conflict behavior for successful scans, and returns the
stored scan as accepted evidence.

`POST /v1/previews/lifecycle-plan` requires
`preview_lifecycle.plan` authorization for the requested product/context,
preserves optional `Idempotency-Key` replay/conflict behavior, writes the typed
preview lifecycle plan record.

`POST /v1/previews/lifecycle-cleanup` requires
`preview_lifecycle.cleanup` authorization for the requested product/context,
preserves optional `Idempotency-Key` replay/conflict behavior, requires storage
that can read lifecycle plan records and write cleanup records, and additionally
requires preview read/write capability before any `apply=true` provider destroy
mutation starts. It rejects missing or product-mismatched `plan_id` values before
writing cleanup state and returns the stored cleanup record as accepted evidence.

`POST /v1/previews/lifecycle-sweep` derives enabled preview profiles from
Launchplane product-profile records, requires both `preview_lifecycle.plan` and
`preview_lifecycle.cleanup` authorization for every selected profile before any
inventory, desired-state, plan, or cleanup mutation starts, requires storage
that can read product profiles and preview/inventory history and write preview,
inventory, desired-state, lifecycle plan, and cleanup records, preserves optional
`Idempotency-Key` replay/conflict behavior, and returns the sweep summary as
accepted evidence.

PR feedback delivery is part of the same preview lifecycle boundary. Product
repos submit thin preview outcome facts to `POST /v1/previews/pr-feedback`;
Launchplane renders the review comment, upserts the anchored GitHub PR comment
when its runtime token is available, and stores an append-only feedback record
with the comment body, delivery action, comment URL, and any skip/failure reason.
The route is native FastAPI; it requires a store capable of writing preview PR
feedback records, preserves optional `Idempotency-Key` replay/conflict behavior,
and supports dry-runs that evaluate authorization without writing records or
comments.
Workflows can be granted explicit `preview_pr_feedback.write`, or generic-web
preview workflows can reuse their matching lifecycle grants: refresh-capable
workflows may report pending/ready/failed feedback, and destroy-capable workflows
may report destroyed/cleanup-failed feedback. Unsupported/fork notices still
require the explicit feedback grant because they are outside the normal
refresh/destroy flow.

### Product profile endpoints

- `GET /v1/product-profiles`
- `GET /v1/product-profiles/{product}`
- `POST /v1/product-profiles`
- `POST /v1/product-profiles/expected-config/apply`
- `POST /v1/product-profiles/health-monitoring/apply`
- `POST /v1/product-profiles/prelaunch-rebuild/apply`
- `POST /v1/product-profiles/preview-tls/apply`

Product profiles are Launchplane-owned product/driver bindings. They are written
through native FastAPI authenticated service ingress and stored in Launchplane
records; product repos do not carry repo-local Launchplane lifecycle manifests.
Writes require `product_profile.write` for the profile product in the
Launchplane service context, validate the profile write contract before storage,
and preserve optional `Idempotency-Key` replay/conflict behavior.

Expected-config apply is a narrower metadata mutation for runtime contract
requirements already owned by product profiles. It requires
`product_profile.expected_config.apply` for the target product in the
Launchplane service context, loads the existing DB-backed product profile, and
appends supplied runtime keys or managed secret binding requirements only when
absent. Dry-run returns the same redacted added/unchanged summary without
writing. Apply updates only the profile `expected_config`, `updated_at`, and
`source` fields; callers must use the live-target-runtime workflow afterward to
sync live provider environment. The route does not accept secret plaintext,
runtime values, or checked-in product catalogs, and workflow authority for real
products must be granted through operator-supplied authz input.

Health-monitoring apply is an exact-instance mutation for one stable-lane
`public_http` or `private_http` check plus the lane's typed `public`, `private`,
or `prelaunch` monitoring intent. Dry-run requires
`product_profile.health_monitoring.plan`; apply requires the separate
`product_profile.health_monitoring.apply` action, the reviewed plan SHA-256, and
an idempotency key. Both actions authorize against the request product, context,
and exact instance. The request excludes URL, domain, provider, proxy,
certificate, and full-profile fields. Launchplane preserves an existing public
check URL or derives it from the current lane profile. Private checks carry only
a registered endpoint key; the service requires an active private endpoint
record owned by the exact lane and never returns or logs its internal URL.
Strict public runtime-identity checks must resolve to a lane-owned HTTPS host.
The requested intent must retain its required enabled public or private check.

Prelaunch-rebuild apply is an exact-instance mutation for one Odoo stable lane's
`odoo_prelaunch_rebuild` policy. Dry-run requires
`product_profile.prelaunch_rebuild.plan`; apply requires the separate
`product_profile.prelaunch_rebuild.apply` action, the reviewed plan SHA-256, and
an idempotency key. Both actions authorize against the request product, context,
and exact instance and require an immutable reusable-workflow identity. The
request may carry only policy approval/source/confirmation/target/domain proof
fields. Launchplane requires `prelaunch` monitoring intent, validates the source
against the separately stored lane data policy, and requires `resettable` data
authority for an `empty` rebuild. It does not accept or mutate data authority,
health checks, routes, provider identifiers, runtime settings, secrets, volume
names, or a replacement product profile. Whole-profile writes and onboarding
updates preserve existing prelaunch-rebuild authority after this bounded path is
established.

The plan binds the complete current profile digest. Apply rebuilds the candidate
from fresh DB-backed state and commits the profile compare-and-write with
completed replay evidence atomically, so reviewed-plan drift and concurrent
profile changes fail stale. Existing monitoring authority cannot be changed by
the broad product-profile write route or overwritten by onboarding; callers use
this reviewed bounded endpoint instead.

Before authentication or JSON parsing, the ASGI boundary requires
`application/json`, exactly one bounded `Content-Length`, no transfer encoding,
and no more than 64 KiB of declared or observed request body.

Preview TLS apply is a field-bounded mutation for Odoo-driver profiles and
`preview.domain_certificate_type`. It requires
`product_profile.preview_tls.apply` for the target product in the Launchplane
service context, loads the profile from DB-backed storage, and returns a fresh,
stateless dry-run plan containing only the current/requested certificate values,
the profile timestamp, and a canonical plan SHA-256. Apply requires the reviewed
SHA-256 and an `Idempotency-Key`; changed reviewed inputs or a profile-row change
during apply make the operation stale. A successful apply uses an atomic
compare-and-write, rebuilds from the current stored record, and changes only the
preview certificate value plus `updated_at` and server-owned `source`;
DB-clock preflight rejects active claims, preserves reconciliation-bound claims,
and releases only expired unbound orphans that cannot have committed this
DB-only atomic write. The transaction inserts a typed `running` mutation
reservation before the profile write and commits the profile plus `completed`
replay evidence together, even when the requested value is already current.
Concurrent same-key requests cannot both write; matching requests replay the
committed response and changed fingerprints fail with
`409 idempotency_key_reused`. The operator workflow
receives the real target product as dispatch input, and its product-specific
authz rule comes from operator-managed desired state reconciled through the
service rather than checked-in runtime authority.

Public ingress notification policy writes use
`POST /v1/public-ingress/notification-policies/apply`. The request carries
`mode: "dry-run"` or `mode: "apply"` and a complete
`PublicIngressNotificationPolicyRecord`. Apply requires
`public_ingress_notification_policy.apply`, DB-backed Launchplane storage, and
an idempotency key when a caller wants retry-safe service semantics. Local
operator calls must include a non-empty reason. Policies store routing intent and
managed secret record ids only, plus a reminder interval bounded from 15 minutes
through seven days. Existing policies migrate to the generic six-hour cadence.
Discord webhook URLs, SMTP credentials, and operator destination values must not
be encoded in text-file defaults or source. Dry-run and apply summaries return
the effective reminder interval without returning secret material.

The Product Ops incident list and detail routes are read-only projections over
the durable incident record family; they do not add a second incident authority
or expose notification-policy mutation. They scope every incident id back to the
product profile's exact context and instance and fail closed when DB-backed
incident/outbox capabilities are unavailable. The detail projection may expose
provider-safe external delivery links and bounded delivery state, but not
destination ids, policy ids, raw outbox payloads, provider operation keys,
provider ids, raw target URLs, secret references, or provider error text.

Every Code notification policy writes use
`POST /v1/every-code/notification-policies/apply`. The request carries
`mode: "dry-run"` or `mode: "apply"` and a complete
`EveryCodeNotificationPolicyRecord`. Apply requires
`every_code_notification_policy.apply`, DB-backed Launchplane storage, and an
idempotency key when a caller wants retry-safe service semantics. Local operator
calls must include a non-empty reason. Policies store repository-scoped routing
intent and managed secret record ids only; Discord webhook URLs and operator
destination values must stay in managed secrets, not source or text-file
defaults. When a worker status update transitions a work request to `blocked`,
Launchplane persists the blocked request first, then attempts configured Every
Code notifications and records delivered or failed attempts under
`GET /v1/every-code/notification-attempts`.

Preview PR feedback notification policy writes use
`POST /v1/previews/pr-feedback/notification-policies/apply`. The request
carries `mode: "dry-run"` or `mode: "apply"` and a complete
`PreviewPrFeedbackNotificationPolicyRecord`. Apply requires
`preview_pr_feedback_notification_policy.apply`, DB-backed Launchplane storage,
explicit product and context scope, and an idempotency key when a caller wants
retry-safe service semantics. Local operator calls must include a non-empty
reason. Policies store product/context/repository-scoped routing intent and
managed secret record ids only; Discord webhook URLs and operator destination
values must stay in managed secrets, not source or checked-in workflow defaults.
When `/v1/previews/pr-feedback` records skipped or failed PR comment delivery,
Launchplane attempts configured preview PR feedback notifications and records
delivered or failed attempts under
`launchplane_preview_pr_feedback_notification_attempts`; operators can read
those attempts with `GET /v1/previews/pr-feedback/notification-attempts`.

Edge endpoint writes use `POST /v1/edge-endpoints/apply`. The request carries
`mode: "dry-run"` or `mode: "apply"`, a complete `EdgeEndpointRecord`, a reason,
and exact confirmation text for apply mode. Apply authorizes with
`edge_endpoint.apply` against product/context `launchplane`/`launchplane`,
requires an `Idempotency-Key` header before mutation, and continues to support
Launchplane record stores that implement the edge endpoint read/write methods.
Dry-runs plan the accepted response without writing the record.

Private health endpoint writes use `POST /v1/private-health-endpoints/apply`.
The request carries `mode: "dry-run"` or `mode: "apply"`, a complete
`PrivateHealthEndpointRecord`, a reason, and exact confirmation text for apply
mode. Apply authorizes with `private_health_endpoint.apply` against the
request endpoint's product/context, requires an `Idempotency-Key` header before
mutation, rejects public URLs through the private endpoint contract validator,
and rejects cross-product/context/instance overwrites for an existing endpoint
key. The accepted response preserves the legacy private-health apply envelope:
the endpoint key/status remain in `result`, while `records` stays empty for
compatibility with existing replays.

Product config writes use `POST /v1/product-config/apply`. The request carries
`mode: "dry-run"` or `mode: "apply"`, product/context/instance, non-secret
runtime values, and write-only managed secret values. Dry-run requires the
`product_config.plan` action; apply requires `product_config.apply`. The route
accepts GitHub Actions OIDC callers, signed-in GitHub human sessions, and the
dedicated local-operator bearer credential, but terminal-agent read bearer
credentials remain read-only and cannot execute the mutation. Signed-in humans
and local operator/admin identities require a non-empty `reason`; their apply
requests also require an `Idempotency-Key` and a previously recorded matching
dry-run. Matching uses the normalized target, runtime input, and managed-secret
input after unifying legacy aliases and defaults, while excluding mode, reason,
confirmation, and source label. This permits a different apply reason without
changing the reviewed runtime or secret content. Idempotency replay is checked
before the matching-dry-run marker so an already completed apply remains
replayable.

The signed-in browser uses the narrower product-owned operation
`POST /v1/products/{product}/environments/{environment}/config/apply`, which is
the only product-config operation in the generated UI write allowlist. It
accepts signed-in GitHub human and configured local operator/admin identities;
GitHub Actions automation continues to use the generic route. Its body contains
mode, reason, exact apply confirmation, runtime settings, or managed secrets;
runtime and secret inputs cannot be combined in one browser request.
Launchplane resolves product, context, instance, scope, and source label from
the stored product profile and lane. Managed-secret inputs identify a declared
binding by both integration and binding key; Launchplane resolves that pair to
the stored requirement and rejects keys or bindings not declared for that
environment. Apply requires exact confirmation text
`APPLY {product}/{environment}` in addition to the matching dry-run and stable
idempotency key.

The matching config read route exposes `write_availability` separately for
runtime settings and managed secrets. It reports plan/apply authz, DB-backed
storage readiness, managed-secret encryption readiness, runtime key-safety
policy readiness for every applicable runtime binding and target, exact
blockers, confirmation text, and dry-run/idempotency requirements. Only the
DB-backed store accepted by the mutation route is reported as storage-ready.
Browser forms fail closed when this authority is unavailable or stale.

The route authorizes the top-level product/context/instance target and rejects
nested runtime or secret targets that try to broaden or change that authorized
target. It reuses the same planner/writer as `launchplane product-config apply`,
returns only actions, keys, counts, actor/source metadata, and secret IDs, uses
generic validation messages for rejected requests, and fails closed when the
record store is not DB-backed, when a secret bundle is submitted without
valid `LAUNCHPLANE_SECRET_KEYS_JSON` (or the migration-only legacy
`LAUNCHPLANE_MASTER_ENCRYPTION_KEY`) in the trusted Launchplane runtime, or when
there is no active runtime key-safety policy that allows the requested managed
secret binding for the target runtime class. Request bodies for this route must
not be copied into logs, issues, docs, or workflow artifacts because they can
contain plaintext secret values.

Product-config dry-run continuity markers and idempotency request fingerprints
covering secret input are persisted only as purpose-separated HMACs keyed from
the active managed-secret root. Concurrent same-key applies that race after the
initial replay lookup converge by re-reading the completed idempotency record
after a transactional write conflict and returning the winner as a replay.

`POST /v1/secrets/reencrypt` is a legacy migration boundary. It refuses
`mode: "dry-run"` with `privileged_operation_planning_required` and
`mode: "apply"` with `privileged_operation_approval_required`; neither
`secret.reencrypt.dry-run` nor `secret.reencrypt.apply` is current effect
authority. Shared and production root rotation instead uses the typed
browser-human privileged-operation plan/approval routes and the supervised
DB-backed worker. The worker is the only service execute path, reauthorizes the
immutable approver before terminal work, and emits redacted counts/statuses
only. Approval may be claimed immediately, so revocation is possible only
before worker claim. Direct CLI apply remains an explicit
bootstrap/recovery-only path guarded by `--allow-direct-db-mutation`, not routine
shared or production authority.

#### Product-config secret source contract

The product-config apply route supports exactly one secret value source today:
write-only plaintext supplied inside the HTTPS request by an approval-capable
operator surface. The value is accepted only for the duration of request
processing, is written into Launchplane managed-secret storage on apply, and is
never returned. This source is appropriate for the signed-in operator UI and for
explicit local-owner operator automation that already holds private credentials
outside the repository.

Helpers and agents must not ask for, echo, persist, or pass plaintext secret
values through chat messages, CLI arguments, issue/PR bodies, workflow logs, or
helper output. For helper-driven product-config work, use this sequence instead:

1. Preflight intent and policy with `POST /v1/agent/write-intents/evaluate`,
   naming `intent: "product_config_apply"`, `mode: "dry_run"`, product/context,
   source URL, reason, optional `secret_bindings`, and the runtime destination.
2. Report only the returned status, reason code, record id, binding keys,
   runtime key-safety finding codes, trace id, and next action.
3. Hand off any new or changed plaintext secret value entry to the signed-in
   operator UI or to explicitly configured local-owner operator automation.
4. Call `POST /v1/product-config/apply` only when the caller has a safe private
   value source and explicit operator intent; dry-run before apply.

The service does not currently support committed secret references, provider env
lookups, stdin/stdout secret transport, arbitrary secret IDs supplied by an
agent, or a request shape that says "reuse the current managed secret value".
Those shapes are unsupported and must fail closed in clients instead of being
translated into a product-config request. Existing managed-secret binding keys
are safe to name only as metadata for intent evaluation, runtime key-safety
checks, and redacted reporting. They are not a plaintext source for this route.

Public-safe examples may show key names and placeholder binding keys, but never
real values:

```json
{
  "intent": "product_config_apply",
  "mode": "dry_run",
  "product": "example-product",
  "context": "example-testing",
  "source_url": "https://github.com/example/repo/issues/123",
  "reason": "Preflight managed secret-backed product config.",
  "secret_bindings": ["EXAMPLE_API_TOKEN"],
  "destination": {
    "kind": "runtime_environment",
    "context": "example-testing",
    "instance": "web"
  }
}
```

Product-config responses are public-safe only after redaction. Clients may
report `status`, `trace_id`, record ids, `result.mode`, runtime key names,
secret `binding_key` values, action names such as `created` or `unchanged`,
counts, `runtime_key_safety.status`, finding codes, and `next_actions`. Clients
must not report request bodies, runtime values, secret plaintext, ciphertext,
provider environment dumps, token prefixes, master-key env names from service
internals, or private hostnames.

Common failure classes are stable enough for helper summaries:

- `authentication_required`: no valid OIDC token, browser session, or allowed
  local-operator bearer credential.
- `authorization_denied`: caller lacks `product_config.plan`,
  `product_config.apply`, or the product/context grant.
- `reason_required`: local-operator calls omitted a concrete reason.
- `matching_dry_run_required`: local-operator apply did not match a prior
  recorded dry-run request.
- `secret_configuration_required`: trusted Launchplane runtime cannot write
  managed secrets.
- `runtime_key_safety_unavailable` or `runtime_key_safety_failed`: the active
  runtime key-safety policy is missing or rejects the requested binding. Policy
  payloads may include paired `allowed_targets` metadata for exact context plus
  dynamic preview instance patterns; they must not include secret plaintext or
  checked-in runtime authority.
- `invalid_request`: malformed payload, secret-shaped runtime key, or nested
  runtime/secret target override.

After apply, `next_actions` can require `live_target_runtime_apply`; helpers
should surface that action and stop. Applying product-config records does not by
itself guarantee the live target process has been synchronized.

Runtime key-safety policy reconciliation uses
`POST /v1/runtime-key-safety/policies/apply`. It is restricted to workflows with
`runtime_key_safety.write` for product/context `launchplane`, requires DB-backed
storage, and writes metadata-only policy records for managed runtime secret
binding keys. Optional `Idempotency-Key` headers preserve replay/conflict
semantics for retry-safe service calls. It merges requested rules into the
latest active policy by binding key so deploy-time bootstrap can add required
classifications without dropping existing policy coverage. Request and response
payloads must not include secret plaintext. Rules can carry exact
context/instance scope and explicit preview instance patterns for dynamic PR
lanes; policy apply merges those scopes additively without making checked-in
files runtime authority.

Product onboarding uses the native FastAPI
`POST /v1/product-onboarding/apply` route. Conventional generic-web onboarding
accepts typed product and immutable repository identity, exposes a no-write
dry-run digest, and requires that exact digest plus the provider-resolved target
id for apply. The resulting bundle includes the product profile, one testing
lane, Dokploy-backed target records, target-id records, preview policy, and any
declared runtime-environment or disabled managed-secret binding placeholders.
The conventional generic-web contract requires an operator-supplied root
preview base URL and writes it as the context-scoped
`LAUNCHPLANE_PREVIEW_BASE_URL` runtime-environment record. The reviewed digest
binds that mutable runtime value; no real domain becomes checked-in authority.
Repeated onboarding merges that owned key into the existing preview-context
record instead of replacing unrelated runtime values.
Generic-web dry-run requires `generic_web_onboarding.plan`; apply and advanced
manifest writes require `product_onboarding.apply`, all for product/context
`launchplane`. The route requires DB-backed storage and returns only sanitized
`provider_target*` summaries.

The manual `Product Onboarding` workflow is the supported conventional caller.
It resolves numeric GitHub repository and owner ids with a narrowly scoped
GitHub App token, plans `create-application` through
`POST /v1/dokploy-targets/setup`, plans the product bundle, and requests a
complete generic-web preview authz reconciliation plan from
`POST /v1/authz-policies/managed-rule-sets/generic-web-preview/plan`. Apply is
one protected review followed by provider setup, record apply, and the existing
digest-bound managed authz reconcile endpoint. A target/record partial success
is safe to replay with the same stable idempotency keys; a later authz failure
leaves the product unauthorized rather than silently broadening access.

The authz planning route requires `generic_web_preview_authz.plan`, reads the
active DB-backed policy, retains unrelated managed rules, and returns a complete
dry-run reconcile envelope. It never writes policy. The existing
`POST /v1/authz-policies/managed-rule-sets/reconcile` route remains the sole
writer and requires `authz_policy_grant.write` for apply. The advanced
`Product Onboarding Manifest (Advanced)` workflow preserves operator-supplied
manifest support for non-conventional products. Manifests must use neutral
`provider_targets`; obsolete `dokploy_targets` input is rejected. Product
records are never loaded from checked-in catalogs or product repos.
The manual `Generic Web Preview Authorization` workflow is the operator surface
for reviewed onboarding, expand/contract rotations, and product-rule retirement
through this same planner and writer contract.

For `onboard`, `expand`, and `contract`, that workflow retains its live GitHub
App repository metadata resolution. For `retire`, it mints no repository-scoped
App token and makes no GitHub API request: the service derives one immutable
repository authority from the current target product's
`operator.generic-web-preview` rules. Launchplane-owned ingress-operator rules
are excluded only from that identity derivation and are still removed with every
other target-product rule. A matching product profile is cross-checked when it
exists; legacy name-only rules require that profile to supply both immutable
IDs. Caller repository values are optional assertions for retirement, never
authority. Missing target/product-repository rules, ambiguity, incomplete
identity, profile disagreement, partial or mismatched assertions, and
`include_ingress_operator=true` fail closed. The plan returns bounded authority
sources, managed rule IDs/count, and a SHA-256 identity digest, never raw numeric
repository IDs; the existing plan digest, policy CAS, protected apply, and
idempotency boundaries remain unchanged.

Provider-target operations use the native FastAPI
`POST /v1/provider-targets/operations` route. The route accepts one
Launchplane-owned route at a time with mode `audit`, `backfill-dry-run`, or
`backfill-apply`, `provider_id`, `context`, `instance`, and an apply-only
`reason`. It requires DB-backed storage and authorizes through
`provider_target.audit` for audit/dry-run or `provider_target.backfill` for
apply, always scoped to product/context `launchplane`. Apply requests are
idempotency-keyed and write only complete non-conflicting Dokploy target/id
projections; existing rows and conflicts are reported rather than overwritten.
The manual `Provider Target Operations` workflow is the supported shared and
production caller for backfill evidence.

Launchplane self-deploy uses the native FastAPI
`POST /v1/drivers/launchplane/self-deploy` route. The route executes only the
Launchplane-owned self-deploy workflow, requires
`launchplane_service_deploy.execute` authority for product/context
`launchplane`, and preserves optional `Idempotency-Key` replay/conflict
behavior for retry-safe deploy requests. Runtime target identity, image
references, OAuth environment changes, and provider credentials remain
operator-supplied runtime inputs or managed secrets rather than checked-in
authority.

Dokploy target setup uses the native FastAPI
`POST /v1/dokploy-targets/setup` route. The route is the service-owned path for
adopting an existing Dokploy target or creating a new application/compose target
while immediately writing the matching Dokploy target, target-id, and
provider-target records. Dry-run accepts the narrow `dokploy_target.plan`
action or the backwards-compatible broader `dokploy_target.setup` action;
apply always requires `dokploy_target.setup`, all for product/context
`launchplane`. Apply requires exact confirmation, an operator reason, and an
idempotency key. Apply
requests keep the `Idempotency-Key` replay/conflict contract; dry-runs remain
repeatable and are not stored as idempotency responses. The manual
`Dokploy Target Setup` workflow is the supported shared and production caller;
product repos must not store live target IDs or provider fixtures as setup
authority. Runtime port is accepted only for `create-compose` domain
reconciliation with at least one domain. Domain pruning is restricted to tracked
compose targets and explicit domain hosts; dry-run reports matched provider
domain ids, while apply deletes only those matching ids and updates the tracked
target domain list. If a provider create succeeds but the service fails before
records are written, recover by re-running the workflow with `operation=adopt`
and the created provider target id, not by creating a second target for the same
lane.

The same route exposes the narrow `repair-domain-authority` operation for
application and compose targets. It reads the current target, target-id, and
provider-target records, fences the exact target identity and expected current
provider-target projection, reads the live Dokploy target and its domains, and
requires the requested normalized bare DNS host tuple to exactly match the live
provider hosts. It never mutates Dokploy. Dry-run returns the projected target
record without writes. Apply compare-and-writes only `domains`, `updated_at`, and
`source_label` on the target plus `updated_at` and `source_label` on the matching
provider-target projection; every authority and identity field remains
unchanged. The operation
uses the dedicated `dokploy_target.repair_domain_authority` authorization action
for apply and `dokploy_target.repair_domain_authority.plan` for dry-run. It
remains fail-closed when any tracked or live identity/evidence is missing or
changed.

Dokploy target inspect uses the native FastAPI
`GET /v1/dokploy-targets/inspect` route. The route is a read-only proof surface
for provider identity before an adoption, creation, or repair: callers may pass
either `context` and `instance` to inspect the current tracked target, or
`target_type` and `target_id` to inspect an explicit provider target. It
requires `dokploy_target.inspect` authz for product/context `launchplane`, reads
Dokploy through Launchplane-managed secrets, and returns a redacted identity
summary only: target ids, names, project/environment/server identity, domain
summaries, source metadata, and environment key names/counts. It must not return
raw provider payloads or environment values. The manual `Dokploy Target Inspect`
workflow is the supported shared and production caller when operators need
provider evidence without mutating Dokploy or Launchplane records.

Callers may additionally provide an exact compose `service`, expected immutable
`expected_image`, and an optional allow-listed structured `event` name for
diagnostics. That bounded
runtime-evidence mode resolves exactly one service container for both image and
bounded internal identity evidence, reads only its state and immutable image
identity, and searches at most 1,000 redacted candidate log lines from the last
day for independent provider-error classification. When `event` is present, it
performs a second read using the code-owned allow-listed event name as the
provider-side fixed-string filter before requiring an exact JSON event field
match. It also reads the DB-backed
privileged-operation worker heartbeat projection and internally compares the
heartbeat's hashed runtime hostname with the hash of Dokploy's provider-observed
container hostname. The provider hostname is accepted only when it is a
Docker-assigned hexadecimal prefix of the selected container ID; configured
custom hostnames fail closed. It returns image-match state, bounded
heartbeat freshness/identity status, event counts, bounded JSON/non-JSON line
counts, fixed provider-error classification, and a `proof_ready` decision;
it never returns container IDs, container config, environment values,
configured mutable image text, worker identity digests, hostnames, or raw log
lines. Provider-error kinds are
`unsupported_logging_driver`, `container_not_found`, `docker_daemon_error`, and
`provider_command_failed`. The first recognized non-JSON provider error sets the
reported kind while every recognized provider-error line is counted. Missing or
ambiguous containers, invalid image identity, and provider failures fail closed.
The manual workflow treats a requested runtime proof as failed unless the
service is running, its immutable configured image exactly matches the
operator-supplied expected image, and a fresh heartbeat matches the same
provider-observed container identity and image. Heartbeat timestamps more
than 60 seconds in the future fail closed; freshness is bounded to four poll
intervals with a 120-second floor and 900-second ceiling. Missing, stale,
future-dated, identity-mismatched, or image-mismatched heartbeat records fail
closed. Other fresh worker rows remain diagnostic because only the exact
provider-selected container identity can satisfy proof. Structured events and
provider log classification are diagnostic only. A log-read failure produces
bounded `unavailable` diagnostics instead of a 503 and does not override a
valid heartbeat proof.

Provider failures expose only a bounded operation stage such as
`provider-config`, `target-inspect`, `container-list`, `service-select`,
`container-config`, `container-identity`, or `image-identity`; raw provider
messages remain excluded.

Runtime-event diagnostics and heartbeat proof remain under
`dokploy_target.inspect` because the
service accepts only code-owned allow-listed event names and returns counts, not
log content, while the DB heartbeat is read-only evidence from existing
Launchplane records rather than a new authorization surface. It is not an
alternate arbitrary log-read surface; caller-selected
log text and raw lines remain exclusively behind `target_logs.read` and exact
context/instance authorization.

The manual `Product Environment Evidence` workflow is the supported read-only
caller for product environment read-model evidence. It uses GitHub OIDC and
`product_environment.read` to call `GET
/v1/products/{product}/environments/{environment}` for the requested target set,
then uploads sanitized summaries only. It must not upload raw product
environment responses because those responses can include provider target
identifiers, runtime key names, managed-secret binding keys, and operational
metadata.

Live target runtime sync uses the native FastAPI
`POST /v1/live-target-runtime/apply` route. The route accepts `mode: "dry-run"`
or `mode: "apply"`, product/context/instance, and optional apply-only deploy
controls. Dry-run requires `live_target_runtime.plan`; apply requires
`live_target_runtime.apply`. The route resolves DB-backed runtime environment
records, managed runtime secrets, and the tracked Dokploy target in the deployed
Launchplane service, evaluates runtime key-safety policy, compares desired and
live env by key, and returns sanitized key/count evidence without runtime values
or secret plaintext. Apply updates only the product profile's expected runtime
environment keys and runtime managed-secret binding keys for the selected lane,
preserves unrelated live env, verifies persistence by key metadata, and can
explicitly trigger a deploy when requested.

Live target runtime applies are service-boundary work. Operators and agents must
not run local CLI live-target mutation commands from arbitrary checkouts to make
shared or production changes, because the local process may lack DB-backed
tracked target authority or use stale bootstrap context. Use the deployed
service route or a workflow that calls it so Launchplane can authorize with
OIDC/session identity, resolve current DB-backed target records in the deployed
runtime, and audit sanitized key/count evidence.

Generic web deploys use native FastAPI
`POST /v1/drivers/generic-web/deploy`. The request names the product, target
instance, immutable artifact/image reference, and source ref; Launchplane
resolves the context from the DB-backed product profile lane and the runtime
target identity from DB-backed provider-target records. Dokploy target records
remain provider execution configuration for Dokploy-backed lanes and must agree
with the provider-target identity before deploy proceeds. The route keeps
optional `Idempotency-Key` replay/conflict handling and stores deploy-pass plus
post-deploy-fail evidence to prevent repeating the completed provider mutation.
This image-backed route is the canonical product-repo integration surface for
simple generic-web services. `cbusillo/repairshopr_api` proved the path in live
Launchplane after #1503 deployed: Launchplane Deploy run `28415366430` attempt 4
called `/v1/drivers/generic-web/deploy` with immutable GHCR image identity and
received `deploy_status: pass` for deployment record
`deployment-20260630T034901Z-repairshopr-sync-prod` on Dokploy target
`cm-repairshopr-sync`.
The former generic-web source-ref deploy route is retired. Products that once
depended on Launchplane temporarily rewriting provider source refs must move to
the image-backed generic-web deploy route instead: the product repo publishes an
immutable artifact, Launchplane validates DB-backed product/profile identity,
and Launchplane mutates the provider using stable target records. Do not add
new product-repo direct provider mutation to replace the retired route.
Product environment reads expose neutral provider-target identity only from
explicit provider-target rows. Paired DB-backed Dokploy target and target-id
records remain visible as provider-specific execution/history metadata and as
audit/backfill comparison material; they no longer synthesize current
provider-target authority when an explicit row is missing.

Generic web prod promotion uses native FastAPI. Trusted automation can exercise
the descriptor routes `POST /v1/drivers/generic-web/prod-promotion` and
`POST /v1/drivers/generic-web/prod-promotion-workflow`; direct browser calls to
the raw promotion route remain dry-run only, and operator identities are
rejected on the raw workflow-dispatch route. The generated operator UI instead
uses product/environment routes for promotion status, direct dry-run, workflow
dispatch, and workflow-delivery status. Those routes derive product, context,
testing/prod lanes, immutable artifact, source revision, provider target,
repository, and workflow identity from DB-backed records. Client bodies cannot
select those values.

The product-owned workflow route requires an accepted direct dry-run marker
matching current testing and production evidence plus bump mode. Live dispatch
also requires the exact server-provided confirmation. The workflow outbox input
includes the reviewed artifact and revision so the raw generic-web promotion
route can reject inventory drift at workflow execution time. The HTTP response reports
`dispatch_status=pending` and `records.outbox_delivery_id`; Launchplane outbox
workers later resolve the managed `GITHUB_TOKEN`, capture the pre-dispatch run
set, persist the provider marker, send the workflow dispatch, and record the
observed workflow run. Once a provider marker exists, reconciliation may only
observe that dispatch; it never sends the workflow again. That
workflow remains responsible for product release/tag behavior while Launchplane
supplies authz, managed token lookup, dispatch inputs, and workflow-run
observation. Native FastAPI owns both paths while descriptor discovery remains
available.

Generic web deploy and prod-promotion responses expose provider-neutral target
metadata with `target_category`, `provider_id`, and `provider_target_type`.
The legacy response-only `target_type` alias is retired; Dokploy execution
configuration still uses provider-specific target type fields internally where
application-vs-compose behavior is required.

Generic web prod rollback planning and apply use native FastAPI routes:
`POST /v1/drivers/generic-web/prod-rollback-plan` and
`POST /v1/drivers/generic-web/prod-rollback`. Both routes resolve the product
profile and destination lane before authorization, then authorize against the
lane context stored in Launchplane rather than request-supplied runtime
authority. Rollback planning writes a `GenericWebRollbackPlanRecord` without
mutating the provider. Rollback apply re-runs planning, applies ready plans via
the common generic-web deploy path, and preserves the post-deploy extension hook
for drivers such as Odoo that inherit generic-web behavior. Optional
`Idempotency-Key` replay is available for non-blocked plan results and for
successful apply results; blocked results and ordinary deploy failures are left
uncached so a later retry can observe recovered Launchplane/provider state.
Apply results where deploy passed but post-deploy failed are cached because the
provider mutation already occurred. The descriptors remain discoverable, and
native FastAPI owns both paths.

Generic web preview desired-state discovery uses
`POST /v1/drivers/generic-web/preview-desired-state`, now owned by native
FastAPI. The request names the product and optional pull-request label/page
limit; Launchplane resolves the repository, preview context, anchor repo, and
preview slug template from the DB-backed product profile before recording
desired preview state. Authorization uses the resolved profile product and
preview context, not caller-supplied runtime authority.

Generic web preview refresh uses native FastAPI
`POST /v1/drivers/generic-web/preview-refresh`. The request names the product,
anchor pull-request number, and immutable image reference. Launchplane resolves
the repository, preview context, and preview slug policy from the DB-backed
product profile before authorization, derives the preview slug from the anchor
pull-request number, and derives the canonical live preview URL from the
context-level `LAUNCHPLANE_PREVIEW_BASE_URL` runtime-environment record plus the
slug. Product workflows may send `anchor_pr_url` and `anchor_head_sha` when the
workflow has precise anchor metadata. `preview_slug` and `preview_url` remain
accepted as compatibility overrides but are not product-repo authority for new
workflows; a supplied slug is rejected when it conflicts with the product profile
slug policy. Preview health failures that return Dokploy Dead Host are
classified as public preview ingress failures so workflow output and persisted
generation evidence point at DNS/ingress routing instead of a generic provider
timeout. The route keeps optional `Idempotency-Key` replay/conflict behavior and
skips blocked or failed-result replay storage so retries can observe recovered
runtime/provider state.

Generic web preview inventory and destroy use
`POST /v1/drivers/generic-web/preview-inventory` and
`POST /v1/drivers/generic-web/preview-destroy`. Both routes run through native
FastAPI. Inventory scans stateless Dokploy preview applications by the preview
application-name prefix in the DB-backed product profile. Destroy deletes
matching preview applications and treats an already-missing preview application
as clean so PR-close cleanup remains idempotent when no preview was ever created.
The destroy response includes a typed `destroy_outcome`: `destroyed` for a real
provider or record teardown, `no_preview_recorded` only when neither provider
resource evidence nor a Launchplane preview record exists, and `failed` for any
non-passing destroy result. Reusable preview feedback maps the successful
no-preview outcome to `cleared`; unknown outcomes remain fail-closed.
The route keeps the legacy preview-destroy idempotency fingerprint that ignores
`destroy_reason` so reason-only retry metadata does not conflict with the
original teardown request. Lifecycle cleanup can dispatch to
this generic path only after a passing plan and a matching stored preview record
are present. The descriptor routes remain discoverable.

### Operator read endpoints

- `GET /v1/products` (native FastAPI for bearer-token and human-session
  callers)
- `GET /v1/products/{product}` (native FastAPI for bearer-token and
  human-session callers)
- `GET /v1/products/{product}/activity` (native FastAPI for bearer-token and
  human-session callers)
- `GET /v1/products/{product}/environments` (native FastAPI for bearer-token and
  human-session callers)
- `GET /v1/products/{product}/environments/{environment}` (native FastAPI for
  bearer-token and human-session callers)
- `GET /v1/products/{product}/environments/{environment}/promotion-status`
  (native FastAPI for bearer-token and human-session callers)
- `GET /v1/products/{product}/environments/{environment}/promotion/workflow-deliveries/{delivery_id}`
  (native FastAPI for bearer-token and human-session callers)
- `GET /v1/previews/{preview_id}` (native FastAPI for bearer-token and
  human-session callers)
- `GET /v1/previews/{preview_id}/history` (native FastAPI for bearer-token and
  human-session callers)
- `GET /v1/inventory/{context}/{instance}` (native FastAPI for bearer-token and
  human-session callers)
- `GET /v1/promotions/{record_id}` (native FastAPI for bearer-token and
  human-session callers)
- `GET /v1/deployments/{record_id}` (native FastAPI for bearer-token and
  human-session callers)
- `GET /v1/artifacts/protected` (native FastAPI for bearer-token and
  human-session callers)
- `GET /v1/contexts/{context}/secrets` (native FastAPI for bearer-token and
  human-session callers)
- `GET /v1/contexts/{context}/instances/{instance}/secrets` (native FastAPI
  for bearer-token and human-session callers)
- `GET /v1/secrets/{secret_id}` (native FastAPI for bearer-token and
  human-session callers)
- `GET /v1/contexts/{context}/operations/recent` (native FastAPI for
  bearer-token and human-session callers)
- `GET /v1/product-profiles` (native FastAPI for bearer-token,
  human-session, and Every Code worker-token callers)
- `GET /v1/product-profiles/{product}` (native FastAPI for bearer-token and
  human-session callers)
- `GET /v1/service/runtime` (native FastAPI for bearer-token and human-session
  callers)
- `GET /v1/service/odoo-workers/status` (native FastAPI for bearer-token and
  human-session callers)
- `POST /v1/service/odoo-workers/reconcile` (native FastAPI on the bearer/OIDC
  write identity path with `launchplane_service.reconcile_odoo_workers`)
- `GET /v1/service/verireel-workers/status` (native FastAPI for bearer-token and
  human-session callers)
- `POST /v1/service/verireel-workers/reconcile` (native FastAPI on the
  bearer/OIDC write identity path with
  `launchplane_service.reconcile_verireel_workers`)
- `GET /v1/drivers/odoo/stable-bootstrap/operations/{operation_id}` (native
  FastAPI for bearer-token and human-session callers)
- `POST /v1/drivers/odoo/stable-bootstrap` (native FastAPI on the bearer/OIDC
  write identity path with `odoo_stable_bootstrap.execute`)
- `GET /v1/drivers/odoo/target-replacement/operations/{operation_id}` (native
  FastAPI for bearer-token and human-session callers)
- `GET /v1/edge-endpoints/records` (native FastAPI for bearer-token and
  human-session callers)
- `GET /v1/edge-endpoints/records/{endpoint_key}` (native FastAPI for
  bearer-token and human-session callers)
- `GET /v1/private-health-endpoints/records` (native FastAPI for bearer-token
  and human-session callers)
- `GET /v1/private-health-endpoints/records/{endpoint_key}` (native FastAPI for
  bearer-token and human-session callers)
- `GET /v1/ingress/canary-routes/records` (native FastAPI for bearer-token and
  human-session callers)
- `GET /v1/ingress/canary-routes/records/{canary_key}` (native FastAPI for
  bearer-token and human-session callers)
- `GET /v1/route-bindings/records` (native FastAPI for bearer-token and
  human-session callers)
- `GET /v1/route-bindings/records/current` (native FastAPI for bearer-token and
  human-session callers)
- `GET /v1/ingress/route-audits/records` (native FastAPI for bearer-token and
  human-session callers)
- `GET /v1/ingress/route-audits/records/{record_id}` (native FastAPI for
  bearer-token and human-session callers)
- `GET /v1/dokploy-targets/inspect` (native FastAPI for bearer-token and
  human-session callers)
- `GET /v1/every-code/summary` (native FastAPI for bearer-token,
  human-session, and Every Code worker-token callers)
- `GET /v1/previews/readiness` (native FastAPI for bearer-token,
  human-session, and Every Code worker-token callers)
- `GET /v1/every-code/work-requests` (native FastAPI for bearer-token,
  human-session, and Every Code worker-token callers)
- `GET /v1/every-code/work-requests/{request_id}` (native FastAPI for
  bearer-token, human-session, and Every Code worker-token callers)
- `GET /v1/every-code/pr-feedback` (native FastAPI for bearer-token,
  human-session, and Every Code worker-token callers)
- `GET /v1/every-code/preview-gates` (native FastAPI for bearer-token,
  human-session, and Every Code worker-token callers)
- `GET /v1/every-code/notification-attempts` (native FastAPI for bearer-token,
  human-session, and Every Code worker-token callers)
- `GET /v1/previews/pr-feedback/notification-attempts` (native FastAPI for
  bearer-token and human-session callers)

These operator reads use the same Launchplane authn/authz boundary as evidence
ingress. The intent is to give operators a minimal typed read surface for the
current Launchplane record nouns without forcing them to infer state from
workflow logs or host-local files. Secret status reads return metadata only:
Launchplane does not expose plaintext secret retrieval through the service
boundary.

Deployment, promotion, and preview single-record reads use the stored record
context for authorization. The service reads the record first, maps a missing
record to `404 not_found`, then checks `deployment.read`, `promotion.read`, or
`preview.read` against product `launchplane` and the record context before
returning the typed record envelope. Preview history reads share the same stored
preview authorization decision, then return the typed preview record plus its
generation history. Inventory single-record reads authorize `inventory.read`
against the path context before store access, then verify the stored inventory
context before returning the typed record envelope. Recent operations reads
authorize `operations.read` against the path context before store access, then
return the typed recent inventory/deployment/promotion/preview operation
envelope. Managed-secret status list reads authorize `secret.list` against the
path context before store access. Single managed-secret status reads load the
metadata-only secret status to discover the stored secret context, then check
`secret.read` against product `launchplane` and that stored context before
returning the typed status envelope. Product profile list reads check
`product_profile.read` against product `launchplane` in the Launchplane service
context, preserve the `driver_id` filter, and continue accepting the dedicated
Every Code worker token for the collection route only. Product profile show
reads load the stored profile first, check `product_profile.read` against the
stored profile product and Launchplane service context, and return the typed
profile envelope. Ingress route audit reads check `ingress_route.plan` against
the requested query product/context before storage access, require those
scope query parameters for list and single-record reads, preserve optional
`status`, `mode`, `provider_host_id`, `trace_id`, `idempotency_key`, and `limit`
list filters, and return `404 not_found` when a record exists outside the
requested scope. Endpoint apply and ingress route apply routes use native
FastAPI write handlers with the apply contracts above.

Environment route-binding reads check `route_binding.read` against the requested
product/context before storage access and require product/context for list reads
and product/context/instance for the singleton read. Responses are redacted read
models: provider-specific host ids, certificate ids, target ids, edge addresses,
provider payload evidence, and certificate references remain stored evidence
and are omitted from the ordinary operator/API read contract. Each read includes
an opaque SHA-256 over the complete stored record so a caller can prove which
redacted authority it inspected without receiving hidden provider evidence. The
reconcile route checks `route_binding.read` for dry-run and `route_binding.apply`
for apply, accepts no caller-supplied domains, provider identifiers, or freshness
timestamps, and fails closed unless the provider-target projection matches its
Dokploy target and target-id records and bounded, terminal, explicitly owned
evidence resolves exactly one binding for the requested tuple. A successful
re-evaluation attests that source-record set for 24 hours. Reconcile is a no-op
while more than 12 hours remain, refreshes service/backfill-owned evidence at
half-life or when source versions change, and reports an explicit conflict if
provider target, domains, ingress, TLS ownership, lifecycle status, operator
ownership, or the expected-current digest differs.

Product/site reads use action `product_environment.read`. They are native
FastAPI routes backed by DB-owned product environment read-model composition.
They compose Launchplane-owned product profiles, driver descriptors, stable lane
records, preview summaries, runtime-environment key summaries, managed secret
binding metadata, action availability, trust state, and one provider-neutral
topology shell shared by Odoo, generic-web, VeriReel, and future drivers. The
topology shell separates product-profile desired URL/domain intent,
route-binding provider-recorded placement/domain/ingress/TLS authority, and
runtime/public-ingress/TLS observations. Missing neutral route authority remains
missing even when provider records exist; it is never reconstructed as healthy
truth from Dokploy or driver-specific payloads. The collection, product,
activity, and environments routes authorize against the Launchplane service
context. Single environment detail reads authorize against the selected lane
context. Raw context names remain evidence metadata, while provider target ids,
host ids, certificate ids, edge addresses, provider evidence maps, runtime
values, secret plaintext, secret ciphertext, and product-specific driver
payloads are not exposed.

For a public-monitoring lane, the read-only topology projection may corroborate
observed placement from the newest fresh passing strict health probe only when
the exact configured check requires runtime identity and the probe's expected
and observed identities both match the recorded placement identity under the
canonical runtime identity comparison, whose recorded health is verified and
passing. The
placement provenance then names that public observation. Environment inventory,
deployment evidence, and their environment-level provenance are neither updated
nor re-timestamped. Non-strict, legacy, base-page-only, superseded, stale,
failing, missing, unchecked, unverifiable, or mismatched observations remain
fail closed, and route, provider-target, TLS, and authorization readiness are
still evaluated independently.

`/v1/repo-product-mapping` and `/v1/agent/context` are also native FastAPI routes.

`GET /v1/products/{product}/environments` returns the product's stable
environment summaries from DB-backed Launchplane records. It is the collection
form of the per-product read model and is intended for operator and UI
navigation before loading a single environment detail page. It uses the same
redaction rules as the product overview: environment summaries include context,
URLs, action availability, trust state, provenance, and the topology projection,
but not runtime values, secret material, or provider-only topology. Typed
warnings make domain, placement, ingress, ownership, TLS, and freshness
divergence explicit. A hostname-mismatch read includes the public name,
recorded terminator/owner, bounded presented certificate names, failure code,
incident linkage, and a likely cause without requiring provider database access.
Open incident summaries additionally expose severity, notification state,
material fingerprint digest, latest material event kind/time, and aggregate next
and last reminder times. Acknowledgement or silence never changes the health or
topology status; those fields describe delivery state only. Suppressed reminder
state does not expose a next-reminder timestamp until delivery is active again.
Raw private endpoint URLs, destination credentials, operator identities, and
notification payloads remain outside the product read model.

`GET /v1/products/{product}/environments/{environment}/config-status` is a
redacted product/site read under the same action. It compares product-profile
expected runtime keys and managed secret bindings with recorded lane runtime
environment records and managed secret binding metadata. Expected keys describe
product intent; status is derived from records. The response exposes configured,
missing, or disabled status plus key/source metadata only; managed secret IDs
remain out of this readiness view.

`GET /v1/products/{product}/contexts/{context}/instances/{instance}/operational-readiness`
is the action-specific operational enrollment preflight. Query parameter
`action` is the exact driver authorization action; `artifact_id` names the
exact persisted candidate when the action declares artifact readiness.
`expected_current_artifact_id` optionally binds the caller's environment read to
the deployment artifact observed by the readiness projection. The route first
requires `product_environment.read` for the exact product/context/instance tuple.
It then reads exactly one active DB-backed authz policy and requires the
authenticated GitHub Actions caller to match exactly one managed rule for the
requested action and lane. Ready workflow authorization also requires numeric
repository identities, an exact caller workflow ref, and an exact reusable
workflow ref pinned to a full commit SHA. The captured managed rule must contain
singleton exact product, context, instance, action, caller-workflow, and
reusable-workflow selectors; wildcard or multi-lane rules remain blocked even if
normal policy matching would allow the current call. Caller-supplied identity
selectors are not accepted. Non-final selector shape is returned as bounded
authorization dimension details such as `job_workflow_refs_not_singleton`,
without returning selector values. During an immutable worker rollout, old and
new workers must therefore use separate exact managed rules with distinct rule
IDs; combining both SHAs in one rule remains blocked even when normal policy
matching authorizes the caller.

The typed response reports overall and per-dimension `ready`, `blocked`,
`stale`, `missing`, or `unsupported` state for product/lane ownership, action
support, authorization, provider target, route binding, runtime-environment
metadata, managed-secret bindings, exact artifact, deployment evidence, and
topology. Only dimensions declared by the driver action are evaluated beyond
the baseline ownership/action/authz checks. Non-ready dimensions identify the
owning record class or a supported service remediation without exposing secret
values, managed-secret IDs, provider credentials/evidence, provider target IDs,
raw identity claims, or runtime values. The endpoint performs no writes or
provider calls; absent production enrollment remains a non-ready result.
The response envelope stores the projection under `readiness`; trusted workflow
clients that persist a bounded evidence file select that member rather than
treating envelope metadata as readiness fields. Error-severity topology findings
block readiness, while warning-severity findings remain visible as details.
Artifact readiness validates the exact candidate artifact named by the caller
and requires its image repository to match the product profile. Deployment
readiness independently validates the current lane deployment, health, runtime
identity, and freshness from one enriched lane snapshot. Provider-target
authority is classified from the exact provider-target record instead of
borrowing deployment/inventory freshness. A persisted replacement candidate is
not required to equal the artifact already deployed, while
`expected_current_artifact_id`
requires current inventory authority and detects a lane change between the
environment read and readiness check. Latest-deployment fallback may inform an
ordinary product read, but it cannot satisfy this fenced target-replacement
preflight without current inventory.

`GET /v1/products/{product}/environments/{environment}/promotion-status` is the
product-owned browser authority for generic-web production promotion. It exposes
only generated runtime identity, inventory freshness, health, target readiness,
workflow configuration, authz availability, and deterministic confirmation
text. The corresponding direct dry-run and workflow-dispatch POST routes accept
reason, evidence fingerprint, bump, and confirmation fields only. The workflow
delivery read validates that the outbox aggregate belongs to the requested
product/context before returning dispatch and observed-run state.

Product-owned workflow dispatch adds the persisted outbox delivery ID as the
configured `promotion_intent_id` workflow input. A later raw live driver call
must present that ID in the request and as `Idempotency-Key`; Launchplane binds
the outbox record to the current product evidence and provider-target
fingerprint before any promotion effect. The reviewed provider target is
resolved once and carried into execution under a durable target-scoped mutation
reservation, so target-record changes cannot redirect an accepted intent and
concurrent uses of the same intent cannot both reach the provider. Completed
requests remain replayable before current evidence is revalidated. Raw live
automation without an intent is denied unless policy explicitly grants
`generic_web_prod_promotion.execute_unreviewed` in addition to the normal
execute action. That grant bypasses product review, not mutation safety: every
live raw promotion requires database storage, a non-empty idempotency key, an
exact current target snapshot, and the durable provider-operation runner.

Promotion availability requires fresh matching testing and production
inventories, digest-pinned artifacts, immutable source commit IDs, and the
explicit current provider-target record for production. Historical deployment
targets and provider-specific compatibility records are evidence only and do
not enable promotion. The accepted direct dry-run fingerprint also binds the
complete current production provider-target record, so target replacement
invalidates workflow-dispatch continuity.

Product activity reads are intentionally record-link oriented. They summarize
deployments, promotions, rollbacks, backup gates, preview identity/lifecycle,
preview feedback, and matching authz-policy changes with driver/action IDs and
record references rather than embedding raw record payloads.
Current managed-authz records are scoped by `audit.diff.changes` and the named
previous/current managed rules, so an unrelated multi-product mutation cannot
appear merely because the cumulative policy still mentions the product.
Removals remain visible through the previous rule even when the replacement
snapshot no longer names the product. Legacy records without managed diff audit
use adjacent snapshot comparison and are omitted when product impact cannot be
proven. Managed diffs also fail closed when their managed-set identity or named
previous record is unavailable; the timeline does not guess across rule sets or
substitute an unrelated adjacent record.

Preview-related product actions are only shown when the product profile enables
previews. That includes generic-web preview discovery and inventory actions,
not just refresh and destroy operations.

Prod-scoped product actions are only shown when the product profile actually
defines a prod lane. Generic-web prod promotion is additionally hidden unless
the testing and prod lanes share the same context.

### Driver execution endpoints

These use the same authn/authz boundary as evidence ingress:

- `POST /v1/drivers/odoo/post-deploy` (native FastAPI)
- `POST /v1/drivers/odoo/config-parameter-override` (native FastAPI)
- `POST /v1/drivers/odoo/website-bootstrap-override` (native FastAPI)
- `POST /v1/drivers/odoo/artifact-publish` (native FastAPI)
- `POST /v1/drivers/odoo/stable-bootstrap` (native FastAPI)
- `POST /v1/drivers/odoo/target-replacement-plan` (native FastAPI)
- `POST /v1/drivers/odoo/target-replacement-apply` (native FastAPI)
- `POST /v1/drivers/odoo/prod-backup-gate` (native FastAPI)
- `POST /v1/drivers/odoo/prod-promotion` (native FastAPI retained operator route)
- `POST /v1/drivers/odoo/prod-rollback` (native FastAPI)
- `POST /v1/drivers/generic-web/prod-promotion` (native FastAPI)
- `POST /v1/drivers/generic-web/prod-rollback-plan` (native FastAPI)
- `POST /v1/drivers/generic-web/prod-rollback` (native FastAPI)
- `POST /v1/drivers/verireel/...` (native FastAPI)

Driver route metadata remains descriptor-backed for UI/action discovery.
VeriReel testing verification, stable environment, runtime verification,
preview inventory, and preview verification are native FastAPI routes; their
descriptors remain discoverable.

`Odoo Driver Route Smoke` is the Launchplane-owned route exposure gate for Odoo
preview and artifact-publish handoff routes. It first sends unauthenticated
public probes and fails when a registered route returns the Launchplane
route-missing response. It then uses the shared request action and GitHub OIDC to
resolve `/v1/drivers/odoo/artifact-publish-inputs` for the caller's product,
context, instance, and source ref. That response includes the image publish
coordinates plus the Odoo devkit, shared-addons, and product repository
identities resolved from Launchplane runtime records; product repos should not
keep those dependency repo defaults in workflow files. Missing runtime records
for those artifact-publish inputs are classified as
`driver_route_dependency_not_found`, not as route-missing or generic invalid
requests. The artifact-publish and artifact-publish inputs routes are owned by
native FastAPI; product-specific artifact publish calls validate the requested
context and instance against the DB-backed product profile lane before
authorization. Native FastAPI owns the paths. Failed publish evidence is not
cached as an idempotent success. Odoo post-deploy, config-parameter override,
and website-bootstrap override are native FastAPI routes too. They preserve
product-profile driver validation, lane-scoped authorization, optional
`Idempotency-Key` replay/conflict behavior, post-deploy transition records, and
Odoo instance override record merge behavior. Odoo preview apply inputs and
preview apply are also owned by native FastAPI. They preserve preview-context
authorization, runtime-environment dependency classification, and the
`odoo_preview_runtime_config_incomplete` details envelope for apply requests
whose template runtime records are incomplete. Ready preview apply inputs are
persisted as identity-scoped issued plans; blocked inputs remain unstored so a
retry can observe recovered runtime/provider state. Preview apply requires the
service-issued plan id as its `Idempotency-Key`, validates exact plan and artifact
continuity, and recomputes current plan authority before a fresh provider effect.
The smoke also
sends authenticated GitHub OIDC probes to `/v1/drivers/odoo/preview-apply-inputs`,
`/v1/drivers/odoo/preview-apply`, and `/v1/previews/pr-feedback`.

`POST /v1/drivers/odoo/stable-bootstrap` is owned by native FastAPI. It
preserves product-profile driver validation before authorization,
lane-scoped `odoo_stable_bootstrap.execute` authorization, required
`Idempotency-Key` operation-record replay/conflict behavior, active-lane
operation rejection with the existing operation payload, dependency-miss `503`
classification, and the stable-bootstrap operation `poll_url`. Its descriptor
remains discoverable.

`POST /v1/drivers/odoo/target-replacement-plan` is owned by native FastAPI. It
preserves product-profile driver validation before authorization, resolves the
requested instance to the owning product lane context, authorizes
`odoo_target_replacement_plan.read` against that lane context, classifies missing
product profiles as `driver_route_dependency_not_found`, and remains
non-idempotent so repeated plan reads can observe changed runtime/provider state.
The request may select a stored immutable `artifact_id`/`source_git_ref` pair
while separately fencing the lane's current inventory artifact. Planning reads
the selected manifest and blocks missing manifests, source or image-repository
mismatches, and Odoo artifacts that omit Launchplane-required safety modules.
Apply repeats the repository, source, and required-module checks before creating
a durable operation, so invalid evidence cannot reserve the lane.
Its descriptor remains discoverable.

`POST /v1/drivers/odoo/target-replacement-apply` is owned by native FastAPI. It
preserves product-profile driver validation before authorization, resolves the
requested instance to the owning product lane context, authorizes
`odoo_target_replacement_apply.execute` against that lane context, requires
`Idempotency-Key`, replays matching operation records for the same caller scope,
rejects changed payload reuse with `409 idempotency_key_reused`, rejects a second
active operation for the same lane with the active operation payload, and returns
the pending operation record plus poll URL in the accepted response. Its
descriptor remains discoverable.
Mutation-capable routes are proven by pre-mutation classification: preview apply
uses a blocked destroy plan and rejects any non-blocked acceptance, while preview
feedback uses the route's `dry_run` request mode so Launchplane evaluates the same
`preview_pr_feedback.write` authorization without writing records or comments.
Product repos should use that reusable smoke or Launchplane-owned reusable
workflows instead of adding repo-local route setup or copied driver request
contracts.

The product-neutral preview lifecycle route should become the common boundary
for preview desired/current-state comparison. Product-specific driver routes can
continue to perform provider runtime work, but repos should not each reimplement
orphan detection once they can submit desired preview anchors to Launchplane.

### Driver discovery endpoints

These are read-only endpoints for the provider-neutral driver descriptor and
read-model contract documented in [driver-descriptors.md](driver-descriptors.md):

- `GET /v1/drivers`
- `GET /v1/drivers/{driver_id}`
- `GET /v1/contexts/{context}/driver-view`
- `GET /v1/contexts/{context}/instances/{instance}/driver-view`
- `GET /v1/contexts/{context}/instances/{instance}/logs?lines=200&source=runtime`

The descriptor and driver-view routes use action `driver.read` and are native
FastAPI routes. Descriptor discovery authorizes against context `launchplane`;
context views authorize against the requested context, and instance views also
authorize the exact requested instance. These routes expose Launchplane
capabilities and repository-backed read state, not runtime-provider primitives.
The generic `odoo` descriptor remains visible through descriptor discovery but
has no checked-in tenant context patterns and never appears as runtime authority
in a product context view. Odoo context views require a DB-backed product
profile whose declared driver is `odoo` and whose lane owns the requested
context and, for instance views, the exact requested instance. The service
returns one profile-scoped descriptor with the effective Odoo and generic-web
inheritance surface. Stable instance names are unique within a product profile;
if multiple profiles claim the same context or exact lane, the context view
fails closed instead of returning competing product descriptors.

All Odoo action routes enforce the same admission boundary before authorization:
the envelope must name a product-specific DB-backed profile, the profile must
use the Odoo driver, and every requested lane must be profile-owned. The base
`product="odoo"` compatibility shortcut is not supported. Production promotion
admission validates both the testing source and prod destination lanes before
dispatch. Route admission also rejects ambiguous cross-profile ownership before
authorization or provider dispatch.

The logs route is the exception to the `driver.read` action because it reads live
provider output. It is a native FastAPI route, uses action `target_logs.read`,
resolves DB-backed tracked target records by context/instance, supports bounded
Dokploy `application` and `compose` runtime logs plus the latest tracked target
deployment log, validates source-specific query parameters before provider
access, and redacts likely secret values before returning lines. Deployment-log
reads use `source=deployment`, require `since=all`, and reject search text.
Launchplane verifies the selected deployment belongs to the requested tracked
target before reading its detached log id. Provider failures expose only a
bounded redacted operation label/detail, and the manual workflow preserves the
redacted response artifact before reporting failure.

Authorization policy schema v2 treats these instance targets as first-class
selectors. A grant scoped to `testing` cannot read `prod` logs or driver state
even when both lanes share one context. Multi-lane operations must satisfy the
rule for every resolved instance.

The preview driver cut stays intentionally narrow but keeps topology in
Launchplane: Launchplane owns preview URL derivation from the
`LAUNCHPLANE_PREVIEW_BASE_URL` runtime-environment value, preview app naming,
runtime refresh, inventory, and teardown. VeriReel still owns image
build/publish, browser verification, and the follow-up preview evidence write.
Browser verification uses the preview URL returned by the driver plus
allow-listed app maintenance actions keyed by preview slug when it needs remote
owner-admin setup/cleanup.
When a VeriReel preview-refresh request is syntactically valid but Launchplane
cannot complete preflight because preview URL, runtime key-safety, managed
secret, Dokploy target, or template `DATABASE_URL` configuration is incomplete,
the route returns an accepted driver result with `refresh_status="fail"` and a
public-safe `error_message`. The same response writes failed preview generation
evidence so product workflows and PR feedback can surface the actionable reason
instead of a generic `invalid_request` rejection. Malformed request payloads and
unauthorized calls still fail closed before provider mutation.

VeriReel app maintenance requires an `intent` alongside the allow-listed
action. Smoke/E2E helpers set the narrow intent, such as
`stable-testing-remote-e2e-grant-sponsored`, `remote-e2e-grant-sponsored`,
`remote-e2e-delete-user`, `owner-route-promote-owner`, or
`owner-route-delete-user`, so Launchplane can validate the requested action
against the expected stable or preview lane before it touches Dokploy schedules.
Legacy action-only maintenance payloads are retired and fail request validation.

The first Odoo driver cuts are intentionally narrow as well: Launchplane owns the
artifact publish handoff, remote post-deploy data-workflow trigger, and prod
rollback for stable Odoo compose targets. Artifact publish resolves DB-backed
runtime records and managed secrets in Launchplane, invokes `odoo-devkit` as the
build engine with a one-shot runtime payload, validates the returned artifact
manifest, and writes it to Launchplane records. Post-deploy reads DB-backed Odoo
instance override records, renders the typed override payload, invokes the
Dokploy data-workflow runner, and writes `last_apply` evidence back to
Launchplane. Prod rollback reads DB-backed release tuples, artifact manifests,
and current promotion/inventory records, then delegates the provider mutation to
stable target replacement. That shared executor deploys the selected
artifact-backed image, injects runtime identity, runs post-deploy maintenance,
verifies health/canonical/logo evidence, reads required runtime identity back
from the lane-owned health endpoint, and writes deployment/release-tuple
evidence only when that identity matches. Failed required identity evidence does
not advance current inventory. The rollback wrapper only adds rollback
provenance to inventory and the current prod promotion record. Local Odoo
runtime commands remain in `odoo-devkit`; these drivers are for remote
control-plane execution only.

Privileged product rollback actions should use a narrow delegated-worker runtime
contract when they require network reach or host authority that does not belong
inside the main Launchplane API container.

Do not generalize the full driver surface before a few product-specific routes
have proven the shape.

## Request And Response Rules

- Requests and responses are JSON.
- Evidence endpoints should be idempotent from Launchplane's perspective when the
  same product/workflow submits the same stable identity twice.
- Launchplane should support an explicit idempotency key header for workflow
  retries.
- Launchplane should return durable record identifiers, not local file paths.
- Launchplane should include a request or trace id in every response.

Recommended first headers:

- `Authorization: Bearer <github_oidc_token>`
- `Content-Type: application/json`
- `Idempotency-Key: <stable-retry-key>`

The current Launchplane service implementation now honors `Idempotency-Key`
for all write routes. Launchplane replays the first successful accepted
response when the same authenticated workflow scope retries the same route
with the same key and the same request fingerprint. Launchplane rejects reuse
of the same key for a different payload on the same route.

Reservation-backed mutations strengthen that completed-response contract by
claiming `(scope, route, key)` before effects. The reservation records a typed
owner, lease, attempt, state, optional provider reconciliation key, and eventual
response. A matching active request reports that execution is already in
progress. An expired reservation without an external operation key may be
reclaimed; an expired reservation with a bound operation key becomes
`reconcile_required` and must not repeat the provider effect. DB-only routes
commit business state and completion evidence atomically. Provider routes must
bind their stable operation/reconciliation key before invoking the provider.
Product preview TLS apply is the first DB-only route migrated to this boundary;
remaining route migrations must preserve the same fail-closed semantics rather
than relying on process-local locks.

Current VeriReel key shapes:

- preview generation: `preview-generation:<product>:<context>:<anchor_repo>:<pr_number>:<sha>`
- preview destroy: `preview-destroyed:<product>:<context>:<anchor_repo>:<pr_number>:<destroy_reason>`
- VeriReel preview refresh driver:
  `verireel-preview-refresh:<product>:<context>:<anchor_repo>:<pr_number>:<sha>`
- VeriReel preview destroy driver:
  `verireel-preview-destroy:<product>:<context>:<anchor_repo>:<pr_number>:<destroy_reason>`

For VeriReel, `destroy_reason` should stay stable per destroy lane so idempotent
retries do not collide. The regular cleanup workflow uses
`external_preview_cleanup_completed`; the janitor backstop uses
`external_preview_janitor_cleanup_completed`.

- testing deployment evidence: `testing-deployment:<product>:<context>:<instance>:<record_id>`
- prod deployment evidence: `prod-deployment:<product>:<context>:<instance>:<record_id>`
- prod promotion evidence: `prod-promotion:<product>:<context>:<from_instance>:<to_instance>:<record_id>`
- generic-web prod promotion driver:
  `generic-web-prod-promotion:<product>:<context>:<from_instance>:<to_instance>:<artifact_id>:<source_git_ref>`
- generic-web preview refresh driver:
  `generic-web-preview-refresh:<product>:<anchor_pr_number>:<sha>`
- generic-web preview destroy driver:
  `generic-web-preview-destroy:<product>:<anchor_pr_number>`
- Odoo isolated preview apply-inputs driver:
  `odoo-preview-apply-inputs:<product>:<preview>:<source-or-destroy>:<run-attempt>`;
  ready responses are persisted under the service-derived plan id, while blocked
  input derivation remains unstored so it can be retried against changed
  runtime/provider records.
- Odoo isolated preview apply driver:
  the `plan_provenance.plan_id` returned by preview apply-inputs; callers must
  not synthesize a separate apply key.

Generic-web product workflow clients live in product repositories as thin
Launchplane callers until Launchplane provides a shared distributable helper.
Those clients must keep Launchplane lifecycle truth out of the product repo and
follow the shared request semantics: request a GitHub OIDC token with an explicit
timeout, apply bounded timeouts to Launchplane route calls, preserve HTTP status
and raw response bodies before attempting JSON parsing on failed responses, do
not retry `AbortError` or timeout aborts, and use stable idempotency keys for the
same product operation. Generic-web preview destroy keys intentionally omit the
free-form destroy reason so the same preview cleanup is idempotent even when the
caller wording changes between cleanup paths.

Odoo preview mutation routes intentionally use the isolated compose
planner/apply pair instead of product-shaped generic preview refresh/destroy
aliases. Product repos call `POST /v1/drivers/odoo/preview-apply-inputs` so
Launchplane derives the live preview URL, runtime bindings, target evidence,
and provider dry-run plan from DB-backed product profile preview configuration,
runtime-environment records, managed secrets, and tracked target records. Tenant
workflows then call `POST /v1/drivers/odoo/preview-apply` with the ready plan.
Launchplane serializes preview provider mutations and runs the blocking apply
and deployment wait outside the ASGI event loop so health and other service
routes remain responsive while a preview deployment is in progress.
The product profile also owns the preview domain certificate mode. Existing
external-wildcard deployments use `none`; products without that wildcard may
select `letsencrypt`, which Launchplane carries through the dry-run plan and
enforces again from the current profile before provider mutation.

`POST /v1/drivers/odoo/preview-apply-inputs` is the Launchplane-owned handoff
between thin tenant preview workflows and isolated Odoo provider apply. The
caller supplies only product, PR number, image reference, source git ref, and
optional preview slug or URL override. Launchplane derives the generic preview
URL, runtime binding evidence, template compose id, Dokploy environment id,
Odoo runtime plan, and redacted Dokploy dry-run plan from DB-backed product
profile, runtime-environment, managed secret, and target records. For existing
preview refreshes and destroy planning, Launchplane discovers the per-PR Dokploy
compose from provider inventory and verifies the matching preview hostname
before it emits target evidence, so tenant workflows do not pass provider ids.
Ready responses can be posted to `POST /v1/drivers/odoo/preview-apply`; blocked
responses include planner blockers but never plaintext runtime values or secret
material. Destroy planning remains fail-closed when no matching preview compose
and hostname can be discovered.

`POST /v1/drivers/odoo/prod-promotion-inputs` is the stable-lane companion for
thin tenant prod promotion workflows. The caller supplies product, context, and
a request ID. Launchplane reads the current `testing` release tuple and artifact
manifest, then returns the artifact ID, source git ref, release tuple ID,
immutable image reference, and deterministic backup-gate record ID required by
the backup-gate and promotion routes. Blocked responses are not cached as
idempotent successes and explain which Launchplane record is missing, so a
tenant workflow does not have to accept hand-entered artifact or source facts.
The route is owned by native FastAPI; its descriptor remains discoverable, the
native route owns execution.

`POST /v1/drivers/odoo/prod-promotion-run` is the preferred thin-workflow
mutation route for Odoo prod promotion. The tenant workflow supplies product,
context, and a stable request ID; Launchplane resolves the promotable testing
artifact, captures the prod backup gate, executes the testing-to-prod promotion,
and returns the phase statuses and written record IDs. The lower-level inputs,
backup-gate, and promotion routes remain available for diagnostics and explicit
operator workflows, but product repos should not own the chain.
The route is owned by native FastAPI and preserves request-context authorization,
reusable Launchplane workflow identity matching, optional `Idempotency-Key`
replay/conflict behavior, and no-cache retry behavior for blocked or failed
driver results. Its descriptor remains discoverable. The older
`POST /v1/drivers/odoo/prod-promotion` compatibility route is also native
FastAPI for explicit operator workflows and diagnostics, but product repos
should prefer the thin `prod-promotion-run` path.

### Tenant Admission, Classification, And Role-Policy API Boundary

`GET /v1/work-graph/tenant-admission/repository-classification` and `POST /v1/tenant-admission/repository-classifications/apply` provide DB-backed authority for repository classification.

- `GET /v1/work-graph/tenant-admission/repository-classification?repository_id=...`:
  Returns the current classification read model (`missing`, `available`, or fail-closed `ambiguous`, plus active record when unique, history count, and generated_at). Requires `tenant_repository_classification.read` authorization against Launchplane service context (`launchplane`).
- `POST /v1/tenant-admission/repository-classifications/apply`:
  Accepts a strict envelope (`schema_version`, `mode: dry_run|apply`, `expected_current_record_id`, `record`).
  Terminal agents are denied (HTTP 403). Requires `tenant_repository_classification.write` authorization against Launchplane service context (`launchplane`).
  Apply mode requires JSON with one exact bounded `Content-Length` (maximum 64 KiB), a non-empty `Idempotency-Key` header, and a `PostgresRecordStore` using the `postgresql` dialect (returns HTTP 503 `database_storage_required` for filesystem, SQLite-backed rehearsal stores, or unsupported stores). Launchplane reserves durable idempotency, locks the repository classification stream, validates CAS, appends the immutable revision, and completes the stored response in one PostgreSQL transaction. Exact same-key, same-payload retries replay the completed response; a different key revalidates current state and cannot replay an already-applied revision.
  Validation uses CAS (compare-and-swap): first revision must be revision 1 with no `supersedes_record_id` and empty `expected_current_record_id`. Subsequent revisions must increment revision by 1, set `supersedes_record_id` to the active current record ID, and match `expected_current_record_id`. Mismatches fail closed with HTTP 409 conflict, and sequence gaps fail closed with HTTP 400.
  Dry-run mode performs full CAS/sequence validation without persisting changes and reports `would_apply` or `would_replay`.
  Pure tenant merge eligibility evaluation uses this DB authority without heuristics, PR label fallbacks, or wildcard matching. Repositories classified as `engineering` take the normal engineering fast path. Repositories classified as `tenant_ui` require one satisfied exact-head path from manager preview approval, technical human waiver, or trusted-maintenance evidence.
  This pure evaluator remains internal and separate from scheduler merge train admission (`merge_train_admission`).

`GET /v1/work-graph/tenant-admission/repository-human-role-policy` and
`POST /v1/tenant-admission/repository-human-role-policies/apply` provide the
first hardened repository-human role-policy service boundary. This split is
limited to current role-policy reads plus dry-run/apply writes.

- `GET /v1/work-graph/tenant-admission/repository-human-role-policy?repository_id=...&product=...&context=...`:
  Returns the current role-policy read model keyed by immutable repository ID,
  product, and context. The model reports `missing`, `available`, or fail-closed
  `ambiguous`; `available` includes the unique active current record, history
  count, and `generated_at`. Requires `repository_human_role_policy.read`
  authorization against the submitted product/context and an explicit
  `AuthorizationTarget(scope="context")`.
- `POST /v1/tenant-admission/repository-human-role-policies/apply`:
  Accepts a strict envelope (`schema_version`, `mode: dry_run|apply`,
  `expected_current_record_id`, `expected_current_role_policy_digest`,
  `record`). Terminal agents are denied (HTTP 403). Requires
  `repository_human_role_policy.write` authorization against the submitted
  product/context and an explicit `AuthorizationTarget(scope="context")`.
  Apply mode requires JSON with one exact bounded `Content-Length` (maximum
  64 KiB), a non-empty `Idempotency-Key` header, and a `PostgresRecordStore`
  using the `postgresql` dialect. Filesystem, SQLite-backed rehearsal stores,
  and unsupported stores return HTTP 503 `database_storage_required` for live
  apply. Dry-run mode may use rehearsal/read stores and writes nothing.
  Launchplane reserves durable idempotency, locks the repository role-policy
  stream, validates the expected current tip record ID and digest, supersedes
  the active tip, inserts the candidate, completes the stored response, and
  commits in one PostgreSQL transaction. Same key plus same canonical request
  replays the stored HTTP 202 response. Same key plus a changed request returns
  HTTP 409 `idempotency_key_reused`. Repeating the exact currently active record
  under a new key also replays without adding history when the request retains
  the original predecessor record ID and digest CAS. In-progress and reconciliation-required
  reservations use the existing mutation error conventions.
  Validation is fail-closed: revision 1 must have no current tip expectation;
  later revisions must increment by one, identify and digest-match the active
  current tip, and set `supersedes_record_id` to that current record. Missing,
  ambiguous, stale, scope-drifted, inactive, sequence-invalid, or conflicting
  candidates are rejected. Request-provided superseded records are not persisted
  as authority; the database writer derives supersession from the locked stream.
  These routes never infer authorization or runtime authority from repository
  names, logins, changed files, paths, actor strings, or request-provided
  superseded history.

The role-policy route still does not add trusted-maintenance evidence, unified
tenant-admission status, controller changes, or any Launchplane authorization-
policy mutation.

`POST /v1/tenant-admission/technical-human-waivers/apply` provides the focused
human technical-waiver mutation boundary. The route accepts `mode: dry_run|apply`
and `action: created|revoked` envelopes containing candidate, source event
kind/id, reason, optional creation `expires_at`, expected current
classification/role-policy/authz record IDs plus digests, and revoke-only
expected current waiver ID plus event digest. The request never accepts
`occurred_at`, author GitHub ID, or author login. Launchplane builds the binding,
authorization provenance, event IDs, digests, and author display login from the
browser session and current records.

The route is browser-human-only. It first passes the normal browser session,
origin, fetch-metadata, and single-use CSRF mutation boundary, then requires a
`GitHubHumanIdentity` with `github_id > 0`. Local admins/operators, GitHub
Actions, terminal agents, Every Code workers, bearer-only callers, and other
non-human identities are rejected. Login is display/audit only; the route scopes
idempotency as `github-human-id|<github_id>` and authorizes only when the active
schema-v2 authz policy has exactly one managed
`tenant_technical_human_waiver.write` GitHub-human rule whose `github_ids`
explicitly contains that numeric caller ID. Login/org/team/role-only matching is
insufficient. The caller must also be the current repository owner in exactly
one active role-policy record for the candidate, and the current classification
must exactly match the candidate and be `tenant_ui`.

Dry-run uses the same pure read/planning helpers against rehearsal-capable stores
and writes nothing. Apply requires JSON with one exact bounded `Content-Length`
(maximum 64 KiB), a non-empty `Idempotency-Key` header, and a real PostgreSQL
`PostgresRecordStore`; filesystem, SQLite-backed rehearsal stores, and
unsupported stores return HTTP 503 `database_storage_required` for live apply.
The PostgreSQL writer reserves idempotency, locks classification, role-policy,
authz-policy, and waiver binding/history authority in deterministic order,
re-reads and validates expected IDs/digests under lock, builds the event with the
database/server timestamp as both `occurred_at` and `recorded_at`, validates
create/revoke lifecycle and revoke CAS, appends the event, verifies the resulting
tenant-admission path, stores the HTTP response, and commits once. Validation
failures remove the reservation; completion failures roll back both event and
reservation.

Same key plus the same canonical request body replays the stored response with
the original trace, while the same key plus a changed body returns HTTP 409
`idempotency_key_reused`. A different key revalidates current authority and
history, so classification, role-policy, authz-policy, head, revocation, or
expiry drift cannot create a false success. Create may only produce a satisfied
technical-waiver path. Revoke requires the exact current satisfied waiver and
must produce a denied path. Exact create event replay is accepted only while the
current lifecycle/CAS proves it is still safe; stale historical replay fails
closed.

This focused waiver slice does not add UI controls, GitHub provider calls,
status/controller projection, trusted-maintenance evidence, rollout behavior,
or authz-policy mutation. Existing tenant merge/admission behavior remains
unchanged until later rollout work wires shared authority into admission
decisions.

`GET /v1/work-graph/tenant-admission/trusted-maintenance-policy` and
`POST /v1/tenant-admission/trusted-maintenance-policies/apply` provide the
focused trusted-maintenance policy read/apply boundary. The policy contract is a
dedicated repository automation authority, not a human role-policy shortcut and
not a generic Launchplane authz-policy reuse. Policy revisions and evidence are
keyed to immutable numeric repository, actor, sender, and exact-head provenance;
display logins are audit only, and no route may infer trust from repository
names, branches, refs, files, labels, commit metadata, PR text, login strings,
or blanket bot status.

- `GET /v1/work-graph/tenant-admission/trusted-maintenance-policy?repository_id=...&product=...&context=...`:
  Returns the current trusted-maintenance policy read model keyed by immutable
  repository ID, product, and context. The model reports `missing`, `available`,
  or fail-closed `ambiguous`; `available` includes the unique active current
  record, history count, and `generated_at`. Requires
  `trusted_maintenance_policy.read` authorization against the submitted
  product/context and an explicit `AuthorizationTarget(scope="context")`.
- `POST /v1/tenant-admission/trusted-maintenance-policies/apply`:
  Accepts a strict envelope (`schema_version`, `mode: dry_run|apply`,
  `expected_current_record_id`, `expected_current_policy_digest`, `record`). The
  route is browser-GitHub-human-only and rejects terminal agents, local
  operators/admin bearers, GitHub Actions, Every Code workers, bearer-only
  callers, and other non-human identities. It requires
  `trusted_maintenance_policy.write` authorization against the submitted
  product/context and an explicit `AuthorizationTarget(scope="context")`; this
  action is intentionally separate from `repository_human_role_policy.write`.
  Apply mode requires JSON with one exact bounded `Content-Length` (maximum
  64 KiB), a non-empty `Idempotency-Key`, and a real PostgreSQL
  `PostgresRecordStore`. Filesystem, SQLite-backed rehearsal stores, and
  unsupported stores return HTTP 503 `database_storage_required` for live apply.
  Dry-run mode validates through rehearsal/read stores and writes nothing.
  Launchplane reserves durable DB-only idempotency, locks the policy stream,
  validates the expected current tip record ID and digest, supersedes the active
  tip, inserts the candidate, completes the stored response, and commits once.
  Exact same-key/same-request retries replay the stored response; same-key
  changed requests return `idempotency_key_reused`; stale CAS returns conflict.
  The route is included in the shared exact-length JSON body guard, so the
  64 KiB limit is enforced before FastAPI/Pydantic parsing.

`GET /v1/work-graph/tenant-admission/evaluation` exposes one read-only exact-PR
operator/agent evaluation. Its query extends the complete candidate binding
with the exact base branch and optional merge method. It requires
`tenant_admission.read`, resolves the managed GitHub credential, and reuses the
controller's `mutate=false` path, so it verifies current numeric repository and
owner identity, head, base, open/merged state, draft/mergeability, admission,
and live required-check policy without acquiring a controller lease or writing
GitHub/provider state. Mergeable tenant UI PRs report required technical-check
readiness even while manager approval or owner waiver remains pending.
Engineering returns `not_applicable` and no human actions. The response adds a
public-safe guidance projection for manager preview approval and repository-
owner technical waiver, with `agent_authoring_allowed=false`; trusted
maintenance remains automatic evidence.

`GET /v1/work-graph/tenant-admission/status` exposes the public-safe unified
tenant-admission read model. The query supplies the complete candidate binding:
product, context, numeric repository ID, numeric repository-owner ID,
`OWNER/REPO`, pull-request number, and exact head SHA. The route requires
`tenant_admission.read` authorization for the submitted product/context and a
context-scoped authorization target. It recomputes from the current DB-backed
classification, role policy, authorization policy, manager-preview lifecycle,
waiver events, maintenance policy, and maintenance evidence; it does not trust
GitHub commit status as decision authority. Engineering returns the normal-flow
category. Tenant UI returns `pending`, `manager-approved`, `technical-waived`,
`maintenance-admitted`, `stale`, `denied`, or `unavailable` and exposes no human
membership, private policy, credential, or provider-topology detail.

`GET /v1/agent/context` may include the same evaluation as a named
`tenant_admission` section when the caller supplies every exact candidate field
and base branch. Ordinary repository-only agent context requests remain
compatible and omit that section. Incomplete candidate input returns HTTP 400;
tenant-admission authorization, storage, stale-head, or GitHub failures are
reported on that section without dropping unrelated read-model sections. The
agent endpoint never gains technical-waiver, role-policy, delegation, manager-
approval, maintenance-policy, reconciliation, controller, or merge write
authority.

`POST /v1/tenant-admission/status/reconcile` accepts a strict schema-v1 envelope
containing that candidate. It is bearer-only, rejects terminal-agent identities,
requires `tenant_admission.reconcile` authorization for the candidate
product/context, and is covered by the exact-length JSON body guard at 64 KiB.
Before evaluation it resolves the managed GitHub credential and re-fetches the
PR, requiring the PR to remain open and its numeric base repository, numeric
owner, full name, and head SHA to equal the submitted candidate. Drift returns
HTTP 409; indeterminate GitHub facts or delivery return retryable HTTP 503.
Launchplane then recomputes the read model and idempotently writes the classic
`tenant-admission` status to the exact head. Matching state, description, and
target URL replay without another write. A provider failure or mismatched write
response never returns success. Engineering is reported as not requiring the
tenant status and performs no status write.

`POST /v1/work-graph/tenant-admission/controller/run-once` accepts a strict
schema-v1 candidate plus exact base branch, merge method, and `mutate` intent.
It is bearer-only, requires `tenant_admission.controller.run_once` authorization
for the candidate product/context, and is covered by the exact-length JSON body
guard at 64 KiB. An explicitly authorized terminal agent may invoke this route:
the caller cannot create manager, waiver, or maintenance authority, and the
controller independently recomputes those DB-backed records before every merge
effect. This is a dedicated privileged controller action: its context-scoped
grant intentionally authorizes a central controller or operator whose source
repository need not equal the target tenant repository. The submitted target
still gains no authority from the caller and must independently satisfy exact
GitHub identity, DB admission, mergeability, and required-check policy.

The controller is tenant-only. An `engineering` classification returns
`not_applicable` without a merge call. For `tenant_ui`, only
`manager-approved`, `technical-waived`, or `maintenance-admitted` may proceed.
Launchplane re-fetches the open PR and requires exact numeric base repository
ID, numeric owner ID, full repository name, same-repository head, requested base
branch, and head SHA. It then evaluates technical commit statuses and check runs
on that exact head against the target branch's live GitHub required-status-check
policy while excluding only the `tenant-admission` and
`manager-preview-approval` contexts. App-bound required checks must be produced
by the exact numeric GitHub App. At least one non-excluded required check must
exist, and every required check must have a matching current passing signal;
missing policy, missing signals, pending, failed, app-mismatched, or
unrecognized evidence blocks without a provider mutation. Open PRs must also be
non-draft and currently mergeable. When the live required-check policy is
strict, the exact PR head must also contain the current base SHA; missing or
malformed strict-policy and draft evidence fails closed.

For `mutate=true`, Launchplane uses the existing repository/base controller
lease only as a shared mutation and crash-reconciliation fence; it does not
enqueue the PR or borrow merge-train scheduler, label, batch, or stack policy.
After the first evaluation it re-fetches PR/base facts, recomputes admission,
and re-reads technical checks immediately before checkpointing and issuing the
GitHub merge request with the exact expected head SHA. Provider uncertainty
leaves the exact request, admission decision, technical-check digest, and merge
phase in durable controller state. A retry adopts success only when GitHub shows
that exact head merged into the requested base and the target branch contains
the merge commit; otherwise it re-evaluates the still-open exact candidate or
remains fail-closed for reconciliation. A reconcile-required row owned by a
different merge-train action is rejected atomically without rewriting that
action's policy or recovery evidence.

The GitHub status remains projection only and is never read as controller
authority. Branch protection, workflow wiring, portfolio rollout, UI, and real
repository policy values remain deferred to staged rollout. Trusted maintenance
is never inferred from a Bot type alone, and no admission decision uses changed
files, repository names, branches, titles, labels, or commit text. Preview
refresh, verification, destroy, and cleanup remain independent from admission,
projection delivery, and merge-controller results.

`POST /v1/engineering-review-decisions/project` and
`POST /v1/owner-acceptance/project` write the stable
`launchplane/engineering-review` and `launchplane/owner-acceptance` check runs
through a dedicated least-privilege GitHub App. Launchplane verifies exact App,
installation, repository, and permission identity, mints a one-repository
Checks-write token, rechecks current server-owned evidence, and uses the exact
decision digest as `external_id`. Replays avoid duplicate writes and same-head
binding changes update only the App-owned run. Both checks complete `neutral`,
remain shadow/non-authoritative, and are excluded from merge-train and tenant-
admission technical inputs.

The CM tenant preview workflow uses tenant-product scope for both artifact
publish input/evidence and preview lifecycle requests. Artifact publish still
uses Odoo driver routes, but source-ref build metadata resolves through the
`odoo-tenant-cm` product profile so Launchplane can return the tenant image
repository, tag, and lane-owned runtime payload. Deploy-maintained GitHub Actions
grants for `cbusillo/odoo-tenant-cm/.github/workflows/odoo-preview.yml` should
therefore include `odoo-tenant-cm` and context `cm` for publish input, publish
evidence, refresh, and destroy actions.

The CM preview workflow also needs `preview_pr_feedback.write` for product
`odoo-tenant-cm` and context `cm` before it can retire tenant-side preview
comment rendering. Normal refresh and destroy outcomes can reuse lifecycle
grants, but unsupported/fork and Dependabot notices sit outside those mutation
paths and require the explicit feedback grant. Fork and Dependabot notices run
from the base branch through `pull_request_target`, so the deploy-maintained
grant set includes both `pull_request` and `pull_request_target` feedback
writers for the same workflow file.

- VeriReel testing deploy driver:
  `verireel-testing-deploy:<product>:<context>:<instance>:<artifact_id>:<source_git_ref>`
- VeriReel testing verification driver:
  `verireel-testing-verification:<product>:<context>:<instance>:<deployment_record_id>`
- VeriReel prod deploy driver:
  `verireel-prod-deploy:<product>:<context>:<instance>:<artifact_id>:<source_git_ref>`
- VeriReel prod backup gate driver:
  `verireel-prod-backup-gate:<product>:<context>:<instance>:<backup_record_id>`
- VeriReel prod promotion driver:
  `verireel-prod-promotion:<product>:<context>:<from_instance>:<to_instance>:<artifact_id>:<source_git_ref>:<backup_record_id>:<promotion_record_id>:<expected_build_revision>:<expected_build_tag>`

The VeriReel prod-promotion request may include the primitive testing-lane
source health status. Launchplane normalizes that status and writes it into the
driver-owned promotion record; product workflows should not post a second
rendered promotion evidence payload for fields the driver can derive.

VeriReel deploy and prod-promotion responses expose provider-neutral target
metadata with `target_category`, `provider_id`, and `provider_target_type`.
The legacy response-only `target_type` alias is retired; Dokploy execution
configuration still uses provider-specific target type fields internally where
application-vs-compose behavior is required.

Recommended first success shape:

```json
{
  "status": "accepted",
  "trace_id": "launchplane_req_01jabc...",
  "records": {
    "preview_id": "preview-verireel-pr-123",
    "generation_id": "preview-verireel-pr-123-generation-0003"
  }
}
```

Recommended first error shape:

```json
{
  "status": "rejected",
  "trace_id": "launchplane_req_01jabc...",
  "error": {
    "code": "authorization_denied",
    "message": "Workflow cannot write preview evidence for verireel-testing."
  }
}
```

## First Stable Payloads

Launchplane should keep the typed evidence payloads already proven in the current
CLI adapters and expose them over HTTP.

### Preview generation evidence

Driver-owned preview verification routes can update those records without
requiring product workflows to render Launchplane record payloads directly.
For isolated Odoo preview provider applies,
`POST /v1/drivers/odoo/preview-apply` accepts the product plus a ready
`OdooPreviewDokployApplyRequest`, authorizes against
`odoo_preview_apply.execute` for the requested product/preview context, and
executes only through the Launchplane service. Callers do not supply plaintext
Odoo runtime env values for the live apply path. Launchplane resolves the
product preview template lane from DB-backed runtime-environment records and
managed secret overlays, then derives per-preview database and volume names from
the compose name before invoking the provider adapter. Responses return only
redacted step evidence, compose/domain identifiers, status, and error summaries.
If the service-side runtime contract is incomplete before any provider mutation,
the route returns `odoo_preview_runtime_config_incomplete` with the affected
context, instance, and missing key names only; it never returns runtime values or
secret material.
The preceding `preview-apply-inputs` call persists each ready plan with its
normalized source/artifact request, canonical fingerprint, and 30-minute expiry.
Its returned plan id is the only accepted apply idempotency key. Apply rejects
unissued ids, caller changes to provider routing or artifact identity, expired
plans, and plans whose fresh service-side recomputation differs. These checks run
before provider effects; a rejected fresh reservation is released. Completed
exact retries still replay their response, while reconciliation observes the
stored original plan instead of creating a new effect. Blocked apply results are
not stored as completed apply responses so retries can recompute after runtime or
provider dependencies recover.

For Odoo preview smoke follow-ups,
`POST /v1/drivers/generic-web/preview-verification` accepts the product,
context, anchor repo/PR, `verification_status`, `verified_at`, optional checked
URLs as an explicit list plus `timeout_seconds`, and an optional failure
summary, then marks the latest preview generation ready or failed. Scalar or
object-shaped `checked_urls` payloads are rejected. The accepted response
includes a `generic_web_preview_verification` result with the generation
identity, final states, status, checked URLs, timeout, and failure summary. The
Odoo-shaped preview verification alias is retired. The route is safe-write
evidence ingestion only; it does not mutate provider state.

For stable smoke follow-ups,
`POST /v1/drivers/generic-web/stable-verification` accepts the product, context,
instance, deployment record, optional promotion record, checked URLs,
`verification_status`, `verified_at`, optional failure summary, and optional
health payload. When a health payload is supplied, Launchplane verifies its
runtime identity against the deployment record before accepting passing or
otherwise unchecked health evidence; explicit structured-health failures remain
accepted as failed evidence. Launchplane updates deployment health evidence and,
when a promotion record is supplied, promotion/inventory evidence. The former
Odoo-shaped stable verification alias is retired. The route is safe-write
evidence ingestion only; it does not mutate provider state.

`POST /v1/evidence/previews/generations`

```json
{
  "product": "verireel",
  "preview": {
    "schema_version": 1,
    "context": "verireel-testing",
    "anchor_repo": "verireel",
    "anchor_pr_number": 123,
    "anchor_pr_url": "https://github.com/example-org/verireel/pull/123",
    "canonical_url": "https://pr-123.preview.example.com",
    "state": "active",
    "updated_at": "2026-04-16T08:10:00Z",
    "eligible_at": "2026-04-16T08:10:00Z"
  },
  "generation": {
    "schema_version": 1,
    "context": "verireel-testing",
    "anchor_repo": "verireel",
    "anchor_pr_number": 123,
    "anchor_pr_url": "https://github.com/example-org/verireel/pull/123",
    "anchor_head_sha": "6b3c9d7e8f901234567890abcdef1234567890ab",
    "state": "ready",
    "requested_reason": "external_preview_refresh",
    "requested_at": "2026-04-16T08:02:00Z",
    "ready_at": "2026-04-16T08:10:00Z",
    "finished_at": "2026-04-16T08:10:00Z",
    "resolved_manifest_fingerprint": "verireel-preview-manifest-pr-123-6b3c9d7",
    "artifact_id": "ghcr.io/example-org/verireel-app:pr-123-6b3c9d7",
    "deploy_status": "pass",
    "verify_status": "pass",
    "overall_health_status": "pass"
  }
}
```

### Preview destroyed evidence

`POST /v1/evidence/previews/destroyed`

This route is native FastAPI for bearer-token callers. It applies the
`preview_destroyed.write` policy action for the request product and destroy
context, preserves `Idempotency-Key` replay/conflict behavior, and writes the
destroyed transition to an existing preview record.

```json
{
  "product": "verireel",
  "destroy": {
    "schema_version": 1,
    "context": "verireel-testing",
    "anchor_repo": "verireel",
    "anchor_pr_number": 123,
    "destroyed_at": "2026-04-16T09:04:00Z",
    "destroy_reason": "external_preview_cleanup_completed"
  }
}
```

The deployment and promotion endpoints should follow the same pattern: stable
typed record payloads inside a Launchplane-owned API envelope.

### Runner host hygiene audit evidence

`POST /v1/evidence/runner-host-hygiene/audits`

Request payload:

```json
{
  "schema_version": 1,
  "product": "launchplane",
  "audit": {
    "schema_version": 1,
    "audit_record_key": "runner-host-hygiene/2026-05-23/chris-testing",
    "status": "planned",
    "request": "RunnerHostHygieneApplyRequest",
    "plan": "RunnerHostHygieneApplyPlan",
    "pre_apply_report": "RunnerHostHygieneReport",
    "post_apply_report": null,
    "message": "planned runner host hygiene apply; no host mutation was executed"
  }
}
```

The route requires `runner_host_hygiene_audit.write` for product/context
`launchplane/launchplane`, writes the typed audit record to Launchplane-owned
storage, and returns the `runner_host_hygiene_audit_record_key` in both the
accepted records and result details. It is native FastAPI evidence ingress for
bearer-token callers with OpenAPI contract coverage and idempotency replay
preservation. It records planned, completed, or failed audit facts supplied by a
future approved executor, but it does not mutate runner hosts itself.

The corresponding read routes require the separate
`runner_host_hygiene_audit.read` action for product/context
`launchplane/launchplane`:

- `GET /v1/evidence/runner-host-hygiene/audits` accepts optional `host_name`,
  `action`, and `audit_status` filters plus a bounded `limit`.
- `GET /v1/evidence/runner-host-hygiene/audits/record` requires an
  `audit_record_key` query parameter because audit keys contain `/` characters.
- `GET /v1/evidence/runner-host-hygiene/history` requires `host_name`, accepts
  optional `cache_key`, and returns timestamped pre/post report points.

List responses contain summaries rather than inventory-bearing audit payloads.
Detail and history responses omit raw image and volume rows and expose bounded
counts, truncation state, finding codes, availability-aware cache telemetry,
and public-safe source-attributed idle convergence. The convergence payload
distinguishes `idle`, `active`, `incomplete`, and `conflicting` states and keeps
unavailable, stale, unauthorized, truncated, and contradictory sources
explicit rather than converting them to zero activity.
Limits default to 25 and cannot exceed 100. Reports written after this contract
preserve `observed_at`; older reports remain readable as `legacy_missing`
without inferring time from an audit key. Read access does not authorize writes,
host mutation, or any new cleanup class. History responses report the fixed
100-audit `scan_limit` and distinguish `scan_truncated` from
`result_truncated`; neither condition implies that older evidence was deleted.

### Runner lane lifecycle audit evidence

`POST /v1/evidence/runner-lane-registration/audits`

Request payload:

```json
{
  "schema_version": 1,
  "product": "launchplane",
  "audit": {
    "schema_version": 1,
    "audit_record_key": "runner-lane-retirement/2026-07-26/product/dry-run",
    "status": "planned",
    "request": {
      "operation": "retire",
      "contract": "RunnerLaneRegistrationRequest"
    },
    "plan": {
      "operation": "retire",
      "contract": "RunnerLaneRegistrationPlan"
    },
    "pre_inventory": "RunnerLaneInventory",
    "post_inventory": null,
    "message": "planned runner lane retirement; no runner mutation was executed yet"
  }
}
```

The route requires `runner_lane_registration_audit.write` for product/context
`launchplane/launchplane`, writes the typed audit record to Launchplane-owned
storage, and returns the `runner_lane_registration_audit_record_key` in both the
accepted records and result details. It is native FastAPI evidence ingress for
bearer-token callers with OpenAPI contract coverage and idempotency replay
preservation. The host-side registration executor owns any GitHub registration
token request and runner `config.sh` execution. The retirement executor uses the
same compatibility route and exact authorized workflow identity, writes
`operation: retire`, and owns the separately guarded service stop, GitHub runner
deletion, and inactive root cleanup.

For VeriReel's first stable-lane Launchplane slice, use context `verireel` for the
long-lived `testing` and `prod` instances. Preview evidence remains separate
under `verireel-testing` because previews are not another durable promotion
lane.

## CLI Relationship

Current commands such as:

- `control-plane launchplane-previews write-from-generation`
- `control-plane launchplane-previews write-destroyed`

are local rehearsal and repair clients for these Launchplane payloads. They are
not the shared integration boundary for external product workflows.

## Driver Relationship

The first Launchplane API should separate two concerns:

- evidence ingress into Launchplane core records
- runtime execution through Launchplane-owned drivers

That keeps the initial service slice small while still allowing a later shift
from repo-owned operational scripts into Launchplane-owned driver execution.

The first explicit drivers should be:

- Odoo driver
- VeriReel driver

Repo-specific variation should stay thin and declarative where possible.

## Product Owner Authority API

The product Owner API is an additive policy-administration and read-model
surface. Policy, requirement, and preferred-routing revisions are written
through separate endpoints and separate authz actions. Their write actions are
classified as `policy_admin`; an invocation grant authorizes the API call but
never satisfies an Owner requirement.

Authority evaluation derives human identity only from immutable provider subject
identity. It does not consume global-admin, bootstrap-admin, manager,
delegation, repository-permission, or routing state as Owner authority. Matching
product Owner requirements feed the exact Owner acceptance decision consumed by
Launchplane merge readiness.

See `docs/product-owner-policy.md` for routes and persisted record contracts.

## Owner Acceptance API

`GET /v1/owner-acceptance/evaluation` accepts only `repository` and
`pull_request_number` query parameters. Launchplane derives repository
identity, head, tree, change-impact policy provenance, affected
product/system/action/environment, and current Owner policy plus requirement
provenance from service-owned providers and records. The pure read cannot
consume a request body and does not expand the cookie-capable mutation route
inventory. Engineering-only changes return `not_required` and write no event.
Incomplete change-impact or Owner authority evidence fails closed.
Preferred Owner routing remains notification-only, does not participate in the
authority decision, and is not part of the exact acceptance binding.
When an enabled product preview has an active record for the exact repository
and pull request, Launchplane requires one unambiguous ready serving generation
whose deploy, verification, and health evidence passed. The binding then adds
the preview/generation IDs, immutable artifact image digest, manifest
fingerprint, canonical preview URL, and an explicit verified-runtime identity
projection. Preview, artifact, manifest, and runtime evidence remains entirely
server-derived. Ambiguous or incomplete evidence fails closed, and a prior
preview-bound event prevents later downgrade to a non-preview binding after
teardown.

`POST /v1/owner-acceptance/events` uses the browser mutation identity path and
requires a browser-authenticated GitHub human plus a bounded `Idempotency-Key`.
It also requires the `expected_binding_sha256` returned by evaluation. The
digest is a compare-only precondition: Launchplane re-resolves all evidence and
returns a conflict without writing when the exact binding changed. For
multi-product changes, that digest selects exactly one current server-derived
product binding; callers cannot name or inject a product. The service then
verifies that the immutable GitHub user ID is a current Owner for the affected exact scope
before writing `accepted`, `changes_requested`, or `revoked`. Agents, workers,
GitHub Actions, local operators, and other bearer identities cannot satisfy this
route. Caller-owned head, tree, policy, Owner, or membership evidence is
rejected by the bounded request contract. `GET
/v1/owner-acceptance/events/{event_id}` reads the persisted append-only event
through the same Owner-acceptance read authority.

The request may include structured `resolution` evidence only for an
`accepted` event that resolves the current `changes_requested` event on the
identical binding. That object requires a non-empty summary and one or more
unique resolved evidence references. The append transaction assigns the next
per-subject `subject_sequence`, validates the complete human transition table,
and inserts the event atomically. Exact replay receives no new sequence;
invalid reaffirmations, unreasoned revocations, and unsupported transitions
return a conflict. Current state folds by sequence only, while timestamps remain
audit and display fields.

The ledger is append-only and authoritative for the Owner merge-readiness facet.
Changed bound evidence or changed
Owner policy/requirement/membership makes prior acceptance stale for the new
binding. Evaluation returns one decision per affected product and is accepted
only when all are current; dropped products stop governing without a read-side
write. The GitHub projection and frontend workbench route reviewers without
becoming authority. Tenant-admission consumers, production authorization, and
legacy manager cleanup remain out of scope. See `docs/owner-acceptance.md` for
the full record and migration boundary.

`GET /v1/governance/projection` accepts only repository, pull request number,
and base branch scope. It requires Owner-acceptance, engineering-review
decision/run/authority, and either the repository policy's service authorization
or the Launchplane merge-train policy-target read permission;
the route fails closed rather than returning a partially authorized projection.
It returns one read-only model containing immutable Owner
history, current Owner evaluation, current ephemeral merge readiness when an
active landing lineage exists, latest immutable merge admission, separate
landing outcome, and non-authoritative GitHub status observations. It reuses the guarded
landing readiness evaluator instead of duplicating readiness logic in the HTTP
or frontend layers, binds the requested branch to the current pull request base
ref, and resolves the repository policy's declared GitHub token source. The
projection is `authoritative=false`, authorizes no
effect, classifies historical head/tree evidence explicitly, and never
interprets a missing landing outcome as landed. See
`docs/governance-evidence.md`.

## Change Impact API

`POST /v1/change-impact/evaluation` accepts only a repository/pull-request
target reference plus optional non-authoritative metadata. Launchplane uses its
managed GitHub credential to resolve immutable repository identity, current
head/tree, and changed files; GitHub Actions callers must also match the OIDC
repository IDs/name and workflow `sha`. The active DB-backed component policy
may declare affected products directly. Additional dependency and reviewer
evidence is read only from Launchplane storage, and reviewer product claims
require trusted same-component dependency evidence. Missing extension records,
stale heads, incomplete provider evidence, and provider failures cannot fall
back to caller input and therefore fail closed.

The response is the authoritative Owner-impact classification with exact policy
revision/digest and repository/PR/head/tree binding. See
`docs/change-impact-policy.md` for the policy, evidence, and persistence contracts.

## Out Of Scope For This First Slice

- full human/operator auth design
- multi-tenant billing or quota models
- generalized plugin marketplace design
- replacing file-backed storage immediately
- moving every current CLI command behind HTTP at once

## Recommended Next Implementation Steps

1. Convert the existing CLI preview evidence commands into local clients of the
   same service-layer handler or payload contract.
2. Add local clients for deployment and promotion evidence where Launchplane-facing
   workflows still write through repo-local CLI adapters.
3. Define the first explicit Odoo and VeriReel driver interfaces after the
   service ingress exists.

## Product retirement

`POST /v1/product-retirement` is the only supported provider-mutation path for
retiring a stable generic-web application. Both `plan` and `apply` require
DB-backed storage, an `Idempotency-Key`, exact instance-scoped authorization,
a reason, an issue reference, and a SHA-256 digest of the tracked provider
target identifier. Launchplane derives the context from the stored product
profile and rejects untracked, ambiguous, non-application, preview-active,
busy, or changed authority.

For present Dokploy deployment history, normal retirement behavior is unchanged:
the application state must be one of `completed`, `done`, `exited`, `idle`,
`ready`, `running`, `stopped`, or `success`, and the latest deployment status
must be terminal. An explicitly recognized empty deployment list uses the local
`no_history` deployment-status sentinel and is retirable only when the current
application state is exactly `idle`; Dokploy `done` can represent a successfully
deployed, possibly serving application. Malformed or unrecognized deployment
responses, a blank or nonterminal deployment status, and changed, deploying, or
unknown application evidence fail closed.

Planning persists an append-only audit record and returns its record ID and
digest without exposing provider identifiers. Apply requires that exact stored
record and digest plus the target-bound confirmation phrase. Before the first
provider effect, Launchplane changes the profile from `active` to `retiring`,
which excludes it from active automation. Reconciliation observation is strictly
read-only; an observed absent application is finalized only by an acquired
provider-operation lease. Durable provider-operation leases and checkpoints
reconcile partial domain deletion, application deletion, lost responses, and
already-absent applications. Mutable runtime and target records
are removed only after provider absence is verified; runtime deletion events
and preserved managed-secret references remain audit evidence. The profile is
never deleted and becomes `retired` with previews disabled.

## Detached application retirement

`POST /v1/detached-application-retirement` is a separate bounded operation for
one Dokploy application that is not owned by Launchplane product/runtime
authority. It does not call product retirement and its adapter/store boundary
has no profile, target, runtime, secret, route, inventory, deployment, or authz
write/delete methods. The only allowed authority-write count is zero.

The request accepts `plan` or `apply`, exact project/environment/application
names, the candidate target identifier SHA-256, a sorted non-empty tuple of
expected protected application target SHA-256 values, reason and issue, plus
apply-only reviewed-plan identity/digest and exact confirmation. Raw target IDs
are never accepted. The candidate digest must not be protected.

Planning combines `/api/project.all` with a bounded, paginated global
`application.search` inventory. The candidate remains bound by globally unique
application name, exact target digest, matching project/search evidence, and its
own `application.one` project/environment evidence. Every accessible
application whose target payload has the same exact project name forms one
logical protection domain across duplicate physical project IDs. Launchplane
requires the exact expected protected digest set and persists each protected
application fingerprint, domains, and history snapshot. Search pagination,
totals, application IDs, and search-item-to-payload identity must remain stable
or planning fails closed.

The candidate ID must also be absent from every bounded Launchplane authority
source capable of carrying or resolving provider-target authority. The proof
stores only source names, record counts, and source digests, has a literal zero
match count, and cannot be built after an ID or exact provider-application-name
match. Deployment and provider-target records with consistent application-typed
target evidence are bound by that provider target ID, so a display-name
collision with a different target does not claim the candidate. Records without
consistent application target evidence and all other authority sources retain
exact provider-application-name matching. A tracked Dokploy `target_name` is
not treated as a provider application name by itself; exact provider IDs and
provider application ownership fields remain authoritative.

Apply requires the exact stored plan and rederives candidate discovery,
authority absence, and the protected snapshot under the same target-key
namespace used by product retirement. It checkpoints immediately before the
single `application.delete` call, treats a provider 404 as already absent,
reconciles unknown outcomes through fresh observation, verifies candidate
absence, and requires protected fingerprints to remain unchanged. Apply writes
one terminal detached-retirement record and no authority records.

Only a local admin or GitHub Actions running the exact SHA-pinned reusable
worker may reach the route. Authorization actions are
`detached_application_retirement.plan` and
`detached_application_retirement.apply` against Launchplane's global
control-plane context, never a product instance. Phase one adds the reusable
`workflow_call` worker and managed authz secret routing only; it intentionally
adds no mutable dispatch wrapper, live managed rule/secret value, deployment,
or provider mutation.

## Privileged-Operation API

The privileged-operation API is a typed governed surface, not an execution
proxy. Human create/read/cancel routes are under
`/v1/privileged-operations/plans`; the counts-only agent read is under
`/v1/agent/privileged-operations/plans/{operation_id}`.

Human routes reject every non-GitHub-human identity before policy evaluation
and use the browser origin/fetch-metadata/CSRF boundary for writes. Every route
requires schema-v2 policy and exactly one matching managed rule carrying both
managed IDs. Bare policy evaluation and unmanaged action-empty rules cannot
authorize the surface. The first planner invokes managed-secret re-encryption
with `apply=False`. Browser-human approval/revocation routes remain governed;
there is no HTTP execute or apply endpoint. Approval can be claimed immediately
by the supervised worker, so revocation is possible only before claim.

Responses and stored records follow the redaction contract in
`docs/privileged-operations.md`.

**Preserved history:** the Phase 1 planning-only API description predates the
supervised Phase 2 worker and is not current operating guidance.
