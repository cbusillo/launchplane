---
title: Legacy Preview Reconciliation
---

# Legacy preview reconciliation

`POST /v1/previews/legacy-generic-web/reconciliation` inspects and reconciles
one legacy generic-web preview whose provider application is already absent.
It takes an exact `product`, `preview_id`, `reason`, and `mode` (`inspect`,
`plan`, or `apply`). It never deletes provider resources or retires a product.
Provider-present previews require the existing authorized teardown path first.

## Review and execution

`inspect` reads the profile, exact preview and historical generations, tracked
target authority, and complete bounded provider inventory. It persists no scan,
plan, preview or provider change. The response reports provider presence or
absence, terminal preview evidence, and whether reconciliation is eligible.

`plan` saves its own evidence under the caller's `Idempotency-Key`, without
changing preview or provider state. The plan digest binds the caller, exact
product and preview, reason, current profile/record/generation/target authority,
and provider observation. Historical generations are retained, including
missing deployment references; those missing records never establish absence.

Review binds this preview's authority and provider classification. Changes to
unrelated products or provider applications do not invalidate an otherwise
unchanged, absent-preview plan. Complete inventory is inspected again at apply.
Sibling target/ownership changes in the same preview context invalidate review
and require a fresh plan.

`apply` requires a different `Idempotency-Key`, `plan_idempotency_key`,
`expected_plan_digest`, and `reviewed_plan: true`. It rechecks the saved plan's
caller binding, current authority and provider absence. It rejects any drift
or blocked plan. The database commit compares authority again under writer
locks, then atomically marks just that preview destroyed and saves the replay
receipt. Active/serving generation pointers are cleared; historical generations
and the latest-generation pointer remain. The product profile, lanes, provider
targets, runtime values and secrets are unchanged.

Repeat the same plan or apply intent with the same key after an uncertain response;
never substitute a key, caller or payload. A successful replay proves the
recorded result, not current provider state. After apply, independently run
`inspect` again and require both `provider_absence_verified: true` and
`preview_state: destroyed` with a destruction timestamp.
Large inventories can outlast the helper's HTTP timeout. Allow the original
scan to finish before repeating the same key; a retry during that scan waits
on the same preview lock. Repeat the identical intent and key after it finishes.

## Evidence and authority

Provider absence uses bounded, complete application search, per-application
identity/configuration and domain reads, and a second inventory enumeration.
Exact application names, generated app names, preview domains and historical
provider IDs identify candidates. Renamed/unbound applications matching the
product's repository, image or naming policy block reconciliation. Targets
uniquely tracked to another live preview or stable lane are inspected and
excluded unless this preview's exact identity or domain matches. Missing,
malformed, truncated, duplicate, changing or uncertain inventory is not absence.
Shared contexts, ambiguous anchors, running generations/reconciliation, missing
generation pointers, and existing preview target authority fail closed.

Inspection and completion run off the HTTP event loop. Generic-web provider
refresh, teardown and reconciliation share the database's preview serialization
lock; HTTP cancellation preserves any atomic completion and its replay receipt.
Generic-web lock waits use separate unpooled database connections, preserving
existing waiting behavior and keeping the shared record pool available. They
use the same database/bootstrap identity and timeouts. Each active/waiting
operation still consumes a database connection; deployments must qualify
database capacity and connection lifetime for their workload.

Inspection and planning use existing `preview_inventory.read` authorization
for the recorded product/preview context. Apply additionally requires existing
`preview_destroy.execute` at preview scope and `preview_destroyed.write` for
the same context. Every call, including replay, checks current authorization.
Testing-lane `product_retirement` authority does not authorize preview writes.
This feature creates no grant, new caller identity or policy exception.

## Bounded service helper

The producer's public agent/admin contract projects the operation. The bounded
client resolves its path from that artifact rather than maintaining another
route map. Configure the existing `LAUNCHPLANE_OPERATOR_URL` and
`LAUNCHPLANE_LOCAL_OPERATOR_TOKEN` privately; do not pass tokens as arguments.
Input JSON and saved plan output stay in a private task evidence directory.

```bash
uv run python -m control_plane.legacy_preview_client \
  --payload-file /private/task/inspect.json
uv run python -m control_plane.legacy_preview_client \
  --payload-file /private/task/plan.json --idempotency-key review-preview
uv run python -m control_plane.legacy_preview_client \
  --payload-file /private/task/apply.json --idempotency-key apply-preview \
  --reviewed-plan-file /private/task/saved-plan.json
```

The plan payload names `mode: plan`; save its JSON helper response before
review. The apply payload names `mode: apply` and otherwise repeats the exact
plan intent. The helper supplies review fields only from that matching,
successful, eligible saved plan. It accepts HTTPS service origins, refuses
redirects, emits bounded protocol metadata, and never retries an uncertain write.
Source delivery and fixture tests do not establish deployed capability,
provider absence, caller authority or live retirement acceptance.
