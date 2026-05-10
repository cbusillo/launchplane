---
title: Launchplane Agent Context Boundary
---

## Purpose

Launchplane gives coding agents compact operational context and scoped intent
preflight without becoming a second planning database, a secret broker, or a
generic production mutation token. GitHub issues, pull requests, Projects,
checks, and deployments remain the planning and code workflow source of truth.
Launchplane compiles product/runtime/evidence facts it owns, links back to
source systems, and records provenance so agents can decide what to inspect or
request next.

## Source Of Truth

- GitHub remains authoritative for issue bodies, PR review, merge state, Project
  fields, dependencies, subissues, and check state.
- Launchplane remains authoritative for product profiles, runtime environment
  records, managed secret metadata, deployment/promotion evidence, Every Code
  work requests, preview gates, authz policy records, and write-intent
  evaluation records.
- Agent-facing payloads should prefer source URLs and compact evidence over
  copied planning prose or provider internals.
- Launchplane read models must mark unavailable provider evidence explicitly
  instead of silently returning partial context as complete.

## Caller Profiles

Agent context callers are identified as compact agent consumers:

- `automation_worker`: GitHub Actions or service automation. Authorization comes
  from exact workflow, repository, event, product, context, and action policy
  rules.
- `owner_local_agent`: trusted local terminal agent using
  `LAUNCHPLANE_TERMINAL_AGENT_READ_TOKEN`. The service admits this identity only
  on `GET` routes; policy still scopes products, contexts, and read actions.
- `owner_local_agent`: trusted local operator using
  `LAUNCHPLANE_LOCAL_OPERATOR_TOKEN` from
  `~/.config/launchplane/local-operator.env`. This identity can submit
  reason-bearing Launchplane mutations such as product-config dry-run/apply from
  a trusted shell; product-config apply requires a previously recorded matching
  dry-run, and authz policy administration remains out of scope.
- `limited_remote_user`: authenticated GitHub human with read-only role. This
  profile fails closed to read and safe-write action families even if a policy
  rule is accidentally broad.
- `human_admin`: authenticated GitHub human admin. Admin capability still flows
  through exact action/product/context policy rules.

These profiles are diagnostics and safety rails. They do not replace exact
Launchplane authz policy matching.

## Read Context

The preferred preflight endpoint is:

```text
GET /v1/agent/context
```

It aggregates existing read models into named sections:

- repo-product mapping
- work graph snapshot and ranked queue
- Every Code summary
- preview readiness

Each section reports `available`, `unauthorized`, or `unavailable`. The endpoint
writes no records, fetches no issue bodies, and should preserve the underlying
read models' redaction, freshness, and provenance fields.

Agents may call lower-level read models directly when they need a narrower
surface:

- `GET /v1/repo-product-mapping`
- `GET /v1/work-graph/snapshot`
- `POST /v1/work-graph/rank`
- `GET /v1/every-code/summary`
- `GET /v1/previews/readiness`

## Scoped Intents

Agents do not receive broad write credentials. They can preflight a scoped
candidate through:

```text
POST /v1/agent/write-intents/evaluate
```

The request names a specific intent, mode, product, context, source URL, and
reason. Launchplane maps the intent to an existing policy action, evaluates the
caller, applies dry-run-first and secret-backed evidence rules, persists a
`launchplane_agent_write_intents` record, and returns a compact result with the
record id. The evaluation route does not execute product/runtime mutations and
does not return reusable credentials.

Follow-on execution routes should require a route-specific policy action. They
should link back to the durable write-intent record rather than treating the
record as a bearer capability.

## Redaction And Provenance

Agent payloads must not include plaintext secrets, ciphertext, token prefixes,
provider environment dumps, local checkout paths, local worker hostnames, raw
webhook delivery ids, issue bodies, or prompt text. Managed secret evidence is
metadata-only: binding keys, runtime destinations, policy ids/digests, and safe
finding codes.

Every agent-facing authorization response should include an `agent_audit`
envelope with decision, safe reason code, subject, action, product, context,
policy source, policy digest, and authz-policy source kind. Read models should
carry compact evidence with source links and trust/freshness states such as
`verified`, `recorded`, `stale`, `missing`, or `unsupported`.

## Operations

- Use the deployed Launchplane service API or operator UI for shared and
  production live mutations.
- Keep service-host environment values bootstrap-only unless a specific doc
  says otherwise.
- Add docs in the same PR as behavior changes to agent-facing endpoints,
  authorization profiles, redaction rules, or write-intent records.
- Prefer narrow follow-on routes for approved intent families. Do not add a
  generic agent write token or a catch-all mutation proxy.
