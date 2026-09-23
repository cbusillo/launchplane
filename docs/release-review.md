---
title: Owner release review
---

Before a customer production promotion, Launchplane compiles the release from
the current production and testing revisions. The product's Owner reviews the
testing site and the **Owner test notes** from every merged pull request in that
commit range at `/ui/owner-review?product=<product>`. No GitHub interaction is
required to read the checklist, accept it, or request changes.

`GET /v1/release-review?product=<product>` serves the same checklist to the Owner
and to callers already permitted to read the product or promote it. Decisions
use `POST /v1/release-review/decisions` and the existing GitHub human session and
CSRF protection. An automation token cannot submit an Owner decision. Identity
comes from the session, never from the request body. The product record's
immutable Owner GitHub ID decides who can accept or request changes; separate
Owner policies and grants are not used.

The server resolves Odoo revisions from release tuples and artifact manifests;
image-based products use deployed inventory runtime identities. GitHub compare
and commit-associated pull-request reads are paginated. Divergent history,
incomplete responses, unavailable source control, and missing lane evidence
fail closed. Missing test notes and commits without a merged pull request are
visible checklist blockers. `Nothing for the owner to test` is valid test notes.
Previous preview acceptance is an annotation, never release approval.
Changes to shared Odoo addon sources or selections are also bound into the
checklist and shown as an explicit blocker. They cannot be represented as an
empty, acceptable website checklist. Until their test instructions are supported
across repositories, an operator must review them and record a release-scoped
override; ordinary Owner acceptance cannot waive the coverage gap.

The decision stores the complete checklist and its digest. The digest includes
the production and candidate artifact and commit, repository, Owner identity,
testing URL, and checklist contents. Promotion recompiles the checklist; changed
release evidence requires a new decision. Decisions do not expire merely because
time passes. A later request for changes replaces acceptance of the same
checklist. Backup and deployment evidence remain independently required.

Recording a decision also creates a release issue in the product repository,
containing that complete checklist, the actor, decision, and reason. The Owner
does not need to open GitHub. The existing managed source-control credential
must be able to read and create issues in that repository; this feature creates
no credentials or grants. Launchplane saves the decision before publishing its
record. A failed publication leaves the decision visible but cannot approve a
promotion. Recording the same pending decision retries its publication with the
same decision ID, including recovery when GitHub accepted a write whose response
was lost. GitHub issue contents and membership never decide release contents or
approval; the saved Launchplane decision remains authoritative.

Product CI must require a nonempty **Owner test notes** section on every pull
request, including changes that need no manual test. The shared
`.github/actions/owner-test-notes` action checks presence only, using the pull
request event as data without checking out or running its code. Run it inside an
existing required CI job and include `edited` among that workflow's pull request
events so editing notes reruns the gate. Pin the action to a full Launchplane
commit SHA. The product repo contract includes the integration pattern.

An operator with the existing `product_profile.write` capability can record an
`overridden` decision through their own human session, with a nonempty reason.
This records the operator's identity and never impersonates the Owner. An
override can account for missing notes or missing Owner setup, but cannot approve
an unavailable checklist or a different artifact. The override is scoped to the
same release digest as an Owner decision and does not bypass backups.

Product records declare `production_use` as `live`, `prelaunch`, or `unknown`.
Only an explicitly recorded `prelaunch` product is exempt from the release gate.
Existing records default to `unknown`, which requires review. No real product
names or classifications are supplied by code or checked-in configuration.
Operators must review this distinction before deploying the gate; deployment
does not change product classifications, Owner identities, or existing grants.
Writing `prelaunch` also requires `production_use_reason`, persisted on the
profile. A transition back to prelaunch requires a new reason, rather than
reusing a reason from an earlier classification.

The gate replaces manager-preview approval in the product promotion read model
and raw generic-web promotion routes. Odoo evaluates it before backup in the
combined run and again before direct promotion. The existing VeriReel service
promotion wrapper also checks it. Readiness and direct dry-runs remain available
while an Owner decision is pending. Recording a decision never merges, backs up,
dispatches a workflow, or deploys.

This gate covers promotion, not the existing direct stable-deploy,
target-replacement, and rollback operations. Those retain their separate
operator authorization boundaries and must not be used to bypass a refused
release. Closing direct production-deploy paths while preserving authorized
recovery is separate work.

An initial product needs a verified prelaunch production-lane baseline before
this comparison can run. Missing production records do not prove that no live
site exists, so the service does not silently interpret missing evidence as a
first release. Bootstrap a non-live lane explicitly, then request review before
live activation. The CM baseline must be verified from deployed service records.

Release decisions are persisted in `launchplane_release_review_decisions`, with
file storage reserved for tests and rehearsal. Deployments must migrate the
database before serving the new routes. This change does not retire the remaining
merge-admission Owner/change-impact machinery; that is the following slice of
the Owner-approval replacement.
