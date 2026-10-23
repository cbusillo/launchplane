---
title: Held testing Shopify identity
---

`GET /v1/products/{product}/environments/testing/shopify-identity` checks Shopify
store separation without starting Odoo or changing a lane. It requires existing
`operations.read` access to both the product's testing and production lanes.
It accepts no query parameters, credentials, SQL, GraphQL or destination overrides.

The service resolves both lanes from the current product profile and verifies
production's immutable running-container identity and image digest. Testing must
retain its `isolation_staging` hold and Shopify `dev_store` allowance. Its prepared
addon settings must select the existing exact-lane, testing-classified managed
API token. Existing addon validation, runtime-delivery selection, retirement and
protected-store checks still run. A token bound outside testing or a target that
aliases another lane is refused. This read does not bypass the hold for runtime
sync or any other write.

Production's applied `ir.config_parameter` store key, API version and existing
credential are read inside its verified container with a read-only PostgreSQL
transaction and statement timeout. The same fixed identity-only Shopify query
obtains its authoritative shop ID and canonical domain. Its credential stays
inside that process. Applied settings are re-read after the query; only identity
hashes or fixed failure codes leave the container. Prepared settings and protected-store lists alone
cannot prove production's identity. Missing or conflicting applied settings,
rejected credentials or an unverifiable production runtime leave separation
unknown. The production query runs after testing's response. Comparing immutable
shop IDs prevents domain aliases from appearing to be separate stores.

Testing's store and API version come from its stored addon settings. Its managed
token is decrypted inside Launchplane and consumed by one fixed
`query { shop { id myshopifyDomain } }` request. Testing needs no running Odoo
container, runtime inventory or delivered provider environment for this read.
The query runs in a local child process with a fixed total deadline, including
DNS resolution and response reads. Its token travels on stdin, never command
arguments, environment, files, the provider host, Docker or result output.
The probe uses HTTPS, refuses redirects and proxy
inheritance, bounds the response and uses no retry. It returns only the shop-ID
and canonical-domain hashes. Testing's returned domain must agree with its requested store. Both shop IDs
and domains must differ before separation is proved. Production runtime/container, settings,
hold and credential authority changes discard the evidence. The hold, provider environment, Odoo database settings and dispatcher
remain unchanged.

After deploying this source, from a checkout containing the matching caller:

```sh
uv run python scripts/read-shopify-identity.py \
  --operator-helper <installed-launchplane-skill>/scripts/launchplane-write-action.py \
  --product <product>
```

The caller reuses the installed admin transport and private configuration.
It prints the trace and strictly validated hash-only result. The caller waits up
to 180 seconds for the two sequential probes and their provider reads. A client
timeout reports `client_timeout`; it is unavailable comparison evidence and never authorizes a retry. Exit zero requires
`separation=separate` with complete verified evidence. `same`, `unknown`,
`token_rejected`, HTTP refusal and malformed replies exit nonzero. Stop there;
retain the trace. An unavailable production runtime needs supported runtime
identity/configuration evidence, not a guessed store identity or grant. A
rejected testing token requires the Director's credential-entry process.

The result also identifies the failed `stage` and a fixed, value-free `reason`.
The API logs unavailable results at WARNING with those same codes and the trace; exceptions and provider responses
are never included. This synchronous read creates no deployment or queued-operation
record. Preserve its response as the run receipt and correlate it with retained
service logs. Older responses without these diagnostics cannot identify which
prerequisite failed.

| Stage or reason | Supported next check |
| --- | --- |
| `authority` | Check the testing hold, dev-store allowance, prepared addon settings and exact-lane managed binding. |
| `production_runtime` | Check production's recorded runtime identity and verified running container. |
| `testing_credential` | Check the existing managed version and decryption configuration without collecting its value. |
| `configuration_unavailable` or `database_identity_unavailable` | Check the production runtime's generated Odoo configuration and database identity. |
| `database_driver_unavailable` or `database_unavailable` | Check the selected runtime's Python database driver and read-only database connectivity. |
| `settings_unavailable` or `settings_changed` | Check production's applied store settings; changed settings invalidate this read. |
| `request_invalid`, `request_failed` or `response_invalid` | Check that side's existing store/API settings, connectivity and identity-only response. |
| `testing_probe` or `production_probe` with `probe_unavailable` | The bounded probe could not complete or return valid evidence; inspect correlated service evidence without replaying it. |
| `token_rejected` | That side's existing token was rejected; use the Director's credential-entry process. |
| `probe_failed`, `runtime_changed`, `authority_changed` or `evidence` | Inspect correlated service/runtime evidence; do not replay the identity check or change the hold to diagnose it. |

When production's probe cannot supply identity, the response retains the two
verified testing hashes only after checking testing authority and production
runtime selection again. A testing-domain mismatch or authority, runtime or
applied-settings drift discards them. The response still reports
`separation=unknown` and exits nonzero. These partial hashes are testing identity,
not a successful comparison with the managed production lane. An operator with
an independently verified business-store identity can compare the same forms:
SHA-256 of the exact lowercase canonical `myshopify.com` domain and the full
`gid://shopify/Shop/<id>` string, with no trailing newline. A numeric ID hash
cannot be compared with a GraphQL GID hash.

Completed comparisons use `stage=complete` and an empty reason. Both `same`
and `separate` are completed comparisons; only `separate` permits exit zero.

A successful comparison proves store separation for this read only. It does not
prove provider environment delivery, applied testing database settings, export
qualification or authority for a later operation. The operational issue owns
those steps and its current holds. No hold lift, restart, post-deploy, reset,
dispatcher activation or Shopify write is part of this command.

Shopify's [shop query contract](https://shopify.dev/docs/api/admin-graphql/2026-01/queries/shop)
defines the returned identity as the shop corresponding to the supplied token.
