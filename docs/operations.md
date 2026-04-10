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
- `uv run control-plane promote execute --input-file <path> --odoo-ai-root <path>`
- `uv run control-plane ship plan --input-file <path>`
- `uv run control-plane ship execute --input-file <path> --odoo-ai-root <path>`

## Operational Rules

- Promotions must reference explicit artifact identifiers.
- Missing control-plane config is a hard error, not a silent fallback.
- Operator-local state belongs under `state/` or another explicit state
  directory outside git.
- Promotion execution history should remain append-only.
- Promote now uses the native artifact-backed promotion request, then uses
  `odoo-ai` only for read-only ship-request export before executing ship from
  the control-plane-owned path.
- Direct `ship` now enters here first as an artifact-backed workflow, not a
  branch-sync fallback.
- Artifact manifests handed off from `odoo-ai` should be persisted here before
  later workflows depend on them.
- Ship execution should persist a deployment record here before and after
  delegation so deploy history no longer lives only in `odoo-ai` process
  output.
- Public `ship` handoff now requires an explicit artifact id and emits an
  artifact-backed ship request with no branch-sync metadata.
- Ship destination health verification now also runs from the control-plane-
  owned boundary after deploy/update execution returns.
- Ship now resolves the concrete Dokploy target before deployment so the
  control plane owns the exact runtime target identity used for the deploy.
- Ship now also owns Dokploy credential loading and Dokploy trigger/wait
  execution directly.
- The Odoo-specific post-deploy update remains the one explicit cross-repo
  runtime seam. When it applies, control-plane ship runs it through the
  canonical `odoo-ai platform update` path.
- Deployment records now persist post-deploy update evidence so operator state
  shows whether that remaining Odoo-owned step was skipped, pending, passed, or
  failed.
- Successful waited `ship` and `promote` executions now also refresh current
  environment inventory under `state/inventory/`, so the control plane can
  answer what artifact/source ref is currently running for each environment.
- Direct `ship` now prefers a stored real artifact id when the requested
  commit matches exactly one persisted artifact manifest, and otherwise fails
  closed until the caller provides an explicit artifact id.
- When a stored artifact manifest is available, ship now syncs
  `DOCKER_IMAGE_REFERENCE=<repo>@<digest>` onto the Dokploy target before
  deploy execution so runtime execution uses the immutable image directly.
- When no stored artifact manifest is available, ship now fails closed instead
  of falling back to branch sync or repo/tag image selection.
- Control-plane-owned Dokploy credentials now come from the control-plane
  repo's untracked `.env` by default, or explicit process env overrides,
  instead of piggybacking on `odoo-ai`'s `.env`.

## Migration Rules

- Keep wrapper logic in `odoo-ai` thin and disposable.
- Do not add new long-term remote release ownership back into `odoo-ai`.
- Move live workflows one coherent slice at a time.
