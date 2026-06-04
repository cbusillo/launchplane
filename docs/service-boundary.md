---
title: Launchplane Service Boundary
---

## Purpose

This document defines the first explicit Launchplane service boundary: the initial
HTTP ingress, the GitHub Actions OIDC trust model, the claim-to-permission
mapping, and the first stable API payloads Launchplane should accept.

It exists to keep new cross-product work aligned with Launchplane's target form:

- long-running service
- authenticated machine ingress
- Launchplane-owned drivers
- thin repo extensions

The repo-local CLI is an operator/client surface around this boundary. Local
file-backed state is allowed only for development, tests, import/backfill,
explicit local rehearsal, and emergency inspection; it is not a production
persistence fallback.

Agent-facing context and scoped write-intent rules are summarized in
[agent-context-boundary.md](agent-context-boundary.md). Keep that page aligned
with this endpoint inventory whenever agent-visible behavior changes.

Product repos should build, test, and publish product artifacts, then call this
boundary with minimal trigger facts. They should not carry Launchplane lifecycle
truth, provider mutation logic, rendered evidence payloads, or copied driver
behavior. See [product-repo-contract.md](product-repo-contract.md) for the
approval gate.

## Current Implementation Status

The service boundary is implemented and deployed for the current Odoo and
VeriReel product paths:

- CLI: `uv run launchplane service serve`
- health route: `GET /v1/health`
- protected artifact inventory route:
  - `GET /v1/artifacts/protected`
- authenticated evidence routes:
  - `POST /v1/evidence/backup-gates`
  - `POST /v1/evidence/deployments`
  - `POST /v1/evidence/promotions`
  - `POST /v1/evidence/previews/generations`
  - `POST /v1/evidence/previews/destroyed`
  - `POST /v1/evidence/runner-host-hygiene/audits`
- product profile routes:
  - `GET /v1/product-profiles`
  - `GET /v1/product-profiles/{product}`
  - `POST /v1/product-profiles`
- product config write route:
  - `POST /v1/product-config/apply`
- product onboarding route:
  - `POST /v1/product-onboarding/apply`
- provider-target operation route:
  - `POST /v1/provider-targets/operations`
- product context cutover route:
  - `POST /v1/product-profiles/context-cutover/apply`
- product legacy context cleanup route:
  - `POST /v1/product-profiles/legacy-context-cleanup/apply`
- authz policy maintenance route:
  - `POST /v1/authz-policies/github-actions/grants`
  - `POST /v1/authz-policies/github-actions/removals`
  - `POST /v1/authz-policies/github-humans/grants`
  - `POST /v1/authz-policies/terminal-agents/grants`
  - `POST /v1/authz-policies/local-operators/grants`
  - `POST /v1/authz-policies/local-admins/grants`
- Every Code local automation work-request routes:
  - `POST /v1/every-code/github-webhook`
  - `GET /v1/every-code/summary`
  - `GET /v1/previews/readiness`
  - `GET /v1/every-code/work-requests`
  - `GET /v1/every-code/work-requests/{request_id}`
  - `POST /v1/every-code/work-requests/create`
  - `POST /v1/every-code/work-requests/claim`
  - `POST /v1/every-code/work-requests/rerun`
  - `POST /v1/every-code/work-requests/status`
  - `GET /v1/every-code/pr-feedback`
  - `POST /v1/every-code/pr-feedback`
  - `POST /v1/every-code/pr-feedback/status`
  - `GET /v1/every-code/preview-gates`
  - `POST /v1/every-code/preview-gates`
- work graph chooser route:
  - `GET /v1/agent/context`
  - `GET /v1/repo-product-mapping`
  - `GET /v1/work-graph/snapshot`
  - `GET /v1/work-graph/merge-train/policy-targets`
  - `GET /v1/work-graph/merge-train/admission`
  - `GET /v1/work-graph/merge-train/controller/status`
  - `POST /v1/work-graph/rank`
  - `POST /v1/work-graph/merge-train/run-once`
  - `POST /v1/work-graph/merge-train/pr-feedback`
  - `POST /v1/work-graph/merge-train/controller/run-once`
- product driver routes:
  - `POST /v1/drivers/generic-web/deploy`
  - `POST /v1/drivers/generic-web/prod-promotion`
  - `POST /v1/drivers/generic-web/prod-promotion-workflow`
  - `POST /v1/drivers/generic-web/prod-rollback-plan`
  - `POST /v1/drivers/generic-web/prod-rollback`
  - `POST /v1/drivers/generic-web/stable-verification`
  - `POST /v1/drivers/generic-web/preview-desired-state`
  - `POST /v1/drivers/generic-web/preview-refresh`
  - `POST /v1/drivers/generic-web/preview-inventory`
  - `POST /v1/drivers/generic-web/preview-readiness`
  - `POST /v1/drivers/generic-web/preview-verification`
  - `POST /v1/drivers/generic-web/preview-destroy`
  - `POST /v1/drivers/odoo/artifact-publish-inputs`
  - `POST /v1/drivers/odoo/artifact-publish`
  - `POST /v1/drivers/odoo/target-replacement-apply`
  - `POST /v1/drivers/odoo/post-deploy`
  - `POST /v1/drivers/odoo/config-parameter-override`
  - `POST /v1/drivers/odoo/website-bootstrap-override`
  - `POST /v1/drivers/odoo/target-replacement-plan`
  - `POST /v1/drivers/odoo/target-replacement-apply`
  - `POST /v1/drivers/odoo/preview-apply-inputs`
  - `POST /v1/drivers/odoo/preview-apply`
  - `POST /v1/drivers/odoo/stable-verification`
  - `POST /v1/drivers/odoo/prod-backup-gate`
  - `POST /v1/drivers/odoo/prod-promotion`
  - `POST /v1/drivers/odoo/prod-rollback-plan`
  - `POST /v1/drivers/odoo/prod-rollback`
  - `POST /v1/drivers/verireel/testing-deploy`
  - `POST /v1/drivers/verireel/testing-verification`
  - `POST /v1/drivers/verireel/stable-environment`
  - `POST /v1/drivers/verireel/app-maintenance`
  - `POST /v1/drivers/verireel/prod-deploy`
  - `POST /v1/drivers/verireel/prod-backup-gate`
  - `POST /v1/drivers/verireel/prod-promotion`
  - `POST /v1/drivers/verireel/prod-rollback`
  - `POST /v1/drivers/verireel/preview-refresh`
  - `POST /v1/drivers/verireel/preview-inventory`
  - `POST /v1/drivers/verireel/preview-destroy`

Launchplane verifies GitHub OIDC, authorizes workflow identity claims, accepts
deployment/promotion/preview lifecycle evidence over HTTP, and executes the
current Odoo/VeriReel artifact, deploy, backup, promotion, rollback, maintenance,
and preview mutations as authenticated Launchplane routes. The authz policy
grant and removal routes accept GitHub Actions OIDC callers and authenticated
admin human sessions, require the `authz_policy_grant.write` action, and remain
the service-owned write/reload boundary for DB-backed GitHub Actions and GitHub
human policy rules. Terminal-agent, local-operator, and local-admin grant routes
use the same policy-admin boundary for DB-backed owner-agent rules. Grant and
removal requests support `dry_run` and `apply` modes. Apply requests must include
an audit reason and write a new active policy record only when the policy
changes, then immediately refresh the in-process policy used by the current
service worker. GitHub Actions removals match complete policy rules by exact
equality; partial selectors do not remove broader or narrower rules.
Responses return record metadata, rule counts, a compact diff, and redacted audit
metadata rather than echoing workflow refs, human logins, owner-agent subjects,
or the full policy body.

The service also serves the built operator UI shell at `/`, with `/ui` retained
as a compatibility alias. Built assets live under `/ui/assets/...`, while
`/ui/*` falls back to the app shell so the frontend can own client-side routes.
Versioned API ingress remains under `/v1`.

Validate the operator UI shell with browser navigation or `GET /ui`. Do not use
`HEAD /ui` as the only availability check, because static app-shell fallback
behavior can differ between request methods.

`POST /v1/every-code/github-webhook` is the only unauthenticated write route.
It trusts the request body through GitHub webhook HMAC verification instead of
OIDC. The route requires `X-Hub-Signature-256`, `X-GitHub-Delivery`, and
`X-GitHub-Event`, supports `issues.labeled` events for the `every-code` label,
and accepts pull-request `closed` events to terminalize linked Every Code work
requests. Other signed events, actions, or labels return `202` with
`skipped: true`. Matching issue-label deliveries create or return the durable
Every Code work request and include `deduped` plus the delivery id in the
response. Matching pull-request close deliveries can close every linked request
referenced by the PR, including still-queued requests that never stored a result
PR URL.

The Every Code worker read, claim, and status routes also accept a dedicated
local-worker bearer token. Configure `LAUNCHPLANE_EVERY_CODE_WORKER_TOKEN` on
the Launchplane service and on the Mac worker host, then run the worker with
`uv run launchplane every-code start --service-url https://...`. That token is
scoped in code to Every Code work-request, PR-feedback, preview-gate, preview
readiness routes, plus read-only `GET /v1/product-profiles` for preview
readiness projection. The worker can create service-owned PR feedback records
only for Launchplane-derived worker signals such as failed-check routing, while
human PR feedback still enters through the signed webhook path and trusted-actor
gate. It cannot create webhook requests, write product records, or access other
Launchplane service routes. This keeps remote DB credentials on the Launchplane
host while still allowing visible local Code/tmux work sessions to claim, rerun
terminal requests, reconcile preview state, route failed checks, and report
progress.

Local terminal agents that need Launchplane context use a separate read-only
bearer credential, not the browser OAuth session cookie and not
`LAUNCHPLANE_EVERY_CODE_WORKER_TOKEN`. Configure
`LAUNCHPLANE_TERMINAL_AGENT_READ_TOKEN` on the service and provide the same
secret to the trusted local terminal agent out of band. Optional
`LAUNCHPLANE_TERMINAL_AGENT_SUBJECT` and
`LAUNCHPLANE_TERMINAL_AGENT_TOKEN_LABEL` values identify the local owner subject
and token label used by `terminal_agents` authz policy rules; the defaults are
`local-owner-agent` and `local-owner-read`. The service only accepts this
identity on `GET` routes, so even an overly broad terminal-agent policy rule
cannot dispatch product config writes, prod promotion, destructive cleanup,
authz policy mutation, read-model POSTs, or plaintext secret reveal routes.
Policy still scopes which redacted read actions and product/context pairs the
agent can access, such as `product_environment.read` for product environment and
config-status diagnostics.

Trusted owner terminals that need to make Launchplane-owned operator mutations
without a browser session can use separate owner-agent bearer credentials.
Configure `LAUNCHPLANE_LOCAL_OPERATOR_TOKEN` on the service and provide the same
secret to trusted local agents through
`~/.config/launchplane/local-operator.env`. Optional
`LAUNCHPLANE_LOCAL_OPERATOR_SUBJECT` and
`LAUNCHPLANE_LOCAL_OPERATOR_TOKEN_LABEL` values identify the actor in audit and
idempotency records; the defaults are `local-owner-agent` and
`local-owner-write`. Routine owner-operator authority is DB-backed by
`local_operators` authz policy rules, scoped by subject, token label, product,
context, and action.

Rare owner-admin operations use `LAUNCHPLANE_LOCAL_ADMIN_TOKEN` with optional
`LAUNCHPLANE_LOCAL_ADMIN_SUBJECT` and `LAUNCHPLANE_LOCAL_ADMIN_TOKEN_LABEL`.
Those credentials are also DB-backed by exact `local_admins` authz policy rules;
the token alone does not grant blanket access. Owner-agent write requests must
include a non-empty `reason`; product-config apply is also rejected until the
service has recorded a matching owner-agent dry-run for the same payload. These
requests still use Launchplane records, redacted responses, runtime key-safety
policy, and managed secret storage. Terminal-agent credentials remain read-only.

`GET /v1/every-code/summary` returns a compact agent-safe projection of Every
Code work requests. It requires `every_code_work_request.read` for
product/context `launchplane` and supports `repository`, `issue_number`,
`state`, `limit`, and `offset` query parameters. Summary entries include source
links, state, summary status, claim metadata, timestamps, result PR URL, and
safe rerun guidance. They intentionally omit raw webhook delivery ids, error
messages, issue bodies, prompt text, local checkout paths, and local worker
hostnames. Entries include compact agent-context provenance and evidence so
callers can tell recorded request state apart from source-of-truth links.

`GET /v1/previews/readiness` returns a compact agent-safe projection of Every
Code preview gate records. It requires `every_code_preview_gate.read` for
product/context `launchplane` and supports `repository`, `pr_number`, `status`,
`limit`, and `offset` query parameters. Readiness items map gate records to
agent-facing states such as waiting on checks, ready, needs attention, or
cancelled, include source links and freshness/provenance, and avoid provider-only
internals, local paths, or secrets. Detail fields and check summaries are bounded
and redacted before they leave the read-model projection.

`POST /v1/work-graph/rank` ranks a caller-supplied work graph snapshot and
returns the queue payload under `result.queue`. The route requires the
`work_graph.rank` action for product/context `launchplane`, performs no storage
writes, and does not make Launchplane authoritative for copied GitHub or Code
Plans state.

`POST /v1/work-graph/merge-train/run-once` executes one policy-backed Level 1
ordered-queue pass for a requested repository/base branch. It requires the
`service_authz` action/product/context declared by the matching merge-train
repository policy, resolves its GitHub token from that policy's
`github_token.env_var`, and fails closed before GitHub calls when no matching
policy or token is available. The route is dry-run by default; `mutate: true`
applies at most one worker transition from one fresh snapshot. This route is the
deployed sequential baseline, not the full batch train target.

`POST /v1/work-graph/merge-train/pr-feedback` writes the public pull-request
feedback surface for train progress. It uses the same repository/base policy and
`service_authz` scope as `run-once`, resolves the same GitHub token, and creates
or updates one Launchplane-managed issue comment per PR using a hidden marker.
Accepted calls persist a `launchplane_merge_train_pr_feedback` record with the
rendered markdown, event, controller action metadata, delivery status, and
GitHub comment id/url. The route fails closed when authorization or token
configuration is missing; callers should use it for queued, waiting, blocked,
stale-policy, and completed transition summaries instead of writing ad hoc
comments from scheduler scripts.

`GET /v1/work-graph/merge-train/policy-targets` returns the authorized
repository/base-branch targets from the active DB-backed merge-train policy. It
performs no GitHub reads or mutations and is the source of truth for operator UI
target selection; callers should not infer merge-train targets from product
inventory or work-graph awareness items.

`GET /v1/work-graph/merge-train/admission` returns the stored-history scheduler
admission decision for a requested `repository` and `base_branch`. It uses the
same merge-train repository policy and `service_authz` scope as `run-once`, but
performs no GitHub reads and no storage writes. Schedulers use this route to
pace calls into `run-once`; execution still re-reads GitHub before any dry-run or
mutation.

`GET /v1/work-graph/merge-train/controller/status` returns the operator read
model for the same repository/base branch. It uses the same authorization as the
policy route, performs no GitHub reads, and composes stored scheduler admission,
latest Level 1 run history, active batch candidates, landing plans, and
stack-collapse plans. Only records that match the active repository policy key
and digest can drive the advertised controller action; stale records stay visible
with a stale reason. Operators can use this route to see the current controller
action, durable record ids, PR numbers, candidate SHA/check state, and compact
entry counts without invoking a worker mutation.

`POST /v1/work-graph/merge-train/controller/run-once` is the operator-facing
one-action controller for the full batch train. Request payloads name
`repository`, `base_branch`, and optional `mutate`; the route uses the same
policy, authorization, and GitHub token boundary as the lower-level merge-train
routes. Each call advances at most one safe phase from DB-backed records and
fresh GitHub evidence: plan stack collapse, execute stack collapse, admit the
collapsed root PR, plan/build/observe a batch candidate, plan landing, or land
the original PRs. Dry-run calls return the next controller action without
writing records or mutating GitHub. Mutation calls reuse the same persisted
candidate, stack-collapse, and landing-plan records as the phase-specific
routes, and reject stale policy digests before advancing stored records. The
response `result.controller_action` is the helper contract for retry/stop
behavior; see [merge-train-policy.md](merge-train-policy.md) for the action
matrix and public-safe reporting fields.

`POST /v1/work-graph/merge-train/batch-candidate/run-once` executes one
policy-backed batch-candidate phase for a requested repository/base branch. The
route accepts `mode: plan`, `mode: build`, or `mode: observe`. Plan mode reads a
fresh GitHub snapshot, derives one deterministic batch candidate from the
currently eligible queued PRs, and writes a
`launchplane_merge_train_batch_candidates` record. Build mode requires a prior
candidate record id, creates or resets the Launchplane train ref, merges queued
PR heads into that ref in order, and records the resulting candidate SHA. Observe
mode requires a prior candidate record id, reads required checks for that exact
candidate SHA, and records whether the candidate is still pending, passed, or
failed. The route never lands original PRs; PR-native landing remains a later
phase with separate records and pre-merge invariants.

`POST /v1/work-graph/merge-train/batch-landing/run-once` executes one
policy-backed batch-landing phase for a requested repository/base branch. The
route accepts `mode: plan` with a passed batch-candidate record id or
`mode: land` with a landing-plan record id. Plan mode writes a
`launchplane_merge_train_batch_landing_plans` record with the original PR order,
expected head SHAs, expected base SHA, and policy merge method. Land mode merges
the original PRs in that order through GitHub's PR merge endpoint, rejects stale
base-branch movement before merging, and relies on GitHub's SHA guard for each
PR head.

`.github/workflows/merge-train-runner.yml` is the first external scheduler for
this route. It mints a GitHub Actions OIDC token for the Launchplane service,
reads admission, and calls one worker entrypoint only when the decision is
`admitted`. Repository and base-branch selection come from workflow inputs or
repository variables, not service code. Manual dispatch defaults to dry-run mode;
scheduled runs also dry-run unless the repository variable
`LAUNCHPLANE_MERGE_TRAIN_MUTATE` is set to `true`. The default runner mode calls
the Level 1 `run-once` route. Manual dispatch or the scheduled repository
variable `LAUNCHPLANE_MERGE_TRAIN_RUNNER_MODE=controller` switches an admitted
pass to one full-controller `run-once` call instead. This keeps activation
explicit after setting `LAUNCHPLANE_MERGE_TRAIN_REPOSITORY`.
Controller-mode dry-runs do not deliver PR feedback comments; feedback delivery
is reserved for mutate runs and explicit manual phase workflows.
Workflow dispatches may select at most one non-`none` phase input across
batch-candidate, batch-landing, and stack-collapse modes; the runner validates
that exclusivity before any phase step mutates state.

`GET /v1/repo-product-mapping` returns the repository ownership/awareness read
model used by work graph and future agent context. The route requires
`product_environment.read` for product/context `launchplane`, performs no
writes, and distinguishes Launchplane-managed runtime repos from awareness-only
Every Code work-request repos. Managed runtime entries come from product profile
records and include product, contexts, stable environments, driver id, and
preview context; awareness entries do not imply Launchplane runtime ownership.

`GET /v1/work-graph/snapshot` returns the current Launchplane-assembled work
graph snapshot for the same authorization boundary. It composes product
overviews and Every Code work-request records into the typed snapshot contract.
When a caller-owned planning ingestion provider is configured, the route can
overlay compact GitHub/Code Plans facts. The first provider is opt-in via
`LAUNCHPLANE_WORK_GRAPH_PROJECT_OWNER` and
`LAUNCHPLANE_WORK_GRAPH_PROJECT_NUMBER`, reads `gh project item-list --format
json`, and supplies Project Focus, Manager, Finish Line, labels, status, updated
time, PR-vs-issue type, dependency counts, subissue counts, and PR check state.
`LAUNCHPLANE_WORK_GRAPH_PROJECT_SIGNAL_LIMIT` bounds the per-snapshot fan-out for
dependency, subissue, and check reads after Project items are loaded. The route
reports product, work-request, and planning-fact source counts, does not fetch or
store GitHub issue bodies, and writes no state. A configured Project or signal
read failure returns an error instead of a silently incomplete snapshot.
The production image includes `gh` for this provider, and deploy automation maps
the `LAUNCHPLANE_WORK_GRAPH_GH_TOKEN` repository secret into service `GH_TOKEN`
when present. Deploy automation also withholds the Project provider env bundle
until that secret exists, so prepared repo variables do not enable
unauthenticated runtime reads. That token is not a Launchplane record and must
remain outside the repo.

`GET /v1/agent/context` is a thin read-only aggregation endpoint for public-safe
skill preflight. It requires `product_environment.read` for product/context
`launchplane`, accepts an optional `repository` filter, and composes the existing
repo-product mapping, work graph snapshot, Every Code summary, and preview
readiness projections under named sections. Each section reports `available`,
`unauthorized`, or `unavailable`; optional work-graph planning provider failures
mark only that section unavailable instead of dropping the whole context or
silently omitting the failure. The endpoint writes no records, fetches no issue
bodies, and must preserve the lower-level redaction/provenance rules.

`POST /v1/work-graph/merge-train/run-once` is the authenticated service ingress
for one ordered-queue read or mutation pass. It resolves repository/base policy
from the active DB-backed `launchplane_merge_train_policies` record before
authorization, token lookup, or GitHub reads. If no active record exists, the
service fails closed with `merge_train_policy_not_configured`; policy creation
and updates are explicit record writes, not checked-in config files or
service-host env overrides. It authorizes against the policy's `service_authz`,
reads a fresh GitHub snapshot, and writes a `launchplane_merge_train_runs` record
for accepted dry-run and mutate calls. Mutation mode still applies at most one
worker transition from that snapshot.

The full batch train will use additional service contracts and records rather
than overloading `run-once`. Its target behavior is to build one combined
candidate from the base branch plus multiple queued PRs, run required checks on
that candidate commit, and then land the original PRs in queue order through
GitHub's normal PR merge path after pre-merge invariants are rechecked.
The first batch service contract is
`POST /v1/work-graph/merge-train/batch-candidate/run-once`; it owns only the
plan/build/observe candidate phases and writes
`launchplane_merge_train_batch_candidates` records.
The second batch service contract is
`POST /v1/work-graph/merge-train/batch-landing/run-once`; it owns landing-plan
creation and guarded PR-native landing, and writes
`launchplane_merge_train_batch_landing_plans` records.

`POST /v1/merge-train/policies/import` is the service-owned write path for merge
train policy records. It requires database storage and
`merge_train.policy_import` on product/context `launchplane`, accepts `dry_run`
and `apply`, and writes the supplied typed record only in apply mode.
Shared and production policy changes should use this route rather than direct DB
CLI writes from an arbitrary checkout.

## Host Assumption

- Launchplane runs behind an operator-owned HTTPS host.
- Launchplane exposes versioned API ingress under `/v1`.
- Launchplane returns JSON for both success and failure cases.

## Boundary Layers

```text
GitHub Actions workflow
  -> OIDC token from GitHub
  -> Launchplane HTTP ingress
  -> Launchplane authn/authz
  -> Launchplane core record/write logic
  -> Launchplane read models and driver hooks
```

## Authentication

Machine callers should authenticate with GitHub Actions OIDC.

Human browser callers authenticate with GitHub OAuth. Launchplane owns the
browser session after OAuth callback and sets an `HttpOnly`, `SameSite=Lax`
session cookie signed with `LAUNCHPLANE_SESSION_SECRET`. Sessions are backed by
the Launchplane database when `LAUNCHPLANE_DATABASE_URL` is configured. GitHub
access tokens stay server-side and are not exposed to the React operator UI.

Local terminal agents should use the dedicated terminal-agent read bearer token
when they only need redacted Launchplane context from a trusted operator shell.
This avoids copying browser session cookies into terminal processes and keeps
agent credentials independent from GitHub Actions OIDC and Every Code worker
automation.

Launchplane should verify:

- `iss` is GitHub's OIDC issuer
- `aud` matches Launchplane's expected audience
- signature validates against GitHub's published keys
- token is not expired and is valid for current time

Recommended audience:

- the Launchplane service host name

That keeps the audience tied to the Launchplane service identity instead of to a
temporary repo or local CLI name.

## Authorization Model

Launchplane should authorize machine callers from workflow identity claims,
not from human repo-admin status and not from copied long-lived service
tokens.

The first policy model should be allow-list based and fail closed.

### Claims Launchplane should rely on first

- `repository`
- `repository_owner`
- `workflow_ref`
- `job_workflow_ref` when reusable workflows are involved
- `ref`
- `ref_type`
- `event_name`
- `environment` when present
- `sub`
- `sha`

### Claims Launchplane should not treat as the primary authorization boundary

- `actor`
- `actor_id`
- human repo role such as admin/maintainer

Those are still useful for audit display, but the authorization decision should
primarily trust the workflow identity GitHub issued.

### First policy shape

Launchplane should map verified claims to a small policy rule set:

```text
rule
  - subject type: github-actions
  - repository match
  - workflow_ref or job_workflow_ref match
  - event_name match
  - environment/ref constraints
  - allowed product
  - allowed contexts
  - allowed actions
```

Example policy intent:

```text
repository: example-org/verireel
workflow_ref: example-org/verireel/.github/workflows/preview-control-plane.yml@*
event_name: pull_request
allowed product: verireel
allowed contexts: verireel-testing
allowed actions:
  - verireel_preview_refresh.execute
  - preview_generation.write
```

Another example:

```text
repository: example-org/verireel
workflow_ref: example-org/verireel/.github/workflows/preview-cleanup.yml@*
event_name: pull_request
allowed product: verireel
allowed contexts: verireel-testing
allowed actions:
  - preview_lifecycle.plan
  - preview_lifecycle.cleanup
  - verireel_preview_destroy.execute
  - preview_destroyed.write
```

Launchplane preview lifecycle sweep example:

```text
repository: example-org/launchplane
workflow_ref: example-org/launchplane/.github/workflows/preview-lifecycle.yml@refs/heads/main
event_name: schedule or workflow_dispatch
allowed products: all preview-enabled products
allowed contexts: each preview-enabled product preview context
allowed actions:
  - preview_lifecycle.plan
  - preview_lifecycle.cleanup
```

Stable-lane examples:

```text
repository: example-org/verireel
workflow_ref: example-org/verireel/.github/workflows/publish-image.yml@refs/heads/main
event_name: push or workflow_dispatch
allowed product: verireel
allowed contexts: verireel
allowed actions:
  - verireel_testing_deploy.execute
  - verireel_stable_environment.read
  - deployment.write
```

```text
repository: example-org/verireel
workflow_ref: example-org/verireel/.github/workflows/promote-image.yml@refs/heads/main
event_name: workflow_dispatch
allowed product: verireel
allowed contexts: verireel
allowed actions:
  - verireel_stable_environment.read
  - backup_gate.write
  - verireel_prod_deploy.execute
  - verireel_prod_promotion.execute
  - verireel_prod_rollback.execute
  - deployment.write
  - promotion.write
```

Odoo stable-lane example:

```text
repository: example-org/product-repo
workflow_ref: example-org/product-repo/.github/workflows/deploy-product.yml@refs/heads/main
event_name: workflow_dispatch
allowed product: odoo
allowed contexts: opw
allowed actions:
  - odoo_post_deploy.execute
  - odoo_prod_backup_gate.execute
  - odoo_prod_promotion.execute
  - odoo_prod_rollback.execute
```

The initial policy engine can be config-backed and static. It does not need a
full RBAC system yet.

Human policy rules use the same reviewed policy file under `github_humans`.
The first supported roles are `read_only` and `admin`. Browser sessions can
authorize read endpoints, but POST mutation routes remain GitHub Actions OIDC
only until browser-initiated mutation workflows get a dedicated CSRF and audit
design.

Agent consumers use the same allow-list policy but are classified into a compact
subject model before diagnostics or downstream intent contracts consume them:

- `github_actions`: workflow subjects from verified OIDC claims. Read actions
  are context-only; write, prod, destructive, secret-backed, and policy-admin
  actions require explicit matching policy grants for the workflow identity.
- `terminal_agent`: trusted local terminal agents authenticated by the dedicated
  read bearer token. These subjects are always read-only context consumers and
  cannot use POST routes, product mutations, authz policy changes, destructive
  cleanup, or secret-backed actions even if a policy rule is too broad.
- `local_operator`: trusted owner terminal agents authenticated by the dedicated
  write bearer token. These subjects can use only product-config plan/apply from
  a trusted shell with a required reason and matching dry-run before apply. They
  cannot call other mutation, destructive, production, secret-backed
  non-product-config, or authz policy routes.
- `github_human`: browser-session humans with `read_only` or `admin` role from
  GitHub human policy rules or bootstrap admin email matching. Read-only humans
  are `limited_remote_user` consumers: even if a rule is accidentally broad,
  Launchplane only allows read and safe-write action families for them, scoped by
  the exact repo/product/context/action rule. Admin humans are `human_admin`
  consumers and are approval-capable for future operator-mediated intent flows,
  but direct mutation routes still require their own CSRF/audit design before
  broad browser writes are allowed.

Action names are classified for agent context as `read`, `safe_write`,
`mutation`, `prod`, `destructive`, `secret_backed`, or `policy_admin`. This
classification is diagnostic and contractual; authorization still fails closed
through the exact action/product/context policy rule.

Agent-facing authorization diagnostics include an `agent_audit` envelope with
the decision, safe reason code, agent subject, action, product, context, policy
source, policy digest, and `authz_policy` source kind. Write-intent evaluations
persist that same provenance as `launchplane_agent_write_intents` records and
return the record id so later action routes can link to durable evidence.

`POST /v1/agent/write-intents/evaluate` is the first scoped intent surface. It
does not execute product/runtime mutations. It validates a requested intent,
maps it to the exact existing policy action, evaluates the caller's policy grant,
persists the evaluation record, and returns status, safe next action, source URL,
record id, and `agent_audit` metadata.
Agents can use it to preflight safe rerun, preview, config, cleanup, and
promotion-dispatch candidates without receiving a generic write token or reusable
credentials. Some intents, such as product config apply and promotion dry-run,
remain dry-run-first even when the caller has the underlying policy action.
Secret-backed intents may name managed secret binding keys plus a runtime
destination. Launchplane evaluates the existing runtime key-safety policy and an
additional `.secret` policy action such as `product_config.apply.secret`, then
returns sanitized binding keys, policy ids, and finding codes only. It never
returns plaintext secret values, ciphertext, provider env dumps, or token
prefixes to the agent.

For first access, `LAUNCHPLANE_BOOTSTRAP_ADMIN_EMAILS` may name comma-separated
verified GitHub email addresses that receive the `admin` role even before a
matching `github_humans` rule exists. The GitHub OAuth client requests
`user:email` so that this bootstrap path works for private profile emails.

## First API Surface

The first Launchplane service surface should focus on evidence ingress and record
writes, not on every possible operator action.

### Evidence ingress endpoints

- `POST /v1/evidence/deployments`
- `POST /v1/evidence/backup-gates`
- `POST /v1/evidence/promotions`
- `POST /v1/evidence/previews/generations`
- `POST /v1/evidence/previews/destroyed`

### Preview lifecycle endpoints

- `POST /v1/previews/lifecycle-plan`
- `POST /v1/previews/lifecycle-cleanup`
- `POST /v1/previews/desired-state`
- `POST /v1/previews/pr-feedback`

The first preview lifecycle endpoint remains the source of the durable decision:
Launchplane can discover desired preview anchors from GitHub PR label state,
record that desired-state scan, compare the anchors with the latest recorded
provider inventory scan, write a durable lifecycle plan, and return
keep/orphaned/missing sets. Cleanup execution uses a second endpoint that
requires an existing lifecycle `plan_id`; it defaults to `apply=false`
report-only behavior and records the cleanup request/result next to the plan.
Destructive provider cleanup is only attempted when `apply=true` is explicitly
supplied by an authorized GitHub Actions workflow.

PR feedback delivery is part of the same preview lifecycle boundary. Product
repos submit thin preview outcome facts to `POST /v1/previews/pr-feedback`;
Launchplane renders the review comment, upserts the anchored GitHub PR comment
when its runtime token is available, and stores an append-only feedback record
with the comment body, delivery action, comment URL, and any skip/failure reason.
Workflows can be granted explicit `preview_pr_feedback.write`, or generic-web
preview workflows can reuse their matching lifecycle grants: refresh-capable
workflows may report pending/ready/failed feedback, and destroy-capable workflows
may report destroyed/cleanup-failed feedback. Unsupported/fork notices still
require the explicit feedback grant because they are outside the normal
refresh/destroy flow.

### Product profile endpoints

- `GET /v1/product-profiles`
- `GET /v1/product-profiles/{product}`
- `POST /v1/product-profiles`

Product profiles are Launchplane-owned product/driver bindings. They are written
through authenticated service ingress and stored in Launchplane records; product
repos do not carry repo-local Launchplane lifecycle manifests.

Public ingress notification policy writes use
`POST /v1/public-ingress/notification-policies/apply`. The request carries
`mode: "dry-run"` or `mode: "apply"` and a complete
`PublicIngressNotificationPolicyRecord`. Apply requires
`public_ingress_notification_policy.apply`, DB-backed Launchplane storage, and
an idempotency key when a caller wants retry-safe service semantics. Local
operator calls must include a non-empty reason. Policies store routing intent and
managed secret record ids only; Discord webhook URLs, SMTP credentials, and
operator destination values must not be encoded in text-file defaults or source.

Product config writes use `POST /v1/product-config/apply`. The request carries
`mode: "dry-run"` or `mode: "apply"`, product/context/instance, non-secret
runtime values, and write-only managed secret values. Dry-run requires the
`product_config.plan` action; apply requires `product_config.apply`. The route
accepts GitHub Actions OIDC callers, signed-in GitHub human sessions, and the
dedicated local-operator bearer credential with a non-empty `reason`, but
terminal-agent read bearer credentials remain read-only and cannot execute the
mutation. Local-operator apply requests additionally require a previously
recorded matching local-operator dry-run. The route authorizes the top-level
product/context/instance target and rejects nested runtime or secret targets
that try to broaden or change that authorized target. It reuses the same
planner/writer as `launchplane product-config apply`, returns only actions,
keys, counts, actor/source metadata, and secret IDs, uses generic validation
messages for rejected requests, and fails closed when a secret bundle is
submitted without `LAUNCHPLANE_MASTER_ENCRYPTION_KEY` in the trusted Launchplane
runtime or without an active runtime key-safety policy that allows the requested
managed secret binding for the target runtime class. Request bodies for this
route must not be copied into logs, issues, docs, or workflow artifacts because
they can contain plaintext secret values.

#### Product-config secret source contract

The product-config apply route supports exactly one secret value source today:
write-only plaintext supplied inside the HTTPS request by an approval-capable
operator surface. The value is accepted only for the duration of request
processing, is written into Launchplane managed-secret storage on apply, and is
never returned. This source is appropriate for the signed-in operator UI and for
explicit local-owner operator automation that already holds private credentials
outside the repository.

Helpers and agents must not ask for, echo, persist, or pass plaintext secret
values through chat messages, CLI arguments, issue/PR bodies, workflow logs, or
helper output. For helper-driven product-config work, use this sequence instead:

1. Preflight intent and policy with `POST /v1/agent/write-intents/evaluate`,
   naming `intent: "product_config_apply"`, `mode: "dry_run"`, product/context,
   source URL, reason, optional `secret_bindings`, and the runtime destination.
2. Report only the returned status, reason code, record id, binding keys,
   runtime key-safety finding codes, trace id, and next action.
3. Hand off any new or changed plaintext secret value entry to the signed-in
   operator UI or to explicitly configured local-owner operator automation.
4. Call `POST /v1/product-config/apply` only when the caller has a safe private
   value source and explicit operator intent; dry-run before apply.

The service does not currently support committed secret references, provider env
lookups, stdin/stdout secret transport, arbitrary secret IDs supplied by an
agent, or a request shape that says "reuse the current managed secret value".
Those shapes are unsupported and must fail closed in clients instead of being
translated into a product-config request. Existing managed-secret binding keys
are safe to name only as metadata for intent evaluation, runtime key-safety
checks, and redacted reporting. They are not a plaintext source for this route.

Public-safe examples may show key names and placeholder binding keys, but never
real values:

```json
{
  "intent": "product_config_apply",
  "mode": "dry_run",
  "product": "example-product",
  "context": "example-testing",
  "source_url": "https://github.com/example/repo/issues/123",
  "reason": "Preflight managed secret-backed product config.",
  "secret_bindings": ["EXAMPLE_API_TOKEN"],
  "destination": {
    "kind": "runtime_environment",
    "context": "example-testing",
    "instance": "web"
  }
}
```

Product-config responses are public-safe only after redaction. Clients may
report `status`, `trace_id`, record ids, `result.mode`, runtime key names,
secret `binding_key` values, action names such as `created` or `unchanged`,
counts, `runtime_key_safety.status`, finding codes, and `next_actions`. Clients
must not report request bodies, runtime values, secret plaintext, ciphertext,
provider environment dumps, token prefixes, master-key env names from service
internals, or private hostnames.

Common failure classes are stable enough for helper summaries:

- `authentication_required`: no valid OIDC token, browser session, or allowed
  local-operator bearer credential.
- `authorization_denied`: caller lacks `product_config.plan`,
  `product_config.apply`, or the product/context grant.
- `reason_required`: local-operator calls omitted a concrete reason.
- `local_operator_dry_run_required`: local-operator apply did not match a prior
  recorded dry-run request.
- `secret_configuration_required`: trusted Launchplane runtime cannot write
  managed secrets.
- `runtime_key_safety_unavailable` or `runtime_key_safety_failed`: the active
  runtime key-safety policy is missing or rejects the requested binding.
- `invalid_request`: malformed payload, secret-shaped runtime key, or nested
  runtime/secret target override.

After apply, `next_actions` can require `live_target_runtime_apply`; helpers
should surface that action and stop. Applying product-config records does not by
itself guarantee the live target process has been synchronized.

Runtime key-safety policy reconciliation uses
`POST /v1/runtime-key-safety/policies/apply`. The route is restricted to
workflows with `runtime_key_safety.write` for product/context `launchplane`,
requires DB-backed storage, and writes metadata-only policy records for managed
runtime secret binding keys. It merges requested rules into the latest active
policy by binding key so deploy-time bootstrap can add required classifications
without dropping existing policy coverage. Request and response payloads must
not include secret plaintext.

Product onboarding uses `POST /v1/product-onboarding/apply`. The route accepts
the same operator-approved manifest as `launchplane product-onboarding apply`
and writes the full Launchplane-owned bundle: product profile, existing
Dokploy-backed target records, target-id records, runtime-environment records,
and managed secret binding placeholders. Manifests must use neutral
`provider_targets`; obsolete `dokploy_targets` input is rejected with a clear
validation error. The route is restricted to `product_onboarding.apply`
authority for product/context `launchplane`, requires DB-backed storage,
returns only sanitized `provider_target*` summaries, and exists so the
Launchplane seed import workflow can seed product records without product repos
storing live lifecycle truth.

Provider-target Phase Two operations use `POST /v1/provider-targets/operations`.
The route accepts one Launchplane-owned route at a time with mode `audit`,
`backfill-dry-run`, or `backfill-apply`, `provider_id`, `context`, `instance`,
and an apply-only `reason`. It requires DB-backed storage and authorizes through
`provider_target.audit` for audit/dry-run or `provider_target.backfill` for
apply, always scoped to product/context `launchplane`. Apply requests are
idempotency-keyed and write only complete non-conflicting Dokploy target/id
projections; existing rows and conflicts are reported rather than overwritten.
The manual `Provider Target Operations` workflow is the supported shared and
production caller for Phase Two backfill evidence.

Live target runtime sync uses `POST /v1/live-target-runtime/apply`. The route
accepts `mode: "dry-run"` or `mode: "apply"`, product/context/instance, and
optional apply-only deploy controls. Dry-run requires `live_target_runtime.plan`;
apply requires `live_target_runtime.apply`. The route resolves DB-backed runtime
environment records, managed runtime secrets, and the tracked Dokploy target in
the deployed Launchplane service, evaluates runtime key-safety policy, compares
desired and live env by key, and returns sanitized key/count evidence without
runtime values or secret plaintext. Apply updates only the product profile's
expected runtime environment keys and runtime managed-secret binding keys for
the selected lane, preserves unrelated live env, verifies persistence by key
metadata, and can explicitly trigger a deploy when requested.

Live target runtime applies are service-boundary work. Operators and agents must
not run local CLI live-target mutation commands from arbitrary checkouts to make
shared or production changes, because the local process may lack DB-backed
tracked target authority or use stale bootstrap context. Use the deployed
service route or a workflow that calls it so Launchplane can authorize with
OIDC/session identity, resolve current DB-backed target records in the deployed
runtime, and audit sanitized key/count evidence.

Generic web deploys use `POST /v1/drivers/generic-web/deploy`. The request names
the product, target instance, immutable artifact/image reference, and source ref;
Launchplane resolves the context from the DB-backed product profile lane and the
runtime target identity from DB-backed provider-target records. Dokploy target
records remain provider execution configuration for Dokploy-backed lanes and
must agree with the provider-target identity before deploy proceeds.
Product environment reads expose neutral provider-target identity only from
explicit provider-target rows. Paired DB-backed Dokploy target and target-id
records remain visible as provider-specific execution/history metadata and as
audit/backfill comparison material; they no longer synthesize current
provider-target authority when an explicit row is missing.

Generic web prod promotion can be exercised directly with
`POST /v1/drivers/generic-web/prod-promotion`; browser sessions may only use this
route with `dry_run=true`. The operator UI then uses
`POST /v1/drivers/generic-web/prod-promotion-workflow` to dispatch the
product-owned GitHub workflow configured by the DB-backed product profile. That
workflow remains responsible for product release/tag behavior while Launchplane
supplies authz, managed `GITHUB_TOKEN` lookup, dispatch inputs, and workflow-run
observation.

Generic web deploy and prod-promotion responses expose provider-neutral target
metadata with `target_category`, `provider_id`, and `provider_target_type`.
The legacy response-only `target_type` alias is retired; Dokploy execution
configuration still uses provider-specific target type fields internally where
application-vs-compose behavior is required.

Generic web preview desired-state discovery uses
`POST /v1/drivers/generic-web/preview-desired-state`. The request names the
product and optional pull-request label/page limit; Launchplane resolves the
repository, preview context, anchor repo, and preview slug template from the
DB-backed product profile before recording desired preview state.

Generic web preview refresh uses
`POST /v1/drivers/generic-web/preview-refresh`. The request names the product,
preview slug, and immutable image reference. Launchplane resolves the repository
and preview context from the DB-backed product profile, derives the canonical
live preview URL from the context-level `LAUNCHPLANE_PREVIEW_BASE_URL` runtime
environment record plus the preview slug, derives the anchor pull request from
the preview slug when possible, and records preview and generation evidence for
both successful and failed provider results. Product workflows may send
`anchor_pr_number`, `anchor_pr_url`, and `anchor_head_sha` when the preview slug
cannot be parsed from the configured slug template or when the workflow has more
precise anchor metadata than the image reference. `preview_url` remains accepted
as a compatibility override but is not the product-repo authority for new
workflows. Preview health failures that return Dokploy Dead Host are classified
as public preview ingress failures so workflow output and persisted generation
evidence point at DNS/ingress routing instead of a generic provider timeout.

Generic web preview inventory and destroy use
`POST /v1/drivers/generic-web/preview-inventory` and
`POST /v1/drivers/generic-web/preview-destroy`. They scan and delete stateless
Dokploy preview applications by the preview application-name prefix in the
DB-backed product profile. Lifecycle cleanup can dispatch to this generic path
only after a passing plan and a matching stored preview record are present.

### Operator read endpoints

- `GET /v1/products`
- `GET /v1/products/{product}`
- `GET /v1/products/{product}/activity`
- `GET /v1/products/{product}/environments`
- `GET /v1/products/{product}/environments/{environment}`
- `GET /v1/previews/{preview_id}`
- `GET /v1/previews/{preview_id}/history`
- `GET /v1/inventory/{context}/{instance}`
- `GET /v1/promotions/{record_id}`
- `GET /v1/deployments/{record_id}`
- `GET /v1/artifacts/protected`
- `GET /v1/contexts/{context}/secrets`
- `GET /v1/contexts/{context}/instances/{instance}/secrets`
- `GET /v1/secrets/{secret_id}`
- `GET /v1/contexts/{context}/operations/recent`
- `GET /v1/product-profiles/{product}/context-cutover-audit`

These operator reads use the same Launchplane authn/authz boundary as evidence
ingress. The intent is to give operators a minimal typed read surface for the
current Launchplane record nouns without forcing them to infer state from
workflow logs or host-local files. Secret status reads return metadata only:
Launchplane does not expose plaintext secret retrieval through the service
boundary.

Product/site reads use action `product_environment.read`. They compose
Launchplane-owned product profiles, driver descriptors, stable lane records,
preview summaries, runtime-environment key summaries, managed secret binding
metadata, action availability, and trust state. Raw context names and provider
target identifiers remain evidence metadata; runtime values, secret plaintext,
secret ciphertext, and product-specific driver payloads are not exposed as
shared top-level fields.

`GET /v1/products/{product}/environments` returns the product's stable
environment summaries from DB-backed Launchplane records. It is the collection
form of the per-product read model and is intended for operator and UI
navigation before loading a single environment detail page. It uses the same
redaction rules as the product overview: environment summaries include context,
URLs, action availability, trust state, and provenance, but not runtime values or
secret material.

`GET /v1/products/{product}/environments/{environment}/config-status` is a
redacted product/site read under the same action. It compares product-profile
expected runtime keys and managed secret bindings with recorded lane runtime
environment records and managed secret binding metadata. Expected keys describe
product intent; status is derived from records. The response exposes configured,
missing, or disabled status plus key/source metadata only; managed secret IDs
remain out of this readiness view.

Product activity reads are intentionally record-link oriented. They summarize
deployments, promotions, rollbacks, backup gates, preview identity/lifecycle,
preview feedback, and matching authz-policy changes with driver/action IDs and
record references rather than embedding raw record payloads.

Preview-related product actions are only shown when the product profile enables
previews. That includes generic-web preview discovery and inventory actions,
not just refresh and destroy operations.

Prod-scoped product actions are only shown when the product profile actually
defines a prod lane. Generic-web prod promotion is additionally hidden unless
the testing and prod lanes share the same context.

Product context cutover audit is read-only and uses `product_profile.read` for
the requested product in the Launchplane service context. It returns redacted
current-authority metadata for source, target, and optional preview contexts:
runtime key names, managed secret IDs/binding keys, Dokploy target metadata,
inventory and release tuple pointers, and append-only evidence counts. It does
not return runtime values, secret plaintext, secret ciphertext, or full provider
environment text.

Product context cutover apply uses `product_profile.write` for the requested
product in the Launchplane service context. It supports `dry-run` and `apply`
modes, copies only current-authority records into the target context, updates
lane/preview product profile context fields, and returns key names/counts only.
It does not copy append-only deployments, promotions, backup gates, or preview
history.

Product legacy context cleanup uses `product_profile.write` for the requested
product in the Launchplane service context. It supports `dry-run` and `apply`
modes after a context cutover has moved the product profile to the target
context. Cleanup refuses to run while the source context is still owned by this
or another product profile. It deletes legacy runtime environment records and
Dokploy target lookup records only when matching target-context records already
exist, disables legacy managed secret records and bindings, and preserves
inventory, release tuple, deployment, promotion, backup gate, and preview
history records as evidence. Responses remain redacted to key names, counts,
target metadata, secret IDs, and binding keys/status.

### Driver execution endpoints

These use the same authn/authz boundary as evidence ingress:

- `POST /v1/drivers/odoo/post-deploy`
- `POST /v1/drivers/odoo/artifact-publish`
- `POST /v1/drivers/odoo/target-replacement-apply`
- `POST /v1/drivers/odoo/prod-backup-gate`
- `POST /v1/drivers/odoo/prod-promotion`
- `POST /v1/drivers/odoo/prod-rollback`
- `POST /v1/drivers/generic-web/prod-promotion`
- `POST /v1/drivers/verireel/...`

The first driver route handlers now in service are admitted from descriptor
action route paths rather than a separate product-driver router allowlist. The
current handlers include:

- `POST /v1/drivers/odoo/post-deploy`
- `POST /v1/drivers/odoo/artifact-publish-inputs`
- `POST /v1/drivers/odoo/artifact-publish`
- `POST /v1/drivers/odoo/target-replacement-apply`
- `POST /v1/drivers/odoo/website-bootstrap-override`
- `POST /v1/drivers/odoo/prod-backup-gate`
- `POST /v1/drivers/odoo/prod-promotion`
- `POST /v1/drivers/odoo/prod-rollback`
- `POST /v1/drivers/generic-web/prod-promotion`
- `POST /v1/drivers/verireel/testing-deploy`
- `POST /v1/drivers/verireel/testing-verification`
- `POST /v1/drivers/verireel/stable-environment`
- `POST /v1/drivers/verireel/app-maintenance`
- `POST /v1/drivers/verireel/prod-deploy`
- `POST /v1/drivers/verireel/prod-backup-gate`
- `POST /v1/drivers/verireel/prod-promotion`
- `POST /v1/drivers/verireel/prod-rollback`
- `POST /v1/drivers/verireel/preview-refresh`
- `POST /v1/drivers/verireel/preview-inventory`
- `POST /v1/drivers/verireel/preview-destroy`

The product-neutral preview lifecycle route should become the common boundary
for preview desired/current-state comparison. Product-specific driver routes can
continue to perform provider runtime work, but repos should not each reimplement
orphan detection once they can submit desired preview anchors to Launchplane.

### Driver discovery endpoints

These are read-only endpoints for the provider-neutral driver descriptor and
read-model contract documented in [driver-descriptors.md](driver-descriptors.md):

- `GET /v1/drivers`
- `GET /v1/drivers/{driver_id}`
- `GET /v1/contexts/{context}/driver-view`
- `GET /v1/contexts/{context}/instances/{instance}/driver-view`
- `GET /v1/contexts/{context}/instances/{instance}/logs?lines=200`

They use action `driver.read`. Discovery authorizes against context
`launchplane`; context and instance views authorize against the requested
context. These routes expose Launchplane capabilities and repository-backed read
state, not runtime-provider primitives.

The logs route is the exception to the `driver.read` action because it reads live
provider output. It uses action `target_logs.read`, resolves DB-backed tracked
target records by context/instance, supports bounded Dokploy `application` logs,
and redacts likely secret values before returning lines.

The preview driver cut stays intentionally narrow but keeps topology in
Launchplane: Launchplane owns preview URL derivation from the
`LAUNCHPLANE_PREVIEW_BASE_URL` runtime-environment value, preview app naming,
runtime refresh, inventory, and teardown. VeriReel still owns image
build/publish, browser verification, and the follow-up preview evidence write.
Browser verification uses the preview URL returned by the driver plus
allow-listed app maintenance actions keyed by preview slug when it needs remote
owner-admin setup/cleanup.
When a VeriReel preview-refresh request is syntactically valid but Launchplane
cannot complete preflight because preview URL, runtime key-safety, managed
secret, Dokploy target, or template `DATABASE_URL` configuration is incomplete,
the route returns an accepted driver result with `refresh_status="fail"` and a
public-safe `error_message`. The same response writes failed preview generation
evidence so product workflows and PR feedback can surface the actionable reason
instead of a generic `invalid_request` rejection. Malformed request payloads and
unauthorized calls still fail closed before provider mutation.

VeriReel app maintenance accepts an optional `intent` alongside the allow-listed
action. Smoke/E2E helpers should set the narrow intent, such as
`remote-e2e-grant-sponsored`, `remote-e2e-delete-user`,
`owner-route-promote-owner`, or `owner-route-delete-user`, so Launchplane can
validate the requested action against the expected stable or preview lane before
it touches Dokploy schedules. The legacy action-only payload remains accepted
for compatibility, but new product calls should prefer the intent-based contract.

The first Odoo driver cuts are intentionally narrow as well: Launchplane owns the
artifact publish handoff, remote post-deploy data-workflow trigger, and prod
rollback for stable Odoo compose targets. Artifact publish resolves DB-backed
runtime records and managed secrets in Launchplane, invokes `odoo-devkit` as the
build engine with a one-shot runtime payload, validates the returned artifact
manifest, and writes it to Launchplane records. Post-deploy reads DB-backed Odoo
instance override records, renders the typed override payload, invokes the
Dokploy data-workflow runner, and writes `last_apply` evidence back to
Launchplane. Prod rollback reads DB-backed release tuples, artifact manifests,
and current promotion/inventory records, then delegates the provider mutation to
stable target replacement. That shared executor deploys the selected
artifact-backed image, injects runtime identity, runs post-deploy maintenance,
verifies health/canonical/logo evidence, and writes deployment/release-tuple
evidence. The rollback wrapper only adds rollback provenance to inventory and
the current prod promotion record. Local Odoo runtime commands remain in
`odoo-devkit`; these drivers are for remote control-plane execution only.

Privileged product rollback actions should use a narrow delegated-worker runtime
contract when they require network reach or host authority that does not belong
inside the main Launchplane API container.

Do not generalize the full driver surface before a few product-specific routes
have proven the shape.

## Request And Response Rules

- Requests and responses are JSON.
- Evidence endpoints should be idempotent from Launchplane's perspective when the
  same product/workflow submits the same stable identity twice.
- Launchplane should support an explicit idempotency key header for workflow
  retries.
- Launchplane should return durable record identifiers, not local file paths.
- Launchplane should include a request or trace id in every response.

Recommended first headers:

- `Authorization: Bearer <github_oidc_token>`
- `Content-Type: application/json`
- `Idempotency-Key: <stable-retry-key>`

The current Launchplane service implementation now honors `Idempotency-Key`
for all write routes. Launchplane replays the first successful accepted
response when the same authenticated workflow scope retries the same route
with the same key and the same request fingerprint. Launchplane rejects reuse
of the same key for a different payload on the same route.

Current VeriReel key shapes:

- preview generation: `preview-generation:<product>:<context>:<anchor_repo>:<pr_number>:<sha>`
- preview destroy: `preview-destroyed:<product>:<context>:<anchor_repo>:<pr_number>:<destroy_reason>`
- VeriReel preview refresh driver:
  `verireel-preview-refresh:<product>:<context>:<anchor_repo>:<pr_number>:<sha>`
- VeriReel preview destroy driver:
  `verireel-preview-destroy:<product>:<context>:<anchor_repo>:<pr_number>:<destroy_reason>`

For VeriReel, `destroy_reason` should stay stable per destroy lane so idempotent
retries do not collide. The regular cleanup workflow uses
`external_preview_cleanup_completed`; the janitor backstop uses
`external_preview_janitor_cleanup_completed`.

- testing deployment evidence: `testing-deployment:<product>:<context>:<instance>:<record_id>`
- prod deployment evidence: `prod-deployment:<product>:<context>:<instance>:<record_id>`
- prod promotion evidence: `prod-promotion:<product>:<context>:<from_instance>:<to_instance>:<record_id>`
- generic-web prod promotion driver:
  `generic-web-prod-promotion:<product>:<context>:<from_instance>:<to_instance>:<artifact_id>:<source_git_ref>`
- generic-web preview refresh driver:
  `generic-web-preview-refresh:<product>:<anchor_pr_number>:<sha>`
- generic-web preview destroy driver:
  `generic-web-preview-destroy:<product>:<anchor_pr_number>`
- Odoo isolated preview apply-inputs driver:
  `odoo-preview-apply-inputs:<product>:<preview_slug>:<sha>`
- Odoo isolated preview apply driver:
  `odoo-preview-apply:<product>:<preview_slug>:<operation>:<sha-or-destroy>`

Generic-web product workflow clients live in product repositories as thin
Launchplane callers until Launchplane provides a shared distributable helper.
Those clients must keep Launchplane lifecycle truth out of the product repo and
follow the shared request semantics: request a GitHub OIDC token with an explicit
timeout, apply bounded timeouts to Launchplane route calls, preserve HTTP status
and raw response bodies before attempting JSON parsing on failed responses, do
not retry `AbortError` or timeout aborts, and use stable idempotency keys for the
same product operation. Generic-web preview destroy keys intentionally omit the
free-form destroy reason so the same preview cleanup is idempotent even when the
caller wording changes between cleanup paths.

Odoo preview mutation routes intentionally use the isolated compose
planner/apply pair instead of product-shaped generic preview refresh/destroy
aliases. Product repos call `POST /v1/drivers/odoo/preview-apply-inputs` so
Launchplane derives the live preview URL, runtime bindings, target evidence,
and provider dry-run plan from DB-backed product profile preview configuration,
runtime-environment records, managed secrets, and tracked target records. Tenant
workflows then call `POST /v1/drivers/odoo/preview-apply` with the ready plan.

`POST /v1/drivers/odoo/preview-apply-inputs` is the Launchplane-owned handoff
between thin tenant preview workflows and isolated Odoo provider apply. The
caller supplies only product, PR number, image reference, source git ref, and
optional preview slug or URL override. Launchplane derives the generic preview
URL, runtime binding evidence, template compose id, Dokploy environment id,
Odoo runtime plan, and redacted Dokploy dry-run plan from DB-backed product
profile, runtime-environment, managed secret, and target records. For existing
preview refreshes and destroy planning, Launchplane discovers the per-PR Dokploy
compose from provider inventory and verifies the matching preview hostname
before it emits target evidence, so tenant workflows do not pass provider ids.
Ready responses can be posted to `POST /v1/drivers/odoo/preview-apply`; blocked
responses include planner blockers but never plaintext runtime values or secret
material. Destroy planning remains fail-closed when no matching preview compose
and hostname can be discovered.

`POST /v1/drivers/odoo/prod-promotion-inputs` is the stable-lane companion for
thin tenant prod promotion workflows. The caller supplies product, context, and
a request ID. Launchplane reads the current `testing` release tuple and artifact
manifest, then returns the artifact ID, source git ref, release tuple ID,
immutable image reference, and deterministic backup-gate record ID required by
the backup-gate and promotion routes. Blocked responses are not cached as
idempotent successes and explain which Launchplane record is missing, so a
tenant workflow does not have to accept hand-entered artifact or source facts.

`POST /v1/drivers/odoo/prod-promotion-run` is the preferred thin-workflow
mutation route for Odoo prod promotion. The tenant workflow supplies product,
context, and a stable request ID; Launchplane resolves the promotable testing
artifact, captures the prod backup gate, executes the testing-to-prod promotion,
and returns the phase statuses and written record IDs. The lower-level inputs,
backup-gate, and promotion routes remain available for diagnostics and explicit
operator workflows, but product repos should not own the chain.

The CM tenant preview workflow uses tenant-product scope for both artifact
publish input/evidence and preview lifecycle requests. Artifact publish still
uses Odoo driver routes, but source-ref build metadata resolves through the
`odoo-tenant-cm` product profile so Launchplane can return the tenant image
repository, tag, and lane-owned runtime payload. Deploy-maintained GitHub Actions
grants for `cbusillo/odoo-tenant-cm/.github/workflows/odoo-preview.yml` should
therefore include `odoo-tenant-cm` and context `cm` for publish input, publish
evidence, refresh, and destroy actions.

The CM preview workflow also needs `preview_pr_feedback.write` for product
`odoo-tenant-cm` and context `cm` before it can retire tenant-side preview
comment rendering. Normal refresh and destroy outcomes can reuse lifecycle
grants, but unsupported/fork and Dependabot notices sit outside those mutation
paths and require the explicit feedback grant. Fork and Dependabot notices run
from the base branch through `pull_request_target`, so the deploy-maintained
grant set includes both `pull_request` and `pull_request_target` feedback
writers for the same workflow file.

- VeriReel testing deploy driver:
  `verireel-testing-deploy:<product>:<context>:<instance>:<artifact_id>:<source_git_ref>`
- VeriReel testing verification driver:
  `verireel-testing-verification:<product>:<context>:<instance>:<deployment_record_id>`
- VeriReel prod deploy driver:
  `verireel-prod-deploy:<product>:<context>:<instance>:<artifact_id>:<source_git_ref>`
- VeriReel prod backup gate driver:
  `verireel-prod-backup-gate:<product>:<context>:<instance>:<backup_record_id>`
- VeriReel prod promotion driver:
  `verireel-prod-promotion:<product>:<context>:<from_instance>:<to_instance>:<artifact_id>:<source_git_ref>:<backup_record_id>:<promotion_record_id>:<expected_build_revision>:<expected_build_tag>`

The VeriReel prod-promotion request may include the primitive testing-lane
source health status. Launchplane normalizes that status and writes it into the
driver-owned promotion record; product workflows should not post a second
rendered promotion evidence payload for fields the driver can derive.

VeriReel deploy and prod-promotion responses expose provider-neutral target
metadata with `target_category`, `provider_id`, and `provider_target_type`.
The legacy response-only `target_type` alias is retired; Dokploy execution
configuration still uses provider-specific target type fields internally where
application-vs-compose behavior is required.

Recommended first success shape:

```json
{
  "status": "accepted",
  "trace_id": "launchplane_req_01jabc...",
  "records": {
    "preview_id": "preview-verireel-pr-123",
    "generation_id": "preview-verireel-pr-123-generation-0003"
  }
}
```

Recommended first error shape:

```json
{
  "status": "rejected",
  "trace_id": "launchplane_req_01jabc...",
  "error": {
    "code": "authorization_denied",
    "message": "Workflow cannot write preview evidence for verireel-testing."
  }
}
```

## First Stable Payloads

Launchplane should keep the typed evidence payloads already proven in the current
CLI adapters and expose them over HTTP.

### Preview generation evidence

Driver-owned preview verification routes can update those records without
requiring product workflows to render Launchplane record payloads directly.
For isolated Odoo preview provider applies,
`POST /v1/drivers/odoo/preview-apply` accepts the product plus a ready
`OdooPreviewDokployApplyRequest`, authorizes against
`odoo_preview_apply.execute` for the requested product/preview context, and
executes only through the Launchplane service. Callers do not supply plaintext
Odoo runtime env values for the live apply path. Launchplane resolves the
product preview template lane from DB-backed runtime-environment records and
managed secret overlays, then derives per-preview database and volume names from
the compose name before invoking the provider adapter. Responses return only
redacted step evidence, compose/domain identifiers, status, and error summaries.
If the service-side runtime contract is incomplete before any provider mutation,
the route returns `odoo_preview_runtime_config_incomplete` with the affected
context, instance, and missing key names only; it never returns runtime values or
secret material.
The route is idempotency-keyed and intended for approved non-production Odoo
preview targets while the isolated runtime migration is being exercised.

For
Odoo preview smoke follow-ups, `POST /v1/drivers/odoo/preview-verification`
remains a compatibility alias for the generic-web preview verification action.
It accepts the product, context, anchor repo/PR, `verification_status`,
`verified_at`, optional checked URLs as an explicit list plus
`timeout_seconds`, and an optional failure summary, then marks the latest preview
generation ready or failed. Scalar or object-shaped `checked_urls` payloads are
rejected. The accepted response includes an `odoo_preview_verification` result
with the generation identity, final states, status, checked URLs, timeout, and
failure summary. The route is safe-write evidence ingestion only; it does not
mutate provider state.

For stable smoke follow-ups,
`POST /v1/drivers/generic-web/stable-verification` accepts the product, context,
instance, deployment record, optional promotion record, checked URLs,
`verification_status`, `verified_at`, and optional failure summary. Launchplane
updates deployment health evidence and, when a promotion record is supplied,
promotion/inventory evidence. Product-shaped routes such as
`POST /v1/drivers/odoo/stable-verification` remain compatibility aliases. The
route is safe-write evidence ingestion only; it does not mutate provider state.

`POST /v1/evidence/previews/generations`

```json
{
  "product": "verireel",
  "preview": {
    "schema_version": 1,
    "context": "verireel-testing",
    "anchor_repo": "verireel",
    "anchor_pr_number": 123,
    "anchor_pr_url": "https://github.com/example-org/verireel/pull/123",
    "canonical_url": "https://pr-123.preview.example.com",
    "state": "active",
    "updated_at": "2026-04-16T08:10:00Z",
    "eligible_at": "2026-04-16T08:10:00Z"
  },
  "generation": {
    "schema_version": 1,
    "context": "verireel-testing",
    "anchor_repo": "verireel",
    "anchor_pr_number": 123,
    "anchor_pr_url": "https://github.com/example-org/verireel/pull/123",
    "anchor_head_sha": "6b3c9d7e8f901234567890abcdef1234567890ab",
    "state": "ready",
    "requested_reason": "external_preview_refresh",
    "requested_at": "2026-04-16T08:02:00Z",
    "ready_at": "2026-04-16T08:10:00Z",
    "finished_at": "2026-04-16T08:10:00Z",
    "resolved_manifest_fingerprint": "verireel-preview-manifest-pr-123-6b3c9d7",
    "artifact_id": "ghcr.io/example-org/verireel-app:pr-123-6b3c9d7",
    "deploy_status": "pass",
    "verify_status": "pass",
    "overall_health_status": "pass"
  }
}
```

### Preview destroyed evidence

`POST /v1/evidence/previews/destroyed`

```json
{
  "product": "verireel",
  "destroy": {
    "schema_version": 1,
    "context": "verireel-testing",
    "anchor_repo": "verireel",
    "anchor_pr_number": 123,
    "destroyed_at": "2026-04-16T09:04:00Z",
    "destroy_reason": "external_preview_cleanup_completed"
  }
}
```

The deployment and promotion endpoints should follow the same pattern: stable
typed record payloads inside a Launchplane-owned API envelope.

### Runner host hygiene audit evidence

`POST /v1/evidence/runner-host-hygiene/audits`

Request payload:

```json
{
  "schema_version": 1,
  "product": "launchplane",
  "audit": {
    "schema_version": 1,
    "audit_record_key": "runner-host-hygiene/2026-05-23/chris-testing",
    "status": "planned",
    "request": "RunnerHostHygieneApplyRequest",
    "plan": "RunnerHostHygieneApplyPlan",
    "pre_apply_report": "RunnerHostHygieneReport",
    "post_apply_report": null,
    "message": "planned runner host hygiene apply; no host mutation was executed"
  }
}
```

The route requires `runner_host_hygiene_audit.write` for product/context
`launchplane/launchplane`, writes the typed audit record to Launchplane-owned
storage, and returns the `runner_host_hygiene_audit_record_key`. It is evidence
ingress only: it records planned, completed, or failed audit facts supplied by a
future approved executor, but it does not mutate runner hosts itself.

For VeriReel's first stable-lane Launchplane slice, use context `verireel` for the
long-lived `testing` and `prod` instances. Preview evidence remains separate
under `verireel-testing` because previews are not another durable promotion
lane.

## CLI Relationship

Current commands such as:

- `control-plane launchplane-previews write-from-generation`
- `control-plane launchplane-previews write-destroyed`

should be treated as temporary compatibility clients of these Launchplane payloads.
They should not remain the permanent integration boundary for external product
workflows.

## Driver Relationship

The first Launchplane API should separate two concerns:

- evidence ingress into Launchplane core records
- runtime execution through Launchplane-owned drivers

That keeps the initial service slice small while still allowing a later shift
from repo-owned operational scripts into Launchplane-owned driver execution.

The first explicit drivers should be:

- Odoo driver
- VeriReel driver

Repo-specific variation should stay thin and declarative where possible.

## Out Of Scope For This First Slice

- full human/operator auth design
- multi-tenant billing or quota models
- generalized plugin marketplace design
- replacing file-backed storage immediately
- moving every current CLI command behind HTTP at once

## Recommended Next Implementation Steps

1. Convert the existing CLI preview evidence commands into local clients of the
   same service-layer handler or payload contract.
2. Add local clients for deployment and promotion evidence where Launchplane-facing
   workflows still write through repo-local CLI adapters.
3. Define the first explicit Odoo and VeriReel driver interfaces after the
   service ingress exists.
