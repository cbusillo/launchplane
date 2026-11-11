# Current Odoo inventory readback

`GET /v1/products/{product}/environments/testing/odoo-inventory-readback`
extends the existing Odoo runtime read boundary under the product's existing
`operations.read` instance scope. It reads current stored database facts, including
inactive parts and bindings. It neither opens the frozen archive used by
[aggregate-counts](copy-aggregates.md) nor invokes an importer, Odoo registry,
model method, sign-in, external source or mutation. No tenant rebuild is needed.

The request requires `context`, `deployment_record_id`, `source_git_ref` (full
commit), `artifact_id`, `image_digest`, `target_id`, positive `company_id`, one
to ten distinct numeric Fishbowl `part_ids`, and
`purchaser_xmlid=cm_custom.user_gunneet`. Select these from the retained original's
native binding and private witnesses. They are expectations checked against the
current selected testing runtime, never permission to select a different target.
There are no table, field, SQL, domain, URL, method or program selectors.

The typed response is `OdooInventoryReadbackResponse` in the generated OpenAPI:
`trace_id` and `InventoryReadResult`. A verified result contains the runtime
identity, Compose target, exact container, observation time, selected company,
stored facts, ordered part witnesses and the purchaser witness. It always says
`applied=false` and `scope=current_database_stored_facts`. Missing schema fields
are null; a missing selected record differs from unavailable schema. A failed
read returns only `runtime=unknown` and a fixed `failure_stage`, with no private
observations or raw exception text. The response is `Cache-Control: no-store`.

The product continues to own import meanings. The fixed read follows the
existing [parts-only importer](https://github.com/cbusillo/odoo-tenant-cm/blob/main/addons/fishbowl_import/models/fishbowl_import_products.py)
and `external.id` bindings: Fishbowl `part` binds `product.template`, and
`vendorparts` binds `product.supplierinfo`. It reports existing distinct bound
parts and supplier-cost rows, stored listing bindings and nonzero current sales
prices. Those current fields cannot establish where a later manual price came
from. Supplier rows, stored costs and stock quantities use the requested company.
Part population and tracking counts cover all currently bound templates.

Current populated copied-field counts and stored `cm.product.import.conflict`
rows are separate from historical import counters. The importer logs copied-field,
listing-conflict and unmapped totals; it does not persist those counters.
`historical_fields_copied`, `historical_listing_conflicts`,
`historical_unmapped_lines` and `historical_tracking_results` remain null.
Current lot/serial counts describe current products, not past move completion.

Stock facts include existing move counts, done-move counts, and the sum of
internal-location quants for bound parts in that company. The quantity total
combines native units and is only a stored-data comparison aid. Each selected
part also includes its own internal quantity. The reader does not replay history,
reconcile values, choose a financial tolerance or assert zero mismatches. Compare
these facts with retained proof and retain any coverage limitation.

Each part witness exposes only its ID, template ID, English name, part number,
barcode, active flag, stored company cost, current sales price, tracking,
internal quantity and at most ten imported supplier costs. Supplier fields are
supplier ID/name, supplier part number, cost, currency and unit; an explicit
flag reports truncation. A missing or ambiguous variant leaves its cost and
barcode unavailable. The purchaser exposes only the declared XMLID, module state,
user ID, active flag, login/name, stored group XMLIDs and count of groups lacking
XMLIDs. It never reads a password or invitation and reports first sign-in as
`not_observed`. Stored membership does not prove a person's workflow succeeded.

The fixed program uses the existing host inspector and private transport. It
verifies container identity and image, the same-project database peer and internal
network, pins the database address, opens a repeatable-read read-only transaction,
and rolls it back on success or failure. It checks configuration, container,
database peer and selected runtime again before returning observations. No old
operation journal, import log or no-effects certificate is a new prerequisite.

Names, costs, supplier information and purchaser identity are private. Keep the
full response in the operator's private evidence. Public handoffs contain only
safe summaries and binding/observation status. The maintained caller must preserve
unknowns and declared fields and reuse the original native binding transport;
source publication alone does not install that caller or authorize a real read.
