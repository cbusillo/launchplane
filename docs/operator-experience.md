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

Low-level records remain useful for diagnostics, but diagnostics are secondary.
Normal operators should not need to choose a raw context or understand provider
lookup rows before taking safe action.

The first product/site read endpoints are:

- `GET /v1/products`
- `GET /v1/products/{product}`
- `GET /v1/products/{product}/activity`
- `GET /v1/products/{product}/environments`
- `GET /v1/products/{product}/environments/{environment}`
- `GET /v1/products/{product}/environments/{environment}/config-status`

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

Product environment config status compares product-profile expected config
requirements against recorded runtime-environment keys and managed secret
bindings for the stable lane. Expected keys are declarative product intent;
configured, missing, and disabled states are derived from Launchplane records.
The response includes key names, binding metadata, status, source, and freshness
only. It never includes runtime values, managed secret IDs, secret plaintext, or
ciphertext.

## Promotion Safety

Browser sessions may dry-run generic-web promotion directly. Live promotion from
the UI should dispatch the product-owned GitHub workflow rather than mutating
prod directly from the browser session.

Before claiming UI promotion is ready, prove the signed-in browser path against
Launchplane:

- dry-run generic-web promotion from the UI
- workflow dispatch with `dry_run=true`
- no GitHub release created during dry-run
- no prod deployment during dry-run
- visible action availability and failure reasons when authz or prerequisites
  are missing

Do not run a live SellYourOutboard promotion with `dry_run=false` until the
dry-run path and evidence are clean.

## Runtime Settings And Secrets

Use operator language:

- Runtime settings: non-secret environment/config values Launchplane owns.
- Secrets: managed secret records and bindings. The UI shows status, binding,
  validation, and audit metadata; it does not reveal plaintext values.

Settings writes require dry-run first, show only key names/counts/status, and
clear submitted secret values immediately on submit and on error.

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

Product list and product detail reads have independent loading, empty, denied,
missing, and failure states. A failed read must not become an empty product list,
and a direct product URL must use `GET /v1/products/{product}` rather than a
driver/context fallback. Environment, settings, secrets, activity, promotion,
maintenance, and Engineering Ops child routes are added only when their typed
views and supported controls exist. Environment diagnosis uses desired,
provider-recorded, and observed topology as distinct evidence; a verified read
is not presented as healthy when the recorded ingress, TLS, or runtime identity
condition is failing.
