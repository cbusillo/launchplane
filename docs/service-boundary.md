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

The current repo-local CLI and file-backed state directory are implementation
scaffolding. This document defines the boundary those adapters should converge
on.

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
- authenticated evidence routes:
  - `POST /v1/evidence/backup-gates`
  - `POST /v1/evidence/deployments`
  - `POST /v1/evidence/promotions`
  - `POST /v1/evidence/previews/generations`
  - `POST /v1/evidence/previews/destroyed`
- product profile routes:
  - `GET /v1/product-profiles`
  - `GET /v1/product-profiles/{product}`
  - `POST /v1/product-profiles`
- product config write route:
  - `POST /v1/product-config/apply`
- product onboarding route:
  - `POST /v1/product-onboarding/apply`
- product context cutover route:
  - `POST /v1/product-profiles/context-cutover/apply`
- product legacy context cleanup route:
  - `POST /v1/product-profiles/legacy-context-cleanup/apply`
- authz policy maintenance route:
  - `POST /v1/authz-policies/github-actions/grants`
  - `POST /v1/authz-policies/github-humans/grants`
  - `POST /v1/authz-policies/terminal-agents/grants`
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
  - `GET /v1/work-graph/merge-train/admission`
  - `POST /v1/work-graph/rank`
  - `POST /v1/work-graph/merge-train/run-once`
- product driver routes:
  - `POST /v1/drivers/generic-web/deploy`
  - `POST /v1/drivers/generic-web/prod-promotion`
  - `POST /v1/drivers/generic-web/prod-promotion-workflow`
  - `POST /v1/drivers/generic-web/preview-desired-state`
  - `POST /v1/drivers/generic-web/preview-refresh`
  - `POST /v1/drivers/generic-web/preview-inventory`
  - `POST /v1/drivers/generic-web/preview-readiness`
  - `POST /v1/drivers/generic-web/preview-destroy`
  - `POST /v1/drivers/odoo/artifact-publish-inputs`
  - `POST /v1/drivers/odoo/artifact-publish`
  - `POST /v1/drivers/odoo/post-deploy`
  - `POST /v1/drivers/odoo/prod-backup-gate`
  - `POST /v1/drivers/odoo/prod-promotion`
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
grant routes accept GitHub Actions OIDC callers and authenticated admin human
sessions, require the `launchplane_service_deploy.execute` action, and remain
the service-owned write/reload boundary for DB-backed GitHub Actions and GitHub
human policy rules. The terminal-agent grant route uses the same boundary for
DB-backed terminal-agent read rules. Grant requests support `dry_run` and
`apply` modes. Apply requests must include an audit reason, write a new active
policy record only when the grant is not already present, and immediately
refresh the in-process policy used by the current service worker. Responses
return record metadata, rule counts, a compact diff, and redacted audit metadata
rather than echoing workflow refs, human logins, or the full policy body.

The service also serves the built operator UI shell at `/`, with `/ui` retained
as a compatibility alias. Built assets live under `/ui/assets/...`, while
`/ui/*` falls back to the app shell so the frontend can own client-side routes.
Versioned API ingress remains under `/v1`.

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

`POST /v1/work-graph/merge-train/run-once` executes one policy-backed
merge-train pass for a requested repository/base branch. It requires the
`service_authz` action/product/context declared by the matching merge-train
repository policy, resolves its GitHub token from that policy's
`github_token.env_var`, and fails closed before GitHub calls when no matching
policy or token is available. The route is dry-run by default; `mutate: true`
applies at most one worker transition from one fresh snapshot.

`GET /v1/work-graph/merge-train/admission` returns the stored-history scheduler
admission decision for a requested `repository` and `base_branch`. It uses the
same merge-train repository policy and `service_authz` scope as `run-once`, but
performs no GitHub reads and no storage writes. Schedulers use this route to
pace calls into `run-once`; execution still re-reads GitHub before any dry-run or
mutation.

`.github/workflows/merge-train-runner.yml` is the first external scheduler for
this route. It mints a GitHub Actions OIDC token for the Launchplane service,
reads admission, and calls `run-once` only when the decision is `admitted`.
Repository and base-branch selection come from workflow inputs or repository
variables, not service code. Manual dispatch defaults to dry-run mode; scheduled
runs also dry-run unless the repository variable `LAUNCHPLANE_MERGE_TRAIN_MUTATE`
is set to `true`. This keeps activation explicit after setting
`LAUNCHPLANE_MERGE_TRAIN_REPOSITORY`.

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
for one merge-train read or mutation pass. It resolves repository/base policy
before authorization, token lookup, or GitHub reads, authorizes against the
policy's `service_authz`, reads a fresh GitHub snapshot, and writes a
`launchplane_merge_train_runs` record for accepted dry-run and mutate calls.
Mutation mode still applies at most one worker transition from that snapshot.

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

Janitor backstop example:

```text
repository: example-org/verireel
workflow_ref: example-org/verireel/.github/workflows/preview-janitor.yml@refs/heads/main
event_name: schedule or workflow_dispatch
allowed product: verireel
allowed contexts: verireel-testing
allowed actions:
  - preview_lifecycle.plan
  - preview_lifecycle.cleanup
  - verireel_preview_destroy.execute
  - preview_destroyed.write
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

Product config writes use `POST /v1/product-config/apply`. The request carries
`mode: "dry-run"` or `mode: "apply"`, product/context/instance, non-secret
runtime values, and write-only managed secret values. Dry-run requires the
`product_config.plan` action; apply requires `product_config.apply`. The route
accepts GitHub Actions OIDC callers and signed-in GitHub human sessions, but
terminal-agent bearer credentials remain read-only and cannot execute the
mutation. The route authorizes the top-level product/context/instance target and
rejects nested runtime or secret targets that try to broaden or change that
authorized target. It reuses the same planner/writer as
`launchplane product-config apply`, returns only actions, keys, counts,
actor/source metadata, and secret IDs, uses generic validation messages for
rejected requests, and fails closed when a secret bundle is submitted without
`LAUNCHPLANE_MASTER_ENCRYPTION_KEY` in the trusted Launchplane runtime or without
an active runtime key-safety policy that allows the requested managed secret
binding for the target runtime class. Request bodies for this route must not be
copied into logs, issues, docs, or workflow artifacts because they can contain
plaintext secret values.

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
and writes the full Launchplane-owned bundle: product profile, Dokploy target
records, target-id records, runtime-environment records, and managed secret
binding placeholders. It is restricted to Launchplane service deploy authority,
requires DB-backed storage, returns only sanitized summaries, and exists so the
Launchplane deploy workflow can seed product records without product repos
storing live lifecycle truth.

Live target runtime sync uses `POST /v1/live-target-runtime/apply`. The route
accepts `mode: "dry-run"` or `mode: "apply"`, product/context/instance, and
optional apply-only deploy controls. Dry-run requires `live_target_runtime.plan`;
apply requires `live_target_runtime.apply`. The route resolves DB-backed runtime
environment records, managed runtime secrets, and the tracked Dokploy target in
the deployed Launchplane service, evaluates runtime key-safety policy, compares
desired and live env by key, and returns sanitized key/count evidence without
runtime values or secret plaintext. Apply updates only the Launchplane-owned
keys on the live target, preserves unrelated live env, verifies persistence by
key metadata, and can explicitly trigger a deploy when requested.

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
runtime target from DB-backed Dokploy target records.

Generic web prod promotion can be exercised directly with
`POST /v1/drivers/generic-web/prod-promotion`; browser sessions may only use this
route with `dry_run=true`. The operator UI then uses
`POST /v1/drivers/generic-web/prod-promotion-workflow` to dispatch the
product-owned GitHub workflow configured by the DB-backed product profile. That
workflow remains responsible for product release/tag behavior while Launchplane
supplies authz, managed `GITHUB_TOKEN` lookup, dispatch inputs, and workflow-run
observation.

Generic web preview desired-state discovery uses
`POST /v1/drivers/generic-web/preview-desired-state`. The request names the
product and optional pull-request label/page limit; Launchplane resolves the
repository, preview context, anchor repo, and preview slug template from the
DB-backed product profile before recording desired preview state.

Generic web preview refresh uses
`POST /v1/drivers/generic-web/preview-refresh`. The request names the product,
preview slug, preview URL, and immutable image reference; Launchplane resolves
the repository and preview context from the DB-backed product profile, derives
the anchor pull request from the preview slug when possible, and records preview
and generation evidence for both successful and failed provider results. Product
workflows may send `anchor_pr_number`, `anchor_pr_url`, and `anchor_head_sha`
when the preview slug cannot be parsed from the configured slug template or when
the workflow has more precise anchor metadata than the image reference. Preview
health failures that return Dokploy Dead Host are classified as public preview
ingress failures so workflow output and persisted generation evidence point at
DNS/ingress routing instead of a generic provider timeout.

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
- `GET /v1/products/{product}/environments/{environment}`
- `GET /v1/previews/{preview_id}`
- `GET /v1/previews/{preview_id}/history`
- `GET /v1/inventory/{context}/{instance}`
- `GET /v1/promotions/{record_id}`
- `GET /v1/deployments/{record_id}`
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
target records, and current inventory, deploys the selected artifact-backed
image, verifies health, and writes durable rollback/deployment/inventory/release
tuple evidence. Local Odoo runtime commands remain in `odoo-devkit`; these
drivers are for remote control-plane execution only.

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
- Odoo preview refresh driver:
  `odoo-preview-refresh:<product>:<anchor_pr_number>:<sha>`
- Odoo preview destroy driver:
  `odoo-preview-destroy:<product>:<anchor_pr_number>`

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

Odoo preview routes intentionally reuse the generic-web preview request schema,
profile resolver, and record writer. Product repos call the Odoo-shaped routes
so authz and driver views remain product-specific, while Launchplane still writes
the shared preview and preview-generation records from DB-backed product profile
preview configuration.

The CM tenant preview workflow uses two product scopes deliberately. Artifact
publish input and publish evidence requests use product `odoo` for context `cm`,
because the publish handoff is an Odoo driver contract. Preview refresh and
destroy requests use product `odoo-tenant-cm`, because preview lifecycle records
and product profile configuration are tenant-product scoped. Deploy-maintained
GitHub Actions grants must include both scopes for
`cbusillo/odoo-tenant-cm/.github/workflows/odoo-preview.yml`; granting only the
tenant product lets preview mutation through but blocks the earlier build input
resolution step.

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
