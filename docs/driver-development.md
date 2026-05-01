---
title: Driver Development
---

## Purpose

Launchplane drivers are the backend-owned boundary for product lifecycle
behavior. A driver declares what a product can do, validates requests, executes
or delegates provider work, writes durable records, and exposes read models for
operators and future UI actions.

Use `generic-web` directly when a product fits the common web-app lifecycle. Add
a new driver type or product driver only when the product has obligations that
should be named, authorized, tested, and operated separately from the generic
web path.

## When To Add A Driver

Add a driver when the product needs one or more of these:

- product-specific backup, restore, or rollback gates
- database bootstrap, migration, seed, clone, anonymization, or cleanup
- post-deploy maintenance commands
- product-specific smoke checks that affect promotion readiness
- platform-specific artifact handling
- runtime behavior that cannot be described with the `generic-web` profile
- a distinct authorization surface for a high-risk action

Do not add a driver just to rename `generic-web` for a product. Prefer a product
profile using `driver_id="generic-web"` until there is a real product-specific
capability to model.

## Driver Shape

Each driver should have these pieces:

- Descriptor metadata in `control_plane/drivers/registry.py`.
- Typed request and result models in a workflow module.
- Service routes under `/v1/drivers/{driver_id}/...`.
- Authz actions that match the driver actions and safety level.
- Storage writes through existing record contracts or a new contract when the
  behavior needs durable query state.
- Tests for request validation, authorization, successful execution, failure
  records, and read-model behavior.
- Docs that explain whether the driver extends `generic-web` or stands alone.

Driver routes that mutate runtime state should write the lifecycle records they
can derive from the request and provider result in the same request. Product
repos should not have to shape preview, deployment, promotion, rollback, or
cleanup records after asking Launchplane to perform the matching action.

Product drivers that reuse common web behavior should declare
`base_driver_id="generic-web"` in the descriptor and delegate common work rather
than copying preview/deploy logic. The product-specific behavior still needs
named capabilities and named routes.

## Capability Design

Use capability names to describe operator-visible behavior, not implementation
mechanics. Prefer names like these:

- `stable_deploy`
- `preview_refresh`
- `preview_destroy`
- `preview_inventory`
- `preview_readiness`
- `preview_pr_feedback`
- `prod_backup_gate`
- `prod_promotion`
- `prod_rollback`
- `app_maintenance`

Provider details such as Dokploy application IDs, endpoint mode, registry
credentials, or deployment job IDs belong behind adapters and evidence records.
Expose them in read models only when operators need them to decide or repair
state.

## Route Design

Driver routes should accept product intent and let Launchplane derive the rest
from records whenever possible.

Good trigger inputs:

- product key
- instance or lane when the action is stable-lane specific
- immutable artifact or image reference
- source ref or commit SHA
- PR number for preview actions
- explicit production confirmation for destructive actions

Avoid requiring product repos to send:

- provider target IDs
- public preview URLs when Launchplane can derive them
- health paths or runtime ports already stored in product profiles
- record IDs that Launchplane can generate idempotently
- rendered feedback markdown
- copied environment values or secret names beyond typed profile policy

If a route temporarily needs one of those fields, document why and add a cleanup
item to move it into product profiles, runtime-environment records, managed
secrets, or driver-owned derivation.

## Implementation Steps

1. Decide whether `generic-web` plus product profile fields is sufficient.
2. Add or extend the driver descriptor in the registry.
3. Add typed request/result models and executor functions in
   `control_plane/workflows/`.
4. Wire service routes and authz action checks in `control_plane/service.py`.
5. Write records through existing storage contracts when possible.
6. Add focused unit tests for validation, authorization, execution, and failure
   evidence.
7. Update docs and any product-repo trigger examples.
8. Seed or migrate DB-backed product profile, target, runtime environment,
   managed secret, and authz policy records outside the product repo.

Keep slices small. Land read-only descriptors and profile shape before
high-risk provider mutations. Land readiness checks before create/update/delete
actions when a provider mutation depends on external target state.

## Product Repo Boundary

Driver development should make product repos thinner, not larger. When a new
driver needs a product workflow, the workflow should only build/test/publish the
artifact and send a minimal Launchplane trigger request. See
[product-repo-contract.md](product-repo-contract.md) for the approval gate.

Legacy product repos may still carry scripts that shape Launchplane evidence or
call provider APIs directly. Treat those as migration candidates: classify them,
move the durable behavior into Launchplane, then delete or shrink the product
repo scripts.

When product-specific smoke checks still run in the product repo, keep the
follow-up contract thin: the repo reports the primitive result facts, and the
driver translates them into Launchplane records. Do not leave rendered evidence
payload construction in the product repo.
