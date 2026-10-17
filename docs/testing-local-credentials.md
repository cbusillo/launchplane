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

Onboarding imports existing provider targets and preserves their credentials.
Actual independent-recipient creation owns generation: Odoo fresh-generation
bootstrap and generic-web testing replacement preparation generate values in their own
atomic record bundles, before provider configuration or application deployment.
Generic-web custody records the resulting metadata digest atomically, so a
configuration retry reuses the generated values. Odoo fresh-copy bootstrap
generates only its three driver-owned keys; omitted extra settings stay omitted. Interrupted/replayed
publication reuses the committed lane records. Ordinary code updates do not
rotate passwords, and no existing database role or person's login is reset here.
Provider delivery and the supported sanitized-copy runner remain responsible for
using the new local values only on the independent recipient. Existing hold,
use, isolation, source and custody protections remain required.

## Existing lanes: plan, apply, read back

Deploy compatible API and workers before using this path. From a task checkout,
use the installed admin helper for its configured transport and identity:

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

Apply independently reads local-credential separation and provenance back. Run
this separately as well:

```bash
uv run python scripts/prepare-testing-credentials.py readback \
  --product <product> --context <testing-context> --operator-helper <helper>
```

An approved shared integration stays visible in the audit while local readback
can pass. Audit exits nonzero for overlap or unavailable coverage. A confirmed
apply whose readback fails reports `accepted_unverified`; retry only its
original key and intent after inspecting the failed readback.
An interrupted generic-web activation retains its original key, placement and
staff hold. Its prepared lane credential intent is for the new recipient; the
retained old target stays excluded. Use original-key readback/resumption; this
path has no abandonment or old-target reactivation operation. Never lift its
hold or redeploy the old target to bypass a partial activation.

An uncertain apply is retried only with its original key and intent; never use a
new key to hide an uncertain result. A deliberate later rotation needs a new
review and key. This updates encrypted bindings only: **do not sync the new DB
password into a running old database**. Until the lane is replaced, an ordinary
Odoo testing deploy onto its retained data refuses before any provider write with
`deploy_blocked.retained_database_credential_changed` when the stored
`ODOO_DB_PASSWORD` differs from the running target's; `path-check --path testing`
then reports it as the blocked `testing_deploy` step. For replacement, continue
the supported independent resource/bootstrap and sanitized-copy procedure. Never reset a
restored person's password to make an admin/bootstrap password work.

## All-product audit

`prepare-testing-credentials.py audit --operator-helper <helper>` reads
`GET /v1/testing-credentials`. Add `--product <product>` for the corresponding
`GET /v1/products/{product}/testing-credentials` readback. Every stored product
and testing lane is included; missing lane read authority refuses the inventory
instead of silently filtering it. It compares managed runtime values inside the
service, including cross-key matches against every product's production lanes.
The product-specific readback compares against that product's own production lanes.
Only product, context, key and binding names (including the production keys
actually compared), generated provenance and coverage
status leave the service. No plaintext, ciphertext or value fingerprint is returned.

Missing local credentials, decryption failure
and ambiguous ownership are `unavailable`, never a pass. `no_production` explicitly
means there was no production lane to compare. Overlaps on external keys are
reported too; an intentionally shared read-only integration is reviewed under its
existing sharing contract, never replaced by a random local password. This audit
covers Launchplane's managed runtime store, not provider-only or unrecorded keys,
nor a proof of current application authentication. Run it after compatible deploy,
generate OPW testing first, then use the same path for other local overlaps.
