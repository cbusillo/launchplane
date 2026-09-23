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
an `in_progress` check while an ordinary Owner decision is pending, `success`
after acceptance or when review is not required, `action_required` for stale or
negative human decisions, and `failure` when authority or evidence is
unavailable. Its summary routes the reviewer to Launchplane, the only Owner
action surface. Owner projection uses one stable aggregate check and lists each
affected product decision instead of product-derived check names.

For product pull requests this check is a leftover of the change-impact model
and is no longer the Owner's signal. Product Owner review is shown by the
`launchplane/owner-review` commit status described in
[preview-workflow-contract.md](preview-workflow-contract.md#owner-review-request).
When Launchplane next writes a ready preview comment for a pull request, an
existing `launchplane/owner-acceptance` check run on the current head that this
App created and that is not already `neutral` is updated once to `neutral` with
the title "Retired" and a summary pointing to `launchplane/owner-review`.
Launchplane never creates the check for that purpose. Launchplane's own merge
train still evaluates Owner acceptance; removing that is a later step of issue
`#2446`.

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
and are revoked after the projection attempt. Once GitHub returns a usable token
string, Launchplane also revokes it before surfacing any later expiry,
permission, repository-count, repository-id, or repository-name validation
failure. A cleanup failure is attached to the original validation error rather
than replacing it. Tokens are never logged or persisted.

Registering and installing the live App is an operator authorization step. The
code and dry-run contracts remain testable before that authorization exists;
missing identity or installation state fails the projection route closed.

## Projection Routes

`POST /v1/engineering-review-decisions/project` projects the latest persisted
engineering decision only after Launchplane re-resolves the exact repository,
pull request, head, and tree evidence.

The retired `/v1/owner-acceptance/project` route and
`/ui/engineering/owner-acceptance` workbench have been removed. Current preview
review uses `/ui/owner-review` and the `launchplane/owner-review` commit status.
`OwnerReviewStatusPublisher` can neutralize an old `launchplane/owner-acceptance`
check on the same pull request; it does not evaluate or write old Owner events.
See [owner-acceptance.md](owner-acceptance.md).

## No Feedback Loop

Launchplane-owned governance check names are excluded from Every Code preview
readiness, merge-train check-run aggregation, and tenant-admission commit-status,
check-run, and required-check inputs. The legacy
`launchplane/engineering-review-shadow` status is also excluded during its
separate cutover. Tests prove that preview, merge, and admission results are
unchanged when GitHub projections are present, `in_progress`, completed, or
failed.

The retired Owner projection is never a required check or merge authority.
Current product review and release checklist decisions are separate Launchplane
records, and the release checklist remains the production Owner gate.
