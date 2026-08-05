---
title: Reusable Preview Workflow Contract
---

## Purpose

Product preview workflows should be thin event adapters. Product repos build and
publish the product artifact; Launchplane owns preview event semantics,
idempotency, provider mutation, durable records, unsupported notices, feedback
comments, inventory, and cleanup.

This contract is the migration target for preview-enabled product repos that
currently carry bespoke preview-control-plane workflows.

## Ownership Boundary

Product repos own:

- GitHub event wiring for `pull_request`, `pull_request_target`, and manual
  `workflow_dispatch` entrypoints.
- Product build, test, package, and immutable artifact publishing.
- Product-specific smoke facts that Launchplane cannot derive from a product
  profile.
- The minimal Launchplane request payload containing product key, PR number,
  source SHA, immutable artifact reference, run URL, and primitive smoke result.

Launchplane owns:

- Preview-request interpretation and idempotency conventions.
- Preview refresh, destroy, inventory, readiness, and lifecycle cleanup routes.
- Scheduled lifecycle sweeps for every product profile where
  `preview.enabled=true`.
- Preview records, generation records, desired-state records, cleanup records,
  and PR feedback records.
- Unsupported notices for fork and Dependabot preview requests.
- PR comment rendering, delivery, replacement, and cleanup.
- Provider credentials, runtime settings, managed secrets, preview URL policy,
  and preview slug policy.

Product workflows must not render Launchplane preview comments directly or keep
their own durable lifecycle truth once a matching Launchplane route exists.

## Event Contract

Use `PreviewWorkflowEvent` and `decide_preview_workflow_operation` from
`control_plane.contracts.preview_workflow_contract` as the shared event contract.
The decision is deliberately small:

- `refresh`: build and publish the product artifact, then call the product
  driver's preview-refresh route.
- `destroy`: call the product driver's preview-destroy route.
- `unsupported_notice`: call `POST /v1/previews/pr-feedback` with
  `status="unsupported"` from a trusted base-branch workflow.
- `ignore`: make no Launchplane mutation.

The same contract applies to SYO, VeriReel, Odoo CM, and future products. Driver
routes may differ by product family, but event interpretation stays the same.

Product repo adapters can ask Launchplane to render that decision from a GitHub
event payload before doing product-owned work:

```shell
uv run launchplane work-graph preview-workflow-decision \
  --event-file "$GITHUB_EVENT_PATH" \
  --event-name "$GITHUB_EVENT_NAME" \
  --product sell-your-outboard \
  --context sellyouroutboard-testing \
  --run-id "$GITHUB_RUN_ID" \
  --run-attempt "$GITHUB_RUN_ATTEMPT"
```

The command is read-only. It does not contact GitHub, call Launchplane service
routes, check out pull-request code, or mutate a provider. It emits the normalized
event, decision, route path, feedback status, and run-scoped idempotency key as
JSON so product workflows can branch on the shared contract instead of
duplicating event semantics.

Same-repository preview build jobs that need an importable helper for GitHub
event labels and image tags should use
`cbusillo/launchplane/.github/actions/setup-preview-prepare-client@<launchplane-sha>`.
The
generated client is read-only and product-agnostic: callers pass the current
repository, head repository, PR author, PR number, source SHA, image name,
labels, and run URL; the client returns refresh/unsupported/noop mode, same-repo
support flags, `pr-<number>` image tags, and full image references. It does not
call Launchplane, choose provider targets, render comments, derive preview URLs,
or store lifecycle truth.

Once the product workflow has decided to refresh, destroy, or send an
unsupported notice, it should hand off to Launchplane's reusable workflow instead
of constructing route payloads locally:

```yaml
jobs:
  launchplane-preview:
    uses: cbusillo/launchplane/.github/workflows/reusable-generic-web-preview-lifecycle.yml@<launchplane-sha>
    with:
      operation: refresh
      anchor_pr_number: ${{ github.event.pull_request.number }}
      anchor_pr_url: ${{ github.event.pull_request.html_url }}
      anchor_head_sha: ${{ github.event.pull_request.head.sha }}
      image_reference: ${{ needs.build.outputs.image_digest }}
```

The reusable workflow derives the product key from the caller repository by
default, derives a run-scoped idempotency key, calls the correct Launchplane
route, and exposes the returned preview slug, preview URL, refresh status,
destroy status, or feedback status as job outputs. Callers should omit preview
context so Launchplane can derive it from the product profile before
authorization and recording. The lifecycle boundary normalizes an omitted or
empty driver timeout to 300 seconds before constructing refresh or destroy
requests. It does not accept `preview_slug`, `preview_url`, provider target ids,
feedback markdown, or idempotency keys as caller inputs.
For generic-web application previews, `preview.domain_certificate_type="none"`
means TLS terminates at the edge ingress: Launchplane creates the Dokploy domain
on HTTP only, while the public preview URL remains HTTPS. `"letsencrypt"`
instead makes Dokploy terminate TLS for that domain. This keeps one TLS owner
per preview route and avoids publishing an inner TLS route when Dokploy has no
certificate to serve.

The generic-web preview-refresh route may return `200` or `202` for a successful
provider mutation. The reusable lifecycle worker accepts both responses and
still validates the returned refresh and readiness statuses. When a generic-web
refresh receives `403`, the lifecycle worker makes one
same-identity call to the redacted GitHub Actions authz diagnostic route before
failing the original refresh. That diagnostic still requires a separate,
narrow `authz_diagnostic.evaluate` grant; without it, the workflow retains the
ordinary generic denial. Its response contains only selector categories and
opaque fingerprints, never policy selectors or other principal rules.

When the stored product profile declares `preview.data_transport_mode="driver"`,
the generic-web lifecycle route delegates refresh and destroy execution to the
registered product-driver extension. This keeps product-specific database,
secret, migration, seed, and cleanup behavior inside Launchplane while the
product workflow remains a thin reusable-workflow caller. Missing driver
extensions fail closed instead of falling back to generic environment copying.

Preview comment updates that are not part of the lifecycle workflow use
`cbusillo/launchplane/.github/workflows/reusable-preview-pr-feedback.yml@<launchplane-sha>`.
Callers provide only primitive display facts such as PR number, status, preview
URL, image references, revision, run URL, and failure summary. The reusable
feedback workflow owns the `/v1/previews/pr-feedback` route, marker, payload
shape, delivery behavior, and run-scoped idempotency key. Callers may omit
preview context; Launchplane derives it from the product profile before
authorization and recording.

Product workflows that still need to translate local job results into a preview
feedback `status` and `failure_summary` should call
`cbusillo/launchplane/.github/workflows/reusable-preview-feedback-status.yml@<launchplane-sha>`.
The reusable workflow maps refresh publish, provision, and product verification
results to `ready` or `failed`, and maps cleanup results to `destroyed` or
`cleanup_failed`, before delegating to the reusable preview feedback workflow. It
does not accept route paths, payloads, markers, idempotency keys, feedback
markdown, provider targets, or runtime facts from callers.

## Required Workflow Shape

Same-repository PR refresh uses `pull_request` because the workflow may check
out and build the PR head:

- `opened`, `reopened`, `synchronize`, `edited`, or adding the preview label
  with the label present: `refresh`.
- Any PR without the preview label: `ignore`.

Same-repository preview cleanup uses `pull_request_target`: closing the PR or
removing the preview label runs `destroy` from the base-branch workflow. This
gives destructive cleanup an exact GitHub OIDC `workflow_ref`; it does not
check out or execute pull-request code. Fork and Dependabot PRs use the same
trusted workflow only for unsupported or cleared notices. The job must call
`cbusillo/launchplane/.github/workflows/reusable-preview-request-notice.yml@<launchplane-sha>`.
The reusable workflow owns the trusted event decision, cleanup lifecycle and
feedback handoff, unsupported/cleared status selection, and failure summary.
Product repos must not check out code, choose a checkout ref, render feedback
markdown, build request payloads, or call `POST /v1/previews/pr-feedback`
directly from their own trusted notice workflows.

Manual `workflow_dispatch` may request `refresh` or `destroy` when a product repo
needs an operator retry path. Manual refresh still follows the same build,
publish, and Launchplane-refresh handoff as a PR refresh.

## Manager Preview Approval

Manager approval is a Launchplane-owned interaction layered on the serving
preview evidence. Product workflows do not parse approval comments, resolve a
person, authorize an actor, compute fingerprints, or write GitHub status. The
signed webhook handler does not check out or execute pull-request code.

When an active managed policy grants `manager_preview_approval.write` for the
product and preview context, Launchplane maintains one credential-owned PR
comment containing the public preview URL, immutable serving identity, current
decision, and these exact role-based commands:

```text
/preview approve <binding_sha256>
/preview changes <binding_sha256> <reason>
/preview revoke <binding_sha256> <reason>
```

The trusted status context is exactly `manager-preview-approval`. It is pending
without an exact approval, successful only for the current head and serving
generation, and non-successful for changes requested, revocation, stale or
unavailable evidence, verification failure, destroy, PR close, preview-label
removal, or authorization-policy drift. Required code-review approvals remain a
separate repository rule and may remain zero.

Stale approval history on an older binding does not prevent a new exact manager
decision for complete current evidence. The webhook accepts the current
fingerprint only while the pull request is open, its head matches the serving
generation, and the exact current binding has not been superseded or invalidated.
Unavailable evidence and exact terminal bindings remain non-actionable.

When a recorded destroy or supersession ends the prior serving binding, a later
verified replacement generation starts pending for its own exact fingerprint.
The terminal event remains append-only audit evidence, but it does not carry a
stale decision forward onto the replacement generation.

Preview refresh, destroy, and verification must never depend on manager
approval. They persist their own lifecycle evidence first, then attempt status
reconciliation. If GitHub is degraded, the lifecycle operation still completes
and an authorized operator retries `POST /v1/manager-preview-approval/reconcile`
with `repository` and `pr_number` after GitHub recovers.

The broader `tenant-admission` status is a separate Launchplane projection. For
a repository classified as `tenant_ui`, it recomputes the exact current
candidate and succeeds when manager preview approval, a technical human waiver,
or trusted-maintenance evidence is satisfied. It does not replace or weaken the
manager-preview binding rules. Preview refresh, verification, destroy, and
cleanup do not wait for either status, and projection delivery failure never
rolls back a completed lifecycle mutation.

Generic-web routes write lifecycle records as part of refresh, destroy, and
verification. Odoo preview apply finalizes the serving preview and generation
evidence after the provider result is `pass` but before the durable provider
reservation releases its target fence. The completed provider response stores
those lifecycle record identities, so exact replay never synthesizes new
evidence under changed profile authority. Successful completion, adoption, and
current exact replay then retry manager projection without repeating the
provider mutation. The service uses the issued plan as the stable generation
identity and returns a non-passing conflict when a delayed refresh or destroy no
longer owns the preview. Odoo destroy writes the approval invalidation event
before its destroyed tombstone when a serving binding is available; this keeps
the append-only event crash-durable while the GitHub call remains best-effort
after the provider reservation completes.

Rollback removes the repository's required `manager-preview-approval` status
and removes or narrows the managed approval rule. This disables merge/promotion
enforcement while preserving the append-only approval ledger and normal preview
cleanup.

## Idempotency

Every Launchplane mutation from a product preview workflow needs a stable
idempotency key. Use `preview_workflow_idempotency_key` or the same shape:

```text
preview-workflow:<product>:<context>:<operation>:pr-<number>:<run-id>:<run-attempt>
```

Run-scoped keys make repeated HTTP attempts safe while preserving a distinct
record for a later retry or GitHub rerun. Ignored decisions do not have
idempotency keys. Launchplane-owned reusable workflows may use an equivalent
route-specific key when the service derives context from product records.

Preview refresh readiness should allow the serving runtime identity to converge
after a provider deploy trigger. A health response that still reports the prior
deployment/artifact/source identity for the same product, context, preview slug,
and environment kind remains a polling signal until the route timeout; a
different product, context, preview slug, or environment kind remains a hard
mismatch.

## Route Handoff

Preview refresh routes receive only product-local facts:

- product key, and context only for compatibility routes that still require it
- PR number and source SHA
- immutable image or artifact reference
- run URL
- primitive smoke/readiness facts when the check is product-specific

Generic-web preview refresh callers should pass `anchor_pr_number` and omit
context, `preview_slug`, and `preview_url`. Launchplane derives the context from
the product profile, derives the slug from the product profile slug policy,
derives the live URL from the preview context's runtime environment records, and
rejects a supplied slug when it conflicts with the derived value. `context`,
`preview_slug`, and `preview_url` remain compatibility fields for older
adapters, not product-repo authority.

Launchplane also owns the reusable request-shape builders for Odoo tenant
preview workflows. Tenant repos may keep thin adapter jobs for checkout, image
publication, runner selection, and product smoke facts, but the route paths,
payload skeletons, JSON-file bindings, fail-result paths, run-scoped inputs
keys, and service-issued apply key for `artifact-publish-inputs`,
`preview-apply-inputs`, and `preview-apply` are Launchplane contract fixtures.
New or migrated tenant
preview workflows should install
`cbusillo/launchplane/.github/actions/setup-odoo-preview-request-client@<launchplane-sha>`
and either use its `request-kind` render mode or import the generated ESM client
to render `launchplane-request` inputs instead of copying inline route paths,
JSON request bodies, file bindings, fail paths, or idempotency key templates.
Render mode accepts primitive workflow facts and emits `route-path`, `payload`,
`payload-json-files`, `fail-result-paths`, `response-output-path`, and
`idempotency-key` outputs shaped for direct handoff to the existing
`launchplane-request` action. The generated request object exposes the same
contract as `routePath`, `payloadInput`, `payloadJsonFilesInput`,
`failResultPathsInput`, `responseOutputPath`, and `idempotencyKey` fields.
`reusable-odoo-preview.yml` is the Launchplane-owned next step above those
rendered requests: it keeps tenant repos responsible for event triggers,
runner selection, and source facts, while Launchplane owns the refresh/destroy
publish-inputs, preview-apply-inputs, preview-apply, and feedback-result
handoff chain. Tenant workflows migrating to it should pass only the product,
context, operation, PR number, source ref, optional runner selector, and the
existing Odoo publish secrets; they should not copy Odoo preview route paths,
JSON request bodies, JSON-file binding paths, fail paths, or idempotency key
templates back into the tenant repo.

The tenant caller also owns the reusable workflow's permission ceiling. Refresh
callers grant `contents: read`, `id-token: write`, and `packages: write` because
they publish the preview artifact. Destroy callers grant only `contents: read`
and `id-token: write`. The reusable refresh job inherits that caller scope
instead of declaring `packages: write` itself; a nested reusable workflow cannot
elevate a least-privilege destroy caller, even when the refresh job would be
skipped for that operation.

Odoo CM is the exception where Launchplane now owns both the isolated provider
apply planning inputs and the stage-preview smoke contract after refresh. Product
workflows should call `POST /v1/drivers/odoo/preview-apply-inputs` with PR,
image, and source facts, then pass a ready redacted dry-run plan to
`POST /v1/drivers/odoo/preview-apply` instead of assembling runtime bindings,
Dokploy environment ids, template compose ids, or Odoo database and volume names
inside the tenant repo. The route discovers existing Odoo preview composes from
provider inventory for refresh reuse and destroy planning, and it blocks destroy
when the matching preview compose and hostname cannot be proven. After refresh,
Launchplane owns `/launchplane/health`, `/web/health`, `/cm-website/health`, `/cell-mechanic`,
artifact/revision evidence, and module install/update evidence. Product
workflows should treat the Odoo refresh response's `result.status="pass"` (the
reusable workflow aliases it to `refresh_status`) as the ready-to-comment signal
instead of independently deciding readiness from raw health checks. A passing
result persists the active preview, ready serving generation, immutable artifact
identity, and verified runtime identity before manager approval projection. The
service requires an exact lowercase 40-character source commit and lowercase
64-character `sha256` image digest, injects its runtime identity into the preview
environment, and requires `/launchplane/health` to echo the matching identity;
callers cannot disable that verification on a refresh request.
Refresh merges the artifact manifest's declared Odoo modules
with Launchplane-required modules into both `ODOO_INSTALL_MODULES` and the
explicit maintenance-only `ODOO_UPDATE_MODULES` input. It also merges the
managed Launchplane and Enterprise addon roots into `ODOO_ADDONS_PATH` so a
stale explicit runtime-environment value cannot override the compose default
and make required module dependencies unavailable. Refresh then deploys the
compose and runs the managed Odoo post-deploy maintenance schedule before any
smoke check can pass. The schedule must prove that exactly one current web
container and script-runner container use the same artifact image, that an
explicit module list was configured, and that the install/update workflow
completed. Launchplane passes the resolved filestore path explicitly to the
workflow even when the live target relies on the compose default. Missing,
false, or unavailable schedule-log evidence fails the refresh. A terminal
provider deployment alone remains an unknown recovery outcome because it does
not prove that database-backed views were upgraded.

When the preview template instance has a deploy-enabled website-bootstrap
record, refresh also renders a preview-scoped copy of that website intent,
replaces its canonical URL with the exact preview URL, and passes it through the
same devkit application/readback path used by stable lanes. The copy excludes
stable config parameters, addon settings, and secret bindings, and never writes
the PR-specific canonical URL back to the stable record. The refresh fails when
the required website-bootstrap application and canonical readback markers are
missing or false.
Like resolved runtime-environment values and managed secrets, the bootstrap
contents come from current DB authority during apply; plan provenance binds the
preview route, target, and artifact facts rather than freezing mutable runtime
configuration values for the plan lifetime.

Ready Odoo apply-inputs responses also include the normalized `plan_request`
and `plan_provenance`: a service-derived plan id, canonical SHA-256 fingerprint,
issuance time, and 30-minute expiry. Launchplane persists that response under the
calling identity and requires the returned plan id as the `preview-apply`
`Idempotency-Key`; workflows must not derive a replacement apply key. Apply
loads the persisted result, requires the caller's dry-run plan and artifact to
match exactly, rejects missing, mismatched, or expired provenance, and rebuilds
the plan from current Launchplane records and current provider discovery before
the first provider effect. Any changed profile, runtime routing, template target,
environment id, or discovered preview target makes the plan stale and requires
new apply inputs. Completed exact retries replay the stored apply response only
while its stored lifecycle evidence remains the current preview owner. A changed
product profile, missing legacy lifecycle evidence, newer serving generation, or
newer destroy returns a conflict and does not publish ready/destroyed feedback.
Uncertain operations reconcile against the originally issued plan instead of
replanning into another effect. Blocked apply-inputs responses have no apply
provenance and are not persisted as issued plans, so a later retry can
re-evaluate recovered dependencies.
If a later browser or product-specific smoke workflow needs to publish common
preview evidence, it should call
`cbusillo/launchplane/.github/workflows/reusable-generic-web-preview-verification.yml@<launchplane-sha>`
with the PR number, `verification_status`, `verified_at`, optional checked URLs
plus timeout, and optional failure summary. The reusable workflow owns the
`POST /v1/drivers/generic-web/preview-verification` payload, route handoff, and
run-scoped idempotency key. Launchplane resolves the preview context and
generic-web base-driver compatibility from the product profile, updates the
latest preview generation, and returns a typed `generic_web_preview_verification`
result while preserving durable status/failure evidence in the same preview
records used by refresh. Odoo preview verification uses this generic-web route;
the former Odoo-shaped preview verification alias is retired.

Preview destroy routes receive the PR number, source/run metadata, and an
explicit destroy reason such as `pull_request_closed`, `preview_label_removed`,
or `manual_destroy_requested`. Generic-web preview destroy follows the same
context and slug policy as refresh: callers should pass PR identity, and
Launchplane derives the preview context and preview slug from the product
profile before provider deletion.

Preview feedback routes receive the status and primitive display facts.
Launchplane derives the marker, rendered markdown, delivery behavior, and record
id. If the feedback comment cannot be delivered, Launchplane records the skipped
or failed feedback result and, when a preview PR feedback notification policy is
configured, emits operator notification attempts from the control plane. Product
workflows should not render fallback PR comments themselves; missing runtime
GitHub credentials and GitHub API failures are Launchplane-owned operator
signals.

Product repos should call the reusable preview feedback workflow instead of
assembling `/v1/previews/pr-feedback` payloads, markers, or idempotency keys in
repo-local scripts.

Use
`cbusillo/launchplane/.github/actions/launchplane-request@<launchplane-sha>`
for the OIDC transport only when a Launchplane-owned reusable workflow does not
exist yet. Pin the full reviewed Launchplane commit SHA.

## Migration Checklist

- Replace product-rendered preview comments with `/v1/previews/pr-feedback`.
- Move unsupported fork and Dependabot notices to a base-branch
  `pull_request_target` job that does not check out PR code.
- Replace bespoke refresh/destroy event branching with the shared decision
  contract.
- For conventional generic-web products, call
  `reusable-generic-web-preview.yml@<launchplane-sha>` so Launchplane owns the
  build/publish/lifecycle/evidence composition while the product repo supplies
  only code-adjacent Docker inputs and its verification command.
- Keep the product verification command and domain behavior product-owned; the
  facade runs it without OIDC and records only its primitive result facts.
- Use run-scoped idempotency keys for every Launchplane mutation.
- Keep product configuration, preview URL policy, provider credentials, and
  cleanup truth in Launchplane records rather than product-repo files.
- Verify one refresh, one destroy, and one unsupported-notice path per migrated
  product repo.
- Treat preview operations for one pull request as serialized rather than
  cancelable. A close followed immediately by reopen may complete destroy before
  the subsequent refresh; the final event converges the preview to the current
  requested state without canceling cleanup in progress.

## Source Workflows

This contract was shaped from the current preview paths in SYO, VeriReel, and
Odoo CM. Odoo CM is the reference thin workflow after its preview feedback moved
to Launchplane. The generic-web preview facade is the bounded proof for moving
the remaining build, lifecycle, verification-evidence, and feedback composition
behind one reusable entrypoint. SYO and VeriReel should not delete bespoke
preview-control-plane logic until that proof passes against a disposable canary.
