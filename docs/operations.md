---
title: Operations
---

## Bootstrap Commands

- `uv run control-plane artifacts write --input-file <path>`
- `uv run control-plane artifacts ingest-odoo-ai --input-file <path>`
- `uv run control-plane artifacts show --artifact-id <artifact-id>`
- `uv run control-plane inventory show --context <ctx> --instance <instance>`
- `uv run control-plane inventory list`
- `uv run control-plane promotions write --input-file <path>`
- `uv run control-plane promotions show --record-id <record-id>`
- `uv run control-plane promote record --artifact-id <artifact-id> --context <ctx> --from-instance testing --to-instance prod`
- `uv run control-plane promote compatibility-execute --input-file <path> --odoo-ai-root <path>`
- `uv run control-plane ship compatibility-execute --input-file <path> --odoo-ai-root <path>`

## Operational Rules

- Promotions must reference explicit artifact identifiers.
- Missing control-plane config is a hard error, not a silent fallback.
- Operator-local state belongs under `state/` or another explicit state
  directory outside git.
- Promotion execution history should remain append-only.
- The first live promote path now uses `odoo-ai` only for read-only ship
  request export and then executes ship directly from the control-plane-owned
  path.
- The direct compatibility `ship` path also enters here first.
- Artifact manifests handed off from `odoo-ai` should be persisted here before
  later workflows depend on them.
- Compatibility `ship` execution should persist a deployment record here before
  and after delegation so deploy history no longer lives only in `odoo-ai`
  process output.
- Public `ship` handoff now includes branch-sync planning metadata so the
  control plane can record the intended branch move even before Dokploy work
  starts.
- Compatibility `ship` execution now also applies the branch-sync git push from
  the control-plane-owned boundary before deploy execution starts.
- Compatibility `ship` destination health verification now also runs from the
  control-plane-owned boundary after deploy/update execution returns.
- Compatibility `ship` now resolves the concrete Dokploy target before
  deployment so the control plane owns the exact runtime target identity used
  for the deploy.
- Compatibility `ship` now also owns Dokploy credential loading and Dokploy
  trigger/wait execution directly.
- The Odoo-specific post-deploy update remains the one explicit cross-repo
  runtime seam. When it applies, control-plane ship runs it through the
  canonical `odoo-ai platform update` path.
- Deployment records now persist post-deploy update evidence so operator state
  shows whether that remaining Odoo-owned step was skipped, pending, passed, or
  failed.
- Successful waited `ship` and `promote` executions now also refresh current
  environment inventory under `state/inventory/`, so the control plane can
  answer what artifact/source ref is currently running for each environment.
- Direct `ship` now also prefers a stored real artifact id when the requested
  commit matches exactly one persisted artifact manifest, so current-state and
  deploy-history records do not fall back to blank artifact identity when the
  control plane already knows the immutable build output.
- When a stored artifact manifest is available, compatibility `ship` now also
  syncs `DOCKER_IMAGE_REFERENCE=<repo>@<digest>` onto the Dokploy target
  before deploy execution so runtime execution can prefer the immutable image
  instead of relying only on branch-sync state.
- On that artifact-backed path, compatibility `ship` now bypasses the old
  branch-sync git push entirely and records that the branch move was skipped
  because deploy execution used the immutable artifact image instead.
- When no stored artifact manifest is available, compatibility `ship` clears
  any stale `DOCKER_IMAGE_REFERENCE` override so the target falls back to the
  normal `DOCKER_IMAGE` + `DOCKER_IMAGE_TAG` contract.
- Control-plane-owned Dokploy credentials now come from the control-plane
  repo's untracked `.env` by default, or explicit process env overrides,
  instead of piggybacking on `odoo-ai`'s `.env`.

## Migration Rules

- Keep wrapper logic in `odoo-ai` thin and disposable.
- Do not add new long-term remote release ownership back into `odoo-ai`.
- Move live workflows one coherent slice at a time.
