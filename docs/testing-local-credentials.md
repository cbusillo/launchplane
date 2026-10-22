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
then reports it as the blocked `testing_deploy` step. Runtime sync of a testing
lane refuses the same difference with `retained_database_credential_changed`, in
dry run and apply. For replacement, continue
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
means there was no production lane to compare. The testing side is what the lane
is delivered: a key in its `retired_provider_keys` is not compared. Overlaps on
external keys are reported too, with the testing binding's recorded
`sharing_kind` when it has one. Only a `read_only_source` decision lets an
external key stay on testing without failing the lane; every other overlap,
including other sharing kinds and any overlap on a local credential, is
`overlap`. Never replace an external key with a random
local password; remove it from testing with `retired_provider_keys`, give testing
its own provider-issued test key, or record a reviewed sharing decision. This audit
covers Launchplane's managed runtime store, not provider-only or unrecorded keys,
nor a proof of current application authentication. Run it after compatible deploy,
generate OPW testing first, then use the same path for other local overlaps.

## External provider observations

A managed requirement may declare `external_provider: "discord_bot"` on its
explicit runtime context. This purpose is stored metadata, not inferred from a
key name or credential value. It cannot also request local generation. To classify
an existing exact requirement through the existing expected-config service API:

```bash
uv run python scripts/declare-external-resource.py dry-run \
  --operator-helper <helper> --product <product> --context <context> \
  --instance testing --binding-key <key> --provider discord_bot \
  --reason '<reason>' --evidence-file <private-review-file>
```

Review the names-only declaration and repeat the same arguments with `apply`,
`--reviewed-dry-run` and `--idempotency-key <stable-key>`. Initial classification of
an already nonlocal binding is additive. Clearing its purpose, changing generation
intent or shadowing a local declaration uses the existing expected-config action
and a live product's admin boundary. A denial stays stopped; it is not permission
to switch identity. The client preserves other requirement metadata. Odoo's
driver-owned local password keys reject external declarations at profile validation.
It creates no account, credential, grant, provider resource or runtime change.
The client checks the saved intent and requirement, then independently reads the
classification back. An uncertain apply retains its original key and intent;
`accepted_unverified` requires investigation of the readback.

Add `--external-resources` to the existing `prepare-testing-credentials.py audit`
command to request `include_external_resources=true` on either audit GET. Without
that option, the existing response and outbound behavior stay unchanged. With the
option, only purpose-declared, delivered bindings are used for external reads.
All participating products and production/testing lanes must first be authorized.
The service alone decrypts the captured managed version and sends it to Discord's
fixed HTTPS API using bot authentication. Responses contain identifiers and bounded
status codes, never tokens, raw provider bodies or value fingerprints.
Before external-use decryption, the service persists a redacted
`resolution_attempted` audit event with caller type, request trace, binding/version
and destination class. Audit persistence failure prevents that external use.

The read observes bot/application identity and guild memberships across every
reported guild partition, following each partition through an empty terminal page.
Discord's `session_start_limit.max_concurrency` supplies the guild partition count;
this is distinct from the gateway's connection shard count. A changed partition
count, repeated or malformed page, timeout, redirect, oversized body or collection
budget leaves the relevant observation unavailable. Collection is bounded to 16
partitions, 48 pages, 4,000 guilds, 256 KiB per response, 16 declared lane bindings
and a shared 90-second network budget. HTTP calls each have at most 10 seconds.

The service captures authorized profiles, delivered bindings and the managed
record/version read set used by the local overlap audit before either audit. It
rechecks those inputs before every provider request and after collection. A change
discards all external entries with `inputs_changed` and marks the accompanying
local lane results unavailable; no database lock spans network
I/O. Missing declarations report `not_configured`, not a successful external audit.

Every external result says `qualification: "not_evaluated"`. Different bot IDs or
disjoint guild memberships do not prove resource isolation, current permissions,
absence of direct-message/global writes, what credentials old local state holds,
or exclusion of future old-generation effects. A guild enumeration is an interval
observation, not an atomic provider snapshot. Deployment and continuation gates
remain unchanged and require their own authoritative evidence.

Provider contract: [Discord current-user guilds](https://docs.discord.com/developers/resources/user#get-current-user-guilds)
and [current application](https://docs.discord.com/developers/resources/application#get-current-application).
