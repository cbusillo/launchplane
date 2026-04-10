---
title: Secrets
---

## Purpose

- Define the control-plane-owned secret contract for deploy and operator
  workflows.

## Current Contract

- Dokploy credentials now belong to `odoo-control-plane`.
- Keep real values in the control-plane repo's untracked `.env` by default.
- `DOKPLOY_HOST` and `DOKPLOY_TOKEN` may also be provided through the current
  process environment.
- If you need a non-default secret file location, set
  `ODOO_CONTROL_PLANE_ENV_FILE` to an alternate untracked env file path.

## Rules

- Never commit `.env`, alternate secret files, or rendered env artifacts.
- Do not rely on `odoo-ai`'s `.env` for control-plane-owned secrets.
- Missing Dokploy credentials are a hard error, not a silent fallback.

## Bootstrap

```bash
cp .env.example .env
```
