---
title: Agent Operator Contract
---

# Agent Operator Contract

Launchplane publishes a checked-in, public-safe contract for external agent and
operator tooling at `contracts/agent-operator-contract.json`. Launchplane owns
the producer artifact and its quality gate. External skills are consumers; they
do not become runtime authority and must not block ordinary Launchplane feature
delivery.

## Generate And Check

Generate the canonical OpenAPI document, UI bindings, and agent/operator
contract together:

```bash
pnpm --dir frontend generate:openapi
```

Generate only the agent/operator contract:

```bash
uv run launchplane service export-agent-contract \
  --output contracts/agent-operator-contract.json
```

Check every generated contract without changing checked-in files:

```bash
pnpm --dir frontend check:openapi-drift
```

The drift check compares `normalization_version` and
`semantic_digest_sha256`. It deliberately ignores provenance-only source SHA
changes.

## Contract Shape

The artifact contains:

- `schema_version`: the public artifact schema version.
- `normalization_version`: the version of the structural projection rules.
  Digests from different normalization versions are not comparable.
- `semantic_digest_sha256`: the canonical digest of the normalized contract,
  excluding provenance.
- `provenance.source_commit_sha`: the source revision recorded when the current
  semantic artifact was generated. It is evidence, not a freshness gate.
- `contract.operations`: an explicit allow-list of agent-relevant HTTP
  operations with live operation IDs and dependency names, supported surfaces,
  mutation modes, idempotency and reviewed-evidence requirements, response
  statuses, and structural OpenAPI fingerprints.
- `contract.protected_workflows`: the protected GitHub workflows that own
  product retirement, detached application retirement, managed authorization
  reconciliation, and bounded stable-lane repair.
- `contract.invariants`: durable lifecycle, deploy identity, reconciliation,
  and governance boundaries that OpenAPI cannot express.

Anything not explicitly selected by the generator is absent. The artifact must
never contain real product, tenant, repository, branch, domain, lane,
provider-target, credential, operator, or runtime-topology authority.

The browser-only activation self-check is intentionally absent from this
agent/operator allow-list. It accepts only the signed-in human's Launchplane
session cookie and has no bearer helper or agent surface.

Authorization recovery is intentionally split: public prepare and redacted
challenge status are inert allow-listed operations so an agent or operator can
inspect an unsigned exact challenge. The hardware-signature apply operation and
all browser key lifecycle operations are absent. The contract grants no
authorization and cannot mint, sign, upload, or apply policy material.

## Normalization Version 1

The structural projection follows these rules:

- Only the allow-listed method/path pairs and schemas reachable from their
  parameters, request body, and responses participate.
- Structural fields such as types, required properties, enums, formats,
  constants, unions, collection shapes, bounds, patterns, defaults, and
  deprecation state are retained.
- Raw OpenAPI descriptions, summaries, titles, examples, and unrelated routes
  or schemas are excluded. Agent-facing purpose and safety summaries are owned
  explicitly by the semantic overlay and are included in the digest.
- Mapping keys and semantically unordered lists are normalized before hashing.
- `provenance` and `semantic_digest_sha256` are excluded from the digest input.

Changing a selected route, operation ID, dependency boundary, structural
request/response shape, protected workflow, or semantic invariant changes the
digest. Adding an unrelated route or schema, editing a raw OpenAPI description,
or changing only the source SHA does not.

## Ownership Boundary

Launchplane closes the producer work when the artifact, generator, drift gate,
tests, and documentation are live. Consumer publication is independent. A
consumer may vendor the artifact and compare its digest offline, while a
separate scheduled check determines whether the vendored digest is current.

Protected workflow dispatch and watching remain owned by the installed
`github_workflow_babysit.py` helper. The contract names supported entrypoints;
it does not authorize a mutation or replace live Launchplane context.
