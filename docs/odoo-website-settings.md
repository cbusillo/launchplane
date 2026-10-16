---
title: Odoo website settings through an accepted release
---

`POST /v1/product-config/odoo-website-settings/apply` prepares typed intent on
the product's production instance-override record. It uses existing
`product_config.plan` / `product_config.apply` authority and exact lane
ownership. Preparation changes no Odoo database, deployment or access grant.

The request names the product/context, `instance: prod`, a reason, and
`website_settings: {artifact_id, settings}`. The artifact must be the product's
current recorded testing artifact. The origin must match the production lane's
recorded HTTPS base URL; configure that URL through the existing product-profile
service API/UI before preparing settings if it is absent. Settings contain the native `website_id`,
`public_origin` (HTTPS), `public_indexing_enabled`, `google_analytics_key`
(`G-…` or empty), `cookies_bar` and `block_third_party_domains`. Empty analytics
supports preparing indexing before the stream is supplied. GA4 requires both
native consent controls. Values belong in runtime records, never addon source.

Send `mode: dry-run` first. The result shows before/after settings and a
`plan_sha256`, without writing. To prepare, send `mode: apply`, the identical
intent, `reviewed_plan_sha256` and an `Idempotency-Key` header. The service
rechecks the plan and product/lane ownership, uses the existing atomic override
compare-and-swap, and reads the record back. Concurrent changes require a fresh
plan. The apply response's readback is intent readback; it is not Odoo readback
or live activation. This operation is exposed through the service API; it has
no protected workflow or installed helper binding.

To withdraw pending intent, send `website_settings: null` through the same
dry-run/reviewed-apply path. This preserves the applied/attempted snapshot and
changes no Odoo state. Subsequent release checklists explicitly inherit verified
applied values for the new candidate, so withdrawal cannot hide an active setting.
To switch collection off, prepare explicit disabled settings for acceptance.
Withdrawal remains available without a testing candidate
and on a two-slot lane; preparing new settings on that unsupported driver is refused.

The Client release checklist compares recorded production values with these
exact candidate choices, identifies kept/changed fields, and includes their
digest in the candidate identity. Changing a setting invalidates that release's
acceptance even when the artifact is unchanged. Production application selects
the exact decision authorizing the durable operation (including a recorded hotfix or override) for the artifact being deployed; newer
prepared settings cannot alter an accepted release. Default deploy/legacy-ship
rendering omits typed website intent; only maintenance supplies the selected
snapshot. Website verification is persisted independently from the generic
override result, so unrelated successful runs cannot confirm an unverified state. For full application the native consumer checks
that the selected website's domain equals the reviewed public origin, writes
and reads back native analytics/consent fields and
`cm_website.public_indexing_enabled` in its transaction. Launchplane requires
the matching post-commit readback digest. The stopped-web maintenance driver
also executes a generated copy of the committed devkit consumer through the
runtime's native Odoo shell; rollback images need not contain the new consumer.
The rendered legacy parameter carries the same indexing choice, so an older
image's startup settings addon cannot re-enable the forward flag.
A failed workflow attempts native verification before restarting web; failed
verification keeps web stopped. Its workflow result remains failed even when
verification allows web to restart. No new credential or provider interface is introduced.

A preparation alone never changes unrelated rollback: historical clearing is
triggered only by a recorded applied/attempted website snapshot. Same-artifact
manual recovery reuses its verified snapshot. Rollback selects the production snapshot retained in that operation's decision. Historical
artifacts without a website snapshot disable CM indexing and native analytics
across the database, without depending on the unverified new selector; they do
not retain forward-release settings. Collection-only historical rollback preserves existing cookie and third-party controls. Successful readback retains the actually
applied snapshot separately from pending intent, so subsequent review compares
against production's applied settings. A failed maintenance retains its exact attempted snapshot as unverified, outside the production checklist; the existing maintenance or durable operation retry must verify it before a new release review.

The supported application path is the stopped-web stable-target replacement driver.
The two-slot host provider currently has no website-settings maintenance receipt;
promotion with website snapshots refuses before provider effects. Preparation
and release review remain supported, and existing products configured for the
stable-target driver use the path above. Two-slot activation requires adding that
provider capability ([#3620](https://github.com/cbusillo/launchplane/issues/3620)); never change a live product's driver to bypass the refusal.

The devkit consumer runs on data workflows and every web startup, clearing
every copied website's native GA key and disabling
the indexing parameter on every nonproduction data workflow, including a
production restore with `--no-sanitize`. Previews never receive the production
settings payload. This exclusion is independent of optional sanitization. Indexing exclusion is
the CM addon contract; other tenants' general indexing policy and arbitrary
custom-script trackers are outside this native-GA4 path.

Maintain the generated maintenance program from one committed source with
`uv run python scripts/generate_odoo_website_settings_program.py --devkit-repo
<checkout> --revision <commit>`. It reads the Git blob at that immutable commit,
not the working tree. Add `--check` to verify exact agreement with the generated module. Commit the generated module with its source revision;
include consumer changes, native-shell/transport tests and review in the same
delivery. Update `tests/fixtures/website-settings/consumer.txt` and its immutable
Git-blob metadata in `artifacts.json` with the generated module. Values still
come only from the accepted runtime payload. The runtime must provide the native
startup shell/config functions covered by the legacy fixture; qualify the actual
testing and retained rollback images before live activation.

Native Odoo consent behavior remains Essential only / Accept all. These fields
are an application contract, not proof of Client consent acceptance, personal-
data exclusion from events, provider event delivery or real visitors. The
product's analytics issue owns those checks and the real Measurement ID.

Startup preflight checks each observed Compose service's literal image against
the slot's immutable image and requires the authoritative `.env` last in its
`env_file` list, without an explicit payload override. An interpolated/mutable
image or a later env-file override refuses before writes; install the admitted
immutable definition before qualification. Recreation always reads the new
container identities, even after a lost reply. An unavailable identity readback
keeps the release held and its observed Compose coordinates available for retry;
it never treats removed container IDs as a successful receipt.
