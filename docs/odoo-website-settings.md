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
resolved HTTPS base URL: the first public hostname on its tracked target takes
precedence, using the same public-host resolver as stable deployment. Without
public hostnames, the recorded product-profile base URL remains the fallback.
The profile's internal origin and health URLs need no rewrite to prepare public
settings. Preparation rechecks this resolution on apply; it changes no target,
profile or live website setting. Release review rechecks it too: if the
resolved origin changes after preparation, the release is blocked before any
backup or deploy until the settings are prepared again for the new origin and
the Client accepts the updated release. Accepted intent is never rewritten.
Settings contain the native `website_id`,
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
or live activation. The supported agent route uses the installed
`launchplane-write-action.py` helper commands `odoo-website-settings-dry-run`
and `odoo-website-settings-apply` through the service API.

To withdraw pending intent, send `website_settings: null` through the same
dry-run/reviewed-apply path. This preserves the applied/attempted snapshot and
changes no Odoo state. Subsequent release checklists explicitly inherit verified
applied values for the new candidate, so withdrawal cannot hide an active setting.
To switch collection off, prepare explicit disabled settings for acceptance.
Withdrawal remains available without a testing candidate
and on a two-slot lane; preparing new settings on that unsupported driver is refused.

When the qualified release checklist needs only the Client's acceptance, changed
prepared settings automatically open the existing Client release request. No
Director click is needed, including settings-only releases on the same build and
engineering releases carrying prepared settings. The intent must match the current
artifact, its settings digest must differ from verified production settings, and
the checklist's stale-intent, readback and other blockers still apply.
The request directs the Client to the prepared indexing, analytics and cookie
consent choices on the release page; those settings are not active on testing.
Settings content uses the invitation's existing delivery receipts, so a new
settings choice notifies once and the same choices on a later build do not ping
again. Missing or withdrawn intent supplies no new trigger. Pure engineering-only
releases keep their [existing invitation rules](release-review.md).
Publishing a request records no acceptance and starts no production action.

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
applied snapshot separately from pending intent. A verified collection-only
rollback receipt remains recovery evidence, rather than a configured website
snapshot in release review. The historical baseline therefore retains its
accepted identity through the drill, allowing the second backup and re-promotion
under the same acceptance. Re-promotion applies the accepted candidate settings.
An accepted rollback restores that baseline even when it had no configured
settings and the artifact is unchanged. Manual same-artifact recovery continues
to reuse its verified applied snapshot.
For configured website snapshots, subsequent review compares
against production's applied settings. Failed maintenance retains its exact
attempted snapshot as unverified, outside the production checklist; the existing
maintenance or durable operation retry must verify it before a new release review.

The stopped-web stable-target replacement driver supports native application.
For two-slot lanes, use the native maintenance, settings, writer and startup
contract in [Odoo two-slot releases](odoo-two-slot-releases.md#typed-website-settings).
A rollback factory refusal records a failed rollback before provider effects;
unavailable installed-host imports or construction expose no package details.
Never change a live product's driver to bypass a capability refusal.

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

The canonical consumer supports legacy and typed string-parameter APIs. Copied
websites remain excluded while their addon is scheduled for upgrade or removal;
Odoo's base-only website model does not enable website settings. The generated
program and immutable source fixture must advance together.

Odoo 20 defaults its HTTP listener to loopback. The runtime environment record
must supply `ODOO_HTTP_INTERFACE` for the container listener that the private
proxy reaches. Verify readiness from that private proxy, not only from inside
the web container. This is a runtime input, not an artifact build setting.

Native Odoo consent behavior remains Essential only / Accept all. These fields
are an application contract, not proof of Client consent acceptance, personal-
data exclusion from events, provider event delivery or real visitors. The
product's analytics issue owns those checks and the real Measurement ID.

Interrupted recreation and inventory readback follow the
[startup recovery contract](odoo-two-slot-releases.md#interrupted-startup-recovery).

## Prelaunch crawler protection

`production_use` on the product profile is the authority for native crawler
protection. The generated consumer comes from the committed devkit receiver.
Its optional `website_indexing` sibling contains exactly `website_id`,
`public_origin` and `production_use` (`prelaunch` or `live`), on `instance: prod`.
Combined website settings must select the same website and origin. The receiver
adds or removes only its managed noindex element in native `custom_code_head`,
preserving other head content, robots rules and the canonical domain. A tag
proves neither search-result nor sitemap removal; crawlers must be able to fetch it.

For configuration without maintenance, use the authenticated service route
`POST /v1/drivers/odoo/post-deploy`:

```json
{
  "product": "example-odoo",
  "post_deploy": {
    "context": "example",
    "instance": "prod",
    "website_indexing": {
      "mode": "dry-run",
      "public_origin": "https://example.test"
    }
  }
}
```

The service derives the state. Optional `website_id` binds a known native site;
otherwise exactly one website must match the recorded public origin. The plan
includes the resolved selector, state, before/after SHA-256, managed-element
state and current binding digest. Review it, then send the result unchanged as
`website_indexing.reviewed_plan`, set `mode: apply`, supply `post_deploy.reason`
and retain one original `Idempotency-Key`. `mode: readback` obtains a fresh
native plan without writing. Plans/readback require `product_config.plan`;
apply requires `product_config.apply`. Terminal-agent identities cannot use it.
The producer advertises the service API; installing a catalog command is a
separate consumer change. Do not substitute direct database or provider calls.

This branch executes only the generated indexing receiver under the existing
Odoo lane reservation, host execution lock and durable provider-operation fence.
It runs no module updates, broad maintenance, restore or deployment. Missing,
ambiguous, stale or mismatched product, route, target, runtime, database UUID,
website or reviewed head evidence refuses the effect. A separate route digest
does not waive topology's `stale_route_authority` warning. Apply locks the
native website row, checks the reviewed baseline, reads back in the transaction,
commits and completes native cache notification, then returns its receipt.
Odoo 20 includes notification in its transaction commit; Odoo 19's caller signals
its invalidated caches afterward so running HTTP workers observe the change.
A lost receipt is unknown, not success.
Reconciliation checks stable target identity and fresh route authority separately.
The exact native after-image proves completion; the unchanged reviewed before-image
permits the original operation to retry. Any other image remains unknown.
Preserve the original request and key for durable reconciliation; never create
a replacement key to bypass uncertain effects.

An accepted first-live Odoo release keeps prelaunch protection through forward,
failed and rollback maintenance. After every required release step passes on
the exact accepted candidate, its configuration continuation removes the managed
element and atomically records its one-time completion with the compare-and-write
of `production_use: live`. It rechecks the native after-image first, and a completed
decision never overwrites a later classification. Unrelated profile edits retry
the compare-and-write against the current record. If that state write loses
authority, it restores protection only against its reviewed native after-image
while the product is still prelaunch. Its inverse uses durable operation records
and leaves the release stopped; uncertainty requires reconciliation of the
original operation. It cannot turn an inverse into a later forward replay.
Release maintenance renders either recorded state through the same native payload,
including when rollback restores a prelaunch database to a Live product. Current
route and runtime proof remains required; a stale route cannot authorize the write.
Release holds, Client binding, signed-in acceptance, backup/drill gates and deploy
drain remain in force. Applying prelaunch protection does not lift a release hold.

Target delivery requires a qualified compatible receiver/runtime, current route
evidence and authorized reviewed plan/apply/readback for each exact product and
hostname. Source deployment alone activates no target setting.
