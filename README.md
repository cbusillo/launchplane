# launchplane

Launchplane is a control-plane service for release records, environment
operations, previews, promotion orchestration, and the merge train. Its Python
package is `control_plane`; its CLI is `launchplane`.

Read the Director's [overall direction](https://github.com/cbusillo/direction/blob/main/DIRECTION.md),
then this repository's [DIRECTION.md](DIRECTION.md) for purpose, priorities,
roles, stop boundaries, and retired designs. Overall direction takes precedence;
issues track work. [AGENTS.md](AGENTS.md) is the agent execution guide and the only
agent-instruction filename, including any directory-specific guidance.

## Development

Use Python 3.13 or later with `uv`, and Node 22.12 or later with the pnpm version
in [frontend/package.json](frontend/package.json). From the repository root:

```bash
uv sync --extra dev
uv run launchplane --help
uv run launchplane service serve --help
pnpm --dir frontend install --frozen-lockfile
pnpm --dir frontend validate
```

Frontend validation runs unit tests, the OpenAPI drift check, TypeScript, and the
production Vite build. `pnpm --dir frontend test:browser` runs the Playwright
browser smoke. Use [`.github/github.json`](.github/github.json) for current gates
and [testing style](docs/style/testing.md) for choosing proportionate tests:

```bash
uv run --extra dev python -m unittest tests.test_module_name
uv run --extra dev launchplane ci unittest-shard local
```

Replace `tests.test_module_name` with the module for the changed behavior. The
second command is the full-suite gate when the change warrants it.

Implementation goes through a task branch and PR. Once required checks pass and
review findings are accounted for, follow [AGENTS.md](AGENTS.md) to hand the PR
to the merge train. Follow DIRECTION.md for merge and release authority.

## Service and runtime records

The service exposes authenticated HTTP APIs and the browser UI at `/` and `/ui`.
Shared runtime truth is DB-backed, including deployment, promotion, inventory,
preview, runtime-environment, authorization, and encrypted managed-secret
records. File-backed records and local CLI operations support development,
rehearsal, and explicit bootstrap repair; they are not shared runtime authority.

Use the deployed service API or Launchplane UI for routine shared/runtime
mutations. Direct DB mutation flags are local/bootstrap repair tools. See the
[configuration boundary](docs/config-boundary.md),
[service boundary](docs/service-boundary.md), and [operations](docs/operations.md)
for supported paths and their prerequisites.

The container entrypoint, [Dockerfile](Dockerfile), and
[compose definition](docker-compose.yml) package the service and built frontend.
Supply the minimal bootstrap inputs described in the
[service deploy posture](docs/operations.md#launchplane-service-deploy-posture)
before starting the service. That page owns OAuth setup, self-deploy workflow
configuration, immutable image digests, health checks, migrations, and rollback.
For a served-UI check use a browser or `GET /ui`; a failed `HEAD /ui` probe alone
does not establish that the UI is unavailable.

## Documentation

Start with [docs/README.md](docs/README.md), which indexes implemented contracts,
targets, and retired compatibility surfaces. Key references:

- [Architecture](docs/architecture.md): control-plane and product-repository boundaries.
- [Artifact provenance](docs/artifact-provenance.md) and
  [event-driven deploys](docs/event-driven-deploys.md): the product build handoff.
- [Client review](docs/owner-acceptance.md) and
  [release review](docs/release-review.md): release decisions and gated promotion.
- [Records](docs/records.md) and [secrets](docs/secrets.md): persistence and secret handling.
- [Merge train policy](docs/merge-train-policy.md): train contracts and evidence.
- [Public readiness](docs/public-readiness.md): public-source posture and runtime-data boundaries.
