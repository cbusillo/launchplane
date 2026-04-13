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
