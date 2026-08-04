---
title: New Product Repo
---

## Purpose

Use this checklist when creating a website or service repo that will be operated
by Launchplane. The goal is a normal product repo with a thin Launchplane
handoff, not a repo that grows its own control plane.

## Build The Product First

Create the repo around the product's normal development needs:

- application source, tests, and local dev commands
- package manager lockfile and dependency policy
- Dockerfile or artifact build contract
- local-only fixtures, seed data, and development database helpers when needed
- product-specific smoke checks that prove real product behavior

Keep Launchplane lifecycle records, lane topology, provider targets, managed
secrets, and deployment truth out of the repo.

## Runtime Contract

Every Launchplane-operated web product should expose a small runtime contract:

- immutable container image or artifact reference
- known runtime port
- health endpoint path
- non-secret build revision or image tag in the health response
- Launchplane runtime identity echo from `LAUNCHPLANE_RUNTIME_IDENTITY_JSON`
  before the lane is marked strict for runtime identity verification
- documented required runtime environment keys
- product-specific smoke check command when generic health is not enough

For most web products, `generic-web` can use this contract directly from the
DB-backed product profile.

Simple service products, such as bots or workers deployed as Dokploy
applications, can also use `generic-web` when their lifecycle is image deploy,
optional health verification, and Launchplane-owned provider mutation. See
[dokploy-service-deployments.md](dokploy-service-deployments.md) for the
service-specific contract, including persistent volumes and internal ports such
as Discord Blue's Every Code bridge port `8787`.

## Launchplane Records

Before wiring workflows, seed or verify these records in Launchplane with an
operator-owned onboarding manifest through the Product Onboarding workflow or
`POST /v1/product-onboarding/apply`. Direct local DB apply from a checkout is a
break-glass local/bootstrap repair path and requires an explicit acknowledgement:

```sh
uv run launchplane product-onboarding apply \
  --database-url "$LAUNCHPLANE_DATABASE_URL" \
  --manifest-file state/product-onboarding/<product>.json \
  --allow-direct-db-mutation
```

The manifest is applied idempotently and writes Launchplane-owned records for:

- product profile with product key, owning repo, driver id, image repository,
  runtime port, health path, and preview policy
- lane profiles for stable instances such as `testing` and `prod`
- provider target records and target-id records
- runtime-environment records for non-secret settings
- disabled managed secret binding placeholders for required secret keys

Each onboarding target entry must include the live provider `target_id`.
Manifests must use the neutral `provider_targets` input name. Launchplane
rejects obsolete `dokploy_targets` input with a validation error and fails
closed instead of seeding a target record that a later deploy cannot resolve.
Product onboarding and context cutover evidence uses provider-neutral response
keys (`provider_targets` and `provider_target_ids`) even when Dokploy remains
the runtime execution provider.

When the Dokploy application or compose target already exists, adopt it into
Launchplane before or after onboarding instead of hand-editing target ids into
repo-local files:

```sh
uv run launchplane dokploy-targets adopt \
  --database-url "$LAUNCHPLANE_DATABASE_URL" \
  --context <product-context> \
  --instance prod \
  --target-type application \
  --target-id <dokploy-application-id>
```

The command is a dry run unless `--apply` is supplied. It fetches the live
Dokploy target, stores only Launchplane-owned target metadata and the target-id
record, and intentionally does not copy provider env text or secret-shaped
values. Local apply also requires `--allow-direct-db-mutation` and is explicit
local/bootstrap repair only; routine shared/live target setup should use the
manual `Dokploy Target Setup` workflow. Use `--project-name`, `--target-name`,
`--domain`, and `--healthcheck-path` when the provider payload does not expose
enough redacted metadata for the record.

For shared or production live mutations, use the manual `Dokploy Target Setup`
workflow instead of local CLI commands. The workflow calls the deployed
Launchplane service route `POST /v1/dokploy-targets/setup` with GitHub OIDC,
supports dry-run and apply modes, and writes the Dokploy target, target-id, and
provider-target records through Launchplane storage. `create-compose` is the
stable Odoo setup path when no Dokploy compose target exists yet; provide the
Dokploy project/environment or project name, server id, target name, optional
domain hosts, runtime port, and an operator reason before applying. Runtime port
is used only for `create-compose` domain reconciliation and requires at least
one domain. If a provider create succeeds but the follow-up Launchplane record
write fails, note the created Dokploy compose/application id from the workflow
logs and re-run the workflow with `operation=adopt` for the same
context/instance instead of creating another target.
When repairing an accidental target-authority collision, pass
`expected_current_provider_target_json` from provider-target audit evidence so
Launchplane replaces the existing provider-target row only if the live DB-backed
authority still matches the reviewed old target. Without that expectation,
target setup continues to fail closed instead of replacing explicit provider
authority.

When the Dokploy application does not exist yet, let Launchplane plan and apply
the provider mutation so the app id is captured in records immediately:

```sh
uv run launchplane dokploy-targets create-application \
  --database-url "$LAUNCHPLANE_DATABASE_URL" \
  --context <product-context> \
  --instance prod \
  --target-name <dokploy-application-name> \
  --project-name <dokploy-project-name>
```

This command is also dry-run by default. With local
`--apply --allow-direct-db-mutation`, it can create a Dokploy project,
environment, and application, then write the matching tracked target and
target-id records. Use `--project-id` or `--environment-id` to reuse existing
provider containers, and `--server-id` when the app belongs on a remote Dokploy
server. Routine shared/live creation should use the manual workflow above. It
still does not configure secrets or copy provider env text;
runtime and secret records remain separate Launchplane-owned setup steps.

Then import or update DB-backed authz policy records for the product's GitHub
Actions workflows. Authz policy merging remains a separate operator step so a
new product onboarding manifest cannot accidentally replace unrelated product
access rules.

Do not store these as product-repo Launchplane manifests. The repo may document
the expected app runtime contract, but Launchplane records are the live source
of lifecycle truth. Store operator manifests under Launchplane state or another
operator-owned state location, not in product repos and not in git-tracked
history when they contain site-specific runtime details.

## GitHub Actions Shape

Start with these workflows:

- CI: lint, test, build, and product-owned checks.
- Security: causal pull-request dependency checks plus absolute default-branch
  and artifact health appropriate for the repo. See
  [dependency-health-contract.md](dependency-health-contract.md).
- Publish image: build and publish an immutable artifact, then trigger
  Launchplane stable deploy for `testing`.
- Preview trigger: for PRs that request preview, build and publish an immutable
  preview image, then trigger Launchplane preview refresh.
- Preview cleanup trigger: on PR close or preview label removal, trigger
  Launchplane preview destroy.

The Launchplane trigger steps should use GitHub Actions OIDC and pass minimal
facts only: product key, source ref or SHA, PR number when relevant, immutable
artifact reference, and optional run URL.

For a conventional generic-web product, use the thin preview facade documented
in [product-repo-contract.md](product-repo-contract.md). One same-repository
`pull_request` caller delegates image publication, preview refresh,
product-owned verification, evidence, and feedback to
`reusable-generic-web-preview.yml`. A second `pull_request_target` caller uses
`reusable-preview-request-notice.yml` for trusted same-repository cleanup and
fork/Dependabot notices. Pin both reusable workflows to full reviewed
Launchplane commit SHAs. Do not grant OIDC to product build or verification
jobs; the reusable workflows scope OIDC to Launchplane requests that do not
check out untrusted code.

When a product workflow needs to turn local publish/provision/verification or
cleanup job results into preview feedback status, call
`cbusillo/launchplane/.github/workflows/reusable-preview-feedback-status.yml@<launchplane-sha>`.
Keep product-owned smoke facts local, pass primitive job results and failure
summaries to the reusable workflow, and let Launchplane derive the final
`status` and `failure_summary` before it calls `reusable-preview-pr-feedback`.
Do not copy Launchplane feedback route, payload, idempotency, marker, or delivery
logic into the product repo.

For direct JSON calls to Launchplane service routes, use the reusable
`cbusillo/launchplane/.github/actions/launchplane-request` action rather than
copying an OIDC/fetch helper into the product repo. Product repos can still keep
small scripts that assemble product-specific payload JSON until Launchplane owns
that request-shaping layer too.

If a product-owned smoke test creates dynamic users and needs Launchplane to
grant, promote, or clean them up during the same browser run, install the
Launchplane-owned smoke maintenance client with
`cbusillo/launchplane/.github/actions/setup-smoke-maintenance-client@<launchplane-sha>` and
import the generated client from the smoke script. The workflow job must grant
`id-token: write` for the client to authenticate to Launchplane. Do not copy
Launchplane OIDC, route, payload, driver intent, idempotency, or retry helpers
into the product repo for that path.

## Choose A Driver

Use `generic-web` when the product is a stateless or mostly stateless web app,
or a simple service deployed as a Dokploy application, whose lifecycle is image
deploy, health check, preview refresh when enabled, preview cleanup when
enabled, and PR feedback.

Create a product driver when the product has named extra obligations:

- database migration, clone, bootstrap, seed, or anonymization
- backup gate, restore, rollback, or destructive repair behavior
- product-specific promotion smoke checks
- post-deploy maintenance commands
- platform-specific artifact or runtime semantics

See [driver-development.md](driver-development.md) for the driver workflow and
[product-repo-contract.md](product-repo-contract.md) for the approval gate.

## Before Approval

Before treating the repo as Launchplane-ready:

- CI and pull-request dependency regression checks pass, and the current
  default-branch/artifact absolute health evidence is acceptable.
- The image or artifact is immutable and traceable to a source SHA.
- Launchplane can read the product profile and target records.
- A non-prod deploy or preview path has been exercised through Launchplane.
- Product workflows do not mutate providers directly.
- Product workflows do not render Launchplane evidence or PR feedback markdown.
- Any remaining Launchplane adapter scripts are small, temporary, and listed as
  migration candidates.
