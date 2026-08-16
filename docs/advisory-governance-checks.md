---
title: Governance Check Projection
---

## Purpose

Launchplane projects its server-owned engineering review and Owner acceptance
decisions into GitHub as check runs. GitHub is a visibility and routing surface,
not an authority source. The projection cannot authorize merge, tenant
admission, promotion, or production deployment.

The two stable check names are:

- `launchplane/engineering-review`
- `launchplane/owner-acceptance`

Engineering review remains a neutral advisory observation. Owner acceptance uses
`success`, `action_required`, or `failure` to make current, pending/stale, and
unavailable states unmistakable. Its summary routes the reviewer to Launchplane,
the only Owner action surface. Owner projection uses one stable aggregate check
and lists each affected product decision instead of product-derived check names.

## GitHub App Identity

Projection uses a dedicated GitHub App installation identity. The App may have
only repository Checks write and GitHub's mandatory Metadata read permission.
Launchplane verifies the configured numeric App id, repository installation,
permission set, exact numeric repository id, and repository full name before it
uses a short-lived installation token.

The App id is the non-secret runtime-environment value
`LAUNCHPLANE_ADVISORY_GITHUB_APP_ID`. The private key is the managed-secret
value `LAUNCHPLANE_ADVISORY_GITHUB_APP_PRIVATE_KEY`. Both belong to the
Launchplane service context in DB-backed runtime records; they are not checked
in, persisted in projection records, or accepted from callers. Installation
tokens are minted for one exact repository with only Checks write permission
and are revoked after the projection attempt. They are never logged or
persisted.

Registering and installing the live App is an operator authorization step. The
code and dry-run contracts remain testable before that authorization exists;
missing identity or installation state fails the projection route closed.

## Projection Routes

`POST /v1/engineering-review-decisions/project` projects the latest persisted
engineering decision only after Launchplane re-resolves the exact repository,
pull request, head, and tree evidence.

`POST /v1/owner-acceptance/project` accepts only a repository and pull-request
reference. Launchplane evaluates current Owner acceptance from server-owned
evidence, re-resolves the exact target, and rejects any mid-flight head, tree,
repository-id, or owner-id drift before writing GitHub.

Owner acceptance `details_url` values point to the server-derived Launchplane
Owner workbench, not directly to GitHub. Launchplane validates the configured
browser public origin and URL-encodes the exact repository and pull-request
query parameters before constructing `/ui/engineering/owner-acceptance`; an
invalid origin or target fails the projection closed. Opening that link expands
Exact lookup and automatically evaluates the same target in the browser.

Each check run stores the Launchplane decision digest as `external_id`.
Identical state replays without a write. Changed binding or decision state on
the same head updates the App-owned check run. A changed head receives its own
new check run and cannot reuse evidence from the prior head.

Browser Owner event writes first replace the exact-head check with a
conservative `action_required` state. Failure to establish that non-green state
blocks the immutable append. Launchplane then projects the stored event's final
decision from a fresh current-ledger evaluation while holding a store-backed
immutable-repository-id projection lock for the pull request. Repository-id
changes are rejected and retried before the critical section, and the resolved
event binding must still match the held lock before projection or append.
Repository renames remain serialized by the stable id. A second evaluation
confirms that the projected state stayed current before the lock is released.
PostgreSQL waiters use dedicated unpooled advisory-lock connections rather than
consuming the record-store pool. Final delivery, token cleanup, or confirmation
failure restores the conservative check and returns a reconciliation-required
error.
Idempotent replay and the explicit projection endpoint can safely retry the
final projection. This sequence remains non-authoritative for admission, and
browser sessions do not gain projection authority.

## No Feedback Loop

Launchplane-owned governance check names are excluded from merge-train check-run
aggregation and from tenant-admission commit-status, check-run, and required-
check inputs. The legacy `launchplane/engineering-review-shadow` status is also
excluded from tenant admission during its separate cutover. Tests prove that merge
and admission results are unchanged when GitHub projections are present, pending,
or failed.

Do not make the Owner projection a required GitHub status until the separate
ruleset reconciliation work proves refresh behavior for every staleness source.
Launchplane recomputes the authoritative Owner decision immediately before
admission; the GitHub check remains a routing and visibility projection.
