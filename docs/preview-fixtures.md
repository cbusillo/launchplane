# Preview data fixtures

A product ships its fixture definitions and Odoo business logic in its own
artifact. Launchplane's existing `POST /v1/drivers/odoo/app-maintenance`
operation accepts `maintenance.action: preview-fixture` and
`maintenance.intent: preview-fixture`. Stable lanes, including testing and
production, are refused. This does not install an addon or accept caller code,
SQL, credentials, commands, paths or business values.

The request names the product, context, instance, preview ID, serving generation
ID and fixture name, with a concrete reason. Resolve these from current service
records. The service verifies the product's active Odoo preview profile, serving
generation, immutable artifact/runtime identity and actual running container.
It reuses the preview's target ownership, disposable-resource protections and
execution lock. A protected use, only copy or known production resource sharing
refuses the operation. Source changes use the product's ordinary build and
preview delivery; an old image cannot run a fixture it does not contain.

`apply` uses the existing `odoo_preview_apply.execute` permission on that
product's preview context. `dry-run` and `readback` use `operations.read` on
the selected instance. No grant or credential is created. The existing
maintenance/post-deploy action remains unchanged for stable lanes.

## Product contract

The image ships `/opt/project/addons/launchplane_preview_fixture.py`. The existing
host package runs this fixed entrypoint in the exact checked web container. Its
stdin is a JSON object containing the service-selected `identity`, `database`,
`fixture` and `mode`. The product checks the native Odoo database host, preview
instance, database name and runtime identity before opening a database. It must
refuse stable/testing/prod instances itself as well. No request may choose a
connection or bypass native configuration.

The product owns its named fixture and a deterministic `fixture_sha256` of that
definition. Hold the existing native data-workflow lock through execution,
commit and fresh-cursor readback. Use read-only database transactions for
`dry-run` and `readback`. Apply is transactional: search its stable fixture
identifiers first, reuse only exact matching records, and refuse conflicting or
partially edited records without overwriting them. Persist the created/reused
record identities with the same commit. A repeat, including a different request
key or an earlier matching UI-created sample, must reuse the matching result.

Return exactly one JSON object on stdout, with no logging or exception prose:
`identity`, `fixture`, `fixture_sha256`, `mode`, `committed`, `verified` and
`records`. `records` contains only `{model, id, disposition}` entries;
disposition is `created` or `reused`. Include all written roots, derived records
and fixture-journal records relevant to the fixture. The same original record
dispositions must be available through readback after a lost response.
`committed` is true only for apply; `verified` must be true. Apply and readback
require nonempty, unique record references. No customer, supplier, price,
receipt reference, copied business values or credential appears in this receipt.

The host drains stderr without retaining it, bounds stdout to 32 KiB and
observation to three minutes, validates the receipt's shape before emitting a
hex result into provider evidence, and never prints arbitrary product output.
The service validates the full typed receipt and all supplied identities again.
A timeout stops the observer; it does not prove the in-container writer stopped.

## Review, apply and recover

Use the repository's `scripts/preview-fixture.py` client with the installed
catalog operator transport. Payload and evidence files stay private, outside the
repository. The payload is the maintenance envelope with the fields above;
omit `reviewed_plan_sha256` and use `mode: dry-run` initially.

```sh
uv run python scripts/preview-fixture.py dry-run \
  --operator-helper /installed/launchplane/scripts/launchplane-write-action.py \
  --payload-file /private/fixture.json --evidence-file /private/review.json
```

The dry run writes only its provider execution evidence and saves the review
privately. Inspect the selected identity, fixture digest and record references.
Apply with the same arguments, `apply`, `--reviewed-dry-run`, and a stable
`--idempotency-key`. The service repeats the inert preflight and compares its
digest with the review before dispatching a write. No product fixture can be
substituted after review without changing the digest.

The existing durable provider runner records the actor, original request,
reason, target, effect checkpoint and bounded receipt. Repeating the identical
apply/key replays a completed receipt. An uncertain outcome retains its original
reservation and blocks another target mutation. Repeat that original request
and key to reconcile through product readback; no write is dispatched by
reconciliation. A readback that cannot prove the full original fixture keeps the
operation unknown. Do not replace the key to get around it.

The separate `readback` client mode invokes the same fixed product read-only
path against the exact generation. It verifies current records rather than
replaying an old response. Product-specific plain names and the generated
receipt reference are checked separately through the product's authorized read
surface and kept out of public control-plane receipts. Coordinate with the
preview's operator before an append while someone is testing it.
