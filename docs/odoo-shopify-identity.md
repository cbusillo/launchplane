---
title: Held testing Shopify identity
---

`GET /v1/products/{product}/environments/testing/shopify-identity` checks Shopify
store separation without starting Odoo or changing a lane. It requires existing
`operations.read` access to both the product's testing and production lanes.
It accepts no query parameters, credentials, SQL, GraphQL or destination overrides.

The service resolves both lanes from the current product profile and verifies
their immutable running-container identities and image digests. Testing must
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
hashes leave the container. Prepared settings and protected-store lists alone
cannot prove production's identity. Missing or conflicting applied settings,
rejected credentials or an unverifiable production runtime leave separation
unknown. The production query runs after testing's response. Comparing immutable
shop IDs prevents domain aliases from appearing to be separate stores.

Testing's managed token is decrypted inside Launchplane and consumed by one fixed
`query { shop { id myshopifyDomain } }` request inside testing's verified container.
The existing provider transport disables terminal echo/history before sending
private input. The token travels on stdin, never Docker arguments, environment,
files or result output. The probe uses HTTPS, refuses redirects and proxy
inheritance, bounds the response and uses no retry. It returns only the shop-ID
and canonical-domain hashes. Testing's returned domain must agree with its requested store. Both shop IDs
and domains must differ before separation is proved. Runtime, container, settings, hold and credential authority changes discard
the evidence. The hold, provider environment, Odoo database settings and dispatcher
remain unchanged.

After deploying this source, from a checkout containing the matching caller:

```sh
uv run python scripts/read-shopify-identity.py \
  --operator-helper <installed-launchplane-skill>/scripts/launchplane-write-action.py \
  --product <product>
```

The caller reuses the installed admin transport and private configuration.
It prints the trace and strictly validated hash-only result. Exit zero requires
`separation=separate` with complete verified evidence. `same`, `unknown`,
`token_rejected`, HTTP refusal and malformed replies exit nonzero. Stop there;
retain the trace. An unavailable production runtime needs supported runtime
identity/configuration evidence, not a guessed store identity or grant. A
rejected testing token requires the Director's credential-entry process.

A successful comparison proves store separation for this read only. It does not
prove provider environment delivery, applied testing database settings, export
qualification or authority for a later operation. The operational issue owns
those steps and its current holds. No hold lift, restart, post-deploy, reset,
dispatcher activation or Shopify write is part of this command.

Shopify's [shop query contract](https://shopify.dev/docs/api/admin-graphql/2026-01/queries/shop)
defines the returned identity as the shop corresponding to the supplied token.
