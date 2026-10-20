---
title: Product discussion topics
---

Launchplane stores discussion topics independently of product configuration.
Each record belongs to one product and has a caller-chosen topic id, a neutral
title beginning with `Decide: `, an HTTPS link to its owning issue, its added
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

Reads use the product's existing `product_profile.read` access, including
Client users who already have that access. A reader cannot enumerate another
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

## Delivery slices

The records and supported API are the first slice of #4230. The product topics
page and scheduled reminder sender remain separate work. There is no sender,
schedule, or default send in this slice. Chris will supply the product's meeting
and reminder times. Discord activation still waits for discord-blue#226's
release and Justin's invitation; source delivery does not post a message or
activate delivery. No initial topic records are seeded by this change.

The additive schema migration follows the approved preview-retirement branch.
It does not apply the held delivery-table drop. See the
[migration continuation](operations.md#prepared-retired-delivery-table-migration)
for the release target and already-applied paths.
