---
title: Reviewed setting and workflow-rule retirement
---

# Reviewed retirement

These service paths prepare exact retirement work. Source delivery performs no
retirement and grants no access. Production/data, access-removal and Client
release decisions keep the boundaries in [DIRECTION.md](../DIRECTION.md).
Neither path calls a provider, syncs a runtime, deploys, or restarts a lane.

## Plain stored settings

`POST /v1/admin/runtime-settings/removal` accepts `mode: dry-run`, `apply`, or
`readback`, an exact `product`, `scope` (`global`, `context`, `instance`),
`context`, `instance`, `keys`, and `reason`. Global scope requires
`product: launchplane` with no context or instance. Context/lane scope requires
the product's exclusive recorded context and supported lane; another product's
context cannot be substituted. This is separate from product-config's merge
writer and does not change its ownership or global-target refusal.

The authenticated Director agent can prepare previews and read back results.
An existing policy administrator can review with `secret.list`. Apply requires
the signed-in immutable-ID administrator and existing `product_config.apply`
authority for the exact selected scope, product and context. A local-admin,
workflow, operator or terminal token cannot apply, even with a matching preview.
Browser origin/fetch metadata/CSRF checks still apply. No new grant is installed.

Dry-run saves a service-owned review containing the complete original record,
request, reviewer and active authority binding. Its wire response contains only
scope, selected/surviving key names, counts, timestamps and digests. It returns
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
Managed-secret records and bindings are untouched. An inherited value can
become visible after a stored override is removed; the preview is not a claim
that a key is absent across all layers or from the provider.

The change and its completion receipt commit in one transaction. Retry an
uncertain reply only with its original request and key. A confirmed replay
returns the original result; it does not reapply. `mode: readback` uses the
same review binding and compares fresh stored survivors/absence and retirement
intent, returning `readback_matches`; false means drift or incomplete removal,
not success. The commit receipt identifies historical completion; readback is
the separate current-state proof.

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
