---
title: Work Graph Read Model
---

## Status

The work graph remains available under the Q543 implementation scope.
[Q543](https://github.com/cbusillo/launchplane/issues/2436#issuecomment-6273441298)
removes the GitHub Project and issue-inbox sources. The future location of this
view is an open Director decision tracked in [#4137](https://github.com/cbusillo/launchplane/issues/4137).

## Purpose

The work graph ranks typed snapshots and assembles a read model from existing
product profiles and durable work requests. Engineering > Work graph continues
to display and rank these items without a GitHub Project or token. The API can
still accept caller-supplied compact planning facts; the deployed service has
no live-forge ingestion provider.

The agent-facing consumption boundary is summarized in
[agent-context-boundary.md](agent-context-boundary.md). This page owns the queue
shape; the boundary page owns how agents consume it.

## Snapshot Contract

A snapshot contains:

- repo classifications: `managed_runtime`, `active_awareness`,
  `support_dependency`, or `out_of_scope`
- issue or PR facts: repository, number, title, URL, Focus, Manager, Finish
  Line, labels, dependency counts, subissue counts, updated time, and optional
  check/deploy state

Every issue must have a matching repo classification. Managed runtime repos must
also identify the Launchplane product they belong to. This keeps the chooser
explicit about awareness versus operational ownership.

## CLI

Rank a snapshot without persisting it:

```sh
uv run launchplane work-graph rank \
  --snapshot-file work-graph-snapshot.json \
  --limit 25
```

The command prints a `WorkGraphQueue` JSON payload with:

- ranked items
- ready/waiting/blocked state
- agent-actionable fields: `safe_to_start`, `next_action`, `why_now`,
  `blocked_by_count`, `source_of_truth_url`, `handoff_url`, and compact
  `evidence`
- recommendation category, such as `quick_win`, `deep_work`, or
  `attention_needed`
- score and explanation reasons
- hidden count for closed, done, or overflow items

Queue items are intentionally compact. `source_of_truth_url` and `handoff_url`
point agents back to GitHub issues or pull requests instead of copying issue
bodies or long planning prose into Launchplane. `safe_to_start` is true only for
ready items that are in the active/next lane or have a failing check/deploy
signal that needs attention. Waiting and blocked items stay visible for
awareness, but agents should inspect the blocker or external signal before
starting implementation. `safe_to_start` means eligible for live issue
inspection, not blanket permission to implement: agents must still read the
current status, finish line, comments, and native relationships before acting.

`evidence` entries carry small source-linked signals with a trust state of
`verified`, `recorded`, `stale`, `missing`, or `unsupported`. The first slice
uses verified/recorded signals from the supplied snapshot. Future persisted
snapshot history can make stale/missing provider evidence explicit without
changing the queue shape.

## Service Route

Authenticated callers can request the same stateless ranking over HTTP:

```sh
POST /v1/work-graph/rank
```

The request body contains `snapshot` and optional `limit`. Launchplane returns
the queue at `result.queue`, requires `work_graph.rank` authorization for
product/context `launchplane`, and does not persist the supplied snapshot.

Authenticated callers can also read the current Launchplane-assembled snapshot:

```sh
GET /v1/work-graph/snapshot
```

The native FastAPI snapshot route uses the same `work_graph.rank`
authorization. It composes current product overviews with durable Every Code
work-request records,
classifies product repositories as `managed_runtime`, classifies other request
repositories as `active_awareness`, and returns source counts with the snapshot.
The deployed service uses product profiles and durable work-request records only.
Project-sourced planning facts and the GitHub issue-inbox reader/reconciler were
removed under [Q543](https://github.com/cbusillo/launchplane/issues/2436#issuecomment-6273441298).
Empty planning facts do not erase work-request facts. Engineering > Work graph
continues to rank and display these non-Project items. No GitHub token is needed
for this view. Where the view lives going forward remains the Director's decision.
The snapshot route writes no records.

Agents and admin tools can read the same repository classification source
without asking for a ranked queue first:

```sh
GET /v1/repo-product-mapping
```

The route requires `product_environment.read` for the Launchplane service
context. It returns `mapping.repositories` with each repository's
classification, product key when Launchplane owns the runtime, display name,
driver id, known contexts, stable environments, preview context, update time,
and source. Product profile records create `managed_runtime` mappings. Durable
Every Code work requests create `active_awareness` mappings only when no product
profile already owns the repo. The mapping is a read model for work graph and
future agent context; it does not make awareness/support repos Launchplane-owned
runtime repos.

## Issue inbox compatibility

`GET /v1/work-graph/github/issues` remains authorized by `work_graph.rank` and
returns an empty, unconfigured inbox in the deployed service. The reconcile
route has no provider and refuses with `invalid_request`; it cannot add Project
items. The response contracts and injected-provider interface remain available
for callers. No environment setting activates a Project or inbox reader.

## Boundary

Do not store copied issue bodies as Launchplane authority. The remaining view
connects durable work requests to product/environment evidence without reading
GitHub Projects or changing GitHub state.
