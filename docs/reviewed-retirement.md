---
title: Reviewed setting and workflow-rule retirement
---

# Reviewed retirement

These service paths prepare exact retirement work. Source delivery performs no
retirement and grants no access. Production/data, access-removal and Client
release decisions keep the boundaries in [DIRECTION.md](../DIRECTION.md).
Neither path calls a provider, syncs a runtime, deploys, or restarts a lane.

## Plain stored settings

For a values-free comparison before selecting a removal, request
`GET /v1/products/{product}/secret-bindings?include_setting_sources=true`.
It reuses product/environment and preview read permissions and refuses incomplete
scope authority or shared context ownership. The additive `setting_sources`
inventory groups configured key names by lane and source, reports retirement
intent, and enumerates retained preview records separately.
It reads no secret versions, decrypts nothing, calls no provider, and saves no
review or configuration. A name overlap is not a value disagreement or proof of
delivery. Odoo overrides report native parameter/addon names, reference scope and
apply phases without their literal values or binding ids. A reference reports
the configured binding's key/context/instance when it is inside this product;
missing, disabled or outside-product pointers remain `unresolved_or_out_of_scope`.
These metadata observations do not validate secret resolution. Secret record names
and delivered binding keys remain distinct identities. Preview policies describe
configured key names; these tables cannot reconstruct provider inputs, exact-preview overrides,
or retained generation settings. Bounds report truncation; the tables
are read independently, so this is an observation, not an atomic migration plan.
The read does not score overlaps or implement a second resolver. Comparisons use
these source names and the existing canonical mapping helpers as task evidence.
Preview targets are enumerated, not compared to their running generations.

Use the repository's bounded reader with the installed operator transport:

```bash
uv run python scripts/read-setting-sources.py --product <product> \
  --operator-helper <installed-launchplane-skill>/scripts/launchplane-write-action.py
```

The reader prints only the source inventory and trace and exits nonzero on
truncated coverage. An older service that
omits the inventory is unavailable, never an empty result. This read does not
authorize a setting fold, a production release, or removal of retained authority.

`POST /v1/admin/runtime-settings/removal` accepts `mode: dry-run`, `apply`, or
`readback`, an exact `product`, `scope` (`global`, `context`, `instance`),
`context`, `instance`, `keys`, and `reason`. Global scope requires
`product: launchplane` with no context or instance. Context/lane scope requires
the product's exclusive recorded context and supported lane; another product's
context cannot be substituted. This is separate from product-config's merge
writer and does not change its ownership or global-target refusal.

The authenticated Director agent can prepare previews and read back results.
An existing admin can review with `secret.list`. Apply requires existing
`product_config.apply` authority for the exact selected scope, product and
context. The signed-in immutable-ID administrator keeps its existing scopes.
The authenticated Director `local_operator` can also apply an instance-scoped
removal for an exclusively owned, enabled product preview context with
`instance: preview`. That context/instance pair must not alias a stable lane.
This narrow path for the Director's agent rejects testing, production, context and global
selections; other caller credentials, `local_admin`, workflow and terminal credentials
cannot use it. The trusted Director marker alone does not make a terminal token
writable, and standing read authority never supplies the required write grant.
The same scope and permission checks run before receipt replay. Browser
origin/fetch metadata/CSRF checks still apply. No new grant is installed.

Dry-run saves a service-owned review containing the complete original record,
request, reviewer and active authority binding. Its wire response contains only
scope, selected/surviving key names, counts, timestamps and opaque digests.
A private per-review random nonce protects value-derived digests against
offline guessing of low-entropy values; it stays only in service custody. It returns
`review_id` and `plan_sha256`; apply must repeat the same intent with those
values as `review_id` and `reviewed_plan_sha256`, plus a stable
`Idempotency-Key`. Every selected key must exist. The complete record, active
policy record and product profile are compared again under the database locks
before commit. Changed values, types, retirement intent, ownership, scope or
policy require a fresh dry-run. No caller supplies surviving values.

Apply removes only the selected stored keys. All surviving values, scalar
types, source label and retirement intent remain unchanged. Removing the last
value deletes a record only when it has no provider-retirement intent; its
append-only delete event links the review. A lane record containing retirement
intent survives with an empty value map so provider cleanup intent is retained.
Before applying a last-value removal that retains retirement intent, complete
its `requires_compatible_readers_and_rollback` prerequisite: deploy readers
that accept retirement-only records to every consumer/replica and retain a
rollback build with that reader support. Earlier builds reject empty value
maps, so rolling back to an older incompatible image after such a write is
unsupported without separately governed data restoration. Source delivery is
not that rollout qualification. The operational retirement issue must establish
those prerequisites before any apply; the flag reports the requirement and
does not attest deployment or approve it. Ordinary partial removals and
last-value deletions without retirement intent keep the previous record format.

Managed-secret records and bindings are untouched. An inherited value can
become visible after a stored override is removed; the preview is not a claim
that a key is absent across all layers or from the provider.

Exact non-preview instance reviews also return `instance_plain_setting_effects`: for each
selected key, whether the context supplies the same string value, a different
value, or no value after the lane override is removed. This comparison follows
the current site plain-setting layers, which exclude global settings. It does
not evaluate preview templates, target overrides, managed secrets, provider
leftovers, actual runtime consumers or legacy global-inclusive resolvers.
Preview, context and global reviews omit this comparison and still require their own
consumer and inheritance qualification.

The service compares selected context values privately and binds them to the
review with a nonce-protected `context_values_sha256`. Apply rechecks this
binding under the existing authority locks, including when an absent context
value has since appeared. A changed fallback requires a fresh review; unrelated
context keys do not invalidate it. Older non-preview instance reviews also require a fresh
review before applying. The comparison stays with the original review in
historical receipts and readback; it is not a fresh runtime observation.

Global dry runs also return `consumer_inventory`, a bounded names-only observation
of every stored runtime record, product lane and preview profile, target overlay,
and matching runtime secret-binding metadata. Enumeration includes inactive
profiles, undeclared contexts, historical lane names and global bindings. Preview
profile matches describe explicit overrides only; they do not resolve the
template. Matching runtime/secret requirements and preview key policies are
listed separately, including omitted/copied/required/template URL/domain names.
An unspecified requirement context applies to all product lanes; it is not
evidence of a global plain-setting source. Declarations do not prove runtime
consumption. Lane entries have null `selected_keys` because their effective values
have not been evaluated. An empty list for a source means no selected name in
that source, not an unused setting. Counts describe the complete enumerated
tables; each displayed consumer and authority list is capped separately and
reports truncation.
Matching entries appear before unmatched entries. The page shows displayed and
total counts, so even a list containing only matches cannot imply full coverage.

The inventory also lists every saved reconciliation request and production
recipient, including completed and failed records. These are potential authority
consumers, not proof that a selected name occurs in their captured authority.
Resolve a held record through its original operation; never replace its key or
capture new authority to bypass drift. Retained operation payloads, preview
generations, baked images, provider environments, managed-secret resolution and
external CLI consumers remain explicitly unqualified. This is no complete-fleet
runtime-consumption verdict.

This observation is collected after the saved review, is outside its digest,
and is neither atomic across tables nor revalidated by Apply. It is not saved in
the review or repeated on Apply/readback. It changes none of their existing
guards. Obtain current operational qualification independently before removal.
The signed-in page displays this evidence on global reviews. Terminal clients
that omit the additive field cannot claim inventory coverage; use the page or
update the bounded client in its owning repository.

The change and its completion receipt commit in one transaction. Retry an
uncertain reply only with its original request and key. A confirmed replay
returns the original result; it does not reapply. `mode: readback` uses the
same review binding and compares fresh stored survivors/absence and retirement
intent, returning `readback_matches`; false means drift or incomplete removal,
not success. The commit receipt identifies historical completion; readback is
the separate current-state proof. A 409 `runtime_setting_removal_stale` means
this exact attempt acquired its reservation but refused changed state before
writing, and the transaction rolled back. The browser may settle that attempt
and request a fresh review. The generic `runtime_setting_removal_unverified`
response does not prove that outcome and preserves continuity on a retry after
an uncertain reply.

Rollback custody stays inside the service's private database in the saved
review's original record. The preview preimage is not an HTTP response and has
no new export route. Preserve it with the database backup/audit retention;
do not prune these reviews as disposable idempotency cache. Restore planning
uses that exact preimage, a fresh current-state comparison, and the supported
admin configuration/repair boundary with its own approval; this endpoint does
not implement automatic restore or authorize a production rollback. Failed
transactions retain the original record and publish no completion receipt.

Historical names outside the bounded printable alphabet are not echoed or
silently treated as absent. Selected names must be safe to print. Surviving
unprintable names stay unchanged and appear as `unprintable_survivor_count`;
that gap is partial name coverage, not absence. Values never appear, including
credential-shaped values held in old plain records.

## Workflow identity scopes and removal plans

`POST /v1/authz-policies/workflow-rule-retirement-plan` uses existing policy
administration read authority. Director-agent standing read authority works;
Client-scoped readers cannot export global rule scopes. It requires the exact
`policy_record_id`, `policy_revision`, `policy_sha256` and a bounded `selection`
of workflow `rule_sha256` fingerprints. Each selection also supplies its exact
`managed_set_id`/`managed_rule_id`, or null for unmanaged rules. A missing,
duplicate, mismatched or ambiguous selection refuses rather than broadening it.

`mode: inspect` returns selected active rule scopes: immutable repository IDs,
repository, caller/reusable-workflow selectors, refs, events, products, contexts,
instances and actions. It distinguishes managed from unmanaged rules and flags
broad/shared selector coverage. It exports no token or credential value.
`mode: plan` additionally computes the surviving workflow list and complete
candidate-policy digest on the server. Every other principal collection and
policy setting remains unchanged; no caller-built desired policy is accepted.
The response includes the survivor digest, plan digest and last-administrator
blockers. No whole managed set is removed by selecting one of its rules.

A scope is not evidence that every source consumer is retired. Dependency
verification remains `unknown_requires_source_review`, especially for wildcard
or shared reusable-workflow rules. Preserve still-needed callers and inspect
actual repository source before proposing any access removal. The active policy
is checked again after planning so a concurrent change invalidates the result.
This route has **no apply mode** (`apply_supported: false`). A later Director
access-removal decision needs this exact plan and a supported reviewed admin
policy operation; a plan digest never grants execution authority.

### Reviewed workflow-rule removal

The existing `managed-authz-policy-set` privileged-operation proposal accepts
an optional `workflow_rule_retirement` containing that exact snapshot and selection
with `mode: plan`. For this contraction, `managed_set_id` is empty and
`desired_policy` is an empty schema-v2 or schema-v3 fragment. No managed set is
reconciled. Schema migration, adoption, quorum changes and nonempty desired rules
are rejected. The active schema, settings and every unselected rule remain exact.
Only unmanaged selections are accepted. Contract a managed rule through its own
reviewed managed set so its independent source cannot silently restore it.

Prepare a private `managed-authz-policy-set` envelope with that request, a stable
`source_event_id`, `reason` and optional `related_issue`. Submit it through the
installed Launchplane helper's `privileged-policy-propose --payload-file` command.
The helper keeps its existing private admin transport, response validation and
full review URL. It never approves or applies. Keep the identical envelope and
source event after an uncertain submission; inspect or replay that proposal
rather than creating another. Older installed clients need the companion
[helper update](https://github.com/cbusillo/codex-skills/issues/2633) before
they accept this optional request field.

The existing proposer grant and signed-in Director approval govern the request;
no new grant or operation descriptor is installed. The Director's review shows
each selected workflow's full scope and permissions, including unmanaged rules.
The ordinary managed-reconcile HTTP route rejects this contraction: execution
belongs to the existing approved-operation worker. Approval binds the selection,
reason, original active policy and candidate digest. The worker replans against
fresh state, writes through the existing locked policy CAS and audit transaction,
and verifies the resulting complete policy digest. An interrupted execution uses
the same operation's existing completion receipt and recovery path.

The inspect/plan route remains inert. Delivering this source performs no
retirement. After approval, read the active inventory again and compare it with
the reviewed candidate; a proposal or historical worker receipt alone does not
prove current absence or unchanged survivors.

After the first proposal containing this request is persisted, retain a rollback
image with this request reader. Older strict readers cannot load that new proposal;
rolling back the service image never restores removed authorization data.

## Signed-in removal page

Engineering Ops → **Remove stored settings** (`/ui/engineering/runtime-setting-removal`)
uses the existing removal endpoint. Enter the product, exact stored scope,
context/lane when applicable, selected names and reason. **Review removal** saves
a fresh service review and shows the exact selected names, survivors, retirement
intent and reader/rollback requirement. Changing any field invalidates that review.
No stored values, secret values or rollback preimages are displayed.
Non-preview instance reviews display the stored context fallback comparison alongside the
selected keys. This evidence narrows the stored-value question without
attesting runtime use or qualifying a removal by itself.

Before confirming, qualify the actual runtime consumers and the effective value
that would be inherited from broader scopes. A context removal can change every
lane without a more specific override; a global removal can reach other products.
The global inventory identifies stored sources but does not attest actual
consumption or effective values. Follow the live-site and
Client boundaries in DIRECTION.md before approving the exact change.

**Remove reviewed settings** uses the signed-in administrator's existing authority,
CSRF protection and one retained operation key. The original request stays in that
browser tab's session storage for same-request recovery after a lost reply or
reload. Recovery is keyed by the administrator and the physical stored record
(scope, context and instance), independently of product ownership. The record
selector stays usable after uncertainty so other records can be reviewed;
returning to the same record restores its original request and key. An uncertain
outcome locks that record's setting names, reason and new reviews; **Retry original removal**
reuses the request and key. Missing recovery storage blocks submission. A stale
attempt that is proven rolled back requires a fresh review, including after
an uncertain reply. An unverified reservation keeps the original operation locked.
A committed removal is followed by an independent readback, and its local saved
request is cleared. Historical completion remains in service custody. An unavailable or
mismatched readback stays unverified. **Check current record**
can repeat that read, including while an Apply is uncertain. The previous
result is cleared while a fresh check runs, and progress is shown until the
request finishes. Completed checks show their service reference so a fast recheck
is distinguishable from its predecessor. A mismatch reports changed state;
a failed check stays unverified. A previous match is not current proof during
the recheck. A current match or mismatch never settles the original Apply. After a confirmed
commit, a fresh review can inspect the current record even if another writer
has changed it; the historical receipt does not promise current agreement.
Runtime sync, deployment and production acceptance remain separate operations.
