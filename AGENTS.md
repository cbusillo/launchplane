# AGENTS.md — Launchplane Operating Guide (Read Me First)

Treat this file as the launch checklist for each engineering session in
`launchplane`, whichever client is operating in the repository.

## What Launchplane does

Launchplane is the control layer for product previews, deployments, backups, restores, releases, and merges.
Its contract records Clients' release decisions and gates delivery on verified artifacts and runtime evidence; adoption and release readiness must be checked per product.
Evidence: [DIRECTION.md](DIRECTION.md) and [README.md](README.md); each product owns its application code and builds.

## Workflow Loop

- Plan → patch → proportionate behavior tests → iterate → required gate.
- Keep changes small and coherent around a single ownership boundary.

## Quality Gates

- Use `.github/github.json` for the current test, lint,
  typecheck, build, inspection, and docs-freshness gates.
- During the [Q326 trial](docs/style/testing.md#local-test-loop), routine
  changes use focused local tests plus lint, type checks, and IDE inspection.
  [Testing style](docs/style/testing.md#local-test-loop) owns safety-module
  selection, Q778 focused PR CI, checked Ready evidence and the full candidate
  merge gate.
- Changes to agent instructions, execution guidance, approval or safety rules,
  credential handling, or destructive helpers need review by another model
  before delivery. Use the `model-review` skill and its finding-disposition
  rules; record the provider/model and how findings were handled in the PR.
- Add targeted tests whenever contract or storage behavior changes. Broaden to
  affected-area modules or integration gates when the changed behavior or a
  failure justifies it; complete-suite proof stays in CI.
- A test must fail when the product breaks and pass when someone makes an
  intended change. No test may assert a literal defined elsewhere (a version,
  toolchain, pin, hash, or count) or assert workflow, config, or docs text;
  run the script or check agreement with the one source instead. Verification
  code must not depend on working-tree state. See `docs/style/testing.md`.

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
