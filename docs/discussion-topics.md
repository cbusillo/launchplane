---
title: Product discussion topics
---

Launchplane stores discussion topics independently of product configuration.
Each record belongs to one product and has a caller-chosen topic id, a neutral
question as its title, an HTTPS link to its owning issue, its added
time, and an optional closed time. Topic changes do not alter product profiles,
release evidence, or deployment settings.

The title and link are Client-visible metadata. Write a fresh, neutral question;
never copy private reasoning from the Director's records. There is no private
reason or general context field. Links are stored without being fetched.
The writer is responsible for choosing an issue safe for that product's Client.

## Supported service API

- `GET /v1/products/{product}/discussion-topics` lists open topics in added order.
  `?include_closed=true` also returns retained closed records.
- `PUT /v1/products/{product}/discussion-topics/{topic_id}` adds a topic with a
  JSON body containing only `title` and `issue_url`. Repeating the same id and
  body returns its current record, including a previous close. Reusing the id
  with different content returns `409 topic_conflict`.
- `POST /v1/products/{product}/discussion-topics/{topic_id}/close` closes a topic.
  Repeating close preserves the first closed time. A missing topic returns 404.

Reads use the product's existing `product_profile.read` access or its recorded
Client identity, matched by immutable GitHub ID on an active product. Client
sessions use the same sign-in and current ownership validation as product review. A reader cannot enumerate another
product's topics. Writes require `discussion_topic.write` and an authenticated
Director agent or administrator. The action is an ordinary metadata write
covered by the Director agent's standing permission. It grants no access,
merge, release, or runtime authority. Other agent and workflow identities cannot
write topics. Signed-in browser administrators use the existing CSRF and
single-use mutation proof. No new grants or credentials are needed for the
Director's agents.

The caller supplies a stable topic id for retries. Closed topics are retained
and cannot be reopened or overwritten by an add. A later distinct discussion
uses a new id. Closing a topic does not record a product decision: record the
outcome on its owning issue first. Shared records require the database-backed
service; there is no local CLI live-target substitute.

## Topics page

`/ui/products/{product}/topics` is a read-only page for the Director and that
product's existing Client users. Product workspaces and Client review pages link
to it. It shows the product display name, open-topic count, each neutral question,
when it was added, and a visible owning-issue link for discussion context. No
`Decide:` prefix is required (Q571); older prefixed titles remain valid.

The page loads only the selected product's topics, without operational inventory
or admin controls for Clients. The service checks access on every read; a link to
another product grants nothing. Refresh reloads open records, removing topics
closed through the supported API. Loading, empty, denied and failed reads are
shown explicitly; a failed refresh hides the previous topic list. Signing in
returns to the same product page.

## Scheduled reminders

The service lifespan runs an opt-in scheduler once a minute. Configuration is
the JSON string `LAUNCHPLANE_DISCUSSION_REMINDERS` in the database-backed
`launchplane` context runtime-settings record. Use the existing authenticated
runtime-settings service API to maintain it. There is no process-environment,
global-setting or product-setting fallback. No configuration means no delivery.
This source change creates no schedule or topic records.

The value follows `DiscussionReminderSettings` in
`control_plane/discussion_reminders.py`:

- `director`: the `policy_id` and `destination_id` of an existing enabled
  Discord destination on Launchplane's own `incident_alerts` notification
  policy, without a check-specific filter. Reuse the verified Director route;
  Client-product policies are rejected. No second secret-reference or credential
  configuration is introduced here.
- `schedules`: one entry per product, with `product`, IANA `timezone`, `times`
  (`weekday`, Monday=0, and local `time`, `HH:MM`), and a short, plain `meeting`
  description. For the Saturday meeting, the description should say
  "Saturday, usually around 2 PM ET". Chris supplies the actual reminder times.
- Each entry defaults `enabled`, `bridge_released`, and
  `invitation_confirmed` to false. All three must be explicitly true after the
  actual release/invitation gates clear. They record verified activation facts;
  they grant no access and do not replace Justin's invitation.
- Optional `overrides` map a local `YYYY-MM-DD` date to a different `HH:MM`
  reminder time, or `null` to skip that day. An override replaces that date's
  normal reminders. Meeting text and times can be changed for a particular week.

The scheduler queues due occurrences through the existing durable outbox.
Replicas, restarts and repeated polls share the same product/occurrence key.
Local times follow the selected zone's daylight saving rules. A nonexistent
local time is skipped; a repeated time uses its first occurrence. Catch-up is
limited to 15 minutes, including at delivery; old reminders are not broadcast
after an extended outage. Queued work rechecks the complete schedule and active
product before sending. Disabled or no-longer-due occurrences and retired
products skip their queued reminders. Edits to meeting text or other dates keep
an occurrence that is still due; it uses the current meeting text. Moving a time
after its reminder was sent creates a distinct occurrence and may send again.

The sender reads the selected product's current open topics at delivery time.
The short message contains only their count (or "No topics to discuss"), the
meeting description, and its `/ui/products/{product}/topics` link using the
service's public HTTPS origin. Topic titles, issue links and private notes are
never included. The page still enforces its existing Client access checks.

Read failures, expired queued reminders, unavailable send capability, and
unconfirmed sends enqueue a separate, deduplicated Director alert. Pre-send read
errors alert and retry under the existing outbox backoff while the occurrence is
in its window and attempts remain. Exhaustion or expiry is terminal and alerts
again with that outcome. Provider
start is recorded before sending. An unconfirmed send is never automatically
replayed; check its delivery record and the destination before deciding on
recovery. Lost or ambiguous provider-marker ownership propagates to the outbox
without sending or publishing a misleading failure alert. Director alerts retry
pre-send read failures and confirmed Discord rate limits under the outbox budget;
confirmed other 4xx rejections and unconfirmed sends terminate with a critical
log. Alerts contain the product, a bounded failure code and the instruction
to check delivery records, without raw provider errors or topic content. Unknown
outcomes are reported as needing attention, rather than asserting a failed send. A
malformed schedule can still alert using the separately parsed Director
destination from its notification policy. Configuration failures alert at most
once per UTC day; the scheduler also limits its configuration-error log to once
per day per process. With no active schedules, no public URL is required. If storage,
the Director destination, or alert delivery itself is unavailable, delivery
records and service error/critical logs retain the failure; this cannot provide
out-of-process monitoring during a total service/database outage.

### CM transport activation prerequisite

`DiscussionReminderSender` is the service capability boundary, receiving only
`product`, a stable `delivery_id`, and the composed `content`. The outbox worker
loop accepts this adapter; it must bind each product to the single verified CM
conversation destination, suppress mentions, and report confirmed delivery.
The default adapter is unavailable and fails closed with a Director alert if a
schedule is mistakenly enabled.

Current discord-blue#226 exposes `/agent-session/conversation` only for an
attached CM Supervisor session. It does not yet expose a session-independent
scheduled sender. Launchplane does not attach as or replace that session, use a
bot token, create a webhook for CM, or fall back to another destination. A
supported session-independent CM sender and its service binding are required
before activation, in addition to discord-blue#226's release, Justin's
invitation, and Chris's schedule configuration. This slice proves scheduling,
delivery through an injected adapter, and failure alerts with isolated tests;
it does not claim an end-to-end CM send or live activation.

The additive schema migration follows the approved preview-retirement branch.
It does not apply the held delivery-table drop. See the
[migration continuation](operations.md#prepared-retired-delivery-table-migration)
for the release target and already-applied paths.
