---
title: Direction
---

This page is the current direction for Launchplane. When an issue, milestone, or
another doc disagrees with it, this page wins and the other source should be
corrected or closed. Issues are a work list, not instructions.

## What Launchplane Is For

A small control layer that lets agents maintain and ship products quickly and
safely: build, preview, deploy, promote, back up, restore, merge, and record an
Owner's accept-or-reject decision. Providers sit behind adapters. GitHub is the
current source-control adapter and Dokploy is the current deployment adapter;
neither is a permanent dependency.

Judge every change by one question: can a product be maintained without anyone
touching Launchplane? Work that adds upkeep to Launchplane itself needs a strong
reason.

## Roles

- **Operator** — the repository owner. Decides what ships and who has access.
- **Operator's agents** — do the engineering: implement, test, review, diagnose,
  merge, and deploy Launchplane itself. They stop only at the boundaries below.
- **Site Owners and their agents** — stakeholders, not engineers. Their ideas and
  pull requests are proposals that the Operator's side validates and lands.
  A site Owner may reject a preview of their own site, which blocks it. Their
  acceptance satisfies a gate and never merges or deploys anything. Veto yes,
  merge no.

## Stop Boundaries

An agent asks the Operator before:

- deploying to a customer production site;
- destructive data operations such as restore or delete;
- creating credentials or granting anyone access;
- spending money;
- deciding something a site Owner should have an opinion on.

Everything else is ordinary engineering and needs no ceremony.

## Delivery Path

The merge train is the delivery path: the enqueue label states intent, required
checks are the engineering gate, the pull request author must be a repository
owner, admin, or allowlisted automation, and Launchplane performs the merge. See
[merge-train-policy.md](merge-train-policy.md).

When Launchplane's own merge path is what is broken, Launchplane pull requests
may merge through the source-control provider's protected branch and required
checks. Record why in the pull request.

The ordinary-agent delegated-delivery design (issue `#2240` Milestone 6,
`ordinary_agent_*`, authorization policy schema v3, enrollment, sessions, leases)
is retired. Do not extend it. Its code and docs are scheduled for deletion; until
then [ordinary-agent-execution.md](ordinary-agent-execution.md) and
[provider-delivery-inspection.md](provider-delivery-inspection.md) are historical.

## Rules Of Thumb

- A read that explains a refusal must never need more authority than the refused
  action. Treat a violation as a bug and fix it.
- Check a gate when the action it guards happens. Do not add evidence that goes
  stale on a timer and needs upkeep while nothing changes.
- Prefer deleting a concept to adding one. A documented blocker is not a
  finished task.
- Write an adapter for a second provider when there is a second provider.

## Launch Gate For A Product

A product is ready for real users when three consecutive real changes travel
pull request → preview → testing → production without anyone touching
Launchplane. Whatever blocks that run is the next piece of Launchplane work.
