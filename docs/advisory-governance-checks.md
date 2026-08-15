---
title: Advisory Governance Check Projection
---

## Purpose

Launchplane projects its server-owned engineering review and Owner acceptance
decisions into GitHub as advisory check runs. GitHub is a visibility surface,
not an authority source. The projection cannot authorize merge, tenant
admission, promotion, or production deployment.

The two stable check names are:

- `launchplane/engineering-review`
- `launchplane/owner-acceptance`

Both check runs complete with GitHub conclusion `neutral`. Their title and
summary expose the current Launchplane decision, exact binding digest, and the
fixed `mode=shadow`, `authoritative=false`, `enforcement_effect=none` contract.
Owner projection uses one stable aggregate check and lists each affected
product decision in the output instead of creating product-derived check names.

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

Successful browser Owner event writes also trigger a best-effort refresh of the
current App-owned check. This delivery attempt is non-authoritative: projection
or token-revocation failure does not roll back the persisted event or alter its
successful API response, and browser sessions do not gain projection authority.

## No Feedback Loop

Launchplane-owned governance check names are excluded from merge-train check-run
aggregation and from tenant-admission commit-status, check-run, and required-
check inputs. The legacy `launchplane/engineering-review-shadow` status is also
excluded from tenant admission during cutover. Tests prove that merge and
admission results are unchanged when advisory checks are present, pending, or
failed.

GitHub rulesets must not make these advisory names required while the contracts
remain shadow-only. Ruleset and CODEOWNERS drift detection and reconciliation
belong to the downstream projection workstream.
