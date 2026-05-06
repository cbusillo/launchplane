---
title: Work Graph Read Model
---

## Purpose

The work graph read model ranks operator work from caller-supplied GitHub and
Code Plans facts. It is a chooser surface, not a second planning database:
GitHub issues, PRs, Projects, checks, and deployments remain the source of
truth.

The first slice accepts a typed snapshot and returns a ranked queue with compact
recommendation reasons. Later service/UI slices can add live GitHub ingestion or
cached snapshots behind the same queue contract.

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

## Boundary

Do not store copied issue bodies or Project fields as Launchplane authority.
When Launchplane needs durable history, store snapshots with provenance and make
their freshness visible. The normal operator view should link back to GitHub for
planning details and use Launchplane only to rank, explain, and connect work to
product/environment evidence.
