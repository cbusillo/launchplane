# Preview Fishbowl parts import

`POST /v1/drivers/odoo/app-maintenance` accepts fixed
`maintenance.action: preview-parts-import` and matching `intent`. It invokes only
the installed `fishbowl.importer.run_full_import()`. This is the product's public
parts-only entrypoint: its native importer delegates with `parts_only=True` as
its default. The callable must accept that fixed no-argument invocation. There is no
private-method invocation, addon installation or recurring importer.

The envelope names `product`; `maintenance` contains `action`, `intent`, `context`,
native `instance`, `preview_id`, `generation_id`, `reason` and `mode` (`dry-run` or
`apply`). Apply also requires `reviewed_plan_sha256` and the original
`Idempotency-Key`. No source, connection, SQL, code, method, fixture, count,
timeout or credential input is accepted.

The service selects the exact serving preview and its completed preparation
original through runtime identity. That preparation must have explicitly selected
`fishbowl_source`. Its proven artifact, source commit, native target, generation,
current managed source version and existing read-only allowance must still agree.
No running stable testing lane is required. Preparation is continuity; import
creates its own durable original and never replays preparation.

The action shares preview maintenance's existing authority, execution mutex,
resource ownership and disposable/in-use/only-copy/production isolation checks.
Apply binds the exact container and database. Readback may use a replacement web
container only within the unchanged identity, generation, target and database;
apply and cancellation retain the original container binding. Selection and managed-source
agreement are rechecked before dispatch and before accepting the receipt. No
grant, account, secret, hold or authorization mode is created.

Inside that same preview, a read-only Odoo transaction verifies installed addon
configuration against the selected managed-source fingerprint. It calls the
installed client's configured connection with fixed `SHOW GRANTS`, returning only
`read_only`, `unsafe` or `unknown`. Only proved read-only grants admit import.
Raw grants and connection values never enter the receipt. Existing managed
private-source TLS behavior is preserved; no new TLS choice is made.

When runtime selection cannot establish an available container, the existing
`preview_import_unavailable` response includes `reason: runtime_container_unverified`.
The maintained transport retains it as `summary.error_reason`. This includes
unavailable container inventory, a missing selected container ID, or inspection
that does not return configuration and state objects with `Running: true`.
It does not distinguish a stopped container from unavailable inspection evidence.
No raw provider detail or exception text is returned. A refused dry run issues
no review digest and requires no invented apply key.

Checks after the failed prerequisite remain unavailable, not passed: container
labels/image/runtime identity, exact database/ownership binding, preparation and
current managed-source/password agreement, read-only allowance and native grants.
Independent retained preparation and request-binding evidence can be assessed
without replaying the plan, but cannot qualify current runtime or source facts.
Apply still needs its separate current disposable/use/only-copy/isolation checks.

Dry run performs preflight without importing. Review its identity, source digest
and qualification. Apply repeats preflight, obtains the native data-workflow lock
and calls the fixed public entrypoint. A supervisor **inside the container**
bounds the native Odoo subprocess to 600 seconds from before spawn, kills its process group and
waits for termination before releasing the native lock. The host observer has a
separate deadline. Losing or stopping that observer does not prove the writer
stopped, rolled back or never committed.

Successful import writes a bounded original-operation witness in the preview's
Odoo database. The importer may commit during its run: timeout, exception, missing
witness or empty readback therefore remains unknown, never absent or retry-safe.
A validated pre-import refusal at configuration, entrypoint or grants proves no
import effect and completes without retaining an uncertain-writer fence.
The original reservation fences refresh/idle and another import. Existing guarded
destruction may discard isolated resources only with the same custody checks and
independently verified original-container cancellation.

## Maintained helper and readback

Use `scripts/preview-fixture.py` with the installed catalog transport. Its
request/review transport is shared with preview fixtures; the discriminator
selects the real import action.

```sh
uv run python scripts/preview-fixture.py dry-run \
  --operator-helper /installed/launchplane/scripts/launchplane-write-action.py \
  --payload-file /private/import.json --evidence-file /private/import-review.json
```

Apply uses the same arguments, mode `apply`, `--reviewed-dry-run` and a stable
`--idempotency-key`. Keep payload and review files private and outside the repo.
After response loss, repeat that **same apply request, saved review and key**.
The durable original replays its completed result or reads its historical witness
without invoking the importer again. There is no public standalone `readback`
mode for imports and no new-key recovery. The helper exits nonzero for unavailable
qualification or uncommitted apply while printing its bounded receipt. Before a recorded trigger, the original
may retry preflight; after a trigger, missing commit evidence retains the fence.

The receipt elapsed time measures the script through its represented stage; the
subprocess deadline also includes Odoo startup.

Evidence includes exact runtime/artifact/generation identity, source digest,
opaque operation identity, grants class, closed stage/outcome, elapsed seconds,
the 600-second limit, binding-current and committed flags, and bounded aggregate
count fields. This importer has no typed measurement return: parts,
supplier-cost rows, copied-field totals by type, conflicts and unmapped rows stay
`null` (unavailable), never assumed zero. Source-delivery tests earn none of the
real import, counts or familiar-part/multiple-listing QA. Root's execution and
product QA earn that proof.
