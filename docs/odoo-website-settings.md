---
title: Odoo website settings through an accepted release
---

`POST /v1/product-config/odoo-website-settings/apply` prepares typed intent on
the product's production instance-override record. It uses existing
`product_config.plan` / `product_config.apply` authority and exact lane
ownership. Preparation changes no Odoo database, deployment or access grant.

The request names the product/context, `instance: prod`, a reason, and
`website_settings: {artifact_id, settings}`. The artifact must be the product's
current recorded testing artifact. Settings contain the native `website_id`,
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

The Client release checklist shows these exact settings and includes their
digest in the candidate identity. Changing a setting invalidates that release's
acceptance even when the artifact is unchanged. Production application selects
the saved accepted decision's snapshot for the artifact being deployed; newer
prepared settings cannot alter an accepted release. The native consumer checks
that the selected website's domain equals the reviewed public origin, writes
and reads back native analytics/consent fields and
`cm_website.public_indexing_enabled` in its transaction. Launchplane requires
the matching readback digest. An older consumer cannot claim successful apply.

Rollback selects the previous artifact's saved accepted settings. Historical
artifacts without a website snapshot disable indexing and analytics; they do
not retain forward-release settings. Successful readback retains the actually
applied snapshot separately from pending intent, so subsequent review compares
against production's applied settings.

The devkit consumer clears every copied website's native GA key and disables
the indexing parameter on every nonproduction data workflow, including a
production restore with `--no-sanitize`. Previews never receive the production
settings payload. This exclusion is independent of optional sanitization.

Native Odoo consent behavior remains Essential only / Accept all. These fields
are an application contract, not proof of Client consent acceptance, personal-
data exclusion from events, provider event delivery or real visitors. The
product's analytics issue owns those checks and the real Measurement ID.
