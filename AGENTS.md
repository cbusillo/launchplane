# AGENTS.md — Codex CLI Operating Guide (Read Me First)

Treat this file as the launch checklist for every Codex session in
`odoo-control-plane`.

## Start Here

- Use the documentation index in `docs/README.md` before reading deeper files.
- Before changing code, open the matching style page in `docs/style/`.
- Keep prompts lean and prefer linking repo docs over pasting large excerpts.

## Project Snapshot

- This repo owns control-plane contracts, persisted records, and promotion/
  deploy orchestration.
- This repo does not own addon code, Odoo business logic, or local Odoo DX.
- Use `uv run ...` for repo commands; do not rely on system Python directly.
- Persist runtime records under `state/` or another explicit state directory,
  not in git-tracked history.

## Operating Guardrails

- Prefer fail-closed behavior over silent fallback.
- Do not reintroduce long-term release ownership back into `odoo-ai`.
- Keep wrapper behavior transitional and document removal criteria.
- Never commit secrets or operator-local overrides.
- Update docs in the same change when behavior or ownership changes.
- Fix root causes, not symptoms; avoid workaround-only flows unless the
  operator explicitly asks for a time-boxed mitigation.

## Workflow Loop

- Plan → patch → targeted tests → iterate → gate.
- Keep changes small and coherent around a single ownership boundary.

## Testing & Scripts

- Use `uv run python -m unittest` for the default test entrypoint.
- Add targeted tests whenever contract or storage behavior changes.
- Run lint only when requested or when the changed files are the only scope.

## Repo Boundaries

- `odoo-control-plane` owns:
  - artifact manifests
  - promotion records
  - environment inventory
  - promotion and deploy orchestration
  - backup and restore control-plane workflows
- `odoo-ai` owns:
  - addon code
  - local DX
  - Odoo-specific test and validation workflows
  - thin compatibility wrappers during migration

## Reference Handles

- Architecture: `docs/architecture.md`
- Operations: `docs/operations.md`
- Records: `docs/records.md`
- Python style: `docs/style/python.md`
- Testing style: `docs/style/testing.md`
- Coding standards: `docs/policies/coding-standards.md`

Keep AGENTS.md thin. Put durable guidance in docs and policies instead of
growing this file into a second handbook.
