---
title: Dependency Health Contract
---

## Purpose

The dependency-health contract separates pull-request causality from absolute
repository and artifact health.

A pull-request comparison answers whether a candidate introduces or worsens a
high/critical finding relative to one explicit baseline. An absolute assessment
answers whether one snapshot currently contains any high/critical findings.
Inherited findings therefore remain visible and keep absolute health red without
making an improving pull request impossible to merge.

This contract does not weaken security policy. Promotion and publication must
eventually consume absolute evidence for the exact immutable artifact, while
pull-request gates consume regression evidence produced under one comparison
context.

## Ownership Boundary

Product repositories own dependency manifests, lockfiles, Dockerfiles, scanner
invocation, and product-specific verification. Launchplane owns the normalized
snapshot schema, Trivy-report normalization, comparison semantics, policy
evaluation, and future persisted health records.

The contract and report adapter are read-only. They do not run scanners, call
GitHub, inspect Dependabot, mutate branches, write Launchplane state, or merge
pull requests. Product workflows must produce trusted baseline and candidate
reports under one scanner database and configuration before invoking
Launchplane.

## Snapshot Contract

Each snapshot contains:

- an exact repository and source commit
- an asserted baseline commit for candidate snapshots
- producer and producer-version identity
- advisory source and exact advisory revision
- scan scope and a SHA-256 commitment to scanner configuration and external
  comparison inputs
- normalized findings keyed by advisory, ecosystem, package, and manifest path
- the affected installed-version set and occurrence count for each finding

The same producer is responsible for canonical package names, advisory IDs,
aliases, and aggregation of multiple affected installed versions into one
finding identity.

Snapshot provenance is asserted evidence, not a cryptographic attestation. A
trusted workflow must independently select the repository, commits, scanner,
advisory data, and external inputs before creating snapshots.

## Comparison Semantics

`launchplane dependency-health compare` refuses to compare snapshots unless all
comparison provenance matches and the candidate's asserted baseline commit is
the exact baseline source commit.

Findings are classified deterministically:

- `introduced`: present only in the candidate
- `worsened`: present in both with a higher candidate severity, a larger
  affected-version cardinality, or more affected occurrences
- `resolved`: present only in the baseline
- `unchanged`: still present with equal or lower candidate severity and no
  affected-version-cardinality or occurrence expansion. Replacing affected
  versions at the same cardinality is intentionally non-regressing for an
  unrelated pull request because exposure has not grown. A security-update
  target still fails until the advisory is absent.

Package versions are evidence, not ordering inputs. The contract does not try
to compare npm, Python, operating-system, or container version syntaxes.
Affected-version cardinality and occurrence expansion are still regressions
because they add affected runtime material without requiring cross-ecosystem
version ordering.

The default policy fails when a candidate introduces a high/critical finding or
worsens an existing finding to high/critical. Existing unrelated findings do
not fail the pull-request comparison.

Security-update callers may provide one or more target advisory IDs. Every
target must exist in the baseline and must be absent from the complete candidate
snapshot. Canonical IDs and aliases are matched case-insensitively, so moving a
target advisory to another package or manifest remains unresolved. A candidate
also cannot claim resolution merely by omitting an alias that identified an
unchanged baseline finding. Alias equivalence follows the full connected
component in the baseline, so multi-hop canonical-ID changes cannot produce a
false resolution.

## Absolute Assessment

`launchplane dependency-health assess` evaluates one snapshot without a
baseline. Any high/critical finding fails absolute health. This is the evidence
shape intended for default-branch, scheduled, publication, and promotion
policy; persistence and artifact binding are later workstreams.

## CLI

Compare two snapshots:

```bash
uv run launchplane dependency-health compare \
  --baseline-snapshot baseline.json \
  --candidate-snapshot candidate.json \
  --policy-file policy.json
```

Omit `--policy-file` for the default no-high-or-critical-regressions policy.
Callers may instead repeat `--target-advisory-id` or provide
`--target-advisory-text-file`; Launchplane extracts GHSA, CVE, PYSEC, and OSV
identifiers from that trusted text. A policy file cannot be combined with the
target options.
The command prints one JSON evaluation and exits `0` on pass or `1` on policy
failure. Invalid JSON, invalid contracts, and incompatible provenance fail
without producing a partial evaluation.

Normalize one Trivy JSON report:

```bash
uv run launchplane dependency-health trivy-snapshot \
  --report trivy.json \
  --repository owner/repository \
  --source-commit "$SOURCE_COMMIT" \
  --baseline-commit "$BASELINE_COMMIT" \
  --producer-version 0.70.0 \
  --advisory-revision "$TRIVY_DB_REVISION" \
  --scan-scope production-lockfile \
  --scan-configuration-sha256 "$SCAN_CONFIGURATION_SHA256"
```

Omit `--baseline-commit` for the baseline snapshot. The adapter normalizes
Trivy language and operating-system findings, extracts advisory aliases from
Trivy references, aggregates repeated package occurrences, and fails on
unknown severities or malformed report evidence. Unsafe or non-path Trivy
targets receive deterministic `trivy-targets/` evidence paths rather than being
silently discarded. Reports must be generated with `--list-all-pkgs`; the
adapter requires non-empty package inventory evidence even for clean results
and rejects Trivy modified/ignored findings. Reports must contain only
`lang-pkgs` and `os-pkgs` vulnerability results; mixed scanner output is
rejected rather than partially normalized.

Assess one snapshot absolutely:

```bash
uv run launchplane dependency-health assess --snapshot snapshot.json
```

The command prints one JSON evaluation and exits `0` when no high/critical
finding exists or `1` otherwise.

## Composite Action

Product workflows may call
`.github/actions/dependency-health-trivy/action.yml` at an immutable Launchplane
commit after producing baseline and candidate Trivy JSON reports. The action
normalizes both reports with the same asserted Trivy version, advisory revision,
scan scope, and configuration digest, then runs the regression policy. It does
not download a vulnerability database or choose repository refs; those remain
trusted product-workflow responsibilities.

Trusted scanner commands must reuse one downloaded database for both reports,
include every severity, and include
`--scanners vulnerability --list-all-pkgs --show-suppressed`. Any ignore file
must live outside candidate-controlled source. The adapter rejects every
reported suppression or modified finding; `--show-suppressed` prevents an
ignore rule from silently removing evidence. The configuration digest must
commit to those choices, the trusted ignore file, and any external artifact
inputs.

The action accepts explicit target advisory IDs and trusted pull-request text.
It emits baseline snapshot, candidate snapshot, and evaluation file paths for
artifact retention and fails the job when the comparison policy fails.

## Dependabot Boundary

Dependabot remains the update and advisory signal. This contract intentionally
does not merge, close, replace, or manually modify Dependabot pull requests.
After downstream adoption, an ordinary Dependabot refresh should make a
non-regressing update green automatically. A genuinely introduced or worsened
high/critical finding remains red until the bot offers a safe update.

## Non-Goals

This slice does not add:

- scanner execution, advisory database downloads, or network access
- database tables or service routes
- GitHub App or workflow mutations
- advisory waivers or checked-in ignore policy
- merge-train admission
- client-repository workflow changes
- real repository, product, or advisory identities in production defaults
