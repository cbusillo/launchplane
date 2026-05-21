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

The agent-facing consumption boundary for this read model is summarized in
[agent-context-boundary.md](agent-context-boundary.md). This page owns the queue
shape; the boundary page owns how agents should consume it without turning
Launchplane into planning authority.

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
starting implementation.

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

The snapshot route uses the same `work_graph.rank` authorization. It composes
current product overviews with durable Every Code work-request records,
classifies product repositories as `managed_runtime`, classifies other request
repositories as `active_awareness`, and returns source counts with the snapshot.
The route can also apply compact planning facts from a caller-owned ingestion
provider. The first provider reads GitHub Project item fields through the
GitHub CLI when `LAUNCHPLANE_WORK_GRAPH_PROJECT_OWNER` and
`LAUNCHPLANE_WORK_GRAPH_PROJECT_NUMBER` are configured. It supplies Focus,
Manager, Finish Line, labels, item status, title, URL, updated time, and
PR-vs-issue type; then it uses bounded GitHub issue and pull-request reads to
supply blocked-by, blocking, subissue, and PR check state. Planning facts can
stand alone for known Launchplane-managed repos when no Every Code work request
record exists yet; Project items for unclassified repos stay out of the snapshot
until Launchplane has an explicit repo classification. Empty planning facts do
not erase Every Code work-request facts. The snapshot route does not fetch or
store GitHub issue bodies and does not write new records.

Agents and operator tools can read the same repository classification source
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

The GitHub Project provider shells out to:

```sh
gh project item-list <number> --owner <owner> --format json --limit <limit>
gh api repos/<owner>/<repo>/issues/<number>/dependencies/blocked_by
gh api repos/<owner>/<repo>/issues/<number>/dependencies/blocking
gh api repos/<owner>/<repo>/issues/<number>/sub_issues
gh pr checks <number> --repo <owner>/<repo> --json bucket,state,name,workflow,completedAt,startedAt
```

`LAUNCHPLANE_WORK_GRAPH_PROJECT_SIGNAL_LIMIT` bounds the per-snapshot dependency,
subissue, and check fan-out after Project items are loaded. Items beyond that
limit still receive Project field facts, but their live signal fields remain
unknown until a later snapshot includes them inside the bound.

The Launchplane image includes the GitHub CLI and disables interactive prompts
for service use. The deployed service should receive `GH_TOKEN` from the
`LAUNCHPLANE_WORK_GRAPH_GH_TOKEN` deploy secret; the token must have enough
access for the configured Project plus issue dependency, subissue, and PR check
reads. Deploy automation only forwards the Project provider env bundle when that
token secret is present, so repository variables can be prepared without turning
on unauthenticated runtime `gh` reads. A configured Project or signal source that
cannot be read fails the snapshot request instead of silently dropping Project
fields.

## GitHub Issue Inbox

Launchplane can expose a read-only grouped GitHub issue inbox for an explicit
repository inventory:

```sh
GET /v1/work-graph/github/issues
```

The route uses the same `work_graph.rank` authorization as the snapshot route
and never writes GitHub or Launchplane state. Enable it with
`LAUNCHPLANE_WORK_GRAPH_ISSUE_INBOX_REPOSITORIES`, a comma or newline separated
list of `owner/repo` values. `LAUNCHPLANE_WORK_GRAPH_ISSUE_INBOX_LIMIT` bounds
the open issue reads per repository and defaults to `100`. The provider shells
out to:

```sh
gh issue list --repo <owner>/<repo> --state open --limit <limit> \
  --json number,title,url,state,labels,updatedAt,createdAt,author
```

The response groups results by configured `owner/repo`, includes empty groups so
the configured inventory is visible, and gives every issue a stable key in the
form `owner/repo#number` plus the source URL. If Code Plans Project env is also
configured, the inbox reads Project item-list once to build membership and each
issue is marked with `present_in_project: true` and
`project_status: "present"` or `present_in_project: false` and
`project_status: "missing"`. Without Project env, issues use
`project_status: "unconfigured"` and `present_in_project: null`.

Project-only issue items that are no longer visible in the configured open issue
inventory remain in the response for operator review. Closed items use
`project_status: "closed"`; other Project-only items use
`project_status: "stale"`. The route reports `stale_project_item_count` but does
not remove or mutate Project items.

Forks and private repositories are supported only through explicit inventory and
the runtime `GH_TOKEN` permissions. Launchplane does not owner-wide search,
fetch issue bodies, or infer product ownership from inbox membership.

When Code Plans Project env is configured, Launchplane can also reconcile the
inbox into that Project:

```sh
POST /v1/work-graph/github/issues/reconcile
```

The request is a small mode selector:

```json
{ "mode": "dry_run" }
```

`dry_run` uses `work_graph.rank` authorization and returns the missing open
issues that would be added. `apply` requires
`work_graph.issue_inbox.reconcile`, rechecks Project membership before each
issue add, and shells out to:

```sh
gh project item-add <project-number> --owner <project-owner> --url <issue-url> \
  --format json
```

Apply responses record counts for `added`, `already_present`, `skipped`, and
`failed` items. Re-running apply against an already reconciled inbox reports
`already_present` instead of creating duplicate Project items. GitHub CLI
failures are reported per item with redacted stderr/stdout detail so one failed
issue does not hide successful additions for the same reconciliation pass.

## Boundary

Do not store copied issue bodies as Launchplane authority. Project fields may be
used as compact transient facts for ranking and display, but GitHub Projects
remain the source of truth. When Launchplane needs durable history, store
snapshots with provenance and make their freshness visible. The normal operator
view should link back to GitHub for planning details and use Launchplane only to
rank, explain, and connect work to product/environment evidence.
