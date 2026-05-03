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
- `GET /v1/products/{product}/environments/{environment}`

These endpoints are profile and driver driven. A standard `generic-web` site
should appear in the read model from Launchplane records alone: product profile,
lane profiles, target records, runtime-environment records, managed secret
bindings, authz policy, and evidence records. The shared read model must not add
product-specific top-level fields; driver-specific data belongs behind driver
descriptor actions, capabilities, panels, or a driver-namespaced extension.

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
