---
title: Legacy Preview Reconciliation
---

# Legacy preview reconciliation

`POST /v1/previews/legacy-generic-web/reconciliation` inspects and reconciles
one legacy generic-web or Odoo preview whose provider resources are already absent.
The existing path and operation ID remain compatible; driver selection comes from
the recorded product profile.
It takes an exact `product`, `preview_id`, `reason`, and `mode` (`inspect`,
`plan`, or `apply`). It never deletes provider resources or retires a product.
Provider-present standalone reconciliations require the existing authorized teardown path first.
For retiring a product, use the [single reviewed product-retirement operation](product-retirement.md),
which includes its own generic-web previews without separate preview-cleanup approvals.
Standalone Odoo reconciliation does not extend integrated product retirement.

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
Historical runtime identities may identify the preview by its record ID or by
the slug written by the generic-web runtime producer. Either form still has to
match this preview's product, context and resolved instance; another preview's
identity blocks reconciliation.

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
Shared contexts, ambiguous anchors, running product reconciliation, missing
generation pointers, and existing preview target authority fail closed.

For Odoo, the service enumerates every project's environments and composes, reads
joined compose details and all live domains, and enumerates again. It also checks
applications for preview identity or domain residue. Physical IDs include their
provider category: an application ID cannot exclude a compose with the same ID.
A legacy target ID needs its matching typed target configuration before it can
exclude an owned sibling or stable target. Unknown renamed raw composes require
safe image-source classification. YAML and environment values are inspected only
in memory; returned evidence contains identities and classifications, never raw
compose source or environment values. Unresolved image expressions, malformed
joins or uncertain source cannot prove absence. Historical compose names that
disagree with the current profile's name remain uncertain and refuse.

Odoo records must use the canonical repository basename and preview ID produced
by the existing Odoo lifecycle. A different legacy identity needs investigation
before reconciliation can establish a safe ordering barrier.

Odoo inspection holds the actual physical executor lock, then the stable logical
preview lock, then the existing preview serialization lock through completion.
The logical key uses the preview context, repository and PR, so a service-issued
plan with a custom physical slug shares the same fence. Original PostgreSQL lock
transactions must remain alive. Durable running or uncertain reservations also
block reconciliation, including legacy raw target keys. Settle or tear down the
original operation through its supported recovery path before inspecting again;
never delete reservations or substitute provider absence for their settlement.

A legacy Odoo generation with no runtime identity may retain interrupted
nonterminal state, including current pointers, when those executor fences exclude
current work and complete absence is established. Its history is retained unchanged.
The atomic Odoo completion stores an exact receipt pointer on the destroyed preview.
Old issued Odoo plans are rejected before provider effects under the same fences.
The same pre-provider check protects existing active histories: a missing or newer
ordering record cannot be bypassed by executing first and publishing evidence later.
Bare preview evidence cannot erase that terminal proof. Generation evidence can
resume the preview only with a fresh generation requested strictly after completion;
changing an old generation's timestamps does not qualify. Stale evidence returns
`409 preview_retirement_conflict` without partially writing generation or preview.
A genuine later service-issued Odoo destroy also retains its normal lifecycle flow.
Unproven older destroyed records keep their existing fail-closed ordering behavior.

For generic-web, an unreferenced historical generation can retain an interrupted nonterminal
state. It no longer blocks inspection when all current generation pointers refer
to terminal evidence and the service holds the preview's actual exclusive
PostgreSQL executor lock (or retirement's exclusive product lock). The original
lock transaction is checked, and the historical record is retained unchanged.
This proves exclusion of current service execution, never provider absence,
unused lanes or disposable eligibility. Provider observation and all existing
ownership, authorization and reviewed-plan checks still apply. SQLite rehearsals
cannot supply this execution proof. The service never substitutes generation
age, a newer sequence, an assertion or another thread's lock for that proof.
Preview verification and the reconciler's subsequent build-evidence write share
the product/preview execution locks and recheck the active profile after waiting.
Verification cannot reopen a destroyed preview. HTTP verification waits run off
the event loop and retain completion/replay evidence after caller cancellation.

Inspection and completion run off the HTTP event loop. Generic-web provider
refresh, teardown and reconciliation share the database's preview serialization
lock; HTTP cancellation preserves any atomic completion and its replay receipt.
Generic-web lock waits use separate unpooled database connections, preserving
existing waiting behavior and keeping the shared record pool available. They
use the same database/bootstrap identity and timeouts. Preview mutations hold a shared product lock plus their exclusive preview
lock on dedicated connections; retirement alone takes the exclusive product
lock. Read-only inspection/planning keeps the per-preview lock, so an unrelated
preview cannot block it. Each held or waiting lock consumes a database connection; deployments must qualify
database/thread capacity and connection lifetime for their workload.
Lifecycle cleanup/sweep completion also runs off the event loop and includes
its idempotency receipt; sweep uses the product-profile scope checked before
dispatch so later profile additions cannot expand the caller's operation.
Concurrent retries of a running lifecycle or direct-destroy intent return `409 mutation_in_progress`
within the same service process. Wait for completion, then repeat the identical
intent and key to recover its receipt. This guard remains held after HTTP
cancellation. Cleanup carries the checked profile into teardown; a saved plan
whose product/context no longer matches is blocked and needs a fresh plan
within the caller's existing authority.
Cleanup also preserves the existing requirement that previews be enabled.

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
