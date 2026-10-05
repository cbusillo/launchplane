---
title: Client release review
---

Before a customer production promotion, Launchplane compiles the release from
the current production and testing revisions. The product's Client reviews the
testing site and the **`Client test notes`** from every merged pull request in that
commit range at `/ui/owner-review?product=<product>`. No GitHub interaction is
required to read the checklist, accept it, or request changes.

The review leads with the product, the viewer's Client or admin role, the
testing-site link, and **What to test**. Pull requests with identical
`Client test notes` appear as one check listing each change it covers, and a change missing
notes appears as its own check. Changes whose notes begin with
`Nothing for the Client to test` collapse into one expandable count. This grouping
is display only: the checklist, its digest, and blockers remain per pull request. A collapsed **Technical details**
section identifies the **Current production version** and **Proposed production
version** (currently in testing). Accepting records approval for that proposed
version to become production; requesting changes replaces earlier approval or
an admin override. The latest decision and its written feedback appear above
the checklist. Admins get a separate **Admin Approval Override** section
and approval-justification field explaining that their decision supplies
approval under their own identity and replaces an earlier
request for changes. A saved decision whose release record has not been
published shows that pending state beside the decision. Only the Client's
acceptance can start a release, and only as described under
[Acceptance starts the release](#acceptance-starts-the-release); every other
decision is recorded without deploying.

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
and commit-associated pull-request reads are paginated. Independent commit
coverage reads run with bounded concurrency so promotion status does not wait
for each network request in sequence; checklist aggregation remains in commit
order. Divergent history,
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
visible checklist blockers. `Nothing for the Client to test` is valid test notes.
Pull requests written before the role words changed say `Owner test notes` and
`Nothing for the owner to test`; Launchplane and the CI action read both.
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
was lost. Repeating the product's newest published decision records nothing new,
so a second Accept never replaces the acceptance a running release depends on. GitHub issue contents and membership never decide release contents or
approval; the saved Launchplane decision remains authoritative.
The complete record uses one issue body. If GitHub rejects publication, the
decision remains saved and promotion stays blocked; records are not split into
comments. Retry recovery finds the earlier issue by the marker on its first
line, `<!-- launchplane:release-decision:<record id> -->`, so a record written
before a wording change is still found. Multiple `Client test notes`
sections are collected together; CI checks presence, not their number or content.

GitHub attributes commits landed through a merge-train batch pull request to
that batch PR only, so the checklist shows the batch PR, not its constituents.
The batch PR body therefore carries every constituent's test notes under the older `Owner test notes`
heading, which every pinned version of the CI action accepts, each
under its own subheading, which satisfies the CI requirement below and shows
the Client each change's notes once; see
[Merge Train Policy](merge-train-policy.md#pr-native-landing). A constituent the
batch body names as having no notes is still a release blocker, as it would be
on its own.

Product CI must require a nonempty test notes section on every pull request,
including changes that need no manual test. Write **`Client test notes`** once the
repository pins the action at or after the commit that introduced that heading;
with an older pin, keep **`Owner test notes`**, which every version accepts. The shared
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
The Director changes classification in the Client panel through
`POST /v1/product-profiles/{product}/production-use` under `product_profile.write`: dry run,
Apply bound to the reviewed plan digest, audit record, and profile read-back.

The gate replaces manager-preview approval in the product promotion read model
and raw generic-web promotion routes. Odoo evaluates it before backup in the
combined run and again before direct promotion. The existing VeriReel service
promotion wrapper also checks it. Readiness and direct dry-runs remain available
while a Client decision is pending. Recording a decision never merges or
dispatches a workflow, and only a Client's acceptance starts a release.

## Acceptance starts the release

Each product profile records `release_on_acceptance`: `held` (the default for
every product), `promote`, `promote_with_rollback_drill` (Odoo), or
`director_standing` (generic web). An admin with
`product_profile.write` changes it in the product's Client settings, through
`POST /v1/product-profiles/{product}/production-use` with the optional
`release_on_acceptance` field: dry run, Apply bound to the reviewed plan digest,
audit record, and profile read-back. A held profile serializes exactly as it did
before the switch existed.

When the product's recorded Client accepts a release of an Odoo or generic-web product that is
not `prelaunch` and not held, the decision stores `release_start`, fixed at that
moment. Recording it starts nothing by itself. Launchplane's Odoo stable worker
checks about every 30 seconds and queues the same operations the admin's
Release panel queues:

1. a verified production backup;
2. the promotion with that backup, which deploys, runs post-deploy and health
   checks, and rolls back on failure. Odoo queues its existing Release operation
   and takes its logical backup. Generic web runs its existing promotion through
   the durable provider-operation runner, with a target fence and heartbeat;
   its promotion record names the deployment and automatic rollback outcome.

For generic web, a signed-in admin can record `director_standing` through the
same reviewed Client setting. This explicitly records that the named Client is
the Director and that the Director's standing acceptance starts releases.
Admin permission never implies that a Client is the Director. A machine caller
can prepare a dry run but cannot apply this mode. Changing the recorded Client's
immutable identity resets this mode to `held`.

The worker compiles each complete candidate's checklist and records an accepted
decision under the recorded Client's identity, with
`acceptance_source: director_standing`; it does not claim a human clicked Accept.
It publishes that exact checklist through the existing release-record path
before queueing the backup. Publication retries reuse the saved decision.
Creation is insert-if-absent: replicas racing on the same decision ID publish
the stored winner, retaining its original Client identity and decision time.
Publication updates only an existing decision's empty issue URL under a storage
lock; a stale writer cannot clear or replace an authoritative publication or
rewrite the checklist and audit fields.
The Client's decision route uses the same creation and publication operations,
so a Client retry cannot upsert a stale unpublished snapshot over the worker's
publication. An already published winner needs no further source-control lookup.
The first stored issue URL is authoritative. Concurrent external issue creation
can still produce a duplicate issue with that decision's marker; publication
serialization is tracked separately in [#3030](https://github.com/cbusillo/launchplane/issues/3030).
Missing notes, missing lane identities or a Client, a held or prelaunch product,
and a request for changes or admin override on that checklist start nothing. A failed
release is not automatically retried with another acceptance of the same
checklist. No agent receives a promotion grant and no product workflow calls
Launchplane. Enabling this setting is a production-release activation; prepare
and verify the Client, testing lane and backup policy before applying it.
Changing unrelated profile metadata does not re-accept or retry that checklist.
Settled decisions on unchanged lane versions require no background GitHub read.
Incomplete standing reviews back off for five minutes on each worker; changing
the profile or lane versions triggers a fresh review. This timer never supplies
acceptance or replaces the checks immediately before a release effect.
A Client can explicitly Accept again after a stopped release; a repeated Accept
while a release is running still returns the original decision.

The stable worker advances Client releases on one background thread per replica,
so a generic-web deploy or rollback wait does not block queued Odoo operations.
Shutdown finishes an admitted operation and starts no further product release.
Failures before any provider effect are recorded as terminal failures for this
release rather than silently retried. An expired promotion lease is shown as
`reconciliation_required`; the provider fence stays in place. Recovery of generic-web
promotion reservations is not yet supported by the deploy-only recovery route,
so [#3002](https://github.com/cbusillo/launchplane/issues/3002) tracks the
administrator recovery capability needed before relying on unattended crash
recovery.

With `promote_with_rollback_drill` it then runs the rollback drill:

3. a rollback to the production version the Client's checklist was compiled
   against, at that artifact's newest passing prod deployment. It never uses the
   default "previous deployment" choice, so an image recorded as a failed
   promotion cannot be picked;
4. a second verified backup, because backup evidence is single-use;
5. the same promotion again.

The rollback restores the checklist's production version, so the checklist
recompiles to the digest the Client accepted, and that acceptance covers the
second promotion and nothing else. A product drills once. After a release's
drill passes, `promote_with_rollback_drill` behaves as `promote`.

Each step runs under a `client_release_acceptance` grant that names the
decision. Before a step is queued, the worker checks all of the following:

- the decision is still the product's newest one, `accepted`, published, and by
  the product's recorded Client;
- releases are not held, and the product is not `prelaunch`;
- the production and testing lanes still carry the checklist's artifacts;
- the recompiled checklist digest still equals the decision's, and it is
  approved.

The worker repeats the decision, Client and hold checks before every provider
effect. The promotion still checks release approval for the exact candidate and
the verified backup. Once generic-web production has changed, automatic rollback
remains allowed to restore the admitted previous deployment even if acceptance
is withdrawn. A crash or uncertain provider outcome keeps the provider operation
fenced for reconciliation rather than repeating the release.

A changed candidate, a newer decision, or a hold therefore stops the release
before its next step and never falls back to a newer testing build. A failed or
cancelled step stops the release, and nothing more runs until the Client
accepts again. An admin override never starts a release; it stays a hand
promotion from the Release panel. No automated identity gains a promote right,
and an automation token still cannot submit a decision.

The review page says before the Accept button whether accepting puts the
version on the live site, naming it, or whether releases are held. After
acceptance it shows each step's status. `GET /v1/release-review` returns
`release_on_acceptance` (what the next acceptance would do), `live_site_url`,
and `release_run`, derived from the step operations, whose ids come from the
decision.

A release that changes shared Odoo addon sources still needs an admin override,
so it remains a hand promotion. The Client cannot undo a live release; that is
an admin rollback.

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
