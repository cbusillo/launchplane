---
title: Work Graph Read Model
---

## Purpose

The work graph read model ranks operator work from caller-supplied GitHub and
Code Plans facts. It is a chooser surface, not a second planning database:
GitHub issues, PRs, Projects, checks, and deployments remain the source of
truth.

The first slices accept typed snapshots, assemble Launchplane-owned snapshots
from existing product and Every Code records, and return ranked queues with
compact recommendation reasons. Later service/UI slices can add live GitHub or
Code Plans ingestion behind the same snapshot and queue contracts.

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
- recommendation category, such as `quick_win`, `deep_work`, or
  `attention_needed`
- score and explanation reasons
- hidden count for closed, done, or overflow items

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

The snapshot route uses the same `work_graph.rank` authorization. It composes
current product overviews with durable Every Code work-request records,
classifies product repositories as `managed_runtime`, classifies other request
repositories as `active_awareness`, and returns source counts with the snapshot.
The route can also apply compact planning facts from a caller-owned ingestion
provider, such as Focus, Manager, Finish Line, labels, dependency counts,
subissue counts, updated time, and check/deploy state. Empty planning facts do
not erase Every Code work-request facts. It does not fetch or store GitHub issue
bodies and does not write new records.

## Boundary

Do not store copied issue bodies as Launchplane authority. Project fields may be
used as compact transient facts for ranking and display, but GitHub Projects
remain the source of truth. When Launchplane needs durable history, store
snapshots with provenance and make their freshness visible. The normal operator
view should link back to GitHub for planning details and use Launchplane only to
rank, explain, and connect work to product/environment evidence.
