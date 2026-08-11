---
title: Config Boundary
---

<!-- markdownlint-disable MD013 -->

## Purpose

- Make Launchplane's intended configuration authority explicit.
- Separate bootstrap/root-of-trust inputs from live mutable control-plane config.
- Keep the DB-backed config boundary explicit now that implicit file/env
  fallback readers have been removed.

## Final Boundary

Launchplane's long-term config model is:

- Launchplane's minimal self-bootstrap/root-of-trust stays outside the database
- all other live mutable config is DB-backed
- checked-in repo files are examples, docs, schemas, tests, and workflows; live
  authz and target files are not repo authority
- checked-in code or config must not act as authority for real product, tenant,
  repository, branch, domain, lane, provider-target, runtime-environment, authz,
  operator, or mutable product/runtime configuration
- local files under `~/.config/launchplane/` are not Launchplane config
  authority and should be archived or deleted when found
- the service never silently falls back across multiple live authorities

In steady state, if a DB-backed config class is missing from Launchplane's
shared store, Launchplane should fail closed.

## Source-Of-Truth Matrix

### Bootstrap Env Only

These values remain outside the database because Launchplane needs them before
it can reach, trust, or decrypt DB-backed state.

This category is only for Launchplane's own startup/root-of-trust wiring. It is
not a general exception for product, tenant, repository, lane, provider,
workflow, or operator configuration.

Launchplane-owned self-management workflows may carry the fixed
`product="launchplane"` value only when the paired service route itself rejects
other products and authorizes the request against Launchplane's own product and
service context. This exception does not apply to product-repo workflows,
reusable workflow defaults, or routes that accept product-owned runtime targets.

Ingress route workflows may forward operator-supplied product, context, domain,
and edge-endpoint intent to Launchplane. They must not carry fixed canary target
topology or GitHub-variable-backed product/context authority in workflow code.

| Class                                     | Current surface                                                                                                                                                                 | Final authority                  | Notes                                                                                                                                                                                                                                                                                                                                 |
| ----------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Database connectivity                     | `LAUNCHPLANE_DATABASE_URL`                                                                                                                                                      | Bootstrap env                    | Required before Launchplane can read DB-backed config.                                                                                                                                                                                                                                                                                |
| Secret decryption root/key-ring bootstrap | `LAUNCHPLANE_MASTER_ENCRYPTION_KEY` or future platform-secret references                                                                                                        | Bootstrap env or platform secret | Minimal root-of-trust needed to decrypt DB-backed managed-secret versions before the secret store is usable. It may identify active and historical decryption roots by non-secret key id, but must not become product/runtime secret authority, provider credential fallback, or checked-in key catalog.                              |
| Authz bootstrap                           | `LAUNCHPLANE_POLICY_TOML`, `LAUNCHPLANE_POLICY_B64`, `LAUNCHPLANE_POLICY_FILE`                                                                                                  | Minimal bootstrap env/file       | Root of trust for first start and DB policy repair only. Live product/workflow grants are DB-backed authz policy records. Existing name-only rules remain readable until managed reconciliation adopts or retires them, but every desired GitHub Actions managed rule must include immutable repository and owner IDs. |
| Bootstrap admin emails                    | `LAUNCHPLANE_BOOTSTRAP_ADMIN_EMAILS`                                                                                                                                            | Bootstrap env                    | First-start GitHub human admin recovery only, before a DB-backed `github_humans` rule exists. Not a production identity authority and not a place for product, tenant, lane, provider, or operator assignment lists.                                                                                                                  |
| Future OIDC identity provider wiring      | OIDC issuer or discovery endpoint, expected audience, Launchplane client id, client secret or managed platform secret reference, session-signing root                           | Bootstrap env or platform secret | Boundary shape only for a future Keycloak or comparable OIDC slice. Required so Launchplane can validate provider tokens before DB-backed records are reachable. Live realms, users, groups, service clients beyond Launchplane's own client wiring, grants, OpenFGA tuples, and operator memberships are not bootstrap authority.    |
| Launchplane self image ref                | `DOCKER_IMAGE_REFERENCE`                                                                                                                                                        | Service target env               | Needed for Launchplane self-deploy and rollback posture.                                                                                                                                                                                                                                                                              |
| Process wiring                            | `LAUNCHPLANE_SERVICE_HOST`, `LAUNCHPLANE_SERVICE_PORT`, `LAUNCHPLANE_SERVICE_AUDIENCE`, `LAUNCHPLANE_STATE_DIR`, `LAUNCHPLANE_APP_ROOT`, `LAUNCHPLANE_COMPOSE_EXTERNAL_NETWORK` | Service target env               | Runtime/process wiring, not product config. `LAUNCHPLANE_STATE_DIR` is a non-authoritative runtime directory; service persistence still requires `LAUNCHPLANE_DATABASE_URL`. The external compose network value is operator-owned provider wiring for Launchplane's own deployed services and must not encode product/lane authority. |
| Every Code webhook ingress secret         | `LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET`                                                                                                                                  | Bootstrap env or platform secret | Required before unauthenticated GitHub webhook ingress can trust the request body. Store it outside repository config.                                                                                                                                                                                                                |
| Manager-preview webhook ingress secret    | `LAUNCHPLANE_MANAGER_PREVIEW_GITHUB_WEBHOOK_SECRET`                                                                                                                            | Bootstrap env or platform secret | Required before the manager-preview GitHub webhook route can trust comment and pull-request lifecycle deliveries. Keep it route-specific, configure the matching GitHub webhook outside repository config, and never reuse the Every Code webhook secret.                                                                           |
| Every Code worker bearer token            | `LAUNCHPLANE_EVERY_CODE_WORKER_TOKEN`                                                                                                                                           | Bootstrap env or platform secret | Shared by the Launchplane service and local worker to authorize worker read/claim/status routes. Store it outside repository config.                                                                                                                                                                                                  |
| Engineering review worker identity        | `LAUNCHPLANE_ENGINEERING_REVIEW_WORKER_RUNTIME_ID`, `LAUNCHPLANE_ENGINEERING_REVIEW_WORKER_HOST`                                                                                | Service target env               | Server-owned identity bound to the Every Code worker token for engineering-review list/claim/start/fail routes. Worker requests cannot select or override these values; they must match the active DB-backed review authority.                                                                                                        |
| Every Code claim-comment GitHub identity  | `LAUNCHPLANE_EVERY_CODE_GITHUB_TOKEN`, optional `LAUNCHPLANE_EVERY_CODE_GITHUB_ACTOR`                                                                                           | Bootstrap env or platform secret | Used only by the local Every Code worker when it posts the public claim comment on a GitHub issue. The worker verifies the token actor before posting and fails closed instead of falling back to active local `gh` credentials.                                                                                                      |
| Terminal-agent read bearer token          | `LAUNCHPLANE_TERMINAL_AGENT_READ_TOKEN`, `LAUNCHPLANE_TERMINAL_AGENT_SUBJECT`, `LAUNCHPLANE_TERMINAL_AGENT_TOKEN_LABEL`                                                         | Bootstrap env or platform secret | Shared by the Launchplane service and a trusted local terminal agent for redacted `GET` context reads only. Store it outside repository config and keep it distinct from Every Code worker credentials.                                                                                                                               |
| Local-operator write bearer token         | `LAUNCHPLANE_LOCAL_OPERATOR_TOKEN`, `LAUNCHPLANE_LOCAL_OPERATOR_SUBJECT`, `LAUNCHPLANE_LOCAL_OPERATOR_TOKEN_LABEL`                                                              | Bootstrap env or platform secret | Shared by the Launchplane service and trusted local owner automation for routine reason-bearing operator mutations. Exact authority is DB-backed by `local_operators` authz policy rules. Store it outside repository config and keep it distinct from read-only terminal-agent credentials.                                          |
| Local-admin write bearer token            | `LAUNCHPLANE_LOCAL_ADMIN_TOKEN`, `LAUNCHPLANE_LOCAL_ADMIN_SUBJECT`, `LAUNCHPLANE_LOCAL_ADMIN_TOKEN_LABEL`                                                                       | Bootstrap env or platform secret | Shared by the Launchplane service and trusted local owner automation for rare privileged mutations. Exact authority is DB-backed by `local_admins` authz policy rules; the token alone does not grant blanket access. Store it outside repository config and load it only for deliberate escalation.                                  |

The Launchplane self-deploy workflow has a manual `omit_every_code_env`
compatibility input for the one deploy that teaches an older running service to
accept the Every Code env keys. Leave it unset for normal deploys so the service
and worker keep the shared token/webhook secret in sync.

The manual `omit_terminal_agent_env` compatibility input omits terminal-agent
read credentials. Owner-agent write credentials use the separate
`omit_owner_agent_env` compatibility input. Leave both unset for normal deploys
after the service accepts terminal-agent, local-operator, and local-admin keys.

The manual `omit_npmplus_env` compatibility input removes NPMplus service env
keys from the deployed Launchplane target instead of only skipping new writes.
Leave it unset for normal deploys so the ingress driver can build its provider
client from the service target env until those provider credentials move behind
managed runtime records.

Public-ingress GitHub issue notifications use the platform-projected
`LAUNCHPLANE_PUBLIC_INGRESS_GITHUB_TOKEN` service env value from the same-named
deploy secret. The token authenticates create, comment, and close delivery
through the managed automation identity; notification delivery fails closed when
the token is absent and never falls back to active local `gh` authentication.

Every Code Discord notification routing is DB-backed. The service reads
`EveryCodeNotificationPolicyRecord` records and resolves Discord webhook values
through managed secret records scoped to Launchplane/Every Code. Do not add a
service-host env var or checked-in file containing the webhook URL, channel, or
real destination authority.

| Work graph GitHub Project read source | `LAUNCHPLANE_WORK_GRAPH_PROJECT_OWNER`, `LAUNCHPLANE_WORK_GRAPH_PROJECT_NUMBER`, optional `LAUNCHPLANE_WORK_GRAPH_PROJECT_LIMIT`, optional `LAUNCHPLANE_WORK_GRAPH_PROJECT_SIGNAL_LIMIT`, optional `LAUNCHPLANE_WORK_GRAPH_GH_BINARY` | Service target env | Opt-in read source for compact Project fields plus bounded dependency, subissue, and PR check signals. Deploy automation forwards these values only when the GitHub Project token secret is present. Requires a `gh` credential with the GitHub CLI `project` scope. Does not store copied issue bodies. |
| Work graph and merge-train GitHub token | `GH_TOKEN` from deploy secret `LAUNCHPLANE_WORK_GRAPH_GH_TOKEN` | Platform secret projected into service target env | Authenticates the service's non-interactive `gh` reads and merge-train GitHub API calls. The token must have enough GitHub access for the configured Project, issue/PR signal reads, and the configured merge-train repository. |

### DB Authoritative

These values are live mutable control-plane config and should resolve from
Launchplane records/secrets instead of repo files or operator-local env.

| Class                         | Current surface                    | Final authority                     | Notes                                                                                                                                                                                                                          |
| ----------------------------- | ---------------------------------- | ----------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Merge-train repository policy | `launchplane_merge_train_policies` | DB-backed Launchplane policy record | Defines supported repository/base branch merge-train policies. The service fails closed when no active policy record exists and for unsupported pairs before GitHub calls. Repo TOML files are not a supported live authority. |
| Tenant repository classification and admission authority | `launchplane_tenant_repository_classifications`, repository human role policies, technical-waiver events, trusted-maintenance policies/evidence, manager-preview records/events, and managed authz policy records | DB-backed Launchplane records | Numeric repository classification selects engineering normal flow or tenant UI admission. Tenant UI admission is recomputed as an exact-head OR across manager preview approval, technical human waiver, and trusted maintenance; the GitHub status is projection only. Revisions/events remain DB authority, filesystem records are rehearsal/import input, and no service-host env or checked-in repository list decides admission. |

| Class                                      | Current surface(s)                                                                         | Final authority                                                                                               | Notes                                                                                                                                                                                                                                                       |
| ------------------------------------------ | ------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Dokploy credentials                        | Launchplane managed secrets (`DOKPLOY_HOST`, `DOKPLOY_TOKEN`)                              | Launchplane managed secrets                                                                                   | Fail closed when the shared store does not have both bindings.                                                                                                                                                                                              |
| Dokploy edge upstream endpoints            | `launchplane_edge_endpoints`                                                               | DB-backed Launchplane edge endpoint records                                                                   | Server identity is human-readable, but provider upstreams passed to NPMplus must be stored IP addresses. Product repos and ad hoc workflow inputs are not durable topology authority.                                                                       |
| Ingress canary routes                      | `launchplane_ingress_canary_routes`                                                        | DB-backed Launchplane ingress canary route records                                                            | Stores canary domain, expected provider host id, certificate id, and edge endpoint key. Workflows select a canary key and do not pass route topology.                                                                                                       |
| Environment route bindings                 | `launchplane_route_bindings`                                                               | DB-backed Launchplane route binding records                                                                   | Joins a product/context/instance to desired domains, runtime target summary, ingress termination, and TLS owner. Provider-specific host ids, certificate ids, target ids, edge IPs, and provider payloads are evidence only, not neutral authority.           |
| Private health endpoint URLs               | `launchplane_private_health_endpoints`                                                     | DB-backed Launchplane private health endpoint records                                                         | Product profiles declare private monitoring intent by check name and optional endpoint key; mutable private URLs live in Launchplane runtime records, not repos or workflow defaults.                                                                       |
| Runtime environment values                 | Runtime-environment records                                                                | Launchplane runtime-environment records                                                                       | Includes shared, context, and instance-scoped values.                                                                                                                                                                                                       |
| Secret-shaped runtime keys                 | Managed runtime secrets overlay                                                            | Launchplane managed secrets                                                                                   | Includes `*_PASSWORD`, `*_TOKEN`, `*_SECRET`, `*_KEY`. Secret versions carry non-secret key-id and rotation metadata in Launchplane records, not service-host env or repo files.                                                                            |
| Runtime key-safety policy                  | `launchplane_runtime_key_safety_policies`                                                  | Launchplane runtime key-safety policy records                                                                 | Classifies managed secret binding keys by runtime class and scope. Requests carry metadata only and cannot replace secret values.                                                                                                                           |
| Relationship authorization tuples/grants   | DB-backed authz policy records; future OpenFGA tuple store if adopted                      | Launchplane records during migration, then authorization provider state plus Launchplane audit/import records | Checked-in model files may define generic relation schemas and validators only. Real tuples, grants, products, repos, branches, domains, lanes, provider targets, operators, clients, groups, and assignments are never repo or workflow-default authority. |
| Ship mode overrides                        | `DOKPLOY_SHIP_MODE`, `DOKPLOY_SHIP_MODE_<CTX>_<INSTANCE>`                                  | Launchplane runtime-environment records                                                                       | Mutable operator behavior, not bootstrap.                                                                                                                                                                                                                   |
| Preview routing/config                     | `LAUNCHPLANE_PREVIEW_BASE_URL`                                                             | Launchplane runtime-environment records                                                                       | Shared control-plane-owned runtime value.                                                                                                                                                                                                                   |
| GitHub workflow runtime integration values | `GITHUB_TOKEN`, `GITHUB_WEBHOOK_SECRET`                                                    | Launchplane runtime-environment records and managed secrets                                                   | Current docs already classify these as DB-backed target state.                                                                                                                                                                                              |
| Product/tenant runtime env                 | Odoo runtime values, tenant-specific env keys                                              | Launchplane runtime-environment records and managed secrets                                                   | Includes shared and per-instance overlays.                                                                                                                                                                                                                  |
| Odoo application override intent           | Former `ENV_OVERRIDE_CONFIG_PARAM__*`, Authentik, and Shopify override shapes              | Launchplane Odoo instance override records plus managed secret bindings                                       | `ENV_OVERRIDE_*` names are migration inputs to retire, not the durable contract.                                                                                                                                                                            |
| Worker/runtime-action config               | Product-specific worker commands, host/user metadata, operation knobs, and secret bindings | Launchplane runtime-environment records and managed secrets                                                   | Delegated-worker dispatch strips inherited process values and injects the DB-resolved runtime contract into the worker environment.                                                                                                                         |
| Dokploy target-id overrides                | DB records                                                                                 | Launchplane target-id records                                                                                 | File catalogs are not a supported authority.                                                                                                                                                                                                                |
| Stable target definitions                  | Launchplane DB-backed target records                                                       | Launchplane DB-backed target records                                                                          | Repo catalogs should be examples only, not seed or authority material.                                                                                                                                                                                      |
| Release tuple baseline authority           | Launchplane release-tuple records                                                          | Launchplane record store                                                                                      | Repo catalogs should not be treated as live mutable authority.                                                                                                                                                                                              |

### Repo Only

These stay in git, but not as live mutable runtime authority.

| Class                     | Examples                                                                   |
| ------------------------- | -------------------------------------------------------------------------- |
| Bootstrap policy snippets | Docs and test fixtures only; no tracked live `config/*.toml` policy source |
| Docs/specs                | `docs/*`, `README.md`                                                      |
| Schemas/tests             | storage schema code, tests, fixtures                                       |

Checked-in workflows and repo metadata may route to Launchplane, run quality
gates, and document examples. They must not define the real product catalog,
repo catalog, lane topology, target inventory, domain inventory, authz grants,
operator identities, or mutable runtime values used by production behavior.
Product-repo deploy workflows may forward operator-owned GitHub variables and
fresh image build outputs into Launchplane request payloads, but fixed image
references, provider targets, domains, and secret values remain outside the
checked-in workflow authority boundary.

### Stale Local Artifacts

These should not be treated as Launchplane config. If found, archive or delete
them after verifying the equivalent authority is represented in DB-backed
records or bootstrap env.

| Class                                        | Final location                                    | Notes                                                                |
| -------------------------------------------- | ------------------------------------------------- | -------------------------------------------------------------------- |
| Legacy operator env file                     | `~/.config/launchplane/dokploy.env`               | Not a supported Launchplane input.                                   |
| Legacy runtime environments file             | `~/.config/launchplane/runtime-environments.toml` | Not a supported Launchplane input.                                   |
| Legacy local policy copies after replacement | `~/.config/launchplane/...`                       | Not a supported Launchplane input once bootstrap policy is replaced. |

## Removed Runtime Fallbacks

The DB-backed cutover removed these surfaces as implicit runtime readers for
DB-backed config classes.

- repo-local `.env`
- repo-local `config/runtime-environments.toml`
- repo-local `config/dokploy-targets.toml` as live target authority
- repo-local `config/launchplane-authz.toml` as live authz policy authority
- `~/.config/launchplane/dokploy.env`
- `~/.config/launchplane/runtime-environments.toml`

These are stale artifacts only. They are not supported import or compatibility
surfaces for DB-backed config classes.

## Current Code Reality

The codebase now points more directly at the final model, but it still mixes
live authority across DB, files, and process env:

- runtime environments fail closed unless DB-backed records exist, then overlay
  managed secrets
- Dokploy target-id overrides resolve from DB in steady state
- Dokploy credentials resolve from Launchplane-managed secrets only and fail
  closed when the shared store is missing either binding
- stable target definitions resolve from DB-backed tracked target records
- release tuple baseline resolution fails closed unless DB-backed release-tuple
  records exist
- VeriReel prod rollback dispatch resolves worker/runtime-action config from
  `verireel/prod` runtime-environment records, then passes those values to the
  delegated worker process
- VeriReel prod backup-gate dispatch resolves the same `verireel/prod`
  delegated-worker runtime contract plus backup-shape values such as
  `VERIREEL_PROD_BACKUP_MODE`, `VERIREEL_PROD_BACKUP_STORAGE`,
  `VERIREEL_PROD_SNAPSHOT_PREFIX`, `VERIREEL_PROD_SNAPSHOT_KEEP`, and
  `VERIREEL_PROD_GATE_HEALTH_TIMEOUT_MS` from DB-backed runtime-environment
  records before it captures the backup gate
- VeriReel app maintenance, preview refresh, preview destroy, and preview
  inventory routes resolve Dokploy host, token, preview URL shape, app identity,
  and target identity from Launchplane-managed secrets plus DB-backed runtime
  and target records. `LAUNCHPLANE_PREVIEW_BASE_URL` is a context-level runtime
  value. Product-repo app-maintenance workflows must pass operation intent;
  product-repo workflows may pass preview slug and GitHub OIDC identity, but not
  Dokploy credentials or preview domain topology.
- VeriReel stable environment metadata, including testing/prod target names,
  target ids, base URLs, and health URLs, is served by Launchplane from
  DB-backed target/runtime records. Product-repo workflows should ask
  Launchplane for those values instead of hard-coding stable lane topology.
- Product onboarding and runtime key-safety policy writes use Launchplane service
  routes with scoped authorization. Product/runtime records are not read from
  checked-in catalogs during deploy or repair.

The remaining transition surface is legacy-path visibility, not runtime fallback
authority or supported import compatibility.

## Cutover Rules

- If a config class is listed as DB authoritative here, Launchplane should read
  it from DB only in steady state.
- Files are not accepted as DB-authoritative config inputs. Use DB-backed
  records or managed secrets directly.
- Launchplane should fail closed when DB-backed config is missing.
- Repo-owned config files may document non-runtime examples, but they should not
  seed or act as live source of truth for production behavior.
- Moving real values from Python into checked-in TOML, JSON, YAML, workflow
  defaults, or repo metadata does not satisfy this boundary; it only moves the
  violation. Use Launchplane records, managed secrets, or explicit
  operator-supplied input instead.

## Inspection

Use the local inspection command to see which parts of the current config
contract are still DB-backed, file-backed, or mixed:

```bash
uv run launchplane service inspect-config-boundary --control-plane-root .
```

That payload is intended to make DB-backed authority and stale legacy files
visible without treating those files as runtime inputs.

Use the read-only config-authority audit to produce a redacted checked-in
surface report with input/finding hashes, file/worktree hashes, explicit allow
reasons, and coverage gaps:

```bash
uv run launchplane service audit-config-authority --control-plane-root .
```

The initial audit is report-only. Use it to classify findings and understand the
baseline before adding changed-file enforcement.

After the baseline is clean for a repo, enforce only the changed files in CI or
review workflows:

```bash
uv run launchplane service audit-config-authority \
  --control-plane-root . \
  --mode changed-files-gate \
  --fail-on-findings \
  --gate-profile product-repo
```

`--fail-on-findings` preserves the JSON or Markdown report, adds a JSON `gate`
summary when enforcement is enabled, and then exits non-zero when the selected
gate profile rejects a finding. In changed-file mode, findings that already
existed at the merge base remain in the report as
`preexisting_changed_file_finding`, but only new unclassified findings block the
gate. If the gate cannot resolve `origin/main` or `main` and has no dirty files
to compare against `HEAD`, it fails closed instead of returning an empty green
report. Allowed docs, tests, schema examples, Launchplane self-bootstrap wiring,
operator-supplied inputs, and thin connector mechanics keep explicit allow
reasons and do not fail the default gate. The `product-repo` profile also
rejects test fixtures that carry Launchplane lifecycle authority such as authz,
runtime-environment, provider target, target-id, managed-secret, route-batch, or
topology material. Product repos should use this changed-file gate to reject
reintroduced
Launchplane-owned authz, route, provider-target, domain, runtime-environment,
managed-secret, topology, or workflow-default fixtures before merge.

When operators need to inspect or mutate tracked Dokploy target records, use the
DB-backed Launchplane CLI surface rather than editing any repo-local file:

```bash
uv run launchplane dokploy-targets list
uv run launchplane dokploy-targets show --context opw --instance testing
uv run launchplane dokploy-targets put-shopify-protected-store-key \
  --context opw \
  --instance testing \
  --key yps-your-part-supplier \
  --allow-direct-db-mutation
```

The mutation commands in that family edit the shared
`launchplane_dokploy_targets` and provider-target record sets directly, so they
require `--allow-direct-db-mutation` and are explicit local/bootstrap repair
only. Routine shared/live target setup should use the deployed service route or
operator workflow.
Product retirement does not add checked-in product or provider authority. The
workflow accepts operator-supplied product, instance, target digest, reason,
issue, reviewed plan, and idempotency values; the service resolves all real
context, provider target, runtime, and secret authority from DB-backed records.
The authorization managed-set secret routes policy material through the
existing protected authz reconciliation workflow and is not a product catalog.
