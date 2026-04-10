---
title: Operations
---

## Bootstrap Commands

- `uv run control-plane artifacts write --input-file <path>`
- `uv run control-plane artifacts show --artifact-id <artifact-id>`
- `uv run control-plane promotions write --input-file <path>`
- `uv run control-plane promotions show --record-id <record-id>`
- `uv run control-plane promote record --artifact-id <artifact-id> --context <ctx> --from-instance testing --to-instance prod`

## Operational Rules

- Promotions must reference explicit artifact identifiers.
- Missing control-plane config is a hard error, not a silent fallback.
- Operator-local state belongs under `state/` or another explicit state
  directory outside git.
- Promotion execution history should remain append-only.

## Migration Rules

- Keep wrapper logic in `odoo-ai` thin and disposable.
- Do not add new long-term remote release ownership back into `odoo-ai`.
- Move live workflows one coherent slice at a time.
