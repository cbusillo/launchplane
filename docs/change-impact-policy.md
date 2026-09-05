---
title: Change Impact Policy
---

## Purpose

Launchplane has an authoritative change-impact classifier for pull
requests. It derives affected products, Owner acceptance impact, and engineering
review tier from Launchplane policy plus trusted repository and change evidence.
Public callers submit only a repository/pull-request target reference and
request metadata. They cannot submit changed files, dependency
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

A component rule may additionally declare `production_affecting`. It marks the
affected product scopes whose changes reach a production surface, which raises
their Owner review class to `production_affecting` regardless of review tier. The
flag is normalized so that only an explicit `true` is stored, keeping existing
rule IDs and policy digests byte-identical.

### Explicit V2 Classification

Policies without `classification_model` retain the original cumulative rule
matching and policy-declared engineering-only behavior. Their rule IDs and
policy digests are unchanged. `classification_model: "v2"` opts into explicit
product authority: every rule must name affected products or
`product_impact: "declared_none"`, never both. A missing match is still unknown;
it cannot imply engineering-only impact.

For each path, v2 selects the longest matching prefix as the product authority.
Equal-specificity rules from different components and noncanonical/root
prefixes are rejected. An explicit narrower rule may define an engineering-only
carveout or a different product scope. Those declarations belong to a reviewed
DB policy revision, not the changed repository content.

Sensitive review, `governance_impact: true`, and `production_affecting` remain
floors across every matching ancestor and the selected rule. Governance impact
requires the existing sensitive two-review requirement without manufacturing a
product Owner subject. An inherited production floor applies to the selected
products and their trusted storage extensions. Both provider-supplied sides of
a rename classify independently and their impact is combined.

Ancestor matches use `review_floor_only: true` with empty products in matched
evidence. They explain inherited requirements but cannot license stored product
extensions; only selected components can do that. Complete path coverage still
does not establish the validity of stored dependency or reviewer evidence.

V2 policy dry-runs validate these rules. V2 apply is currently unavailable and
returns a policy conflict without writing records, pending scoped binding/replay
integration and fail-closed rename-origin handling. Deploying this code does
not activate v2, migrate policy, add a grant, or reuse historical Owner
acceptance under different semantics. Policy activation remains a separately
reviewed CAS operation through existing policy-administrator authority.

## Evidence Authority

The GitHub provider retains each rename destination with an explicit
`previous_path` and the corresponding removed origin path. Missing or invalid
rename origins and repeated real provider paths fail evidence resolution for every
policy version; they cannot silently erase a classification boundary. Valid
legacy changes keep their existing product and review classification. Recreated
origins and rename swaps preserve real change kinds; synthetic removed origins
are added only where the provider supplied no real entry for that path.

Launchplane resolves evaluation evidence through two server-owned boundaries:

- A server-authenticated GitHub provider resolves immutable repository identity,
  the current pull-request head and tree, and the complete changed-file set. A
  second pull-request read rejects a head that changes during collection, and
  renamed files preserve both old and new paths for policy matching.
- The same provider resolves the reviewed base ref and base SHA, plus numeric
  GitHub contributing identities over the reviewed commit range from pull-request
  authorship and GitHub-linked commit author/committer evidence. Bot or agent
  work pushed under a human identity resolves to that human. A commit with no
  linked numeric identity, a login that maps to two different numeric IDs, or a
  range beyond the provider page bound resolves as `unresolved` or `conflicting`
  and is never repaired.
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

## Generated boundaries in v2

A v2 component can declare `generated_by` instead of direct products or
`product_impact: declared_none`. It names 1–20 distinct terminal component
identities in the same DB-backed policy, with at most 400 edges per policy.
Missing, self-referential, implicit-empty, and generated targets are rejected;
multi-hop generator chains are unsupported. An operator must explicitly flatten
a composed generator into terminal authority components in a reviewed CAS input.

The winning path rule inherits the union of those generators' products. It has
declared-none authority only when every generator explicitly declares none.
Review, governance, and production floors join upward from the artifact rule,
its path ancestors, its generators, and ancestors of every generator prefix.
Ancestor products never enter the union. A losing generated ancestor contributes
only its own floors; its generator edges are not expanded.

For v2, file and stored-evidence rows report the component's derived review,
governance, and production flags, including its generator boundary. Path-ancestor
floors remain separate floor-only evidence and are joined into the aggregate
decision; per-row flags do not replace that aggregate authority.

Resolution is pure and bounded, computed once per evaluation into immutable
per-generator contributions for later scoped decision binding. Mapping changes
remain distinguishable even when aggregate products and floors are identical.
Generator attribution does not prove generated bytes are current: existing
schema generation and drift checks remain independent requirements. These
fields do not alter v1 policy hashes when absent, and the v2 live-apply guard
remains until the complete binding and admission pipeline is integrated.

## Output

Every evaluation returns:

- affected product/system scopes and per-product Owner acceptance requirement;
- engineering review tier and the resulting one- or two-review requirement;
- matched rules and trusted evidence;
- exact repository/PR/head/tree and policy provenance;
- explicit unknown evidence when classification fails closed.

After policy and target validation, `coverage` independently reports whether
every provider-supplied changed path matched a policy rule. It contains the
total distinct unmatched-path count, at most 20 lexicographically sorted path
samples, and `truncated`. Each sample is capped at 256 characters; truncation
means either samples were omitted or a displayed path was shortened. The
unmatched-path entries in `unknown_evidence` use the same bounded samples and
include a truncation summary when needed. Renames retain the provider's old
and new paths, so either side can contribute an uncovered path.

Pure coverage gaps return `policy_coverage_incomplete` with unknown status,
sensitive engineering review, and the existing fail-closed Owner result.
Contradictory or missing stored evidence retains
`ambiguous_or_missing_evidence`, even when coverage gaps also exist; `coverage`
still reports those gaps separately. Complete path coverage does not validate
stored evidence or authorize admission. `coverage` is null when evaluation
stops before path matching, such as missing policy or a stale caller target.

These diagnostics do not change rule accumulation, product scope, review
requirements, policy selection, or acceptance-binding identities. Full policy
provenance remains authoritative; coverage is not a replacement for it.

The evaluation is the authoritative source for which product Owner decisions are
required by Launchplane merge readiness. GitHub checks only project the resulting
state and are never accepted as substitute evidence.

## HTTP API

Reads:

- `GET /v1/change-impact/policy`
- `POST /v1/change-impact/evaluation`

CAS apply/dry-run endpoint:

- `POST /v1/change-impact/policies/apply`

Policy writes are `policy_admin`. Evaluation reads are server-derived authority inputs.
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
