---
title: Prepared public serving during Odoo database updates
---

`prepared_public_site`, `prepared_public_pause` and the typed
`contracts/prepared_public_site` contract supply the public-serving foundation
for the one-phase release design in #3237 and #3262. The two-slot provider
orchestrator (#3266) owns production activation, authoritative route switching,
private readiness, backup/update ordering and recovery. This capability does not
change the current production deployment path or grant access.

## Preparation

A Launchplane-owned `PublicSitePlan` binds product, environment, release,
serving artifact/image and database to explicit public routes, retained assets,
personal/editor exclusions and all authoritative writer targets. These are
runtime input, never a checked-in tenant catalog. The inventory must include
routes absent from sitemap, redirects and linked price pages. Anonymous public
capture must be attested against the exact bound serving runtime; the capture
adapter must use no Cookie or Authorization and must exclude personalized
responses. Warming/capturing may itself write in Odoo and occurs before the
pause, under the existing release authority.

`prepare_public_site` renders passive HTML, recursively captures local
HTML/CSS/media references and retains script files and explicitly named old
assets as inert bytes. Any missing page/asset, unconfigured linked route,
redirect cycle, identity mismatch, private/session-varying response, unsupported
markup/content type or resource budget failure aborts preparation before a writer
pause can begin. A homepage 200 never establishes coverage.

The supported document grammar is explicit balanced HTML with local styles,
fonts, raster images and audio/video. CSS escapes, comments and imports and
SVG assets currently fail preparation; the adapter must not hide that failure
or silently omit those references. Dynamic page content must already be rendered
in the capture. A site's browser coverage proof must confirm navigation and
content work without its scripts before marking preparation ready. A plan for
an unsupported site fails rather than claiming browsing continuity.

All executable site scripts, inline handlers, session/CSRF-bearing data
attributes, hidden inputs and forms are removed. The page contains a visible
notice and disabled submission control. Script execution, connections, frames,
objects and form actions are denied by the serving CSP. JS forms and live metrics
therefore cannot produce requests. Only safe response headers are retained;
Set-Cookie, CSRF/session headers and upstream cache/security policies are never
stored. Prepared serving rejects every non-GET/HEAD method, cookies,
authorization and excluded paths with 423. Unknown paths/queries get 404 and
never fall back to Odoo. Public HTML/assets and redirects are served from the
immutable bundle, with no capture/provider/database handle in the serving app.

The bundle includes a digest over both content and its plan/binding. It must be
verified on load and before serving or pausing. Keep this copy retained until
all visitors referencing its assets have drained; new Odoo asset GC cannot
remove its independent bytes. Do not mount the authoritative writable filestore
into prepared serving.

## Writer pause and recovery

`PreparedPublicPause` accepts a record-store interface and a provider capability.
Persist `fencing` and `began_at` before publishing prepared serving or blocking
forms. Publication must read back the bundle digest; drain independently reads
current serving authority again. The provider fences and drains every configured
web, cron, queue, mail, integration and asset-GC target. Every observation must
match the release and pause, report a fence, zero active jobs and a durable
provider evidence ID. Missing, duplicate, stale or in-flight writer evidence
prevents the `drained` state and therefore prevents maintenance admission.

A failure leaves the pause open and writers visibly paused; it cannot invent an
end time. Recovery persists `resuming` and its exact expected recovered artifact
binding before the provider resumes. The provider must privately verify the
recovered authoritative runtime, switch/read back serving, and resume exactly
one owner per configured target using an idempotent pause operation. Binding,
pause ID, serving evidence and writer ownership read back before completion.
An uncertain retry cannot change the recovery target. A controller restart
loads the same record; `finish` retries the recorded target idempotently and a
completed finish does not resume twice. Candidate artifact identity may differ
from the prepared old artifact; product/environment/release/database remain
bound. The two-slot orchestrator must never call recovery before required
backup, update and readiness evidence passes.

The record exposes begin, end, full elapsed duration, `prepared-public` served
mode and `reduced_service=true`. The interval includes public-write blocking,
fence/drain, backup coordination, maintenance, readiness, switch and writer
resumption. Zero 5xx means readers stayed served, not full application service.
Use these typed records in #3266's DB-backed release integration. The provided
`FilePublicPauseStore` and bundle JSON utilities are for an explicit isolated
state directory/rehearsal only; shared production truth remains DB-backed.
Provider adapters must enforce exclusive/idempotent operations on a release;
these file utilities are not a distributed lock or production record store.

## Isolated proof

`uv run --extra dev python -m unittest tests.test_prepared_public_site` uses a
small database-backed website with public/hidden price routes, redirects,
forms, metrics JS and CSS/media references. It migrates the authority database,
deletes an old asset, then removes the database entirely while all copied
routes/assets still serve; writer attempts remain fenced. It also covers
failed preparation/publication/drain, personal-content exclusion, restart,
uncertain recovery, duplicate ownership and complete pause timing.

For independent browser/external HTTP probes, run the task-owned localhost
fixture (its database is destroyed before serving):

```bash
uv run --extra dev uvicorn tests.support.prepared_public_fixture:browser_app --factory --host 127.0.0.1 --port 8768
```

Observe the notice, click price/contact/redirect navigation, inspect blocked
forms, fetch assets and probe direct POST/personal paths. This is representative
isolated source proof, not an Odoo production or real-release acceptance run;
the latter remains on #3268.
