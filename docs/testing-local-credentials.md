---
title: Testing local credentials
---

Launchplane generates local testing credentials inside the service with the
operating system's cryptographic random source (32 random bytes per value).
Values are encrypted before storage and never appear in requests, responses,
audit metadata or this client. Production records and running targets stay
unchanged. This does not generate accounts or external-service credentials.

The Odoo driver owns its three local password keys: `ODOO_ADMIN_PASSWORD`,
`ODOO_DB_PASSWORD`, and `ODOO_MASTER_PASSWORD`. Other drivers declare their
local keys on the stored product profile's managed-secret requirements with
`testing_local_credential: true`. This is runtime metadata, supplied through
the existing product onboarding/expected-config path, never a checked-in product
catalog or a new authorization grant. External credentials retain their existing
input path. A local requirement cannot also request Client credential input or
name a worker/service secret store.

New testing lanes generate these credentials in the onboarding authority bundle.
Reimporting an existing lane preserves them. Odoo fresh-generation bootstrap and
generic-web testing replacement publication generate fresh values in their own
atomic record bundles, before application deployment. Interrupted/replayed
publication reuses the committed lane records. Ordinary code updates do not
rotate passwords, and no existing database role or person's login is reset here.
Provider delivery and the supported sanitized-copy runner remain responsible for
using the new local values only on the independent recipient. Existing hold,
use, isolation, source and custody protections remain required.

## Existing lanes: plan, apply, read back

Deploy compatible API and workers before using this path. From a task checkout,
use the installed operator helper for its configured transport and identity:

```bash
uv run python scripts/prepare-testing-credentials.py dry-run \
  --operator-helper <installed-launchplane-skill>/scripts/launchplane-write-action.py \
  --product <product> --context <testing-context> \
  --reason '<reason>' --evidence-file <private-review-file>
```

Review the names-only receipt, then repeat the exact arguments with `apply`,
`--reviewed-dry-run` and `--idempotency-key <stable-key>`. Apply uses the existing
`POST /v1/product-config/apply` service contract and its `product_config.apply`
authorization, matching saved dry-run and atomic idempotent receipt. Each secret
entry uses `generate: "testing_local"`, its service-selected binding name and
`secret_class: "testing"`; no value or copy/adoption input is accepted. Generation
is restricted to declared local keys on an exclusively owned exact testing lane
in `runtime_environment`. A shared source is shadowed by a new testing record,
never rotated. Source provenance is `generated`; audit metadata records
`value_source: service_generated` and, for lifecycle writes, the generation.

Apply independently reads the audit back. Run `readback --product <product>
--context <testing-context> --operator-helper <helper>` separately as well.
An uncertain apply is retried only with its original key and intent; never use a
new key to hide an uncertain result. A deliberate later rotation needs a new
review and key. This updates encrypted bindings only: **do not sync the new DB
password into a running old database**. For replacement, continue the supported
independent resource/bootstrap and sanitized-copy procedure. Never reset a
restored person's password to make an admin/bootstrap password work.

## All-product audit

`prepare-testing-credentials.py audit --operator-helper <helper>` reads
`GET /v1/testing-credentials`. Add `--product <product>` for the corresponding
`GET /v1/products/{product}/testing-credentials` readback. Every stored product
and testing lane is included; missing lane read authority refuses the inventory
instead of silently filtering it. It compares managed runtime values inside the
service, including cross-key matches with each production lane of that product.
Only product, context, key and binding names, generated provenance and coverage
status leave the service. No plaintext, ciphertext or value fingerprint is returned.

Missing local credentials, missing production counterparts, decryption failure
and ambiguous ownership are `unavailable`, never a pass. `no_production` explicitly
means there was no production lane to compare. Overlaps on external keys are
reported too; an intentionally shared read-only integration is reviewed under its
existing sharing contract, never replaced by a random local password. This audit
covers Launchplane's managed runtime store, not provider-only or unrecorded keys,
nor a proof of current application authentication. Run it after compatible deploy,
generate OPW testing first, then use the same path for other local overlaps.
