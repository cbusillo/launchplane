---
title: Operations
---

## Bootstrap Commands

- `uv run control-plane artifacts write --input-file <path>`
- `uv run control-plane artifacts ingest --input-file <path>`
- `uv run control-plane artifacts show --artifact-id <artifact-id>`
- `uv run control-plane backup-gates list --context <ctx> --instance <instance>`
- `uv run control-plane backup-gates write --input-file <path>`
- `uv run control-plane backup-gates show --record-id <record-id>`
- `uv run control-plane deployments list --context <ctx> --instance <instance>`
- `uv run control-plane deployments show --record-id <record-id>`
- `uv run control-plane inventory status --context <ctx> --instance <instance>`
- `uv run control-plane inventory overview [--context <ctx>]`
- `uv run control-plane inventory show --context <ctx> --instance <instance>`
- `uv run control-plane inventory list`
- `uv run control-plane environments resolve --context <ctx> --instance
<instance> [--json-output]`
- `uv run control-plane harbor-previews list [--context <ctx>]`
- `uv run control-plane harbor-previews show --context <ctx> --anchor-repo
<repo> --pr-number <number>`
- `uv run control-plane harbor-previews history --context <ctx> --anchor-repo
<repo> --pr-number <number>`
- `uv run control-plane harbor-previews write-preview --input-file <path>`
- `uv run control-plane harbor-previews write-generation --input-file <path>`
- `uv run control-plane harbor-previews request-generation --preview-input-file
<path> --generation-input-file <path>`
- `uv run control-plane harbor-previews mark-generation-ready --input-file
<path>`
- `uv run control-plane harbor-previews mark-generation-failed --input-file
<path>`
- `uv run control-plane harbor-previews destroy-preview --input-file <path>`
- `uv run control-plane harbor-previews ingest-pr-event --input-file <path>`
- `uv run control-plane promotions list --context <ctx> --to-instance <instance>`
- `uv run control-plane promotions write --input-file <path>`
- `uv run control-plane promotions show --record-id <record-id>`
- `uv run control-plane promote record --artifact-id <artifact-id> --context
<ctx> --from-instance testing --to-instance prod`
- `uv run control-plane promote resolve --context <ctx> --from-instance
<instance> --to-instance <instance> --artifact-id <artifact-id>
--backup-record-id <record-id>`
- `uv run control-plane promote execute --input-file <path> [--env-file <path>]`
- `uv run control-plane ship plan --input-file <path>`
- `uv run control-plane ship resolve --context <ctx> --instance <instance>
--artifact-id <artifact-id>`
- `uv run control-plane ship execute --input-file <path> [--env-file <path>]`

## Operational Rules

- Promotions must reference explicit artifact identifiers.
- Missing control-plane config is a hard error, not a silent fallback.
- Operator-local state belongs under `state/` or another explicit state
  directory outside git.
- Promotion execution history should remain append-only.
- Backup-gate evidence should be stored here before promotion execution, rather
  than trusted only as inline request JSON.
- Promote uses the native artifact-backed promotion request, then uses
  control-plane-native ship request resolution before executing ship from the
  control-plane-owned path.
- `promote resolve` renders the typed artifact-backed promotion request
  directly from `odoo-control-plane`, so promotion planning does not depend on
  any legacy request-export step from a code repo.
- Direct `ship` enters here first as an artifact-backed workflow, not a
  branch-sync fallback.
- `ship resolve` renders the typed artifact-backed ship request directly from
  `odoo-control-plane` by reading this repo's Dokploy source-of-truth and
  control-plane env values, so operators do not need any legacy
  request-export step or sibling code-repo path just to plan a control-plane
  workflow.
- Control-plane-owned Dokploy route definitions live in tracked
  `config/dokploy.toml` by default.
- Live Dokploy `target_id` values come from untracked
  `config/dokploy-targets.toml` by default. Set
  `ODOO_CONTROL_PLANE_DOKPLOY_TARGET_IDS_FILE` when an operator needs an
  alternate local target-id catalog path.
- Set `ODOO_CONTROL_PLANE_DOKPLOY_SOURCE_FILE` when an operator needs an
  alternate local route catalog path.
- Dokploy source-of-truth loading fails closed if a target entry is
  missing `target_id`, if multiple entries claim the same `context`/`instance`
  route, or if the operator-local target-id catalog contains routes that are
  not present in the tracked source-of-truth.
- Artifact manifests handed off from upstream build/export steps should be
  persisted here before later workflows depend on them.
- Ship execution should persist a deployment record here before execution
  begins and after final status is known so deploy history lives in
  control-plane state instead of transient process output.
- Public `ship` handoff requires an explicit artifact id and emits an
  artifact-backed ship request with no branch-sync metadata.
- Ship destination health verification also runs from the control-plane-owned
  boundary after deploy/update execution returns.
- Ship resolves the concrete Dokploy target before deployment so the
  control plane owns the exact runtime target identity used for the deploy.
- Ship also owns Dokploy credential loading and Dokploy trigger/wait execution
  directly.
- Waited compose deployments run the Odoo-specific post-deploy update
  natively through a control-plane-owned Dokploy schedule workflow.
- `--env-file` is an optional explicit env overlay for that compose
  post-deploy update path when operators need to sync runtime env values
  before the update runs.
- That overlay is fail-closed and currently supports only
  `ODOO_DB_NAME`, `ODOO_FILESTORE_PATH`, and
  `ODOO_DATA_WORKFLOW_LOCK_FILE`.
- Deployment records persist post-deploy update evidence so operator state
  shows whether that native control-plane step was skipped, pending, passed,
  or failed.
- Successful waited `ship` and `promote` executions also refresh current
  environment inventory under `state/inventory/`, so the control plane can
  answer what artifact/source ref is currently running for each environment.
- `inventory status` joins current inventory with the linked live
  promotion and backup-gate record, plus the latest promotion/deployment
  history for the same environment, so operators can answer what is live, what
  backup authorized it, and what happened last without opening raw JSON.
- `inventory overview` renders that same read model across all live inventory
  entries, with an optional context filter for tenant-scoped operator views.
- The legacy static operator UI was intentionally removed during the Harbor
  reset so preview/product work does not inherit the old dashboard model.
- Harbor preview inspection now starts with JSON read surfaces owned by this
  repo: `harbor-previews list`, `harbor-previews show`, and
  `harbor-previews history`.
- Harbor preview record mutation also starts here through builder-backed
  `harbor-previews write-preview` and `harbor-previews write-generation`
  commands that accept typed JSON input files.
- Harbor preview lifecycle transitions also have thin command wrappers:
  `request-generation`, `mark-generation-ready`, `mark-generation-failed`,
  and `destroy-preview`.
- Harbor PR-event ingest starts with a manual typed JSON path through
  `harbor-previews ingest-pr-event`, which parses a GitHub-style PR event,
  looks up any stored Harbor preview, and returns Harbor's action decision plus
  the next explicit mutation intent without requiring a webhook server yet.
  Add `--apply` to let Harbor execute the resolved mutation intent for the
  currently supported path instead of only printing the dry-run payload.
- Harbor also accepts raw GitHub `pull_request` webhook payloads through
  `harbor-previews ingest-github-webhook --event-name pull_request`, which
  adapts the raw delivery into Harbor's typed PR-event contract and then reuses
  the same classify/resolve/apply/feedback path.
- For captured/local replay, Harbor also accepts a single replay envelope file
  through `harbor-previews replay-github-webhook`, so event name, signature,
  and payload travel together instead of requiring separate CLI flags.
- To generate that envelope from saved local inputs, use
  `harbor-previews build-github-webhook-replay-envelope` with a raw payload
  JSON file plus optional captured headers/evidence files. The helper emits the
  same replay-envelope contract that `replay-github-webhook` already consumes,
  so capture-to-replay stays one format instead of splitting into a second
  local-only shape.
- The builder also supports one `--http-capture-file` as an alternative to the
  split payload/header inputs. Harbor currently expects a narrow UTF-8 HTTP
  request transcript shape for that path: a `POST ... HTTP/...` request line,
  header lines in `Name: value` form, one blank line, and the raw JSON body.
  That single-file path still emits the same replay envelope rather than a new
  local capture contract.
- Replay envelopes now also accept an optional captured-delivery `capture`
  block with recorded-at/source metadata plus selected headers/evidence. When
  the top-level envelope omits `event_name`, `signature_256`, or `delivery_id`,
  Harbor can resolve those fields from captured GitHub headers such as
  `X-GitHub-Event`, `X-Hub-Signature-256`, and `X-GitHub-Delivery` while still
  reusing the same verification and adaptation path.
- By default `harbor-previews ingest-github-webhook` now verifies the raw body
  against `--signature-256` using `GITHUB_WEBHOOK_SECRET` from the resolved
  Harbor context before it trusts the payload. Use `--allow-unsigned` only for
  explicit local/manual replay when signature verification is intentionally
  bypassed.
- Current Harbor anchor eligibility is tenant-repo only: `tenant-opw -> opw`
  and `tenant-cm -> cm`. `shared-addons` stays companion-only, and infra/tooling
  repos such as `devkit`, `control-plane`, and image repos are ignored.
- `harbor-previews show` is the one-preview status payload shaped for the
  first Harbor page, including canonical preview URL, trust summary, health,
  and retained evidence for destroyed previews.
- `harbor-previews list` is a compact inventory surface for operator review;
  destroyed previews remain visible there rather than disappearing.
- `harbor-previews history` exposes full generation ordering and serving/latest
  markers so operators can distinguish a failed replacement from the last
  healthy serving generation.
- `harbor-previews write-preview` resolves the dedicated Harbor preview base
  URL from runtime environment config, reuses any stored preview identity for
  the same anchor PR, and fails closed when preview routing config is missing.
- `harbor-previews write-generation` requires an existing preview record for
  the same anchor PR and assigns the next generation sequence automatically
  when the input file does not pin one explicitly.
- `harbor-previews request-generation` applies Harbor's in-progress replacement
  semantics in one step: it persists the generation, advances `latest` and
  `active`, and preserves any older serving generation until cutover.
- `harbor-previews mark-generation-ready` updates the stored generation and
  cuts the preview over so `active`, `serving`, and `latest` all point at the
  now-ready generation.
- `harbor-previews mark-generation-failed` updates the stored generation and
  marks the preview failed while preserving any older healthy serving
  generation when one exists.
- `harbor-previews destroy-preview` clears runtime-serving links while keeping
  retained Harbor evidence visible through the read-model surfaces.
- `harbor-previews ingest-pr-event` is the first explicit event-ingest surface;
  it classifies whether Harbor should enable, refresh, destroy, or ignore a PR
  event under the current label-driven trigger contract and now emits the
  matching Harbor mutation intent. Generation intent remains manifest-resolution
  dependent only when Harbor cannot yet resolve the exact preview build tuple,
  such as companion-ref cases that still need external PR-head lookup.
- Harbor preview-request metadata now starts as one Harbor-owned fenced PR-body
  block using the `harbor-preview` info string with TOML content. Missing blocks
  are treated as absent metadata; malformed or non-allowlisted companion refs
  are reported fail-closed in the ingest payload.
- For the current no-companion path, Harbor now resolves an exact preview
  manifest from the anchor PR head SHA plus a control-plane-owned baseline
  release tuple of exact repo SHAs and emits a full generation mutation request
  directly from event ingest.
- `harbor-previews ingest-pr-event --apply` reuses Harbor's existing preview
  mutation helpers to write preview/generation records or destroy a preview
  when the event intent is fully resolved. Unresolved companion cases stay
  read-only and report an explicit no-op reason instead of guessing.
- `harbor-previews ingest-pr-event` now also emits a reviewer-facing Harbor
  `feedback` payload with concise markdown plus structured preview/apply facts.
  On applied preview paths it includes the canonical preview URL and manifest
  evidence; on unresolved paths it explains why Harbor stayed fail-closed.
- Add `--deliver-feedback` to `harbor-previews ingest-pr-event` to post that
  same Harbor feedback payload back to the anchor PR through GitHub. Harbor
  uses one hidden marker so later runs update the Harbor-owned PR comment
  instead of spamming duplicates, and it stays explicit no-op when GitHub auth
  or PR ownership context is missing.
- `harbor-previews ingest-github-webhook` supports the same `--apply` and
  `--deliver-feedback` flags after adaptation, so Harbor can process a raw
  GitHub webhook delivery without a hand-authored intermediate event file.
- If signature verification fails, Harbor rejects the raw delivery before event
  adaptation or preview decision logic runs.
- Signed replay envelopes must include the raw `payload_text` so Harbor can
  verify the original bytes; unsigned replay remains available only through the
  explicit `allow_unsigned` envelope path.
- Both raw webhook ingest and replay now surface lightweight delivery metadata
  such as GitHub delivery id and delivery source alongside the existing webhook
  verification/adaptation payload so traces remain identifiable without
  affecting Harbor preview decisions.
- Replay output also surfaces the optional `webhook_replay.capture` metadata for
  local traceability, but Harbor treats that capture evidence as observational
  only rather than part of preview decision logic.
- The builder currently packages the raw payload as `payload_text` so signed
  replays preserve the exact bytes Harbor verifies, even when the envelope also
  carries parsed capture metadata for operator traceability.
- When `--http-capture-file` is used, Harbor parses headers from that capture
  directly and rejects malformed request lines or header syntax before it emits
  a replay envelope.
- That HTTP-capture path now also preserves the original request line under
  `capture.evidence.http_request.request_line` so local replay traces keep the
  saved request shape without turning it into a Harbor decision input.
- If a supported saved HTTP capture includes `Content-Length`, Harbor now also
  validates that declared length against the saved request body bytes and fails
  closed for mismatched or malformed values before it emits a replay envelope.
- If a supported saved HTTP capture includes `Content-Type`, Harbor now also
  requires a JSON media type such as `application/json` or `application/*+json`
  and fails closed for clearly non-JSON values before replay packaging.
- If that supported HTTP capture includes common method-override headers such as
  `X-HTTP-Method-Override` or `X-Method-Override`, Harbor now also requires
  them to stay aligned with the captured `POST` request shape and rejects
  conflicting override values early.
- If that supported HTTP capture includes `Transfer-Encoding`, Harbor currently
  only tolerates the explicit no-transform `identity` case and rejects
  unsupported values such as `chunked` for the local saved-capture path.
- If that supported HTTP capture includes `Content-Encoding`, Harbor likewise
  only tolerates the explicit no-transform `identity` case and rejects
  unsupported values such as `gzip` for the local saved-capture path.
- If that supported HTTP capture declares `Trailer`, Harbor rejects it early on
  the local saved-capture path because the replay helper does not parse trailing
  headers.
- If that supported HTTP capture declares `Expect`, Harbor also rejects it
  early on the local saved-capture path because the replay helper does not
  implement request-handshake semantics such as `100-continue`.
- If that supported HTTP capture declares `Connection`, Harbor also rejects it
  early on the local saved-capture path because the replay helper does not
  model hop-by-hop connection semantics.
- If that supported HTTP capture declares `Pragma`, Harbor also rejects it
  early on the local saved-capture path because the replay helper does not
  model cache or proxy semantics.
- If that supported HTTP capture declares `Cache-Control`, Harbor also rejects
  it early on the local saved-capture path because the replay helper does not
  model cache semantics.
- If that supported HTTP capture declares `Upgrade`, Harbor also rejects it
  early on the local saved-capture path because the replay helper does not
  model protocol-switch semantics.
- Harbor can now resolve the first allowlisted companion path when it has both
  a GitHub owner from the anchor PR URL and a usable `GITHUB_TOKEN` from the
  control-plane runtime context. If either input is missing, companion cases
  stay explicit but unresolved.
- Until Harbor ships a replacement UI, use the Harbor preview JSON commands for
  preview state and the inventory/environment JSON commands for live shared
  environment state.
- Direct `ship` requires an explicit artifact id that already has a stored
  artifact manifest in control-plane state.
- When a stored artifact manifest is available, ship syncs
  `DOCKER_IMAGE_REFERENCE=<repo>@<digest>` onto the Dokploy target before
  deploy execution so runtime execution uses the immutable image directly.
- When no stored artifact manifest is available, ship fails closed instead
  of falling back to branch sync or repo/tag image selection.
- Control-plane-owned Dokploy credentials come from the control-plane
  repo's untracked `.env` by default, or explicit process env overrides,
  instead of piggybacking on a separate code repo's `.env`.
- Promote fails closed unless a stored backup-gate record explicitly proves
  the destination environment passed the pre-deploy backup gate.

## Boundary Rules

- Keep code-repo convenience commands thin and explicit, and delete only true
  compatibility bridges that still exist for migration reasons.
- Do not add new long-term remote release ownership back into code or local-DX
  repos.
- Any remaining cross-repo handoff must stay explicit, narrow, and fail
  closed.
