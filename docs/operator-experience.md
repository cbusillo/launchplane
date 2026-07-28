---
title: Operator Experience
---

## Direction

Launchplane operator work is API-first. Finish the product/environment API
contract before rebuilding the browser UI. The current React UI is transitional;
do not spend time refining its context picker or product-config layout except
for secret-safety regressions.

The rebuilt UI should be a product operations surface, not a raw record browser.
The first screen should show products, their stable environments, current
operational state, and the next safe action.

## Primary User And Job

The primary user is an operator who may also be a developer, but who is acting
in an operations context. Their job is:

> Understand what is running for one product, whether it is healthy, why it is
> unhealthy, and what action is safe to take next without understanding or
> bypassing Launchplane's provider plumbing.

The same person may switch into engineering maintenance work, but Product Ops
and Engineering Ops are separate jobs and must have separate navigation:

- **Product Ops** owns product environments, previews, settings, secrets,
  promotions, maintenance, activity, and diagnosis.
- **Engineering Ops** owns the work graph, GitHub issue reconciliation, Every
  Code queues, merge-train controls, and platform maintenance.

Product Ops is the default surface. Engineering Ops may reuse the same session,
theme, and API transport, but it must not dominate the product workspace or the
first screen.

## Representative Operator Journeys

The clean-slate product model is grounded in recent Launchplane work rather than
generic dashboard assumptions.

### Recover A Product Safely

In the rebuilt UI, an operator who discovers that a product profile or scoped
workflow grant is missing must see which product record, provider target, runtime
configuration, secret bindings, and grants are missing. The operator reviews a
dry-run, applies only the missing authority through the service, runs an
isolated preview canary, destroys it, and sees clean lifecycle evidence.

### Diagnose A Public TLS Failure

In the rebuilt UI, an operator who sees that a preview or stable environment is
unreachable must receive an environment view that explains the bound domain,
runtime placement, ingress termination, TLS owner, certificate observation,
provider/runtime identity, and the evidence behind the failure. Normal
diagnosis must not require direct Dokploy, edge-proxy, DNS-provider, or database
inspection.

### Prove Preview Apply And Destroy

The rebuilt UI must let an operator compare desired and actual previews,
refresh one through a reviewed plan, verify health and runtime identity,
destroy it, and confirm that no provider or Launchplane inventory remains
orphaned. Apply, destroy, and report-only reconciliation are one understandable
journey.
Reviewed Odoo preview plans are service-issued, short-lived, and bound to their
artifact and provider-routing evidence. If that evidence changes or the plan
expires, the UI must present a stale-plan result and obtain a new plan rather
than offering a force-apply control.

### Promote A Verified Release

The rebuilt UI must show what artifact is in testing and production, why
promotion is enabled or blocked, and how to run a browser-safe dry-run,
dispatch the product-owned workflow, and follow promotion evidence without
creating a release or mutating production during dry-run.

### Change Runtime Settings Or Secrets

The rebuilt UI must show expected runtime keys separately from managed-secret
bindings and let an operator review missing/stale/disabled state, submit a
dry-run, and apply the change without plaintext secret values remaining in the
browser, response, logs, or activity record.

## Golden Paths And Anti-Goals

The primary golden paths are:

1. Select a product and understand testing, production, previews, warnings, and
   the next safe action.
2. Open an environment and diagnose placement, domain, ingress, TLS, runtime
   identity, configuration, and health evidence.
3. Review and execute a supported dry-run, apply, workflow dispatch, refresh,
   destroy, or maintenance action.
4. Follow activity and evidence until the action reaches a clear terminal or
   reconciliation state.

The rebuilt product must not become:

- a generic card dashboard
- a raw context, route, provider-ID, or record browser
- a fleet-wide engineering queue presented as Product Ops
- a collection of buttons that only prepare requests or perform no operation
- a source of inferred, fixture-backed, or reassuring placeholder state
- a second authority for runtime configuration or provider topology

## Information Architecture

```text
Launchplane
  Product Ops
    Products
      Product workspace
        Overview
        Testing
        Production
        Previews
        Runtime settings
        Managed secrets
        Promotions
        Activity
        Maintenance
        Diagnostics
  Engineering Ops
    Work graph
    Issue reconciliation
    Every Code
    Merge train
    Platform maintenance
```

The product workspace is the primary object. Stable lanes and previews are
visually distinct children of that product. Runtime settings and managed
secrets are separate surfaces. Activity is an operator timeline. Diagnostics
contains raw contexts, provider IDs, route paths, record IDs, and provider-only
evidence that ordinary operation does not require.

## Action Taxonomy

Every visible action must declare one of these behaviors:

- `inspect`: read-only navigation or evidence retrieval
- `dry-run`: computes and records a plan without a live mutation
- `apply`: performs a supported service mutation after required review
- `workflow-dispatch`: starts a product-owned or operator-owned workflow
- `destructive`: removes or disables live mutable state and requires explicit
  confirmation plus replacement/recovery evidence
- `unsupported`: visible only when explaining why the capability is unavailable

No control may look enabled when it only prepares a request, copies a command,
or has no execution handler. Disabled actions show exact prerequisite,
authorization, evidence, or trust-state reasons.

The environment `Actions` child route is the browser capability inventory. It
classifies every advertised descriptor action through an explicit typed adapter
registry and shows server blockers separately from browser implementation
blockers. Descriptor `route_path` values are discovery evidence only and are
never executed dynamically. Until an action-specific typed form owns its input,
confirmation, idempotency, replay, and result states, the inventory remains
non-executable even when the server reports the action as enabled.

## First-Run And Empty States

Empty states are part of the product contract:

- no products: explain how a product becomes Launchplane-owned and link to the
  supported onboarding action or documentation
- product without stable evidence: show the profile and missing records, not an
  empty dashboard
- no previews: distinguish previews disabled, no desired previews, and missing
  inventory evidence
- missing settings/secrets: list required key or binding names and the safe
  action that can resolve them
- unsupported capability: name the driver limitation without presenting an
  actionable control
- stale or failed reads: preserve the last recorded value only with an explicit
  trust state and timestamp

Loading, empty, blocked, missing, unsupported, and error are different states
and must not share a reassuring generic placeholder.

## Responsive Product Contract

Desktop layouts optimize for fast comparison across testing, production, and
previews. Narrow layouts preserve the same hierarchy in one column: product
identity, warnings, next action, lane state, and supporting evidence. Critical
status, trust, timestamp, and action labels must not depend on hover or color
alone.

## Product Model

The primary operator model is:

```text
Product
  - testing environment
  - prod environment
  - previews
  - runtime settings
  - secret bindings
  - promotions
  - activity
  - maintenance
```

Product names are display names such as `SellYourOutboard`, `VeriReel`,
`Odoo CM`, and `Odoo OPW`. Raw context strings are routing identifiers and
should appear only as diagnostics or evidence metadata.

For SellYourOutboard, the canonical product key is `sellyouroutboard`. Stable
environments live under that product as `testing` and `prod`. Legacy names such
as `sellyouroutboard-testing` are transition details and must not be the primary
picker model.

These names describe the current operator model. Launchplane records remain the
authority for live product keys, lanes, contexts, repositories, domains,
targets, and bindings.

## API Contract

Add product/environment read models before replacing the UI:

- product overview: display name, product key, owning repo, driver, stable
  environments, preview summary, warnings, and available actions
- environment detail: addressed by product plus environment, with target,
  domain, deploy, promotion, runtime, and secret summaries
- settings summary: grouped runtime variables and managed secret bindings with
  `configured`, `missing`, `disabled`, `unvalidated`, `stale`, or `unsupported`
  states and no plaintext values
- action availability: dry-run, workflow dispatch, settings apply, preview
  refresh, and cleanup actions with explicit enabled/disabled reasons
- activity: deployments, promotions, preview events, cleanup events, and authz or
  policy changes that matter to operators
- incidents: active cross-product summaries plus environment-scoped incident
  history and detail with linked observations, material events, reminder state,
  and redacted delivery evidence

Low-level records remain useful for diagnostics, but diagnostics are secondary.
Normal operators should not need to choose a raw context or understand provider
lookup rows before taking safe action.

Incidents remain children of product environments rather than a separate raw
record browser. Product summaries surface active incident count and severity;
the environment view owns lifecycle state, material evidence, observation
history, and delivery evidence for one occurrence. GitHub, email, and Discord
notifications are sinks, not authority. Incident surfaces are `inspect` actions
only until a typed write contract owns acknowledgement or silence input,
confirmation, authorization, idempotency, replay, and result states. The read
model may show provider-safe external links and bounded delivery failures, but
must not expose destination or policy identities, raw outbox payloads, provider
operation internals, raw target URLs, secret references, or provider error text.

The first product/site read endpoints are:

- `GET /v1/products`
- `GET /v1/products/{product}`
- `GET /v1/products/{product}/activity`
- `GET /v1/products/{product}/environments`
- `GET /v1/products/{product}/environments/{environment}`
- `GET /v1/products/{product}/environments/{environment}/config-status`
- `GET /v1/products/{product}/environments/{environment}/public-ingress/incidents`
- `GET /v1/products/{product}/environments/{environment}/public-ingress/incidents/{incident_id}`
- `GET /v1/products/{product}/contexts/{context}/instances/{instance}/operational-readiness?action={authz_action}&artifact_id={artifact_id}&expected_current_artifact_id={expected_current_artifact_id}`

These endpoints are profile and driver driven. A standard `generic-web` site
should appear in the read model from Launchplane records alone: product profile,
lane profiles, target records, runtime-environment records, managed secret
bindings, authz policy, and evidence records. The shared read model must not add
product-specific top-level fields; driver-specific data belongs behind driver
descriptor actions, capabilities, panels, or a driver-namespaced extension.

Product activity is an operator timeline composed from existing Launchplane
records. Events carry product, context, environment, driver id, action id,
status, timestamp, and record links so the UI can render deployments,
promotions, rollbacks, backup gates, previews, cleanup, feedback, and relevant
authz changes without loading raw record payloads.

Authz events are attributed from the managed reconcile mutation delta, not from
membership in the resulting cumulative policy snapshot. Grant, removal, and
update copy names the affected product and operation without exposing raw rule
selectors. For older records without managed diff audit, the timeline compares
adjacent policy snapshots and only emits a clearly labeled legacy change when a
product-specific effect can be proven; otherwise it omits the record.

Product environment config status compares product-profile expected config
requirements against recorded runtime-environment keys and managed secret
bindings for the stable lane. Expected keys are declarative product intent;
configured, missing, and disabled states are derived from Launchplane records.
The response includes key names, binding metadata, status, source, and freshness
only. It never includes runtime values, managed secret IDs, secret plaintext, or
ciphertext.

Operational enrollment readiness is a separate exact-lane, exact-action read.
It requires `product_environment.read` for the requested product, context, and
instance, then evaluates the authenticated GitHub Actions caller against the
single active DB-backed authorization policy using the same managed,
instance-scoped semantics as durable operations. A ready authorization result
requires the captured managed rule itself to use singleton exact product,
context, instance, action, caller-workflow, and immutable reusable-workflow
selectors; a broader rule remains blocked even when it happens to allow the
current request. Supported driver actions
declare which provider-target, route-binding, runtime-environment,
managed-secret binding, artifact, deployment, and topology dimensions are
required. Overall and per-dimension results use `ready`, `blocked`, `stale`,
`missing`, or `unsupported`; every non-ready result names its owning Launchplane
record class or supported service remediation. Runtime values, managed-secret
IDs, secret material, provider credentials, provider evidence maps, and raw
OIDC claims are never returned.

Readiness treats error-severity topology findings as blockers. Advisory warnings
such as the intentionally limited visibility into externally managed ingress
remain visible in dimension details without blocking a lane whose recorded
authority and public observations otherwise pass. Provider-target authority,
deployment evidence, route authority, and observed topology each derive state
from their own owning evidence rather than inheriting an uninitialized aggregate
lane status.

The endpoint is read-only. Missing production enrollment remains a truthful
blocked or missing result and does not create a route, grant, provider target,
deployment, secret, or scheduler target. An exact artifact ID is required only
when the selected driver action declares artifact readiness; Launchplane reads
the persisted manifest and never rebuilds or infers it.

The environment `Actions` route lets an operator select one unambiguous primary
authorization action advertised by the environment read model, then derives the
product, context, instance, candidate artifact, and expected current artifact
from that same Launchplane-owned response before reading readiness. It does not
accept caller-entered lane or artifact selectors. The browser renders overall
state, every returned dimension, bounded owner/evidence records, advisory
details, and remediation metadata. A ready dimension may still carry a clearly
labeled non-blocking advisory.

A browser readiness read evaluates the signed-in browser identity and therefore
does not prove immutable workflow authorization. The UI calls this out directly;
only the pinned GitHub Actions worker can prove its exact caller and reusable
workflow refs by running the same preflight. Remediation methods and route paths
are evidence, not dynamic browser controls. They remain non-executable until a
typed browser operation or reviewed workflow owns the input, confirmation,
idempotency, replay, and result contract. A non-ready dimension with no supported
no-effect remediation says so explicitly instead of inventing a provider or
record mutation.

## Promotion Safety

The Actions view reads
`GET /v1/products/{product}/environments/{environment}/promotion-status` before
rendering generic-web promotion controls. That product-owned status derives the
source artifact and revision from generated testing runtime identity evidence,
binds them to current testing and production inventory, and reports freshness,
health, runtime-identity, provider-target, storage, managed-GitHub-credential,
and authz blockers. Missing, stale, mismatched, or unauthorized evidence keeps
every control disabled.

Browser sessions use only the product-owned
`POST /v1/products/{product}/environments/{environment}/promotion/dry-run`
route for direct promotion. The body contains an operator reason, reviewed
evidence fingerprint, and bump mode; it never contains product context,
artifact identity, source revision, provider identity, or a direct-live switch.
Launchplane accepts workflow dry-run or live dispatch only after the same
operator identity has an accepted direct dry-run matching the current evidence
and bump mode.

Live promotion is never executed directly from the browser. The UI dispatches
`POST /v1/products/{product}/environments/{environment}/promotion/workflow-dispatch`
with the exact server-provided confirmation. The confirmation names the product,
source artifact, source revision, production lane, bump mode, and release/deploy
side effects. The outbox response remains `pending`; the UI reads
`.../promotion/workflow-deliveries/{delivery_id}` to distinguish dispatch state
from the observed GitHub run state without claiming completion.

Launchplane includes a unique `promotion_intent_id` workflow input backed by
the persisted outbox delivery. The product workflow must pass that value in the
raw live-promotion request and use it as the `Idempotency-Key`. Launchplane then
re-checks the current evidence fingerprint, production target, source artifact,
and source revision before executing. It resolves that reviewed provider target
once, acquires a durable target-scoped mutation reservation, and passes the same
snapshot into deployment; concurrent retries are rejected while the first call
is running, and completed retries replay the original response. Trusted
automation that intentionally bypasses product-owned review requires the separate
`generic_web_prod_promotion.execute_unreviewed` grant; the normal execute grant
alone cannot run raw live promotion. The escape hatch still requires database
storage, an `Idempotency-Key`, the same target snapshot check, and the same
durable provider-mutation reservation.

Both testing and production inventory must be fresh, healthy, and bound to
matching generated runtime identity. The source and
destination artifacts must be digest-pinned `@sha256:` references and source
revisions must be immutable commit IDs. Current production provider-target
authority comes only from the explicit provider-target record; deployment
history and provider-specific legacy records do not satisfy availability. The
provider-target record is part of the reviewed evidence fingerprint, so target
replacement requires a new direct dry-run.

If a dispatched direct dry-run or workflow request becomes uncertain,
Launchplane keeps its idempotency identity and exact request payload in session
storage and permits only that exact-payload retry. Navigation or refresh must
not silently create a replacement operation. After workflow dispatch is
accepted, the browser also retains the accepted delivery receipt, resumes its
status read after navigation, and keeps replacement dispatches locked until the
outbox delivery reaches a terminal observed state.

Before claiming UI promotion is ready, prove the signed-in browser path against
Launchplane:

- dry-run generic-web promotion from the UI
- workflow dispatch with `dry_run=true`
- no GitHub release created during dry-run
- no prod deployment during dry-run
- workflow dispatch rejected when the reviewed evidence or bump changes
- live dispatch rejected without the exact confirmation
- replay evidence shows current trace, original trace, and replay state
- visible action availability and failure reasons when authz or prerequisites
  are missing

Do not dispatch a live product promotion until the direct dry-run, workflow
dry-run, blockers, and evidence are clean.

## Runtime Settings And Secrets

Use operator language:

- Runtime settings: non-secret environment/config values Launchplane owns.
- Secrets: managed secret records and bindings. The UI shows status, binding,
  validation, and audit metadata; it does not reveal plaintext values.

Settings writes require dry-run first, show only key names/counts/status, and
clear submitted secret values immediately on submit and on error.

`GET /v1/products/{product}/environments/{environment}/config-status` is the
browser authority for these controls. Its `write_availability` field separates
runtime-setting and managed-secret plan/apply availability, exact authz and
prerequisite blockers, matching-dry-run and idempotency requirements, the
confirmation text, and generic irreversible or live-sync consequences. The UI
must keep a form disabled when this authority is missing or blocked, and it
must unmount write forms while a refresh is unresolved or failed rather than
execute against cached authorization evidence.

The browser writes through
`POST /v1/products/{product}/environments/{environment}/config/apply`. The
service resolves the stored product profile and lane from the path, accepts only
profile-declared runtime keys or managed-secret bindings, and supplies the
context, instance, scope, and source label itself. A managed-secret selection
includes both its displayed integration and binding key so repeated binding-key
names remain unambiguous; the server resolves that pair against the stored
profile rather than trusting it as target authority. The browser does not send a
raw context picker or checked-in product defaults.

Runtime-setting and managed-secret forms remain separate. Both require a reason
and a dry-run. Apply requires the exact server-advertised confirmation, the
matching normalized payload, and a stable idempotency key. The confirmation
surface shows product/lane scope, changed runtime and secret counts,
irreversible consequences, and any separately required live-target sync before
the operator can apply.

Managed-secret values exist only in uncontrolled password inputs and the
immediate request local variable. The UI clears those inputs before dispatch and
again on secret-input validation failure, request failure, route change, and
unmount. Apply requires the operator to re-enter the same values; retained
browser operation state contains only a fingerprint, idempotency key, redacted
result, trace, and failure evidence. An uncertain apply locks every editable
draft field so the only mutation retry preserves the original operation key and
payload. Live-target endpoints returned in `next_actions` are rendered as
inspect-only evidence until they have a separate generated browser adapter.

## Cleanup Safety

Legacy cleanup is an admin or maintenance action, not a primary product flow.
Cleanup must refuse to delete canonical product contexts and must only remove or
disable current-authority legacy rows after replacement coverage is proven.

Preserve historical records such as deployments, promotions, backup gates,
preview history, inventory evidence, and release tuples. Delete or disable only
mutable current-authority rows that Launchplane can prove are legacy.

## Data Trust

Every operator-visible field needs a trust state:

- `verified`: directly refreshed from a provider or workflow within the expected
  freshness window
- `recorded`: real Launchplane evidence exists, but it is not freshly provider
  verified
- `stale`: evidence exists but is outside its freshness window
- `missing`: Launchplane has no evidence for the field
- `unsupported`: the driver intentionally does not expose the capability

Do not show fixture, demo, fallback, inferred, or placeholder operational data in
production UI without a visible trust state.

Agent-facing context uses the same trust vocabulary and applies stricter safety
rules because payloads may be copied into local terminal sessions. Agent context
must include compact provenance for the source record or source URL, distinguish
verified, recorded, stale, missing, and unsupported evidence, and redact local
paths, secret-shaped values, bearer tokens, provider-only topology, raw issue
bodies, and worker hostnames before response serialization.

## UI Rebuild

When the API contract is ready, rebuild the UI around:

- product list and product overview
- environment detail for `testing`, `prod`, and previews
- runtime settings and secrets grouped by product/environment
- promotion dry-run and workflow dispatch
- preview state and lifecycle actions
- activity and diagnostics

Reusable pieces from the current UI may survive only if they fit the new model:
session/auth client, API request wrapper, status formatting, evidence formatting,
and theme basics. The current context-picker/product-config flow should be
hidden or removed once the new settings flow covers its use cases.

The handwritten frontend contract mirror is not reusable authority. Generated
backend contracts should replace request/response types; handwritten frontend
types remain only for UI state and view models.

### Browser Route Contract

The clean-slate shell uses URL-owned product selection under the service-owned
`/ui` prefix:

- `/ui/products` lists Launchplane-owned products by display name with compact
  testing, production, preview, warning, trust, and safe-inspection summaries.
- `/ui/products/{product}` is the canonical product workspace. The route key is
  the stored product key, while the visible identity remains the product display
  name.
- `/ui/products/{product}/environments/{environment}` is the canonical stable
  environment view. Runtime settings, managed secrets, and diagnostics are
  separate child routes under that environment so secret-binding state is not
  blurred into ordinary runtime configuration.
- `/ui/products/{product}/environments/{environment}/actions` classifies the
  server-advertised actions and renders exact server and browser blockers. It
  does not execute descriptor paths; action-specific forms are enabled only
  when a generated browser write operation is explicitly adapted.
- `/ui/products/{product}/activity` is the operator timeline. It is labelled
  Recent activity because the current backend read model returns a bounded
  latest-event window rather than a paginated complete history.
- `/ui/engineering` is a separate Engineering Ops boundary. Product routes do
  not load work-graph, issue-reconciliation, Every Code, merge-train, or platform
  maintenance data.
- `/ui/engineering/work-graph` reads the Launchplane-assembled snapshot and uses
  the generated browser-supported rank operation. Recommendation reasons,
  compact source evidence, repository identities, and safe-to-start state remain
  Engineering Ops evidence; the route does not load or select a product route.
- `/ui/engineering/issue-inbox` reads the explicit repository inventory and
  Code Plans membership. Browser reconciliation is visibly unsupported because
  the reconcile POST requires the native GitHub Actions OIDC or trusted
  owner-agent write identity boundary and is not in the generated browser write
  contract. The page renders no dead Dry Run or Apply control.
- `/ui/engineering/every-code` reads a bounded summary window and labels it as a
  recent operator snapshot rather than complete history. Rerun and worker
  transitions remain outside the browser surface.
- `/ui/engineering/merge-train` selects only DB-backed policy targets and reads
  controller status, policy digest, lease, reconciliation, latest run, and
  durable record evidence. It does not infer targets from products or work graph
  items and does not dynamically call worker routes.

Product list and product detail reads have independent loading, empty, denied,
missing, and failure states. A failed read must not become an empty product list,
and a direct product URL must use `GET /v1/products/{product}` rather than a
driver/context fallback. Environment, settings, secrets, activity, promotion,
maintenance, and Engineering Ops child routes are added only when their typed
views and supported controls exist. Environment diagnosis uses desired,
provider-recorded, and observed topology as distinct evidence; a verified read
is not presented as healthy when the recorded ingress, TLS, or runtime identity
condition is failing.

Each Engineering Ops child route owns an abortable request lifecycle. Initial
loading, denied, empty, unavailable, and cancelled states are distinct. A failed
or cancelled refresh may retain the last accepted response only when the page
marks it as cached evidence and preserves the service trace where available.
