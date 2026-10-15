# Health alert drills

Use the deployed service's `GET /v1/health-alert-drills` to inspect failure-alert
routing for one existing `product`, `context`, `instance`, and `check_name`.
These four query parameters select a declared lane check; no destination,
webhook, endpoint URL or policy override is accepted. The read reports routing
as `enabled`, `missing`, `disabled` or `unavailable`, plus the count of enabled
matching Discord destinations. Matching uses the real incident policy matcher,
including product/lane/check-kind selectors and wildcard policies. Inactive
products, disabled checks and checks ineligible for real incidents cannot drill.
This read does not decrypt notification credentials or prove delivery.

`POST /v1/health-alert-drills` accepts the same four fields in JSON. Its default
`mode: "dry-run"` returns routing and receipt evidence without sending or writing
anything. To send, explicitly select `mode: "send"` and supply the `window`
returned by the fresh read/dry run. An expired or future send window is rejected;
repeat requests in that window return existing evidence without sending again.
Reads may supply a past `window` to recover its durable receipt. Recheck current
routing separately from that historical delivery: a delivered old receipt does
not prove the destination is still enabled.

The read and dry run require existing `product_environment.read` access for the
exact product/lane. Sending also requires existing
`public_ingress_monitor.run_once` access in the Launchplane service context,
through normal mutation authentication. This capability installs no grants and
does not work around a denial. Use the supported authorization denial procedure
when an existing identity cannot send.

The database admits one attempt per lane per ten-minute UTC window, shared by
all callers and all that lane's checks. The limit uses an atomic unique insert
into the existing idempotency evidence store, independently of real notification
attempts, reminders, deduplication and outbox state. This is a fixed window,
not a rolling ten-minute interval: adjacent windows may each admit an attempt.
An unavailable receipt is committed before any send; interruption never causes
an automatic resend within that window. A lost final response or ambiguous
provider outcome must be investigated with the same window read, rather than
retried as a new drill in another window.

The send uses current enabled matching Discord destinations, the monitor's
managed-secret resolver and Discord driver/HTTP transport. Routing is rechecked
before each send. A policy change or unavailable credential produces unavailable
evidence; provider failures produce failed evidence. Receipts contain a drill
id, named scope, attempt time, aggregate counts and `delivered`, `failed` or
`unavailable` status. They contain no destinations, secret identifiers, webhook
URLs, provider exception text or production endpoint URLs. Missing/disabled
routing sends nothing and creates no delivery receipt. `delivered` means the
Discord webhook accepted the request; it does not prove Chris read it.

Every message has a prominent **DRILL** title and body, states that no production
failure was observed, names the check and receipt, and disables mentions. It may
still notify the configured channel's users and consume Discord capacity. The
drill contacts no application endpoint, probes no health check and never writes
an observation, incident, incident event, reminder, real notification attempt or
outbox item. Synthetic in-memory envelopes exist only to reuse matching and
delivery code. It proves routing and transport, not failure detection, incident
transitions, scheduler cadence or outbox scheduling.

Source landing does not establish deployed availability. The first real drill
is a later authorized operation after this version is deployed; preserve the
receipt on the owning operational issue. This source change sends no real alert
and changes no production configuration.
