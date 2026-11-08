---
title: Credential inventory
---

The administrator's **Engineering Ops → Credentials** page at
`/ui/engineering/credentials` shows metadata collected by an existing-access
collector. It displays names, SSH fingerprints and restriction names, purpose
notes, explicit rotation dates, drift findings and source coverage. It never
loads a credential value. Collection runs on the collecting machine; Launchplane
does not acquire SSH access or copies of private stores.

## Snapshot custody and reads

`POST /v1/admin/credential-inventory` accepts a `CredentialCollection`: the attempt
timestamp, `collected` or `failed`, and the existing collector's snapshot (or
`null` for failure). Its closed schema rejects additional fields at every level,
including value-bearing fields. Source names and metadata fields are bounded;
findings must name a reported source, and `missing` requires complete coverage.
Use only the metadata-only collector as input, never a credential file or dump.

The service stores each accepted collection as an immutable evidence-only receipt
in its existing idempotency store. The canonical envelope digest deduplicates an
identical retry. Ordering uses collection time, including fractions of a second,
so delayed delivery of an older attempt cannot replace the newest attempt. No
provider call, runtime configuration or access change occurs. No migration is
required. These receipts do not expire automatically; daily metadata retention
has the same database backup custody as existing evidence receipts.

`GET /v1/admin/credential-inventory` returns the latest attempt and last collected
snapshot. Missing, failed and stale collection remain distinct. A failure keeps
the previous snapshot with an explicit failure notice. No snapshot means unknown
inventory. More than 26 hours since the snapshot means stale, regardless of its
receipt time. Drift and rotation findings come from the collector's snapshot,
using its collection date; the page does not invent another rotation policy.
Undated rotation remains unknown. Partial or inaccessible sources never prove absence;
complete means only the collector's named scope, not all credentials everywhere.

Both routes accept the existing authenticated local administrator and Director
agent transports. Browser reads require the current stored policy's named
administrator, not a product-scoped admin role. Ordinary product agents and
workflow identities cannot read or publish the Director-wide inventory. Browser
writes retain normal CSRF checks. Successful and error responses are `no-store`.
The browser has only read/refresh controls, refreshes once a minute and ages the
snapshot against the service's expiry timestamp even when refresh fails. A failed
refresh marks status unconfirmed. A 401 or 403 removes the cached inventory and
pauses automatic reads; explicit refresh can recover when access returns.

## Collection and recurring delivery

On the collecting machine, use a stable, installed Launchplane checkout and the
existing catalog's `launchplane-write-action.py` authentication helper. The publisher invokes the supplied
metadata collector, validates its JSON and sends only that closed projection.
It accepts collector exit codes 0, 1 and 2, so drift or partial coverage still
arrive. A failed, timed-out or invalid collection sends a failed-attempt receipt
without replacing the last snapshot. Failures carry a bounded code for a missing
collector, timeout, collector exit, excessive size or invalid format, rather than
raw exception text. Empty or null rotation dates normalize to unknown. Temporary files hold metadata only and are
removed when collection ends. Raw stdout, stderr and transport exceptions are
never printed. No credential is created, copied, moved or exported.

Keep the publisher checkout on the deployed service version. Receipt confirmation
uses their shared snapshot model; a model mismatch is a delivery failure even if
the service accepted a newer projection. No mismatched receipt is treated as proof.

For the approved Chris-Studio collector, after this service capability is
deployed, run from the stable Launchplane checkout:

```bash
uv run launchplane-credential-inventory \
  --collector /Users/cbusillo/Developer/shiny-infra-ops/scripts/credential-inventory.py \
  --catalog /Users/cbusillo/Developer/codex-skills
```

The command and collector both use the catalog helper's default authentication
configuration. No publisher-specific authentication override exists, so collection
and delivery use the same source selection. An unattended job needs the catalog's
existing default configuration available without shell exports. No file or key is
copied. Its output contains only collection
status and a receipt id, or a bounded delivery failure. It makes no automatic
transport retry. Exit 0 means a collection was accepted (including drift/partial
coverage); 1 means a failed attempt was accepted; 2 means delivery did not confirm.
If delivery fails entirely, the last service snapshot ages into stale state.
An unread page does not send an external notification; the recurring report is
delivered to the Credentials page.

Generate a macOS user LaunchAgent from the same stable checkout:

```bash
uv run launchplane-credential-inventory \
  --collector /Users/cbusillo/Developer/shiny-infra-ops/scripts/credential-inventory.py \
  --catalog /Users/cbusillo/Developer/codex-skills \
  --write-launch-agent "$HOME/Library/LaunchAgents/com.launchplane.credential-inventory.plist"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.launchplane.credential-inventory.plist"
```

Generation refuses an existing file and does not load the job. The schedule runs
on load and every 24 hours using existing user access and `uv run --frozen`.
It contains paths and
timing only, never authentication values. Do not generate it from a temporary
task worktree. An optional `--manifest` selects the reviewed metadata manifest;
it is passed unchanged to the collector. To stop recurring collection, boot out
the exact LaunchAgent; retain its last report as historical evidence:

```bash
launchctl bootout "gui/$(id -u)/com.launchplane.credential-inventory"
```

Schedule generation failures exit 64 with `schedule_refused` and a bounded reason
(`already_exists`, `parent_unavailable`, `uv_unavailable` or `write_failed`).
Generation attempts no collection or service call.

Source delivery does not prove a running schedule or an accepted live snapshot.
Verify the loaded job with `launchctl print` and the accepted receipt/page through
the supported service before claiming recurring collection is active.
