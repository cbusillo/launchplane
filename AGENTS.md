# AGENTS.md — Every Code Operating Guide (Read Me First)

Treat this file as the launch checklist for each Every Code session in
`launchplane`.

## Start Here

- Use the documentation index in `docs/README.md` before reading deeper files.
- Before changing code, open the matching style page in `docs/style/`.
- Keep prompts lean and prefer linking repo docs over pasting large excerpts.

## Project Snapshot

- This repo owns control-plane contracts, persisted records, and promotion/
  deploy orchestration.
- This repo does not own addon code, Odoo business logic, or local Odoo DX.
- Use `.github/github.json` for repo commands and quality gates;
  do not rely on system Python directly.
- Persist runtime records under `state/` or another explicit state directory,
  not in git-tracked history.
- Do not store real product, tenant, repository, branch, domain, lane,
  provider-target, runtime-environment, authz, operator, or other mutable
  runtime configuration as authority in production code or checked-in config
  files. Code owns schemas, validators, generic behavior, and fail-closed
  defaults; Launchplane records or operator-supplied input own real identities
  and values.
- The only runtime configuration exception for checked-in or process-level
  config is Launchplane's own minimal bootstrap/root-of-trust wiring required
  for the service to start and reach DB-backed records and managed secrets.

## Operating Guardrails

- Prefer fail-closed behavior over silent fallback.
- Do not reintroduce long-term release ownership back into code or local-DX
  repos.
- Keep cross-repo boundaries explicit; do not move release ownership back into
  tenant, shared-addon, or local-DX repos.
- Never commit secrets or operator-local overrides.
- Prefer Launchplane-owned runtime-environment records and managed secret
  records over ad hoc service-host env for product/runtime configuration.
- Use the deployed Launchplane service API or the operator UI for shared and
  production live mutations. Do not use local CLI live-target commands from an
  arbitrary checkout as a fallback; add or use a service endpoint first.
- Treat service-host env as bootstrap-only unless a repo doc explicitly calls
  out a narrower scoped bootstrap or rehearsal exception.
- Do not hard-code real tenant, product, repository, branch, domain, or operator
  values into production defaults, fallback behavior, or checked-in catalogs;
  see the coding standards for the docs/tests boundary.
- Do not replace code hard-coding with checked-in config hard-coding. A real
  product/repo/domain list in TOML, JSON, YAML, workflow defaults, or repo
  metadata is still runtime authority unless it is docs, tests, or the
  Launchplane self-bootstrap exception above.
- Update docs in the same change when behavior or ownership changes.
- Fix root causes, not symptoms; avoid workaround-only flows unless the
  operator explicitly asks for a time-boxed mitigation.

## Workflow Loop

- Plan → patch → targeted tests → iterate → gate.
- Keep changes small and coherent around a single ownership boundary.

## Quality Gates

- Use `.github/github.json` for the current test, lint,
  typecheck, build, inspection, and docs-freshness gates.
- Add targeted tests whenever contract or storage behavior changes.

## Repo Boundaries

- `launchplane` owns:
  - artifact manifests
  - release tuple catalogs
  - backup-gate records
  - promotion records
  - deployment records
  - environment inventory
  - Launchplane preview and generation records
  - promotion and deploy orchestration
  - backup and restore control-plane workflows
- Tenant/shared/devkit repos own:
  - addon code
  - local DX
  - Odoo-specific test and validation workflows
  - tenant-root convenience commands that preserve the ownership boundary

## Reference Handles

- Architecture: `docs/architecture.md`
- Operations: `docs/operations.md`
- Records: `docs/records.md`
- Secrets: `docs/secrets.md`
- Python style: `docs/style/python.md`
- Testing style: `docs/style/testing.md`
- Coding standards: `docs/policies/coding-standards.md`

Keep AGENTS.md thin. Put durable guidance in docs and policies instead of
growing this file into a second handbook.
