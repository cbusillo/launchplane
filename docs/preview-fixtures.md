# Preview data fixtures

A product ships its fixture definitions and Odoo business logic in its own
artifact. The existing `POST /v1/drivers/odoo/app-maintenance` accepts
`maintenance.action: preview-fixture` and `maintenance.intent: preview-fixture`.
Testing and production are refused. Callers cannot supply code, SQL,
credentials, commands, paths or business values.

The request names the product, context, instance, preview ID, serving generation,
fixture and reason. Launchplane verifies the active Odoo preview profile,
artifact/runtime identity, exact container and exclusive resource ownership.
Mutation retains the existing disposable-lane, in-use, only-copy and production
resource-sharing protections. Read-only modes do not require refresh eligibility.
The fixture shares the preview's execution mutex and durable lifecycle
reservation: an uncertain writer also fences refresh and idle operations.

The authenticated Director agent uses the existing standing preview authority
described in [disposable-lane engineering](authorization-authority.md#disposable-lane-engineering).
Other callers need `odoo_preview_apply.execute` for that product's preview
context. Dashboard read access cannot execute product code. Every apply still
qualifies the exact current preview and its mutation protections. No grant or
credential is created. Stable-lane maintenance is unchanged.

## Product contract

The image ships `/opt/project/addons/launchplane_preview_fixture.py`. The fixed
host program executes it inside the exact selected web container, passing only
`identity`, native `database`, `fixture`, `mode` and `expected_fixture_sha256` as
JSON stdin. The internal expected digest is empty for inert modes and carries
the reviewed definition digest for apply; it is not a public request field. The product
checks native database host, preview instance, database name and runtime identity
before opening a database. Only the ephemeral `deployed_at` field may be ignored
when comparing identities.

The product owns the fixture and its deterministic `fixture_sha256`. `dry-run`
qualifies current matching data in a read-only transaction. First apply holds the
existing native global data-workflow lock, refuses conflicting data, reuses exact
matches, and creates the sample and a historical commit witness in one transaction.
Validate the complete wire receipt before commit, then verify the witness through
a fresh cursor. An existing witness makes apply a no-op without recreating,
resetting or overwriting records someone has subsequently used or deleted.

`readback` reads the historical witness in a read-only transaction without the
native workflow file lock or untouched-sample qualification. It reports what the
transaction wrote; later scanning, backorders, edits and deletions do not change
that fact. Empty readback means the witness was not observed. It does not prove
that an interrupted writer cannot still commit.
Readback may use a replacement web container only within the same verified
generation, runtime identity, native database and exclusively owned target.
Apply and cancellation retain their exact original container binding.

Return exactly one bounded JSON object on stdout: `identity`, `fixture`,
`fixture_sha256`, `mode`, `committed`, `verified` and `records`. Records contain
unique `{model, id, disposition}` identities, with disposition `created` or
`reused`, including relevant derived and witness records. Preserve original
historical identities and dispositions through recovery. Optional bounded
`reference` identifies a generated synthetic record, such as the actual sample
receipt number. Ordinary generated sequence letters and spaces are allowed;
copied business references and private prose are not.

Successful apply has `committed: true` and nonempty records. An expected semantic
refusal rolls back before returning an explicit apply rejection with
`committed: false`, `verified: true` and empty records. Other modes never claim a
commit. Unexpected errors, timeouts and malformed or missing receipts remain
unknown. Receipts contain no customer, supplier, price, credential or copied
business data.

The host discards stderr, bounds stdout to 32 KiB and observation to three
minutes, then validates the receipt before emitting hex evidence. The service
validates the typed receipt and selected identities again. Stopping the observer
does not prove the in-container writer stopped. Disabled manual schedule names
and script paths derive from the complete immutable host invocation. Identical
invocations reuse them; a changed mode, fixture, container, generation or host
package gets a different path, so updating a schedule cannot retarget a queued
job. Each provider execution retains its own history.

## Review, apply and recover

Use `scripts/preview-fixture.py` with the installed catalog transport.
Payload and review files stay private and outside the repository. Start with the
maintenance envelope above, `mode: dry-run`, and no reviewed digest:

```sh
uv run python scripts/preview-fixture.py dry-run \
  --operator-helper /installed/launchplane/scripts/launchplane-write-action.py \
  --payload-file /private/fixture.json --evidence-file /private/review.json
```

Inspect the selected identity, fixture digest and current qualification. The
review binds the definition and artifact. Apply uses the same arguments, `apply`,
`--reviewed-dry-run` and a stable `--idempotency-key`. Valid historical evidence is
adopted before current-sample qualification; otherwise the service repeats the
dry run before dispatch. Initial delivery follows apply with a separate dry run
to verify the actual untouched sample.

The durable operation retains actor scope, original intent, reason, exact
target/generation/container binding, effect checkpoint and bounded receipt.
Repeating an identical completed request/key replays its result. After a lost
response, repeat that original request and key: verified historical commit
evidence adopts the result without another write. Empty or unavailable readback
after a trigger keeps the reservation held. No recorded trigger checkpoint
permits the original operation to retry its current preflight, even when native
readback is unavailable. Never replace a key to bypass it. Explicit product
rejections settle without claiming a data commit. Pre-dispatch refusals release
fresh reservations. Read-only modes use service and provider execution evidence
rather than synthetic idempotency records.
Disabled schedule setup runs the existing guards but does not checkpoint a data
effect; the fixture checkpoint is immediately before schedule trigger. A valid
empty uncommitted apply receipt definitively rejects the write, including a
definition mismatch detected before commit.

If the original writer or native lock cannot be recovered, existing guarded
preview destruction can cancel the fixture by discarding its isolated resources.
Refresh cannot supersede it. Ownership, in-use, only-copy and disposable-resource
checks still apply, with volumes included in destruction. The expired fixture
reservation transfers atomically to destroy, retaining the unresolved-effect
fence through every failure. A replacement destroy retains the same original
fixture intent and first verifies the preceding destroy is quiescent. Refresh
and volume-preserving destroy cannot replace a cancellation. Missing or malformed
cancellation custody keeps the fence. Cancellation does not claim the fixture never wrote.
Destroy settles only after ordinary cleanup evidence and independent observation
that the exact original container is stopped or absent. Destroy apply checkpoints
and stops a surviving exact original container after checking its identity and
the existing mutation guards. This also recovers a provider deletion that left
an orphan. Observation remains inert: verified live-container evidence may request
an original-key destroy retry while retaining the checkpoint. Missing Docker
evidence keeps the fence. A stale native lock file is never deleted as a substitute.
