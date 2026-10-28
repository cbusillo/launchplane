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

Use `uv` with the Launchplane service and development runtime selected by
[.python-version](.python-version). The supported-product floor remains the
separate `requires-python` contract in [pyproject.toml](pyproject.toml); reusable
workflows likewise retain caller-selected interpreters. Node and pnpm are selected
by `engines.node` and `packageManager` in
[frontend/package.json](frontend/package.json). CI reads these authorities.
Docker image tags must be literal at Dockerfile parse time, so its Python runtime
and Node image major are necessary copies reviewed against those files. The
PyCharm preparation version in `.github/github.json` is likewise a literal IDE
integration setting and follows the runtime pin.
From the repository root:

```bash
uv sync --extra dev
uv run launchplane --help
uv run launchplane service serve --help
(cd frontend && pnpm install --frozen-lockfile)
(cd frontend && pnpm validate)
```

Frontend validation runs unit tests, the agent-contract drift check, TypeScript,
and the production Vite build. The canonical OpenAPI JSON, UI slice and TypeScript
client are ignored build artifacts generated from Python source before frontend
tests, development, type checks and production builds, including Docker and CI.
Python contract tests and inventory scripts also derive schemas from source.
After a backend contract change, run `(cd frontend && pnpm generate:openapi)`
and commit only the agent contract. CI still rejects drift in that runtime contract.
For IDE type resolution before the first build, run
`(cd frontend && pnpm generate:openapi-types)` once.
`(cd frontend && pnpm test:browser)` runs the Playwright
browser smoke. Use [`.github/github.json`](.github/github.json) for current gates
and [testing style](docs/style/testing.md) for choosing proportionate tests:

```bash
uv run --extra dev python -m unittest tests.test_module_name
```

Replace `tests.test_module_name` with the module for the changed behavior.
Follow [testing style](docs/style/testing.md#local-test-loop) for local safety
module selection, the Q778 focused PR pilot and the full candidate merge gate.

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
