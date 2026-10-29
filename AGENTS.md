# AGENTS.md — Launchplane Operating Guide (Read Me First)

Treat this file as the launch checklist for each engineering session in
`launchplane`, whichever client is operating in the repository.

## What Launchplane does

Launchplane is the control layer for product previews, deployments, backups, restores, releases, and merges.
Its contract records Clients' release decisions and gates delivery on verified artifacts and runtime evidence; adoption and release readiness must be checked per product.
Evidence: [DIRECTION.md](DIRECTION.md) and [README.md](README.md); each product owns its application code and builds.

## Start Here

- Read the Director's [overall direction](https://github.com/cbusillo/direction/blob/main/DIRECTION.md),
  then this repository's [DIRECTION.md](DIRECTION.md).
- Read [README.md](README.md), then use [docs/README.md](docs/README.md) before
  reading deeper files.
- AGENTS.md is the only agent-instruction filename; use nested AGENTS.md files
  when a directory needs additional guidance.
- Before changing code, open the matching style page in `docs/style/`.
- Keep prompts lean and prefer linking repo docs over pasting large excerpts.

## Project Snapshot

- This repo owns control-plane contracts, persisted records, and promotion/
  deploy orchestration.
- This repo does not own addon code, Odoo application/business logic, or local
  Odoo DX. It does own Launchplane-side Odoo operational drivers: control-plane
  code that invokes Odoo operations, not addon models, views, controllers, or
  tenant business logic.
- Persist file-backed local, test, and rehearsal runtime records under `state/`
  or another explicit state directory, not in git-tracked history. Shared
  runtime truth is DB-backed.
- Follow the shared [Runtime Authority Boundary](https://github.com/cbusillo/codex-skills/blob/main/skills/launchplane/SKILL.md#runtime-authority-boundary).
  See [coding standards](docs/policies/coding-standards.md) for the docs/tests boundary.
- The only runtime configuration exception for checked-in or process-level
  config is Launchplane's own minimal bootstrap/root-of-trust wiring required
  for the service to start and reach DB-backed records and managed secrets.

## Delivery Boundary

- Follow [DIRECTION.md](DIRECTION.md) for delivery authority and retired designs.
  Use the shared [merging procedure](https://github.com/cbusillo/codex-skills/blob/main/skills/github/SKILL.md#merging)
  for handoff and train routing.
  Without explicit train authority, use the checked `gh-pr.py ready` handoff
  on the current PR head.
  The train admits trusted, proven current-head handoffs between candidates;
  see [train admission](docs/merge-train-policy.md#ready-handoffs). Until that
  source is deployed, the Supervisor's interim watcher supplies enqueue labels.
  An executing agent does not merge by hand or operate the train without
  explicit authority for that task.
- Use [event-driven deploys](docs/event-driven-deploys.md) and
  [artifact provenance](docs/artifact-provenance.md) for the product build handoff,
  and [Client review](docs/owner-acceptance.md) and
  [release review](docs/release-review.md) for the release contract. Consult
  DIRECTION.md for its retired list; do not extend those surfaces.
- Keep Launchplane merge/delivery provider-neutral. GitHub is the current source-
  control adapter and Dokploy is the current application deployment provider.

## Operating Guardrails

- Prefer fail-closed behavior over silent fallback.
- Do not reintroduce long-term release ownership back into code or local-DX
  repos.
- Keep cross-repo boundaries explicit; do not move release ownership back into
  tenant, shared-addon, or local-DX repos.
- Never commit secrets or admin-local overrides.
- Do not use local CLI live-target commands from an arbitrary checkout as a
  fallback; use a supported service capability or record the exact missing
  service/activation prerequisite. See [operations](docs/operations.md).
- Treat service-host env as bootstrap-only unless a repo doc explicitly calls
  out a narrower scoped bootstrap or rehearsal exception.
- Update docs in the same change when behavior or ownership changes.
- Fix root causes, not symptoms; avoid workaround-only flows unless the
  Director explicitly asks for a time-boxed mitigation.
- Do not propose, add, or apply new GitHub-secret/workflow-managed
  authorization grants; granting access is a stop boundary. On
  `authorization_denied`, name the exact denied action and follow
  `docs/authorization-authority.md#denial-handling`. Continue with work that does
  not depend on the refusal.
- Follow the shared [protected workflow dispatch and waiting procedure](https://github.com/cbusillo/codex-skills/blob/main/skills/github/references/repo-workflow.md#protected-workflow-dispatch-and-waiting).

## Workflow Loop

- Plan → patch → proportionate behavior tests → iterate → required gate.
- Keep changes small and coherent around a single ownership boundary.

## Quality Gates

- [Workflow metadata](.github/github.json) records repository commands and gates.
- [Testing style](docs/style/testing.md#local-test-loop) owns the Q326 trial,
  safety-module selection, Q778 focused PR CI, checked Ready evidence and the
  full candidate merge gate.
- Changes to agent instructions, execution guidance, approval or safety rules,
  credential handling, or destructive helpers need review by another model
  before delivery. Use the `model-review` skill and its finding-disposition
  rules; record the provider/model and how findings were handled in the PR.
- Add targeted tests whenever contract or storage behavior changes.
- Follow the shared [Tests rule](https://github.com/cbusillo/codex-skills/blob/main/instructions/global.md#tests)
  and [testing style](docs/style/testing.md).

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
