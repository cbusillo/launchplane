---
title: Client release review
---

Before a customer production promotion, Launchplane compiles the release from
the current production and testing revisions. The product's Client reviews the
testing site and the **`Owner test notes`** from every merged pull request in that
commit range at `/ui/owner-review?product=<product>`. No GitHub interaction is
required to read the checklist, accept it, or request changes.

The review leads with the product, the viewer's Client or admin role, the
testing-site link, and **What to test**. Pull requests with identical
`Owner test notes` appear as one check listing each change it covers, and a change missing
notes appears as its own check. Changes whose notes begin with
`Nothing for the owner to test` collapse into one expandable count. This grouping
is display only: the checklist, its digest, and blockers remain per pull request. A collapsed **Technical details**
section identifies the **Current production version** and **Proposed production
version** (currently in testing). Accepting records approval for that proposed
version to become production; requesting changes replaces earlier approval or
an admin override. The latest decision and its written feedback appear above
the checklist. Admins get a separate **Admin Approval Override** section
and approval-justification field explaining that their decision supplies
approval under their own identity and replaces an earlier
request for changes. A saved decision whose release record has not been
published shows that pending state beside the decision. Every decision is
recorded without deploying; deployment remains a later operation with its own
release and backup checks.

`GET /v1/release-review?product=<product>` serves the same checklist to the Client
and to callers already permitted to read the product or promote it. Decisions
use `POST /v1/release-review/decisions` and the existing GitHub human session and
CSRF protection. The named Client signs in without any policy role; see
[Client review](owner-acceptance.md). An automation token cannot submit a Client
decision. Identity
comes from the session, never from the request body. The product record's
immutable Client GitHub ID decides who can accept or request changes; separate
Client policies and grants are not used.

The server resolves Odoo revisions from release tuples and artifact manifests;
image-based products use deployed inventory runtime identities. GitHub compare
and commit-associated pull-request reads are paginated. Divergent history,
incomplete responses, unavailable source control, and missing lane evidence
fail closed. Every successful production promotion writes production's
environment record from the deployment it made, so the next review starts from
the version actually running. When the checklist cannot be compiled, the
response carries one fixed `unavailable_reason` code, shown on the review page
with the trace ID and logged with it: `testing_lane_missing`,
`source_control_access_unavailable`, `production_identity_missing`,
`candidate_identity_missing`, `release_record_missing`, or `github_read_failed`.
Provider error text is never returned or logged, because it can contain private
URLs. Missing test notes and commits without a merged pull request are
visible checklist blockers. `Nothing for the owner to test` is valid test notes.
Previous preview acceptance is an annotation, never release approval.
Changes to shared Odoo addon sources or selections are also bound into the
checklist and shown as an explicit blocker. They cannot be represented as an
empty, acceptable website checklist. Until their test instructions are supported
across repositories, an admin must review them and record a release-scoped
override; ordinary Client acceptance cannot waive the coverage gap.

The decision stores the complete checklist and its digest. The digest includes
the production and candidate artifact and commit, repository, Client identity,
testing URL, and checklist contents. Promotion recompiles the checklist; changed
release evidence requires a new decision. Decisions do not expire merely because
time passes. A later request for changes replaces acceptance of the same
checklist. Backup and deployment evidence remain independently required.

Recording a decision also creates a release issue in the product repository,
containing that complete checklist, the actor, decision, and reason. The Client
does not need to open GitHub. The existing managed source-control credential
must be able to read and create issues in that repository; this feature creates
no credentials or grants. Launchplane saves the decision before publishing its
record. A failed publication leaves the decision visible but cannot approve a
promotion. Recording the same pending decision retries its publication with the
same decision ID, including recovery when GitHub accepted a write whose response
was lost. GitHub issue contents and membership never decide release contents or
approval; the saved Launchplane decision remains authoritative.
The complete record uses one issue body. If GitHub rejects publication, the
decision remains saved and promotion stays blocked; records are not split into
comments. Retry recovery finds the earlier issue by the marker on its first
line, `<!-- launchplane:release-decision:<record id> -->`, so a record written
before a wording change is still found. Multiple `Owner test notes`
sections are collected together; CI checks presence, not their number or content.

GitHub attributes commits landed through a merge-train batch pull request to
that batch PR only, so the checklist shows the batch PR, not its constituents.
The batch PR body therefore carries every constituent's `Owner test notes`, each
under its own subheading, which satisfies the CI requirement below and shows
the Client each change's notes once; see
[Merge Train Policy](merge-train-policy.md#pr-native-landing). A constituent the
batch body names as having no notes is still a release blocker, as it would be
on its own.

Product CI must require a nonempty **`Owner test notes`** section on every pull
request, including changes that need no manual test. The shared
`.github/actions/owner-test-notes` action checks presence only, using the pull
request event as data without checking out or running its code. Run it inside an
existing required CI job and include `edited` among that workflow's pull request
events so editing notes reruns the gate. Pin the action to a full Launchplane
commit SHA. The product repo contract includes the integration pattern.

An admin with the existing `product_profile.write` capability can record an
`overridden` decision through their own human session, with a nonempty reason.
This records the admin's identity and never impersonates the Client. An
override can account for missing notes or missing Client setup, but cannot approve
an unavailable checklist or a different artifact. The override is scoped to the
same release digest as a Client decision and does not bypass backups.

Product records declare `production_use` as `live`, `prelaunch`, or `unknown`.
Only an explicitly recorded `prelaunch` product is exempt from the release gate.
Existing records default to `unknown`, which requires review. No real product
names or classifications are supplied by code or checked-in configuration.
The Director must review this distinction before deploying the gate; deployment
does not change product classifications, Client identities, or existing grants.
Classification changes use the existing authorized product-profile write path.

The gate replaces manager-preview approval in the product promotion read model
and raw generic-web promotion routes. Odoo evaluates it before backup in the
combined run and again before direct promotion. The existing VeriReel service
promotion wrapper also checks it. Readiness and direct dry-runs remain available
while a Client decision is pending. Recording a decision never merges, backs up,
dispatches a workflow, or deploys.

Direct deploys cannot change what a production lane runs. The generic-web
deploy, VeriReel prod-deploy, and Odoo target-replacement apply routes, and the
worker that runs queued target replacements, refuse a production artifact other
than the recorded one with `409 promotion_required` before any provider call.
They can still redeploy the current artifact, for example after a settings
change. A new release goes through promotion, which carries this gate and the
backup gate; rollback stays the recovery path. Only a product recorded as
`prelaunch` is exempt, which is also how an initial production lane is
bootstrapped.

An initial product needs a verified prelaunch production-lane baseline before
this comparison can run. Missing production records do not prove that no live
site exists, so the service does not silently interpret missing evidence as a
first release. Bootstrap a non-live lane explicitly, then request review before
live activation. The CM baseline must be verified from deployed service records.

Release decisions are persisted in `launchplane_release_review_decisions`, with
file storage reserved for tests and rehearsal. Deployments must migrate the
database before serving the new routes. The retired change-impact, `product-owner-policy`, and manager-preview evaluators
and administration routes are deleted. Their historical records remain readable
and cannot satisfy this release gate.
