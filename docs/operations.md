---
title: Operations
---

## Bootstrap Commands

- `uv run control-plane artifacts write --input-file <path>`
- `uv run control-plane artifacts ingest-odoo-ai --input-file <path>`
- `uv run control-plane artifacts show --artifact-id <artifact-id>`
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
- The first live promote path may delegate the underlying `platform ship`
  worker back to `odoo-ai`, but that delegation is an internal compatibility
  detail. The promote boundary itself belongs here.
- The direct compatibility `ship` path also enters here first and then
  delegates to the internal `odoo-ai` worker during transition.
- Artifact manifests handed off from `odoo-ai` should be persisted here before
  later workflows depend on them.
- Compatibility `ship` execution should persist a deployment record here before
  and after delegation so deploy history no longer lives only in `odoo-ai`
  process output.

## Migration Rules

- Keep wrapper logic in `odoo-ai` thin and disposable.
- Do not add new long-term remote release ownership back into `odoo-ai`.
- Move live workflows one coherent slice at a time.
