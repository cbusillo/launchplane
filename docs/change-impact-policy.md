---
title: Change Impact Policy
---

## Purpose

Launchplane now has an additive, shadow-only change-impact classifier for pull
requests. It derives affected products, Owner acceptance impact, and engineering
review tier from Launchplane policy plus trusted repository and change evidence.
Public callers submit only a repository/pull-request target reference and
non-authoritative request metadata. They cannot submit changed files, dependency
or reviewer facts, head/tree expectations, product impact, sensitive areas, or
review tiers.

The classifier is intentionally deterministic for obvious policy matches. It
does not dispatch an extra LLM classification run for routine changes. Missing,
unknown, stale, mixed, ambiguous, or contradictory evidence returns a non-success
classification and fails closed to the stricter engineering-review requirement.

## Policy Records

`ChangeImpactPolicyRecord` is scoped to immutable GitHub repository identity:
numeric repository ID, numeric owner ID, and owner/name. Each revision contains
component rules that bind path prefixes to Launchplane components, affected
product/system scopes, and an engineering review tier of `routine` or
`sensitive`.

Policy records are revisioned, digested, active/superseded records. A decision
must bind the exact repository, pull request number, head SHA, tree SHA, policy
revision, and policy digest. Stale head/tree or stale policy claims return
`stale_head` or `stale_policy` and never produce a success result.

## Evidence Authority

Launchplane resolves evaluation evidence through two server-owned boundaries:

- A server-authenticated GitHub provider resolves immutable repository identity,
  the current pull-request head and tree, and the complete changed-file set. A
  second pull-request read rejects a head that changes during collection, and
  renamed files preserve both old and new paths for policy matching.
- The active DB-backed component policy is authoritative for the affected
  products it declares directly. Launchplane storage is the only source for
  additional dependency or reviewer evidence. Stored reviewer evidence may add
  affected products only when trusted dependency evidence exists for the same
  matched component, and it cannot downgrade a deterministic sensitive match.

GitHub Actions callers are additionally bound to the OIDC repository ID, owner
ID, repository name, and workflow `sha`. A repository or head mismatch is a
non-success result. Human and operator callers still receive current provider
facts rather than caller assertions.

Missing dependency records do not invalidate a product scope declared directly
by the active component policy. They are required only when stored evidence is
used to extend that declared scope. Reviewer-only product claims remain
insufficient and return `unknown`. Provider unavailability and incomplete
pagination fail closed; no caller-controlled evidence fallback is available.

## Output

Every evaluation returns:

- affected product/system scopes and per-product Owner acceptance requirement;
- engineering review tier and the resulting one- or two-review requirement;
- matched rules and trusted evidence;
- exact repository/PR/head/tree and policy provenance;
- explicit unknown evidence when classification fails closed.

The current API is shadow-only: `mode=shadow`, `authoritative=false`, and
`enforcement_effect=none`. It does not alter required GitHub checks.

## HTTP API

Reads:

- `GET /v1/change-impact/policy`
- `POST /v1/change-impact/evaluation`

CAS apply/dry-run endpoint:

- `POST /v1/change-impact/policies/apply`

Policy writes are `policy_admin`. Evaluation reads are observational evidence.
Generated OpenAPI is the client contract source.

The evaluation request schema contains only `target.repository`,
`target.pull_request_number`, and optional non-authoritative metadata. Extra
fields are rejected. The production route uses Launchplane-managed GitHub
credentials and an injectable repository-evidence provider protocol for tests.

## Persistence

Filesystem rehearsal records live under `launchplane_change_impact_policies/`.
PostgreSQL uses `launchplane_change_impact_policies`. Migration
`d2e4f6a8b0c2` creates an empty table and indexes only; it does not infer or
backfill product inventory from repository text.
