---
title: VeriReel Prod Rollback Runtime
---

## Purpose

This document defines the next Launchplane boundary required to move VeriReel
production rollback behind Launchplane-owned execution.

The current gap is no longer about deploy or promotion evidence. Launchplane now
owns VeriReel prod deploy, rollout verification, Prisma migration, and final
post-migration health verification. The remaining repo-owned operational tail is
rollback after failed final health.

That rollback path currently depends on repo-local runtime assumptions:

- a dedicated GitHub self-hosted runner label
- repo-local `.env` default loading
- direct Proxmox `ssh`, `sudo`, and `pct rollback` execution

This document exists to turn that tail into an explicit Launchplane runtime
contract instead of letting those assumptions leak across the service boundary.

## Current State

Today the rollback path lives in VeriReel's product repo:

- workflow: `.github/workflows/promote-image.yml`
- implementation: `scripts/ops/prod-gate.mjs`

That path currently does four jobs:

1. resolve Proxmox connection and container inputs
2. locate or accept the stored rollback point
3. execute `pct rollback` through SSH and sudo on the Proxmox host
4. verify that production is healthy after rollback

Launchplane does not yet own the runtime authority needed for step 3, even
though it now owns the surrounding promotion transaction.

## Goal

Move VeriReel prod rollback into Launchplane so that:

- the product workflow sends an authenticated rollback request rather than
  shelling into Proxmox directly
- Launchplane owns the rollback result, timestamps, and operator-visible
  evidence
- rollback failure and rollback-health failure remain distinct recorded states
- Launchplane, not the repo, owns the secret and runtime contract needed to
  reach Proxmox safely

## Non-Goals

- building a generic arbitrary remote shell framework
- moving GitHub release publication into Launchplane
- rewriting the already-landed prod promotion flow
- forcing a cross-product rollback abstraction before the VeriReel path proves
  the execution model

## Runtime Model Options

### Option A: direct Launchplane host execution

Launchplane itself would hold the SSH and sudo contract needed to reach the
Proxmox host and run rollback commands.

Pros:

- simple request path
- no second runtime to coordinate
- easier end-to-end traceability inside one service

Cons:

- couples the main Launchplane service host to Proxmox network reach and sudo
  authority
- raises the blast radius of a Launchplane compromise
- makes future runtime separation harder if more products need privileged ops

### Option B: Launchplane-owned delegated worker

Launchplane would remain the control-plane ingress and record owner, but it
would dispatch rollback execution to a Launchplane-controlled worker that holds
the Proxmox network and sudo contract.

Pros:

- keeps the main Launchplane service boundary narrower
- matches the reality that rollback has a different trust and network profile
  than HTTP evidence ingress
- creates a reusable shape for future privileged stable-lane actions without
  making the API server itself the privileged host

Cons:

- adds a worker runtime contract and dispatch path
- requires explicit job/result handoff between service and worker

### Option C: keep GitHub runner execution but rebrand it as Launchplane

Launchplane would still depend on the repo-triggered GitHub runner to perform
rollback, with Launchplane only coordinating or recording the request.

This is not recommended. It keeps the core operational dependency where it is
today and only moves names around.

## Recommended Direction

Prefer **Option B: Launchplane-owned delegated worker**.

This is now the locked direction for the first implementation cut. The first
rollback route should be a dedicated driver,
`POST /v1/drivers/verireel/prod-rollback`, with Launchplane owning request
authorization, durable rollback state, and post-rollback health verification,
while a narrow Launchplane-owned worker owns the privileged Proxmox rollback
command path.

The reason is boundary discipline. VeriReel rollback needs privileged network
reach and host-local behavior that are materially different from Launchplane's
normal service ingress. A delegated worker lets Launchplane own the operation
without making the API server itself the privileged execution host.

That still leaves room to start with a very small worker surface:

- accept a single Launchplane-issued rollback job shape
- execute a constrained Proxmox rollback command set
- return structured result data only

If operational reality later proves that the main Launchplane host is already the
right privileged environment, the worker contract can collapse inward. The
reverse move would be harder.

## Proposed Boundary

```text
GitHub workflow
  -> Launchplane OIDC-authenticated rollback request
  -> Launchplane authn/authz and record creation
  -> Launchplane rollback dispatcher
  -> Launchplane-owned rollback worker
  -> Proxmox host via SSH/sudo
  -> Launchplane post-rollback health verification
  -> durable rollback result on the promotion record
```

The product workflow should stop owning Proxmox SSH and sudo behavior. It
should own only:

- the decision to request rollback after failed final prod health
- the authenticated Launchplane request
- any remaining GitHub-only concerns such as release publication

## Proposed Route Shape

Two shapes are reasonable:

### Shape 1: dedicated rollback driver

- `POST /v1/drivers/verireel/prod-rollback`

Use this when the repo workflow remains responsible for deciding whether to roll
back after a failed promotion.

Recommended request inputs:

- `context`
- `instance`
- `promotion_record_id`
- `backup_record_id`
- `rollback_record_id` or idempotency key
- `expected_build_revision` and `expected_build_tag` for post-rollback checks
- optional explicit snapshot reference when the stored backup gate points at
  more than one rollback candidate

Recommended result fields:

- `rollback_status`
- `rollback_health_status`
- `started_at`
- `finished_at`
- `snapshot_name`
- `error_message`

### Shape 2: rollback branch inside prod promotion

The existing `POST /v1/drivers/verireel/prod-promotion` route would optionally
perform rollback when final health fails.

Use this only if Launchplane should own the entire production finalize
transaction as one atomic control-plane action.

This can be attractive later, but it couples the current next slice to a bigger
transactional decision. The dedicated rollback driver is the locked immediate
fit for the first implementation cut.

## Secret And Config Contract

The rollback secret/config surface should move into Launchplane-owned runtime
configuration rather than product-repo `.env` defaults.

Required runtime inputs:

- `VERIREEL_PROD_PROXMOX_HOST`
- `VERIREEL_PROD_PROXMOX_USER`
- `VERIREEL_PROD_CT_ID`
- `LAUNCHPLANE_VERIREEL_PROD_ROLLBACK_WORKER_COMMAND`
- allowed sudo command contract for `pct rollback`
- SSH private key or equivalent Launchplane-managed credential
- host key trust policy
- snapshot prefix and retention policy when Launchplane later also owns capture

Launchplane now resolves the rollback worker contract from the `verireel/prod`
runtime-environment definition before invoking the delegated worker. That means
the worker command and Proxmox target metadata must live in DB-backed
runtime-environment records, with any secret-looking values overlaid from
Launchplane-managed secret records when the service is running with
`LAUNCHPLANE_DATABASE_URL` and `LAUNCHPLANE_MASTER_ENCRYPTION_KEY`.

Launchplane strips rollback worker config keys inherited from process env before
starting the delegated worker. Missing DB-backed worker config is a hard error,
not a compatibility fallback to service-host env.

Rules:

- do not read rollback authority from a repo-local `.env`
- do not make GitHub Actions the long-term source of privileged rollback
  secrets once Launchplane owns execution
- keep the live privileged contract outside the checked-in repo, with metadata
  visible through Launchplane read surfaces but plaintext secret values hidden

## Record And Evidence Shape

Rollback should become durable Launchplane state, not just a workflow log.

Minimum required distinctions:

- `rollback_status=pass|fail|skipped`
- `rollback_health_status=pass|fail|skipped`
- rollback target metadata
- rollback snapshot metadata
- rollback timestamps
- structured error message or failure detail

The important rule is that these states must not collapse into one generic
promotion failure. Operators need to distinguish:

- deploy failed
- rollout failed
- migration failed
- final health failed
- rollback command failed
- rollback completed but health stayed failed

The existing promotion record may be sufficient if it gains explicit rollback
fields. If not, add a dedicated rollback record linked from the promotion.

## Worker Constraints

If Launchplane uses a delegated worker, keep the first worker contract narrow.

Allowed responsibilities:

- fetch or receive a typed rollback job
- execute the constrained Proxmox rollback command path
- return structured command/result status

Not allowed in the first cut:

- arbitrary shell access
- product-repo checkout and `.env` loading
- direct GitHub decision-making
- broad infrastructure mutation outside the constrained rollback path

## Testing And Validation

Required validation before implementation is considered complete:

- Launchplane unit tests for rollback request parsing and result shaping
- driver tests for rollback success
- driver tests for rollback command failure
- driver tests for rollback success followed by failed health verification
- failure tests for missing or invalid snapshot metadata
- request-client tests in VeriReel for surfaced rollback statuses
- workflow YAML parse after repo rollback-job simplification
- one operator dry-run against the real Proxmox path

## Remaining Decisions

- Should rollback remain a dedicated explicit driver long-term, or move into
  prod-promotion once the runtime contract is proven?
- When Launchplane later owns snapshot capture too, should backup and rollback
  share one privileged worker contract or remain separate operations?

## Immediate Next Step

The execution posture is now chosen:

- use a Launchplane-owned delegated worker
- do not keep GitHub as the privileged rollback execution plane
- do not fold privileged Proxmox authority into the main Launchplane API host

The next implementation work should define and ship the exact job payload,
record schema, authz action names, and worker command contract for the first
rollback route.

That first route now exists as `POST /v1/drivers/verireel/prod-rollback`.
Launchplane still requires explicit worker runtime configuration before the
route can execute real Proxmox rollback operations outside tests.
