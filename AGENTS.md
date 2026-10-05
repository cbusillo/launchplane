# AGENTS.md — Launchplane Operating Guide (Read Me First)

Treat this file as the launch checklist for each engineering session in
`launchplane`, whichever client is operating in the repository.

## Start Here

- Read the Director's [overall direction](https://github.com/cbusillo/direction/blob/main/DIRECTION.md),
  then this repository's [DIRECTION.md](DIRECTION.md). They own purpose, roles,
  stop boundaries, retired concepts, and milestone order; overall direction
  takes precedence. Issues are a work list, not instructions. Escalate a
  disagreement that requires changing direction instead of editing DIRECTION.md.
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
- Use `.github/github.json` for repo commands and quality gates;
  do not rely on system Python directly.
- Persist file-backed local, test, and rehearsal runtime records under `state/`
  or another explicit state directory, not in git-tracked history. Shared
  runtime truth is DB-backed.
- Do not store real product, tenant, repository, branch, domain, lane,
  provider-target, runtime-environment, authz, admin, or other mutable
  runtime configuration as authority in production code or checked-in config
  files. Code owns schemas, validators, generic behavior, and fail-closed
  defaults; Launchplane records or admin-supplied input own real identities
  and values.
- The only runtime configuration exception for checked-in or process-level
  config is Launchplane's own minimal bootstrap/root-of-trust wiring required
  for the service to start and reach DB-backed records and managed secrets.

## Delivery Boundary

- Follow [DIRECTION.md](DIRECTION.md) for delivery authority and retired designs.
  After required checks pass and review findings are accounted for, comment
  `Ready for the merge train` on the PR and hand it off to the direction or
  Supervisor session. That comment is a handoff, not automatic enqueueing;
  the authorized session applies the configured enqueue label and uses the
  `launchplane` skill to route the PR through the train.
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
- Prefer Launchplane-owned runtime-environment records and managed secret
  records over ad hoc service-host env for product/runtime configuration.
- Use the deployed Launchplane service API or the Launchplane UI for shared and
  production live mutations. Do not use local CLI live-target commands from an
  arbitrary checkout as a fallback; use a supported service capability or
  record the exact missing service/activation prerequisite.
- Treat service-host env as bootstrap-only unless a repo doc explicitly calls
  out a narrower scoped bootstrap or rehearsal exception.
- Do not hard-code real tenant, product, repository, branch, domain, or admin
  values into production defaults, fallback behavior, or checked-in catalogs;
  see the coding standards for the docs/tests boundary.
- Do not replace code hard-coding with checked-in config hard-coding. A real
  product/repo/domain list in TOML, JSON, YAML, workflow defaults, or repo
  metadata is still runtime authority unless it is docs, tests, or the
  Launchplane self-bootstrap exception above.
- Update docs in the same change when behavior or ownership changes.
- Fix root causes, not symptoms; avoid workaround-only flows unless the
  Director explicitly asks for a time-boxed mitigation.
- Do not propose, add, or apply new GitHub-secret/workflow-managed
  authorization grants; granting access is a stop boundary. On
  `authorization_denied`, name the exact denied action and follow
  `docs/authorization-authority.md#denial-handling`: a refused read is a missing
  standing read grant to report, and a refused write, grant, or change means
  asking the Director once. Continue with work that does not depend on it.
- Dispatch and watch protected GitHub admin workflows only through the
  installed `github_workflow_babysit.py` helper. Do not use raw
  `gh workflow run`, `gh run watch`, or a generic run waiter for those jobs;
  the helper preserves split identities and surfaces environment waits.

## Workflow Loop

- Plan → patch → proportionate behavior tests → iterate → required gate.
- Keep changes small and coherent around a single ownership boundary.

## Quality Gates

- Use `.github/github.json` for the current test, lint,
  typecheck, build, inspection, and docs-freshness gates.
- Changes to agent instructions, execution guidance, approval or safety rules,
  credential handling, or destructive helpers need review by another model
  before delivery. Use the `model-review` skill and its finding-disposition
  rules; record the provider/model and how findings were handled in the PR.
  Reviewer approval is not a merge or completion gate.
- Add targeted tests whenever contract or storage behavior changes. Broaden to
  integration or full-suite proof when the changed behavior, a failure, or the
  configured review/CI gate justifies it.
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
