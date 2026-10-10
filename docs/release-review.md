---
title: Client release review
---

Before a customer production promotion, Launchplane compiles the release from
the current production and testing revisions. The product's Client reviews the
testing site and the **`Client test notes`** from every merged pull request in that
commit range at `/ui/owner-review?product=<product>`. No GitHub interaction is
required to read the checklist, accept it, or request changes.

Checklist compilation replaces links to preview hostnames (including hosts in
the product's retained preview records) and per-PR `/ui/owner-review` pages with
the fixed line `Check this on the testing site.`. Other notes and links remain;
the source PR body is never edited. Affected items carry `preview_era_notes: true`
in the response, and the admin/read-only view identifies their PR numbers.
The digest includes the displayed notes and this marker: sanitizing old notes
changes the digest and requires review of the displayed checklist. Unaffected
items omit the false marker, preserving historical digests. Prior preview
acceptance annotations still do not change the digest.

When the checklist is complete and only the Client's approval remains, the
stable worker posts a release-review request mentioning the recorded Client,
through the existing Launchplane Delivery App's release-record access. It links
the Client review page and explains whether Accept starts the gated production
release or records approval while releases are held. Incomplete or unavailable
checklists, prelaunch/retired products, standing Director acceptance and candidates
with a recorded decision receive no request. Missing access is reported; no grant
or credential is created. The worker uses Launchplane's own bootstrap
`LAUNCHPLANE_PUBLIC_URL` for the review link and checks at most every five minutes
per unchanged profile and lane versions until delivery. After a confirmed receipt,
the worker remembers delivery in memory until the reminder is due or the candidate
changes; a replica restart reads the durable receipt again. Once the single
reminder is delivered, unchanged candidates need no further GitHub reads. Settled versions also need no
GitHub read. This cache never supplies acceptance or bypasses release checks.

The product repository keeps one issue for these requests. Launchplane finds it
by a standalone `release_request_issue_marker(product)` line in the issue body;
without one, it creates a "Release review requests" issue. To use an existing
go-live or release issue, append that marker to its body before deploying this
publisher. Lookup stops at the first page containing a marked issue, newest
updated first; keep one marked issue per product. Multiple matches on that page
or incomplete reads refuse publication. Discovery is bounded to 1,000 entries
(including PRs); for a larger repository, mark its destination before enabling
the publisher so it appears at the front of the updated-issue list. A closed
marked issue is intentionally reused; its mentions still notify the Client.
Comment lookup is also bounded to 1,000 entries; keep the requests issue unlocked
and preserve its receipts. The Client must already be able to read the product
repository for the mention to notify them; missing access is a reported prerequisite,
not a reason to grant access automatically.
The marker functions live in `control_plane/release_invitation.py`; there is no
checked-in destination catalog.

Each request includes `release_invitation_marker(product, candidate)`, derived
from the candidate's artifact, commit and shared-input identity. The worker
serializes publication across replicas using the existing release-publication
lock and checks for that receipt before posting. A retry after a lost response
adopts the existing comment; edits to checklist notes do not notify again.
The invitation greets the Client, shows the Client test notes for changes they
reviewed or were asked to review on preview, and asks them to open the release
page, check the testing site, and press Accept or Request changes. It explains
what Accept does and displays the update time in Eastern time. Engineering-only
titles are omitted; the complete checklist remains on the release page.
Its first visible line names **Release review** and asks whether the site is
working with these changes so they can go live. This is the site's release
decision, separate from the one-change preview decision described in
[Client review](owner-acceptance.md).

Before the Client decides, a candidate containing new Client-facing changes posts
one new invitation mentioning the Client and naming only the additions. A batch
of changes gets one invitation. Client-facing evidence is an existing product
preview decision or a bound per-PR review request in Launchplane's persisted
preview feedback; titles, labels and the existence of a preview are not classifiers.
For a batch landing, the stored landing plan binds the batch PR and merge commit
to its constituent PRs; each constituent uses its own review evidence and its
section of the batch's Client test notes.
Durable `release-client-change` receipts record which PRs have been announced in
this open review. Engineering-only candidates update the current invitation
silently, with no new comment or mention. Notes edits and already-announced changes
do not notify again. The `release-request` marker binds the open review to the
product, repository, Client identity and latest Client decision. A new candidate
after acceptance or a request for changes opens a new review and mentions the Client.
The worker adopts the newest legacy candidate-marked request created after that
decision (or the newest one if there has been no decision). Legacy PR links,
including batch links bound to their constituents, seed the announcement history.
An adopted manual receipt for the exact current candidate covers its included
changes even without individual PR links. Bound request evidence compares
repository names without case sensitivity, as GitHub does.
The publisher updates the wording and marks older invitations in the same open
review replaced once, collapsing the earlier wording and neutralizing its
mentions while retaining delivery receipts and historical text. Release decision
records remain the authority for accepted releases. Invitations from already
decided reviews stay unchanged. After every publication, exactly one invitation
in the open review is current. Lost post or replacement responses are recovered from those receipts;
cleanup is completed before delivery is cached. Missing timestamps or identities
refuse publication. No manual live-thread cleanup is needed.

An undecided request gets at most one reminder, no sooner than three days after
the current invitation's creation. Silent candidate updates do not reset that
clock or send a reminder during the update. A fresh Client-facing addition resets
the reminder deadline; the open review still gets at most one reminder. A reminder
also replaces the older invitation. A separate `release-reminder` marker binds the
reminder to the open request, so retries and worker restarts cannot send it twice,
even when another candidate arrives. A decided or incomplete review gets no
reminder. Markers are delivery
receipts only and never supply release acceptance. Preserve them during edits;
deleting a receipt permits another notification.

For a candidate already requested manually, append its candidate marker on its
own line, without indentation or trailing spaces, to the existing request comment
and the issue marker to that issue's body before the
new publisher starts. Leave its wording and mention unchanged during marker
adoption; later candidates follow the in-place update behavior above. Read the exact
candidate from the supported release-review endpoint and pass its `ReleaseVersion`
to the marker command below; do not infer it from a shortened commit or from the newest
testing build after it has changed. This imports delivery evidence without sending
another request or submitting a decision.

Save the endpoint's exact `checklist.candidate` object as a private JSON file
and use the supported read-only marker command:

```sh
uv run python -m control_plane.release_invitation --product example-site --candidate-file /path/to/candidate.json
```

It prints the issue and comment markers and makes no service or GitHub request.

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
Changes to existing shared Odoo addon sources appear in separate Client checklist
sections, with each repository's exact production-to-testing SHA range, merged
pull requests and Client test notes. The same commit coverage and missing-note
checks apply to product and shared repositories. Added or removed sources,
unexplained selection changes, and ambiguous or inconsistent source evidence
remain blockers. A failed shared-repository read or a non-forward SHA range stays an explicit
coverage blocker, preserving the existing release-scoped admin review path.
Launchplane uses that repository's existing scoped Delivery App access and never
adds a grant. The complete shared evidence is saved in the release decision and
published in its release record. Ordinary Client acceptance cannot waive missing
coverage.

The server reports checklist completeness for the Client button, including
missing notes inside merge-train batches. Historical shared-input overrides
remain readable, but replacing the blanket blocker with detailed shared coverage
changes the checklist digest and requires a new decision.

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
Publishers of the same saved decision serialize lookup, issue creation and the
stored URL acknowledgement with a per-decision storage lock. After waiting, a
publisher reads the saved decision again and reuses its authoritative URL. The
PostgreSQL holder reads and acknowledges that exact decision using its lock
transaction's connection, so waiting publishers cannot exhaust the pool it
needs to finish. The URL commits with that transaction before publication
returns. Lock waits are bounded to five seconds (or an earlier configured
statement timeout); storage failures leave the saved decision pending for retry.
The PostgreSQL transaction lock and local file lock release when their worker exits;
an interrupted acknowledgement recovers the existing issue by its marker before
any new creation. Local SQLite publication requires a file-backed database so
separate processes share the file lock.
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
every product), `promote`, `promote_with_rollback_drill` (Odoo or generic web), or
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

### A control-plane replacement drains release effects first

The shared self-deploy route records a database admission fence before changing
the provider. Its transaction lock also covers backup and Odoo release claims
(including failure recovery) and generic-web release reservations. A Client can
accept while replacement is preparing; that decision and pending steps remain
durable, but no new release effect starts behind the fence. Operations admitted
before it finish their admitted backup, deployment, drill, post-checks or
compensation and commit normally. Separately queued Odoo compensation remains
durable and resumes on replacement workers if it was not yet admitted. The API
remains available during this drain. Completed step IDs
are reused after replacement, so polling or a worker restart cannot duplicate
their effects. The release read explains the pause; the engineering runtime read
exposes `runtime.release_drain` and its running operation IDs.

`POST /v1/drivers/launchplane/self-deploy` uses its existing authorization and a
fresh, never-reused `deploy.oauth_env.LAUNCHPLANE_DEPLOYMENT_MARKER`. The workflow
generates it from the run and attempt; direct callers supply a new marker for
each new intent. While operations run it
returns `result.deploy_state=draining`, without changing provider configuration
or caching that poll as a completed idempotent response. Repeat the same payload
and key until it returns `requested`. The Deploy Launchplane workflow does this
with its existing request action. Each drain poll renews a two-minute pre-effect
fence; an abandoned drain expires without stranding pending Client releases.
Final admission rechecks the fence and running operations under the same lock.
The typed Odoo and shared backup queues also contain workflow-started operations;
their claims pause at this worker boundary and resume after replacement. Worker
debug logs report that pause, and the runtime read exposes its cause. Generic-web
workflow requests outside Client releases retain their existing reservations;
this change covers Client-started generic-web releases.
An active provider request returns pollable `dispatch_in_progress`, also without
caching the intermediate response. An expired release lease stays a drain
blocker: losing its heartbeat does not prove the provider effect ended. Reconcile
that operation through its existing recovery route before replacement.
The request action tolerates network errors while polling the drain. Its
latest drain response is retained on timeout; a later failed request clears that
response to retain uncertainty. Rollback skips only positively pre-effect
outcomes. The authorized runtime read reconfirms its own matching image/marker
when repair refusal restored a request whose startup raced the repair.
The workflow performs this authorized read even after deployment or repair failure.
Keyed and keyless self-deploys use the same canonical request fingerprint;
per-caller idempotency response ownership is unchanged. A definite,
non-retryable 4xx refusal of the first environment write records `refused`, frees
this attempt's fence, and never dispatches or replays that request. The previous
fence is restored: a refused repair cannot unlock an earlier uncertain replacement
or unquarantine old workers. Timeouts, 5xx,
remote command failures, or failures after that write remain uncertain and fenced.
Bootstrap key-ring rollback keeps its existing expected-value/absence guards;
when those refuse uncertain compensation, use the explicit service repair input
with `bootstrap_secret_operation=preserve` after reviewing provider state.

Provider dispatch is recorded before its first effect and never automatically
replayed after an uncertain response. Per-request receipts survive later service
replacements, so an old lost-response request stays settled. Only startup of the exact requested image
and deployment marker confirms the replacement. The requested fence does not
expire, and old worker processes remain fenced even after confirmation. Pending
operations then run on the matching replacement workers. A changed candidate,
revocation or admin release hold still refuses forward release work. A lost self-deploy dispatch response returns
`self_deploy_reconciliation_required` on replay; matching replacement startup
can settle it without another dispatch. If no matching replacement starts,
the existing authorized self-deploy route accepts a new repair request with a
new key, a compatible immutable image, a fresh marker, and
`deploy.supersedes_deployment_marker` equal to the stuck fence's marker from
`runtime.release_drain`. The target must also match that fence. This deliberately
requests one new service replacement; it never replays the original dispatch
or clears the fence before matching API process startup. Confirmation precedes
the container health checks; failed deployment health starts a new repair fence,
and already admitted effects must still finish before that replacement.
Automatic service rollback
uses the same marker-bound repair, including same-image configuration failures.
For a cancelled run, manually dispatch Deploy Launchplane with the compatible
`image_reference`, a new `self_deploy_idempotency_key` and the exact
`supersedes_deployment_marker` from `runtime.release_drain`; this uses the existing
workflow authorization. Automatic runs cannot set the repair input. After gated break-glass restores a compatible
service, use this route to reconcile any remaining fence; never edit its DB row.
Provider-call serialization refuses a repair while an earlier service request
is still executing, so a late original dispatch cannot overtake that repair.
A drain timeout before dispatch requests no service rollback; its pre-effect
fence expires. Uncertain release outcomes retain their
existing stopped or reconciliation state and are never retried by replacement.
The reconciliation routes described below remain their supported recovery path.

The workflow prepares separate drain and deployment-observation budgets before
any provider effect. After `release_drain_complete=true`, image, marker and health
waits share a new deadline using deployment/health budgets and other Compose worker
graces. The release workers' drain allowance is excluded because their admitted
operations already committed. A broken replacement therefore consumes the
deployment budget rather than a two-hour capture window. Failed-image recovery
retains the existing service rollback and gated break-glass paths in
[operations](operations.md#launchplane-service-deploy-posture).

First rollout must still occur with no active or invited Client release:
containers running earlier code do not enforce this fence. The additive schema
migration also requires a schema-compatible repair image; an older image whose
schema guard rejects the new head cannot resume workers. Qualify the installed
service and both worker families before the Supervisor retires its temporary
Client-invitation landing hold. This protects planned service replacements, not
arbitrary host loss or a forced kill of an unsafe provider effect; those remain
truthfully interrupted and require the existing reconciliation path. The isolated
rehearsal is `tests.test_client_release_redeploy`; it drains during each step for
both drivers, starts a separate replacement API process through its factory-store
lifespan and health read, reopens worker storage, resumes the drill once, and preserves failed
forward outcomes after automatic recovery. PostgreSQL integration separately
proves both admission-versus-drain race orders with real database locks.
The local rehearsal uses synthetic providers and SQLite; its API starts in a
new process while the test reopens worker storage. Installed worker-process and
provider qualification remains the Supervisor's next step.

### Interrupted provider operations

Failures before any provider effect are recorded as terminal failures for this
release rather than silently retried. An expired promotion lease is shown as
`reconciliation_required`; the provider fence stays in place. The deploy recovery
route does not recover promotion reservations. For a Client-started generic-web
release, an existing scoped admin can select the exact decision through
`GET /v1/admin/generic-web/promotion-recovery/{product}/{decision_record_id}`.
The response contains an opaque recovery reference, reservation state and
checkpoint; it exposes neither the original key nor provider coordinates.
Selection does not call the provider or run health checks; `hold_unknown` means
the outcome still needs dry-run inspection.
`POST` to that path's `/dry-run` with a written `reason` inspects the original
accepted checklist, backup, reservation, target and durable promotion outcome.
Both reads require the existing product-scoped `product_environment.read`
capability and write no records or provider state.

Separately reviewed `/apply` requires the same reason, `recovery_reference`,
`expected_recovery_digest`, and current production-scoped
`generic_web_prod_promotion.execute` authority. Browser writes use the existing
session/CSRF protection; terminal-agent and workflow identities cannot apply.
Local admin tokens use the same scoped authority.
Recovery rechecks exact configured/running immutable images, operation deployment
IDs and current-lane runtime-identity health. It can finish interrupted health
checks on an exact, durably recorded successful deployment, or adopt a proven
final promotion or verified automatic rollback, preserving a failed release as
failed. It completes the exact reservation, finishes promotion/deployment health
evidence and repairs a lagging inventory in one compare-and-adopt transaction;
changed stored evidence refuses the transition. Completed recovery replays without
effects. No provider deployment, rollback, new reservation or grant is created.
Original promotion workers also fence their evidence writes in the same database
transaction as the lease check, so a paused worker cannot overwrite the recovered
records after its lease expires. Before deploying, the pending promotion stores
the previous production deployment and inventory lineage; a rollback checkpoint
can then identify its exact recorded rollback deployment after a process crash.

Active leases wait. Missing, ambiguous, changed or unknown evidence, no-effect
reservations, unfinished deployments, failed health checks, and unproven or failed
rollback deployments remain held. A deployed image alone never proves the
promotion or its checks succeeded. This is admin-assisted reconciliation, not
unattended crash recovery or an automatic retry. Real recovery still requires
the Director's separate authorization under [DIRECTION.md](../DIRECTION.md).
Recovery also holds when the current lane has no verifiable health URL. Recorded
failed health checks are retained as failures. Older interrupted rollback records
without the saved target evidence remain held. Successful recovery settles the
existing accepted release step; the release worker can continue any remaining
steps under that decision's normal acceptance and hold checks.
The current Dokploy runtime observer proves compose targets only; application
targets remain held until their provider supports the same exact runtime proof.

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

Odoo and generic-web products use this same sequence and acceptance checks.
Generic web redeploys the explicitly selected passing production deployment's
recorded image, verifies its runtime identity and health, and restores that
version's inventory before the second backup and promotion. Its drill uses the
same provider fence and durable step reservation as promotion; an unknown
rollback outcome stops the run for reconciliation. A failed drill or second
backup never starts re-promotion. Automatic rollback after a failed promotion
remains permitted even when the Client's forward release authority is revoked.

Before the first backup, a generic-web drill requires a deployable passing
production record for the checklist's exact artifact and source commit. A
missing target is shown as a release blocker without changing production.
For an interrupted drill, use the same scoped admin recovery route above with
`?step=rollback` on selection, dry-run and Apply. It binds the original target
through the original deployment and reservation fingerprint, verifies the exact
recorded rollback deployment against the provider's configured/running image,
deployment ID and current health, then adopts the existing result atomically.
It starts no provider effect and grants no access. Active leases, no-effect
reservations, missing or changed records, failed health and unproven runtime
stay held; a completed adoption replays. Normal acceptance and hold checks
still gate the second backup and re-promotion.

Each step runs under a `client_release_acceptance` grant that names the
decision. Before a step is queued, the worker checks all of the following:

- the decision is still the product's newest one, `accepted`, published, and by
  the product's recorded Client;
- releases are not held, and the product is not `prelaunch`;
- the production and testing lanes still carry the checklist's artifacts;
- the recompiled checklist digest still equals the decision's, and it is
  approved.

The worker repeats the decision, Client and hold checks before forward provider
effects. The promotion still checks release approval for the exact candidate and
the verified backup. Once generic-web production has changed, automatic rollback
remains allowed to restore the admitted previous deployment even if acceptance
is withdrawn. A crash or uncertain provider outcome keeps the provider operation
fenced for reconciliation rather than repeating the release.

A changed candidate, a newer decision, or a hold therefore stops the release
before its next step and never falls back to a newer testing build. A failed or
cancelled step stops forward progress; the successful rollback drill never runs
for a failed promotion. An admin override never starts a release; it stays a hand
promotion from the Release panel. No automated identity gains a promote right,
and an automation token still cannot submit a decision.

For Odoo, promotion admission pins the checklist's production artifact and its
passing deployment in the operation's initial checkpoint. The worker verifies
that binding again before the first production write and records that write's
boundary durably. A determinate deployment or post-deploy/health failure commits
the failed promotion and queues its recovery rollback in one lane-locked
transaction. Recovery explicitly names that failed promotion and redeploys only
the pinned artifact with existing data; it does not restore a database or reuse
a backup for another forward promotion. It verifies post-deploy, health,
canonical URL, logos and runtime identity through the existing replacement path,
and writes the recovered deployment, inventory, release tuple and rollback
outcome. The release stays failed, with a separate `recovery` step in its
readback, labeled automatic recovery rather than a rollback drill. Failure on
the second promotion has its own recovery operation. A missing passing baseline
prevents the first backup from being queued and appears as `blocked_reason` in
the release run.

Recovery uses the original admitted acceptance, even if releases become held,
the Client changes, a newer decision replaces acceptance or testing moves after
the first write. Its worker checks the exact source operation, original
published decision, failed promotion and pinned passing deployment; it supplies
no caller promotion or manual rollback permission. Missing recovery provenance
holds the lane for reconciliation. Pre-write failures queue no recovery. A lost
response, unobserved deployment, post-deploy timeout, interrupted recovery or
expired lease after effects remains `reconciliation_required`; the worker does
not replay an uncertain write or report it passed. A determinate failed recovery
is recorded as failed and is not automatically retried. Admin reconciliation is
still required for uncertainty; deployed-path qualification is separate from
these deterministic provider tests.

Queued Odoo administrator promotions and rollbacks also hold uncertain effects
for reconciliation. Synchronous non-worker callers retain their existing result
handling. Client promotions queued by an older worker without a recovery pin
are refused before writing; requalify pending release operations when deploying
this worker, and record fresh acceptance if such a release was stopped.

The review page says before the Accept button whether accepting puts the
version on the live site, naming it, or whether releases are held. After
acceptance it shows each step's status. A stopped step also shows its bounded,
redacted recorded reason and code. Expand **Failure details** for the failure
record, operation and recorded trace IDs. Cancellation can replace an operation's
saved trace with the cancellation request's trace. A missing saved trace says **Not
recorded**: this includes older queued operations and generic-web reservations
stopped before a response was saved. New queued backup and Odoo release
operations persist a trace; generic-web reservations expose their saved response
trace after settling.
The activity read derives stopped-step events from those same operations, with
the reason in `summary` and record, operation, decision and available trace IDs
in `records`. Reading this evidence never retries a stopped release.
`GET /v1/release-review` returns
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
