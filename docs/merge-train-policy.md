# Merge Train Policy Contract

## Terminology

Launchplane has two GitHub-backed runner modes. The Level 1 ordered merge queue
reads a fresh GitHub snapshot, orders eligible pull requests, selects the first
eligible entry, and applies at most one worker transition per service call. The
controller runs the batch-validating train and is the preferred entrypoint; see
`POST /v1/work-graph/merge-train/controller/run-once` below.

The merge train is provider-neutral and batch-validating. Source-control-specific
reads and effects belong behind an adapter; the steps below describe the current
GitHub adapter:

1. Collect eligible queued pull requests for one repository/base branch.
2. Build one combined batch candidate from the base branch plus queued pull
   requests in train order.
3. Run required checks against that exact candidate commit.
4. If the candidate passes, land the original pull requests in train order using
   GitHub's normal pull request merge path so repository UI and audit hints stay
   attached to each PR.
5. If the candidate fails or cannot be built, split or reduce the batch to
   isolate blockers, then mark or requeue entries according to policy.

Launchplane merge trains use an explicit repository policy before any worker is
allowed to enqueue, update, or merge pull requests. Live service routes resolve
the active `launchplane_merge_train_policies` record from Launchplane storage.
If no active record exists, service routes fail closed with
`merge_train_policy_not_configured`. Admins change live policy by writing a
new DB-backed policy record, not by relying on checked-in config files,
service-host env, or generic service-code conditionals.

## Preparing An Ordinary-Agent Target

> Ordinary-agent delegated delivery is retired under
> [DIRECTION.md](../DIRECTION.md). This surface remains only until its code is
> deleted; do not build on it.

The Engineering Ops Merge-train policy workbench can prepare one new target
without reconstructing the active policy. The supported service surface is:

- `GET /v1/privileged-operations/merge-train-targets/inputs`: current tracked
  repository identities, active-policy provenance, and configured target keys.
- `POST /v1/privileged-operations/merge-train-targets/prepare`: one immutable
  repository ID, an explicit base branch, and the new target's engineering
  choices, with a stable source-event ID for retries.

Both routes use the existing descriptor's managed human proposal authority;
the POST also requires the normal browser mutation and CSRF checks. They do not
introduce an authorization-policy admin requirement. Repository names
come from current unambiguous tracked inventory. The caller supplies labels,
merge method, review and failure policy, enqueue rules, and merge-identity
metadata. An optional provider-protection expectation is a reviewed input;
absence means the target remains unqualified.
Incomplete, unavailable, or ambiguous inventory fails closed; preparation never
selects a repository from an incomplete history scan.

The server preserves all unrelated targets, derives the candidate record,
timestamp, digest, and routine reason, then invokes the existing
`managed-merge-train-policy-import` planner. An identical configured target
returns `already_satisfied` without a write; a different target at the same key
conflicts. This surface cannot update or remove a target. It returns an inert
plan for the existing review and approval lifecycle and never installs a policy.

Preparation retains the original intent and expected active-policy ID, digest,
and normalized timestamp in the proposal. The planner rejects a changed
baseline or any candidate difference beyond the one addition before persisting
the plan. Retrying the same actor, source event, and intent returns the original
operation before reading newer policy state. Changed intent and collisions with
an operation lacking preparation context fail closed. Historical generic import
requests omit the optional context and retain their original serialized shape.
Generic human and terminal proposal inputs cannot supply preparation context.

A prepared target has an empty token-environment binding and a disabled,
nonmutating scheduler. Normal controller dispatch consequently cannot execute
it. Ordinary delivery still needs its separate DB-backed custody, protection,
eligibility, and activation evidence. `merge_identity` is policy metadata for
this path, not proof of an installed App or credential. The shared
`service_authz` gate does not select or grant a repository. Preparing or
installing this policy does not activate an ordinary worker, create credentials,
or establish preview readiness. Clients do not use this engineering
administration surface.

## Credential Source And Policy Readback

A repository policy names one GitHub credential source:

- `github_token.env_var` reads the named Launchplane service bootstrap variable
  for existing policies.
- `github_token.runtime_context` resolves `LAUNCHPLANE_GITHUB_TOKEN` through the
  named DB-backed runtime context, including global shared values and global
  secret bindings that the runtime-context contract intentionally includes.
  Selecting a context selects its resolved credential for GitHub operations;
  this is a credential-authority decision for the policy reviewer. It does not
  create a credential or expand its provider permissions.
- `github_token.github_app` names an `app_id`, immutable `repository_id`, and
  `private_key_context`. The key comes from the managed secret integration
  `merge_train_github_app`, binding key `private_key`, in that exact context.
  One configured binding to a context-scoped, write-only current secret is
  required; global, instance-scoped and duplicate bindings are refused. The
  policy's merge identity must have kind `github_app`. Each resolution mints a
  fresh installation token for exactly the policy's repository, with Contents
  and Pull requests write, Workflows write, and Checks
  and Commit statuses read. The installation must provide at least those grants;
  it may also serve other approved operations. The freshly issued train token
  must contain only the native train permissions, regardless of the installation's
  broader capabilities. App and
  installation identity, returned repository ID/name, token permissions and
  expiry are verified; a token that fails verification is revoked. Tokens are
  not stored as runtime configuration. No Administration permission is used.
  Every train token has this same permission set, including tokens resolved for
  reads; Workflows write lets branch updates and merges include workflow files.
  The event reconciler also mints narrower tokens from a product repository's
  App: a read-only build-provenance token, and a pull-request feedback token
  with only Pull requests write and Contents read
  ([event-driven deploys](event-driven-deploys.md#pull-request-feedback)).
  A missing installation grant is reported by name in service logs. Coordinate
  installation approval with this service capability: an older service with an
  installation permission ceiling will refuse a broader shared installation.

The sources cannot be combined. If all are empty, the target remains
unconfigured. If the selected source cannot resolve a token, the service
refuses the operation; it never tries a service-host bootstrap token, a different
context, or an agent's local credential. Configured global runtime values are
part of the selected context, not an alternate source. Controller, phase-specific operations, historical proof and
current governance readiness use the same resolver. The controller's landing
admission reads pull-request evidence with that same policy credential, so
enrolling a repository never also requires the service-wide
`LAUNCHPLANE_GITHUB_TOKEN` to reach it. Adding optional source fields
does not change existing policy bytes or digests. Selecting a managed source
changes the full policy digest and therefore requires a new reviewed policy
revision. Check every repository in that policy for active train work before
applying the revision: in-flight candidates, landings and controller fences bind
the full digest. It does not change the repository's delivery-semantics digest.

`GET /v1/work-graph/merge-train/policy` returns the active persisted policy record
for callers with the existing global `merge_train.policy_targets` read action.
The response includes source names and record provenance, never resolved token
values. It grants no execution or policy-write authority. Use this read before
preparing an exact policy update so unrelated targets and settings are preserved.
The target-list route remains the smaller scoped summary.

## Separation From Tenant Repository Admission

Scheduler merge train admission (`merge_train_admission`) governs pull request queueing, batch candidate construction, and landing order under active `launchplane_merge_train_policies` records.

Tenant merge eligibility (`evaluate_tenant_merge_eligibility`) and repository classification records (`launchplane_tenant_repository_classifications`) operate independently under their own DB authority:
- Repository classifications explicitly categorize repositories as `engineering` (taking the normal engineering fast path) or `tenant_ui`; both take the normal technical merge flow, and retired human admission paths do not qualify a merge.
- Repository classifications use exact immutable identity and CAS admin recovery without heuristics or PR label fallback.
- Unified tenant admission is recomputed from DB records. The GitHub `tenant-admission` commit status is a public projection, not merge authority.
- The tenant admission controller is a separate exact-PR landing path. It re-fetches current GitHub identity/head/mergeability facts, recomputes admission, reads the live required-status-check policy, filters admission projection contexts out of that policy, enforces strict base freshness when configured, and rechecks all three immediately before an expected-SHA merge. Missing or malformed required-check policy or evidence fails closed. It does not enqueue work or reuse scheduler ordering, labels, batch candidates, stack collapse, or failure policy.
- The tenant controller and merge train share the repository/base controller-state row only as a mutual-exclusion and crash-reconciliation fence. Acquisition writes a controller-specific initial action atomically and adoption is action-aware, so one controller cannot rewrite another controller's unfinished recovery state even before its first checkpoint. The row cannot make a tenant admission decision and a green GitHub status is never merge authority.
- Advisory `launchplane/engineering-review` and
  `launchplane/owner-acceptance` check runs are visibility projections and are
  excluded from merge-train and tenant-admission technical-check inputs. Their
  presence, state, or provider replay cannot change a Launchplane verdict.
- Engineering repositories remain on their existing flow; invoking the tenant controller for an engineering classification returns not-applicable without merging.

## Controller Lease And Resume State

Mutating controller passes are fenced by a Launchplane-owned repository/base
controller lease record. One controller lease holder may actively mutate one
`repository/base_branch` pair at a time; a second holder must fail closed while
the current lease is still valid. The lease record stores holder, expiry,
active action/phase, and reconciliation detail under Launchplane storage, not
in workflow inputs or checked-in config.

Every GitHub effect that can cross a crash window has a durable pre-effect
checkpoint. Candidate construction can safely reset and deterministically
rebuild its temporary ref. Landing records each merged PR before advancing.
Stack collapse records each child-to-parent merge, and child disposition
re-observes its managed comment, label, and closed state before applying a
missing effect. Candidate-ref cleanup remains an active durable phase until the
ref is observed absent. When a controller restarts after a crash, it re-reads
GitHub state and adopts already-completed effects when the stored phase evidence
still matches the live repository/base policy and expected SHAs. If lease
ownership, expected SHA guards, or reconciliation evidence no longer match, the
controller stops in `reconcile_required` state instead of continuing
optimistically.

Reconciliation detail distinguishes retryable provider interruptions from
admin-required conflicts. Retryable states retain the exact active phase and
may be adopted by the next mutating controller pass. Deterministic policy,
expected-SHA, or provider rejection states use an `operator_required:` detail
so admins can repair the repository or provider condition before retrying
through the service. The next mutating controller pass adopts that exact phase
and re-observes provider state; no out-of-band record edit is required or
supported. Read-only controller calls report either state without acquiring or
mutating the lease record.

If an approved policy change invalidates an unlanded native batch, the controller
can retire its old landing plan after fresh GitHub reads prove every PR is still
open at its recorded head/tree. An unresolved attempt also requires the original
base/tree; when every admission already has a rejection outcome (or no admission
was issued), unrelated base movement does not prevent retirement. It records
no-effect reconciliation for any unresolved admission, appends a stale landing
record, and supersedes only that batch's old-policy candidates. The next pass
plans a fresh candidate under the current policy; old checks, admissions, and
terminal records from the previous policy cannot authorize or suppress it.
Candidate refs remain as recovery evidence; a rebuild reuses one only when its
base and heads still identify the same batch.
The same retirement applies when landing admission is denied with
`landing_lineage_changed` before any entry has merged, for example when an
older pull request is labelled after the plan and now sorts ahead of it. Such a
plan can never match the live queue again, so the controller retires it instead
of blocking on every pass, and the next pass plans a candidate from the live
queue.
An interrupted retirement resumes from its persisted evidence. Partial landings,
collapsed stacks, retired ordinary-agent jobs, and unreadable or conflicting
provider evidence still require explicit reconciliation.

Lease expiry semantics are fail closed. PostgreSQL observes acquisition,
heartbeat, and expiry time inside the advisory-locked transaction rather than
trusting a caller timestamp. Once a lease expires and another controller
acquires the same repository/base fence, the stale holder cannot heartbeat,
checkpoint, or release because every transition compares both holder and
acquisition token under the same storage lock. Local filesystem rehearsal uses
an atomic per-controller file lock with the same transition contract.

## Fields

Each repository policy contains:

- `repository`: GitHub `owner/name` repository.
- `base_branch`: Branch the train protects and merges into.
- `enqueue_label`: Label required before a pull request can enter the train.
- `blocked_label`: Label Launchplane applies when a queued pull request blocks.
- `stack_child_disposition_label`: Label Launchplane applies to child PRs after
  a same-repository stack has landed through its root PR. It must be configured
  for stack child disposition and must differ from `enqueue_label` and
  `blocked_label`.
- `merge_method`: GitHub merge strategy, one of `merge`, `squash`, or `rebase`.
- `engineering_review_mode`: Advisory review evidence only (`advisory`, the
  default). New policy imports and target preparation reject `required`, which
  is retired by the overall DIRECTION.md. Historical policy records retain
  their original values and digests, but even an active legacy `required`
  policy cannot make engineering-review evidence block a merge. To prepare a
  new target from a legacy active policy, import a replacement policy with
  every repository set to `advisory`; preparation reports
  `merge_train_engineering_review_mode_retired` until that replacement.
  Historical privileged-operation requests remain readable; planning and
  executing a policy import reject retired required-review candidates.
  A pending legacy import must be cancelled and replaced with an advisory
  candidate. Retrying its original required-mode preparation or policy import
  is rejected; historical records remain available through policy and operation
  list/detail reads.
- `provider_delivery_protection_expectation`: Optional exact provider-protection
  expectation for ordinary guarded delivery. Absence preserves legacy policy
  bytes and digests and supplies no ordinary readiness capability. Only the
  governed `managed-merge-train-policy-import` lifecycle may change its effective
  active projection, including removal. It is separate from authorization
  policy schema 3; see [provider delivery inspection](provider-delivery-inspection.md).
- `failure_policy`: Whether Launchplane pauses the whole train or continues
  after marking the blocked pull request.
- `enqueue`: Requirements for who may enqueue. Human authority remains role-based
  through `allowed_actor_roles`. Trusted automation is an independent,
  repository-scoped allowlist of immutable GitHub numeric user ids in
  `trusted_automation_github_user_ids`.
- `merge_identity`: Token or workload identity allowed to update branches and
  merge pull requests.
- `service_authz`: Launchplane authz action/product/context required before the
  service endpoint may run the policy.
- `github_token`: Launchplane explicit GitHub credential source used for live GitHub
  API calls.
- `scheduler`: Optional DB-backed scheduler intent. When enabled, Launchplane's
  merge-train worker runs this target on its own timer (and the GitHub Actions
  schedule reads it from the deployed Launchplane API until that schedule is
  removed); checked-in workflows and GitHub variables are not target authority.

The default enqueue policy is intentionally narrow: the enqueue label must be
present and the PR author must be a repo owner or repo admin. A repository may
explicitly opt a known automation account in by storing its immutable GitHub
numeric user id in `trusted_automation_github_user_ids`. Matching identities are
reported as `trusted_automation` in controller dry-run output. The default list
is empty, so existing owner/admin-only policies remain fail-closed and unchanged.
Logins are diagnostic labels, not policy identity, because logins can be renamed.

PRs labelled for Client review require the newest `launchplane/owner-review`
commit status on their current head. Missing status is pending, even if check
runs already passed; pending or failed review cannot admit the PR. Only active
product profiles' configured review labels mark this boundary; an unrelated
label on a repository without such a profile creates no review requirement.
Every batch member is checked during planning and again before the provider
landing effect. The controller, standalone candidate planning and landing, and
run-once (including scheduled Level 1 runs) all read the product profiles and
refuse with `client_review_profiles_unavailable` when they cannot. Standalone
planning applies the same every-member check as the controller and reports a
waiting or blocked queue without building a candidate. Run-once re-reads the
review on the head it is about to merge and treats a changed decision as a
stale head.
Successful review still requires all other current-head checks.

Only an actor allowed to enqueue may put a pull request in the train: a trusted
automation identity, or an actor whose role is in `allowed_actor_roles` (by
default the repository owner and its admins). Pull-request write access
includes labels, so a Client's GitHub App or any other collaborator can apply
the enqueue label; the train reads who applied it from the pull request's latest
`labeled` event, not from its author. A GitHub App that applies the label with
a user's access token is always refused, including a repository owner's token,
before either the trusted-automation or allowed-role check. An App's own
installation token acts as its bot account and needs a trusted automation id. A
label applied by anyone else, or whose labeler cannot be read, is ignored for
admission. The dry run reports the specific refusal reason and identifies the
labeler when available. Removing the label, by anyone, still takes the pull
request out of the queue.

`dependency_update_github_user_ids` names dependency-update bots, for example
Dependabot. Each id must also be in `trusted_automation_github_user_ids`. A pull
request from one of them enqueues without the enqueue label only when every
change commit on it is authored by that bot and signed by GitHub (committer
`web-flow`, verified), nobody else force-pushed the branch, the commits match the
pull request head the train read, and every dependency the update names stays
within one major version. Transitive lockfile changes are not classified; the
pull request's required checks still gate them. The adapter derives that from Dependabot's
`updated-dependencies` commit trailer and the "from X to Y" lines, because the
trailer often omits `update-type`. A major version, a `0.x` minor bump, a
changed version suffix, a non-version reference such as a commit SHA, an
unparseable message, or a commit by anyone else leaves the pull request
ineligible with `dependency update needs agent review`. The label still
enqueues any trusted pull request as before. An empty list, the default, changes
nothing and is omitted from the policy digest.

The train's own base-refresh merge commits do not count as dependency changes
when a persisted branch-refresh record binds the exact commit, repository, PR,
and base branch. The live commit must be GitHub-signed, its two parents must
match the recorded previous head and merged base, the previous head must occur
in the same PR commit list, and that merged base must still be an ancestor of
the PR's observed base. The change against that merged base must have identical
file names, statuses, and text patches before and after the refresh. Unrelated
base edits can change a file's resulting blob while keeping that patch when
hunk positions and context are unchanged. Shifted hunk positions or context
require agent review; patchless pure renames still require an identical blob.
Unreadable, binary, or truncated comparisons require review. This proof is used
in controller snapshots and landing admission. Older standalone phase and CLI snapshot readers do not consult these
records and remain conservative; use the controller dry-run to assess a refreshed
dependency update. Missing records or failed reads withhold automatic admission;
unrecorded merges and other authors still need agent review. Checks are read
again on the refreshed head before landing.

## Failure Semantics

Policies using `pause_train` stop processing later queued pull requests after a
selected pull request cannot update, pass checks, or merge. Launchplane marks
the blocking pull request with `blocked_label` before stopping.

`continue_after_blocking_pr` is reserved for repositories that explicitly choose
higher throughput over strict ordering. A worker must still mark the failed pull
request with `blocked_label` before considering later entries.

For the reconciled pilot, ordinary missing acceptance or check evidence holds the
affected change rather than pausing unrelated eligible work. A broader pause is
valid only for a proven dependency edge, a shared-state/integration fence, or an
unknown effect that makes later mutation unsafe. Existing active policies retain
their current behavior until a reviewed DB-backed policy replacement is
activated; this target paragraph does not change live scheduling.

## Batch Train Target

The batch train is the first full-train implementation target because it proves
that many queued pull requests are compatible together while preserving normal
GitHub pull request UX. It differs from merging every individually green PR: the
batch candidate must pass required checks as a combined tree before Launchplane
starts landing the original pull requests.

### Candidate Construction

A batch candidate represents:

```text
base branch + queued PR #1 + queued PR #2 + ... + queued PR #N
```

The candidate is built in deterministic queue order on a temporary
`launchplane/construct/<digest>` branch. The digest is the SHA-256 of the
canonical candidate ref, so retries use the same construction branch. Only
after every entry's rolling commit and tree are verified does the native
GitHub adapter publish `launchplane/train/**` at the completed candidate SHA.
Base and intermediate construction pushes therefore do not start required
workflows. The canonical candidate ref, persisted candidate identity, and
rolling provenance remain the inputs to checks and landing.

Publication has a bounded exact-SHA readback, including temporary 404s while a
new branch becomes visible. A failed or interrupted publication never returns
`ready_for_checks`. Retrying a build resets the construction branch and
reconstructs the candidate; a crash after publication but before persistence
can still require a new publication and checks.

After verified publication, the adapter deletes the construction ref through
the semantic effect executor. An already missing ref is clean. Other cleanup
failures are logged with the ref and HTTP status without discarding the verified
candidate or restarting its CI; the retained ref has no landing authority.
Failed or interrupted builds retain their construction ref as recovery evidence.
The native controller checkpoint records its exact `construction_ref`, and a
failed build returns that locator with the provider status. A ref locator is
not proof that the ref still exists.
Ref naming is an implementation detail, not mutable repository policy.

After GitHub creates a candidate merge commit, Launchplane performs a bounded
read-after-write convergence check before declaring the candidate ref stale.
This tolerates transient GitHub ref-read lag without weakening the exact
expected-commit comparison; exhaustion still fails closed as stale state.

### Candidate Validation

Required checks must pass on the candidate commit that includes the queued PRs
being considered for the batch. Checks on each PR's own head are useful
screening evidence, but they do not prove the combined tree is safe to land.
Launchplane must fail closed when candidate check evidence is missing, pending,
failed, stale, or attached to a different commit SHA.

Repositories using batch candidates must run their required workflows for
pushes to `launchplane/train/**` and exclude `launchplane/construct/**` from
those triggers. A workflow matching every branch would still run intermediate
checks and could supply check evidence before the final train push registers.
Aggregate required-check jobs must also run on those push events and treat the
candidate as same-repository work when no pull
request payload exists. Otherwise the candidate has no exact-SHA check evidence
and remains fail-closed in `ready_for_checks`.

Candidate observation reads the target branch's live required-status-check
policy and requires every named check, including any pinned GitHub App id, to be
present and successful on the candidate SHA. Partial workflow evidence cannot
promote a candidate. It reads the enforced required-check projection from
GitHub's [Get a branch endpoint](https://docs.github.com/en/rest/branches/branches#get-a-branch),
which requires `contents: read` for the repository. An unprotected branch,
missing protection metadata, disabled enforcement, or an empty required-check
list fails closed; observation does not request administration access. A pinned
app id must be proven by check-run evidence; commit statuses can satisfy only unpinned or
GitHub `app_id = -1` any-app requirements. Once unrelated evidence is terminal,
missing pinned or named evidence is surfaced explicitly instead of remaining an
unexplained pending candidate.

Native landing admission reads that same required-check projection. The train
always requires current-base freshness, independently of GitHub's optional
strict setting: its technical evidence records `strict: true` as the train's
requirement and retains the observed commit-containment result. Admission can
also accept a proved recorded-rolling tree, as described in
[structural provenance](merge-train-structural-provenance.md). Missing or stale
structural proof still blocks. This does not claim to observe GitHub's strict
setting and does not grant, change, or bypass provider protection; GitHub's
guarded merge endpoint continues to enforce its own policy.

Candidate-ref workflow concurrency keeps create/force-reset pushes separate
from ordinary ref updates. Native construction now publishes only the completed
candidate; existing concurrency rules remain for publication retries and
previously created refs. Create/reset runs retain their SHA-keyed group so a
cancelled duplicate cannot replace the protected base commit's successful
required-check evidence. Candidate-specific cancellation does not broaden a
workflow's cancellation policy for ordinary base-branch pushes.

### PR-Native Landing

This section documents the current GitHub adapter. Other source-control
providers must implement the same Launchplane-owned landing contract behind a
provider adapter.

After a multi-entry candidate passes, repositories configured for `merge` use a
Launchplane-created batch pull request whose head is that exact candidate. Its
body identifies every constituent PR and reviewed head, and its
`Owner test notes` section (the older heading every pinned CI action accepts)
carries each constituent's own notes under a
`### #<number> <title>` subheading, read when the batch PR is created. Headings
inside those notes are demoted so they stay within the section, and a
constituent without notes is named as having none. Later edits to a
constituent's notes are not copied into an existing batch PR. The original PRs and
source branches remain intact. GitHub enforces normal protected-PR checks,
reviews, CodeQL, and base freshness on the batch PR; Launchplane never pushes
the protected base ref. The batch PR has no enqueue label and is not another
entry in its own queue.

The controller creates or finds that PR once candidate construction is complete,
before observing required checks, so PR-triggered checks participate in the
normal candidate wait. Retries find the same exact ref/head binding and never
recreate a closed batch PR as a hidden fallback.

Closed, failed, or superseded candidates are terminal for that exact queue.
Before abandoning one, the service closes unmerged PRs on its exact generated
candidate ref, including PRs whose head, body, draft flag, or base was edited;
source PRs and all branches remain intact. Historical closed PRs from another
candidate SHA do not prevent a rebuilt candidate from getting its own PR. Policy
changes also retire the prior batch PR, including a change to squash or rebase.
A changed member or base reflows to a
new candidate without retaining a reconciliation fence. Landing rechecks member
identity before waiting on checks, so a pending or failed check cannot hide a
new source head. A manually closed batch PR is not automatically reopened.
Change or remove the queued source entries to build a replacement; an unchanged
failed candidate remains visibly failed rather than being rebuilt in a loop.
A candidate that failed on check evidence may be re-read at its recorded SHA,
so re-running the failed check lets it continue once the re-run is no longer
failing. A multi-entry batch whose batch PR was closed is never reopened. It
has one narrow rebuild exception: a changed generated batch body, confirmed
closed and unmerged binding, and a persisted unused retry budget, as described
under `candidate_failed` below.

The landing plan binds `candidate_pull_request_number` into its immutable
digest. The controller evaluates every constituent before appending the first
admission, so an unready later member does not grow rejected-prefix records on
every pass. Every constituent receives fresh admission against the same unchanged
base before one SHA-guarded provider merge of the batch PR. The controller's
provider checkpoint records that shared PR and all constituent admission IDs.
Afterward, it verifies the merge parents and tested tree, protected-base
containment, and each original PR's exact head, target, and merged state.
GitHub's branch readback and indirect PR completion may lag; bounded read-only
retries absorb brief delays within the landing pass. A successful merge response
alone does not finish the batch. A resumed pass observes the same batch PR and reconciles
the original admissions without issuing another merge.

Single-entry candidates and existing landing plans continue to land original
pull requests through the configured `merge_method`. Multi-entry squash and
rebase policies retain that path; the service does not reinterpret their merge
method to obtain ancestry-based batch completion. Before creating a landing
plan, the controller revalidates that
the passed candidate still matches the live eligible queue, PR head SHAs, and
base SHA. Drift supersedes the active passed-candidate record and resumes normal
planning from a fresh snapshot instead of creating a stale landing plan.
Transient mergeability or check-readiness changes do not invalidate an
identity-stable candidate. A collapsed-stack candidate is compared against its
admitted root entry, and proven drift resumes the existing stack-collapse state
rather than creating a duplicate collapse plan.
Before each PR merge, Launchplane must also verify that the PR still matches the
candidate evidence it is about to rely on. At minimum, the PR head SHA, exact
target base ref and current base SHA, queue position, policy digest, and
candidate batch identity must match the recorded landing plan. If GitHub state
changes, Launchplane must stop, re-read, and rebuild or requeue rather than
continuing from stale batch evidence.

Stack-collapse records participate in landing only when their repository, base
branch, policy digest, root PR, and expected root head match the landing plan.
Older waiting stack-collapse records stay visible as status evidence, but they
must not block or annotate an unrelated unstacked batch landing.

The protected batch PR is the auditable provider effect for a multi-entry
merge-method batch. Each constituent outcome links through its admission and
landing-plan digest to that shared PR. Legacy partial plans are never converted
into this mode. A member pushed after final validation is not reported as its
new head having landed; mismatched original-PR completion requires reconciliation.
If the provider has already merged the batch but the exact source-PR completion
cannot be established, the controller retains the fence and never retires that
effect as unused. Current recovery requires matching provider evidence; there is
no automatic service disposition for permanently contradictory source heads.
Admin diagnostics cover every unresolved member of the shared effect.
An out-of-controller merge without preceding admissions, or after conclusive
rejection of the recorded attempt, likewise remains fenced even when Git proves
the code landed. It does not retroactively acquire a Launchplane admission. The
generated PR explicitly instructs admins to let the controller merge it and
to leave its generated branch unchanged.

### Stacked Pull Requests

The batch train remains flat: queued entries must target the repository policy's
`base_branch`. Launchplane may read broader pull request topology so it can see
stacked PRs, but PRs whose base is another feature branch are not admitted as
independent train entries.

Stacked PR support is a pre-train normalization workflow. For same-repository
linear stacks, Launchplane should detect the stack rooted at a PR targeting the
protected base branch, collapse child changes into that root with explicit
stored evidence and fresh SHA guards, wait for the root PR to pass required
checks against the base branch, then admit only the root PR to the flat batch
train. The root PR's `enqueue_label` starts the train for the stack, but it
does not speak for the children: collapsing merges a child into the root, so
every child must itself be ready to land under the same queue eligibility as a
root (open, not a draft, carrying `enqueue_label` applied by an allowed actor
or an allowed dependency update, from an allowed author). A child that is not
ready refuses the whole collapse; the controller reports `stack_unsupported` with a `blocking_reason`
naming each child and reason. Execution reads each child again from GitHub
just before merging it, so a child held while a recorded plan runs is not
merged; a child GitHub already shows as merged by this collapse is recovered,
not re-read. Launchplane records a stack collapse plan
before mutating branches so the root PR, child order, expected SHAs, mutation
sequence, policy digest, and idempotency evidence remain auditable. Ambiguous,
forked, cyclic, or unsupported branch-protection cases must fail closed with
admin-visible reasons.

Stack collapse mutates from the leaf PR back toward the root PR. Each child is
merged into its parent feature branch, and the parent merge commit becomes the
child head evidence for the next mutation up the stack. Launchplane admits the
root PR only after the live root head matches the stored final root mutation
SHA and the root passes required checks from the protected base branch.

### Blocker Isolation

When the combined candidate fails checks, Launchplane must not assume all queued
entries are bad. The first implementation may split the batch or fall back to
smaller batches/one-at-a-time validation to identify the blocking PR. Once a
blocker is identified, Launchplane applies `blocked_label` and either pauses or
reflows later entries according to `failure_policy`.

A failed candidate remains a stop state while the current eligible queue and
base SHA still match that candidate. If the eligible queue changes, for example
because a new queued PR repairs train validation, the controller may supersede
the failed candidate batch lineage and plan a replacement candidate from the
fresh snapshot.

Candidate construction can fail before required checks run when GitHub rejects
one rolling merge entry as stale or conflicting. Its partial state is retained
on the construction ref; no new canonical train ref is published. The controller
persists that candidate as `failed`, reports the exact pull request reached by the build, and
releases the controller lease without replaying the rejected merge. The same
queue-change rule then governs replacement planning; an unchanged queue remains
stopped for Director attention.

A merge conflict between queued pull requests is caught before the build.
GitHub computes a pull request's mergeability only against its base, so two
queued pull requests can each be clean and still conflict with each other.
Before a mutating pass plans a candidate with more than one entry, the
controller runs a conflict probe: it resets a dedicated ref in the
`launchplane/construct/` namespace to the base SHA and merges each queued head
in queue order. A head that does not merge cleanly onto the heads accepted
before it writes no commit; the probe records it and continues. The probe ref
is unique to the controller's lease acquisition. The probe renews the lease
before each merge, and the controller renews it again before persisting the
planned or replacement candidate. A pass that lost its lease stops before its next merge,
deletes only its own ref, and cannot touch the probe of the pass that adopted
the train. The probe ref is deleted afterwards. It never writes the canonical
train ref or a pull request branch. A probe costs
one ref write, one merge per queued pull request, and one delete.

The candidate is planned from the heads that merged cleanly. Each conflicting
pull request and head is recorded as `held_out` with reason `entry_conflict` and
`conflicts_with`, the pull requests accepted ahead of it (empty when it does not
merge onto the base). The controller result reports the probe as
`conflict_probe`, and PR feedback tells each held-out pull request which pull
requests it conflicts with. Later candidates carry the hold-out forward while
its head is unchanged, so the rest of the queue lands. A new head brings it
back into the queue; until then, its author resolves the conflict, typically
after the others land. When the probe reduces a changed queue back to a failed
batch's membership, the batch stays stopped and keeps its retry budget; the
failed candidate records the new hold-out, and feedback says the batch ahead is
stopped, so later passes do not probe the same conflict again. A dry run writes
no ref and reports
`conflict_probe.status: will_run` with the pull requests a mutating pass would
probe.

The build keeps the same rule as a backstop. When an entry does not merge
cleanly into the candidate built before it, the controller reports
`merge_train_candidate_entry_conflict`, records that pull request and head as
`held_out` on the failed candidate, and the replacement candidate leaves it out.

An exhausted final-publication readback also fails closed, with no individual
failed pull request: its checkpoint identifies the publication phase and the
retained construction ref.

GitHub refuses to merge into a base that requires conversation resolution
while any review thread is unresolved. The snapshot reader reads that rule from
classic protection through the base branch's `refUpdateRule`, which GitHub shows
without the Administration permission the train token does not hold, and from
the branch's active rulesets, which need only Metadata read. When the rule is on, each open,
non-draft pull request's review threads are read, and one with an unresolved
thread is ineligible with a reason that counts them; file paths stay out of
public reasons. A thread opened by code scanning tells the author to fix the
code rather than resolve the thread.
The other entries plan and land without it, and resolving the thread brings it
back. An unreadable rule is not taken as absent: unresolved threads still make
the entry ineligible, and the reason says the rule could not be read. A planned
entry that gains a thread before landing blocks admission with
`pull_request_conversations_unresolved`. The batch PR is checked the same way
after its checks pass and before any admission; an unresolved thread there,
such as a code-scanning finding on the combined change, blocks with
`batch_pull_request_conversations_unresolved` instead of a refused merge.

## Example Policy Entries

The example below is documentation/import material only. It is not packaged as a
runtime config file and the service does not read it implicitly.

```toml
schema_version = 1

[[policies]]
repository = "cbusillo/sellyouroutboard"
base_branch = "main"
enqueue_label = "ready-to-merge"
blocked_label = "merge-blocked"
stack_child_disposition_label = "stack-landed"
merge_method = "merge"
failure_policy = "pause_train"

[policies.enqueue]
label_required = true
allowed_actor_roles = ["repo_owner", "repo_admin"]
trusted_automation_github_user_ids = []

[policies.merge_identity]
kind = "github_actions_oidc"
name = "launchplane-merge-train"

[policies.service_authz]
action = "merge_train.run_once"
product = "launchplane"
context = "launchplane"

[policies.github_token]
env_var = "GH_TOKEN"

[policies.scheduler]
enabled = true
runner_mode = "controller"
mutate = false

[[policies]]
repository = "cbusillo/codex-skills"
base_branch = "main"
enqueue_label = "ready-to-merge"
blocked_label = "merge-blocked"
stack_child_disposition_label = "stack-landed"
merge_method = "merge"
failure_policy = "pause_train"

[policies.enqueue]
label_required = true
allowed_actor_roles = ["repo_owner", "repo_admin"]
trusted_automation_github_user_ids = [123456789]

[policies.merge_identity]
kind = "github_actions_oidc"
name = "launchplane-merge-train"

[policies.service_authz]
action = "merge_train.run_once"
product = "launchplane"
context = "launchplane"

[policies.github_token]
env_var = "GH_TOKEN"
```

## Admin Changes

To add or change a repository policy without editing generic service logic,
import a new active `launchplane_merge_train_policies` record. The service
resolves repository/base branch requests from the active typed policy record
before authorization, token lookup, or GitHub calls, so unsupported pairs fail
closed.

The `Merge Train Policy Import` workflow accepts an optional comma-separated
`trusted_automation_github_user_ids` input. It parses positive integers,
deduplicates them, and writes only the resulting numeric identities into the
DB-backed policy payload. Do not put bot logins or user ids into workflow
defaults or checked-in runtime authority. Empty allowlists are omitted from the
serialized policy record so owner/admin-only records remain readable by older
strict service binaries. Deploy service support for this field to every replica
before importing a non-empty allowlist; older strict policy models reject the
unknown field. A rollback must first restore an empty, old-compatible allowlist
while the newer service is still running.

Prepare a TOML payload with every repository/base policy the service should
support, store it outside the repo or generate it from admin automation, then
import it through the deployed service API. GitHub Actions admin workflows
should build the JSON request with Launchplane's typed CLI and call the shared
`launchplane-request` action rather than fetching GitHub OIDC tokens in shell:

```sh
uv run launchplane merge-train-policies build-import-request \
  --policy-file path/to/merge-train-policy.toml \
  --source-label operator:update \
  --reason "Configure merge train repositories" \
  --apply \
  > merge-train-policy-import-request.json
```

```yaml
- uses: cbusillo/launchplane/.github/actions/launchplane-request@<launchplane-request-sha> # launchplane-request
  with:
    launchplane-url: ${{ vars.LAUNCHPLANE_PUBLIC_URL }}
    route-path: /v1/merge-train/policies/import
    payload-file: merge-train-policy-import-request.json
    idempotency-key: merge-train-policy-import:${{ github.run_id }}
```

Reviewed merge-train policy changes use the typed `managed-merge-train-policy-import` privileged-operation path
instead of adding workflow, `local-operator`, or local-admin
`merge_train.policy_import` authority. The privileged-operation proposal accepts
one complete candidate record and produces redacted active/candidate digests,
target counts, and stable policy-key changes. A signed-in immutable-ID GitHub
human with the exact `merge_train_policy_operation.*` managed rule approves the
plan; the service worker performs active-policy CAS, operation-scoped
idempotency, and exact active/superseded read-back. Existing
`authz_policy_operation.*` grants do not authorize merge-train policy imports.

Policy record timestamps must be timezone-aware ISO-8601 values. The schema
migration that installs the one-active-record fence inspects only active legacy
records, keeps the uniquely latest active record, and supersedes older active
records. If the latest active timestamp is invalid or tied, the migration stops
explicitly instead of guessing. Resolve exactly one active row through the
approved database-repair procedure, updating both the promoted `status` column
and `payload.status`, then rerun the migration; do not use an ordinary import as
a migration-repair shortcut.

For local admin terminals, the compatibility CLI import path still reads the
bearer token from `LAUNCHPLANE_SERVICE_TOKEN` unless a browser
`--session-cookie` is supplied:

```sh
uv run launchplane merge-train-policies import-policy \
  --service-url "$LAUNCHPLANE_SERVICE_URL" \
  --policy-file path/to/merge-train-policy.toml \
  --source-label operator:update \
  --reason "Configure merge train repositories" \
  --apply
```

Direct `--database-url --apply` import is reserved for local development and
DB repair, requires `--allow-direct-db-mutation`, and is not for shared or
production live mutation.

For local development or DB repair only:

```sh
uv run launchplane merge-train-policies import-policy \
  --database-url "$LAUNCHPLANE_DATABASE_URL" \
  --policy-file path/to/merge-train-policy.toml \
  --source-label operator:update \
  --allow-direct-db-mutation \
  --apply
```

List active policy records with:

```sh
uv run launchplane merge-train-policies list \
  --database-url "$LAUNCHPLANE_DATABASE_URL" \
  --status active
```

## Discoverability

The contract is available to dry-run tooling with:

```sh
uv run launchplane work-graph merge-train-policy \
  --policy-file path/to/merge-train-policy.toml \
  --repository cbusillo/codex-skills \
  --base-branch main
```

Admins can validate an external TOML before importing it as a policy record:

```sh
uv run launchplane work-graph merge-train-policy \
  --policy-file path/to/merge-train-policy.toml
```

Workers should load the same typed contract before enqueuing or merging. A
missing policy for a repository/base branch is a hard failure, not a fallback to
implicit behavior.

The ordered queue dry-run accepts a JSON snapshot of candidate pull requests and
reports queue order plus the next intended action without mutating GitHub:

```sh
uv run launchplane work-graph merge-train-dry-run \
  --snapshot-file path/to/merge-train-snapshot.json \
  --policy-file path/to/merge-train-policy.toml
```

The run-once command reads a live GitHub snapshot for the selected
repository/base branch and reports the same worker-step intent without mutating
by default:

```sh
GH_TOKEN=... uv run launchplane work-graph merge-train-run-once \
  --policy-file path/to/merge-train-policy.toml
```

The deployed Launchplane service projects the work-graph GitHub credential into
`GH_TOKEN` from the `LAUNCHPLANE_WORK_GRAPH_GH_TOKEN` deployment secret, so the
imported policies normally reference that same explicit GitHub credential source.

Passing `--mutate` applies exactly one ordered-queue worker transition from that
fresh snapshot. Use it only from the intended admin environment for the smoke
or configured target; the command is a narrow bootstrap surface, not the full
batch train scheduler.

The dry-run orders eligible pull requests by `created_at` and then PR number. It
excludes draft, closed, unlabeled, or unauthorized entries and fails closed when
the snapshot repository/base branch has no explicit policy.

When the selected pull request is blocked by failed checks or conflicts, the
first live mutation is idempotent application of `blocked_label`. Repositories
using `pause_train` stop after that label action; repositories using
`continue_after_blocking_pr` may continue to the next eligible pull request once
the blocked pull request has been labeled.

For service-controller batches using merge commits, two or more eligible
entries land through a batch PR built and checked on the current base. Planning
and failed-candidate reflow therefore preserve their source heads even when
GitHub reports them behind the base. Required checks and conflict guards still
apply. The conflict probe re-evaluates this decision after holding out entries:
a remaining batch preserves its heads; a lone remaining PR still refreshes.
Direct PR landings, including single-entry candidates and squash/rebase methods,
retain the branch-refresh requirement.

When the selected pull request needs a branch refresh, Launchplane updates that
pull request using the observed head SHA as the compare point. The worker must
then re-read mergeability and required checks before any later merge decision;
pre-update check results are stale after a branch refresh. The controller
records each refresh it requested (`launchplane_merge_train_branch_refreshes`),
so a Client's acceptance of the refreshed pull request can carry to the new head
when the change itself is unchanged (see
[carried acceptance](owner-acceptance.md#carried-acceptance)).

The reread step rebuilds the dry-run decision from a fresh pull request snapshot.
If checks are still pending or mergeability is unknown, the next action remains
`wait_for_checks`; Launchplane must not merge from pre-refresh evidence.

The wait step records the selected pull request, its observed head SHA,
mergeability state, and required-check status as a polling boundary. It does not
merge or mutate GitHub. A later worker pass must read a fresh snapshot for the
same repository/base branch and continue only when that fresh dry-run result
selects `merge`.

A Level 1 ordered-queue worker pass applies at most one transition from one
fresh snapshot. It may add the block label, request a branch refresh, record a
wait boundary, perform one guarded merge, or report an idle queue; it must not
chain follow-up reads or mutations in the same pass. The controller's batch
train uses separate batch candidate and landing-plan records instead of treating
a single selected PR as the whole train state.

The service endpoint `POST /v1/work-graph/merge-train/run-once` uses the same
policy. Request payloads name `repository`, `base_branch`, and optional
`mutate`; the service finds the repository/base policy before any GitHub call,
authorizes the caller through `service_authz`, resolves the GitHub token from the policy's explicit credential source, reads a fresh snapshot, and either returns the dry-run
result or applies exactly one worker step. Accepted calls write a
`launchplane_merge_train_runs` record with the policy digest, fresh snapshot,
dry-run decision, selected pull request metadata, and optional worker mutation
result. Unsupported repository/base pairs, missing token configuration, and
denied authorization all fail closed. Generic service code must not contain
product repository conditionals.

The batch-candidate service endpoint
`POST /v1/work-graph/merge-train/batch-candidate/run-once` also uses the same
policy-backed, DB-backed boundary. It accepts `mode: plan`, `mode: build`, or
`mode: observe`; writes `launchplane_merge_train_batch_candidates` records; and
does not land original PRs. Plan mode reads a fresh snapshot and records the
eligible queued PRs as one candidate. The candidate base SHA comes from the live
base branch head, not from any individual pull request's base metadata, so stale
or ineligible open PRs cannot move the candidate off the target branch. Build
mode creates or resets the Launchplane train ref and merges queued PR heads into
that ref in order. Observe mode records required-check state for the exact
candidate SHA. Landing the original PRs remains a later PR-native phase with
separate records.

The controller service endpoint
`POST /v1/work-graph/merge-train/controller/run-once` is the preferred admin
entrypoint for the full train. It accepts the same repository/base selector and
`mutate` flag as the Level 1 route, but chooses the next safe batch phase from
the latest DB-backed records. Repeated calls can drive an unstacked train through
candidate plan, build, observe, landing plan, and landing. For a same-repo
linear stack, repeated calls first plan and execute stack collapse, then admit
only the collapsed root PR into the same candidate/build/observe/landing path.
The controller does not introduce new live configuration or repo-specific
conditionals; it fails closed on missing policy, missing token, stale policy
digests, stale root heads, and the existing batch landing guards.

Controller requests use this shape:

```json
{
  "schema_version": 1,
  "repository": "example/repo",
  "base_branch": "main",
  "mutate": false
}
```

`github_api_base_url` may be supplied only for a controlled GitHub-compatible
test endpoint. Normal callers omit it. The service resolves the DB-backed
repository/base policy, authorizes the policy's declared `service_authz` action,
loads the GitHub token named by that policy, and then returns a public-safe
envelope:

```json
{
  "status": "accepted",
  "trace_id": "launchplane_req_example",
  "records": {
    "merge_train_batch_candidate_record_id": "merge-train-batch-candidate-example"
  },
  "result": {
    "repository": "example/repo",
    "base_branch": "main",
    "mode": "dry-run",
    "controller_action": "build_candidate",
    "merge_train_batch_candidate_record_id": "merge-train-batch-candidate-example"
  }
}
```

The response `records` object contains durable record ids for records written or
selected by this call. The response `result` object is the controller decision.
It always includes `repository`, `base_branch`, `mode`, and
`controller_action`; it may also include redacted `dry_run_result`, `candidate`,
`landing_plan`, `stack_discovery`, `stack_collapse_plan`, and matching record id
fields. Callers may report controller action, record ids, PR numbers, check
states, binding-safe labels from policy, trace id, and compact details. They
must not report GitHub tokens, raw request headers, private API base URLs, local
paths, or unchecked provider responses.

Controller actions have these retry/stop semantics:

- `plan_stack_collapse`: A same-repo linear stack was found and a collapse plan
  is next. Dry-run may report. Mutate once, then call again.
- `execute_stack_collapse`: A stored planned or partially `collapsing` plan
  should be applied or resumed. Mutate once, then call again. Stop if the
  resulting plan is `blocked` or `stale`.
- `wait_for_root_checks`: The collapsed root PR's required checks are still
  running. Stop and poll later; do not call phase endpoints. Any other state
  of the collapsed root is answered from the whole queue, the same as for any
  queued pull request: a root behind its base refreshes for a direct landing
  or keeps its head when joining a multi-PR merge batch, a root
  with failed checks or conflicts reports `block`, and a root that left the
  queue lets the other ready pull requests proceed. A refreshed root still
  disposes of its stack's children when it lands. A mutating pass retires all
  progress records of an inapplicable wait when the root head changes or the
  root leaves the open snapshot, recording the reason in the record source.
  Dry runs leave records unchanged. A waiting root remains selectable from its
  latest waiting progress even after an independent
  stack completes; a completed stack cannot hide another stack's saved wait.
  A root that returns at its collapsed head
  can resume its retired wait when its visible children still have the stored
  heads. A root waiting on checks or ready for admission then skips another
  collapse; other states continue through ordinary queue handling. Retirement preserves the collapse history:
  landing reconciles every collapsed root whose landed head equals or descends
  from its collapsed head, including retired waits. If a root was collapsed
  again, its newest applicable plan supplies the current child-head expectations.
  Each stack and child has
  its own persisted checkpoint, so an interruption resumes unfinished children
  without repeating completed stacks. The response's singular fields report
  the newest plan; all reconciled plans remain in the stored collapse history.
- `admit_collapsed_root`: The collapsed root PR is ready to enter the batch
  candidate path. Mutate once, then call again.
- `stack_unsupported`: A stack exists but is not a supported same-repo linear
  stack. Stop and surface the redacted stack discovery details.
- `plan_candidate`: The queue can produce the next batch candidate. Mutate once,
  then call again.
- `build_candidate`: A stored candidate still matches the fresh queue and base
  and needs its train ref built or refreshed. Planned or interrupted-building
  candidates are rechecked before any candidate-ref write. If their head, queue,
  or base has changed, dry-run reports the replacement action; mutate supersedes
  the stale records under the controller lease and replans. An empty queue
  returns `idle`, without building the obsolete ref.
  Mutate once, then call again.
- `observe_candidate`: A built candidate needs check observation. First compare
  its queue, PR heads, and base with a fresh snapshot. Drift supersedes the old
  records under the controller lease and replans, even when the old checks never
  started. An unchanged candidate keeps waiting for its real checks; transient
  readiness changes alone do not replace it. Mutate or dry-run later until
  checks pass, fail, or remain pending.
- `candidate_failed`: Candidate checks failed and still matches the current
  eligible queue. Stop and surface the candidate record id and failed check
  evidence. If the eligible queue/base changes, the next controller action may
  become `plan_candidate` for a superseding candidate. A failed candidate with
  no recorded candidate SHA never reached checks, so the controller may also
  supersede and replan it against the unchanged queue after a transient build
  failure is repaired. Failed candidates with a recorded candidate SHA are not
  rebuilt until the queue or base changes. If one failed on check evidence and a
  re-run of the failed check at that SHA is now pending or passing, the controller
  returns `observe_candidate`, retires the failed record, and continues from the
  re-read evidence. A multi-entry merge batch whose service PR is closed and
  confirmed unmerged may be rebuilt once for the same ordered heads and base,
  only when the body Launchplane would generate now differs from that bound
  failed PR's body. The replacement uses a separate candidate ref and persists
  `batch_body_retry_of`; candidate history preserves the used budget across
  controller restarts. No second rebuild is admitted for that queue/base, even
  if generation changes again. Missing, unbound, open, or merged batch evidence
  cannot authorize recovery. Ambiguous provider-effect evidence still requires
  reconciliation rather than being treated as an ordinary failed check.
  The replacement still passes current checks,
  constituent validation, and every admission gate. Otherwise the controller
  reports the failed candidate and its recovery reason.
  When the current queue head waits for checks, both read-only and mutating
  passes report `wait_for_checks` with that PR and its dry-run reason, retaining
  the failure and retry budget until the wait resolves.
- `plan_landing`: A passed candidate still matches the live eligible queue,
  recorded PR head SHAs, and base SHA and is ready for PR-native landing-plan
  creation. Mutate once, then call again.
- `land_batch`: A landing plan with planned or in-progress merge entries is
  ready to merge or resume the original PRs in order. Mutate once only after
  Director intent; call again to verify terminal state.
- `batch_landed`: The batch already landed. Stop; the train phase is complete
  for that batch.
- `block`: The selected PR is blocked by conflicts or failed checks. Stop and
  surface `dry_run_result.next_action_detail`.
- `update_branch`: The selected PR is behind its base and will land directly.
  A mutate call updates
  the PR branch through GitHub with the expected head SHA and reports
  `branch_update_result`; call again once the new head's checks pass. A dry-run
  call changes nothing.
- `wait_for_checks`: Required PR checks are pending. Stop and poll later.
- `idle`: No eligible queued work exists. Stop.

All controller calls are one-action calls. A caller that wants to drive the
train should repeat `run-once` only after reading the returned action and should
stop on terminal or attention states: `batch_landed`, `candidate_failed`,
`stack_unsupported`, `block`, `wait_for_checks`,
`wait_for_root_checks`, and `idle`. A failed HTTP response with `status:
"rejected"` is also terminal for that attempt. Public-safe helper summaries
should include `error.code`, `trace_id`, and the retry/stop recommendation, not
the original request body.

Schedulers and admins report train progress through
`POST /v1/work-graph/merge-train/pr-feedback`. The route accepts a
repository/base selector, pull request number, feedback event, and optional
controller action, record id, and message. Launchplane renders one managed
comment per PR with a hidden marker and updates that same comment as the train
moves through queued, waiting, blocked, stale-policy, and completed states. Each
call also writes a `launchplane_merge_train_pr_feedback` record so delivery
status, rendered markdown, and GitHub comment identity remain auditable. Callers
must keep the message public-safe: no tokens, raw headers, private API base URLs,
local paths, or unchecked provider responses.

The batch-landing service endpoint
`POST /v1/work-graph/merge-train/batch-landing/run-once` owns that PR-native
landing phase. It accepts `mode: plan` with a passed candidate record id and
writes a `launchplane_merge_train_batch_landing_plans` record, or `mode: land`
with a landing-plan record id and merges the original pull requests in recorded
queue order. Landing fails closed if the base branch head has moved from the
candidate base SHA. Immediately before each merge, the PR must still be open at
the recorded head SHA and target the recorded base ref. Rolling-base allowance
comes only from the active unchanged landing plan plus structural provenance:
every prior entry must be recorded landed at its exact head, and the observed
base must equal the recorded prior landing result;
the merge request also uses GitHub's head-SHA guard. Retried landing is
idempotent across already-merged entries when GitHub shows the pull request was
merged with the exact recorded head SHA into the recorded base ref and the
target branch contains that merge commit. If the live base is already ahead of
a persisted merged entry, Launchplane revalidates that entry's PR evidence and
continues through later planned entries before deciding whether the landing plan
is stale. Unrelated base movement or mismatched head evidence still stops the
landing attempt. Stale landing evidence is reported as a merge-train stale-state
conflict, while real GitHub transport/API failures include upstream status
details for debugging. When every landing-plan entry is already merged with its
recorded head SHA and the live base branch equals or descends from the recorded
final merge commit, the retry writes terminal landing evidence instead of continuing to
advertise the stale `land_batch` action. If GitHub proves a pull request merged
with a different head SHA than the landing plan recorded, Launchplane writes
terminal stale landing evidence for the plan. That stale evidence does not count
as a successful Launchplane landing, but it does retire the stale plan so a fresh
controller pass can read current GitHub state and admit later eligible work.
After Launchplane persists a landing plan whose final merge commit remains in
the target branch history, it
enters a durable cleanup phase for the generated `launchplane/train/...`
candidate branch ref. Missing candidate refs are treated as already-clean so
landing retries remain idempotent. A cleanup failure does not roll back the
persisted landing result, but the controller remains `reconcile_required` with
the cleanup phase and candidate ref intact. The next lease holder resumes that exact
phase before planning new train work.

### Guarded Level 3 landing

Controller landing and direct batch landing use the same per-entry guarded
boundary documented in [merge-admission.md](merge-admission.md). Immediately
before each provider merge, Launchplane re-resolves current engineering-review,
technical-check, policy, candidate, queue, rolling-base, head/tree, lease, and
expected-effect evidence. Retired Client and change-impact gates are not read. It persists one
immutable admission before mutation and a separate truthful landing outcome
afterward.

An existing admission is never a retry token. Missing outcomes and latest
`reconcile_required` outcomes block another provider effect until GitHub is
observed and append-only reconciliation establishes either exact landing or
conclusive no effect. A provider rejection requires a fresh L2 evaluation and a
new admission. Candidate-ref cleanup remains downstream of landing evidence and
cannot downgrade it.

Scheduler admission is a deterministic decision over the latest stored
`launchplane_merge_train_runs` record for the repository/base branch. Dry-run
records do not throttle the scheduler. Mutation records with `reread_required`
are admitted immediately because the next pass must re-read GitHub before any
new decision. Mutation records with `poll_required` defer until the configured
poll interval elapses. Other mutation records defer until the configured backoff
interval elapses. Admission decisions are scheduling hints only; every admitted
worker pass still reads a fresh GitHub snapshot before choosing an action.

External schedulers can read the same decision from the native FastAPI route
`GET /v1/work-graph/merge-train/admission?repository=owner/name&base_branch=main`.
The route is policy-backed and store-only: it does not require a GitHub token,
does not read GitHub, and does not write run records. This read and the
controller status read below accept either the repository policy's
`service_authz` or the read-only `merge_train.policy_targets` action, so an
identity that may not run the train can still explain why it refused a pull
request.

Admin views can read the broader stored controller state from the native
FastAPI route
`GET /v1/work-graph/merge-train/controller/status?repository=owner/name&base_branch=main`.
That route returns the same admission decision plus the latest Level 1 run record,
controller lease holder, active action and phase, lease and heartbeat age, reconciliation
state, and compact summaries for active batch candidates, landing plans, and
stack collapse plans. When the latest run is dry-run evidence, the response also
includes a compact queue summary with the intended next action, selected PR,
eligible count, queued count, and visible ineligible reasons from the persisted
dry-run payload. Stored controller records only influence the advertised
controller action when their policy key and digest match the active repository
policy; stale records remain visible in the summaries with a stale reason. It is
also store-only, so it can power dashboards and status summaries without
consuming GitHub API capacity or advancing the train.

For an unresolved `land_batch` controller fence, `reconciliation_diagnostics`
adds bounded stored admission/outcome classifications for entries in the active
landing plan. The controller and plan must match the current repository/base
policy and the recorded candidate effect. The read uses the stable landing-plan
lineage, so a progress record does not hide a preceding immutable admission.
An unset active PR is supported by reading unresolved entries in that plan;
callers cannot select arbitrary PRs through this route.
Plans with more than 25 entries, or an entry with more than 25 stored admission
attempts, return `binding_unavailable` instead of a silently truncated diagnosis.

Each diagnostic contains the repository/base, active plan record and stable
plan identifiers, entry PR and expected head/tree, and one classification:
`missing_preceding_admission`, `admission_without_outcome`,
`outcome_reconcile_required`, `outcome_rejected`, `outcome_landed`,
`binding_unavailable`, or `binding_stale`. Missing or conflicting bindings do
not prove an absent admission. A closed-enum `binding_detail` distinguishes
policy drift, incomplete or changed plan references, entry/history limits,
missing readers, invalid history, and admission/outcome binding failures without
returning exception text. It is empty for a classified admission/outcome. If the
bound plan has no unresolved entry and no active PR, the diagnostic list is empty.
These are stored states, not fresh provider
observations or authority to release the fence. A missing admission does not
establish who performed a merge; a stored terminal outcome does not establish
that the provider still agrees.

The existing repository policy's `service_authz` authorizes this bounded
controller diagnostic, as it does the controller's reconciliation errors. It
does not expose Client decisions, engineering review/readiness payloads, or the
full governance projection. Reading it performs no provider calls or writes and
cannot reconcile a landing, create an admission, change policy, or release the
controller fence. Recovery requires its own supported action and current
evidence.

An explicitly selected [historical-completion preflight](merge-train-historical-completion.md)
on the existing controller route can verify the provider's exact merge evidence
without advancing the controller. Its read-only result distinguishes positive
proof from unsupported or indeterminate evidence; it neither creates an
admission nor releases a fence. The native PostgreSQL path additionally supports
an explicit atomic historical disposition under current repository service
authority. It records the observation without inventing admission history and
releases only the exact legacy fence. It does not enable ordinary-agent
execution or alter the scheduler policy.

Launchplane runs the scheduled pass itself. The `launchplane-merge-train-workers`
service (`launchplane service merge-train-workers run`) starts a pass every five
minutes (`--interval-seconds`, or `LAUNCHPLANE_MERGE_TRAIN_SCHEDULER_INTERVAL_SECONDS`
in the start script). Each pass reads the active policy record and, for every
target with `scheduler.enabled = true`, reads admission and, when admitted, runs
that target's `runner_mode` once with its `mutate` value, the same calls the
routes below make. Scheduled passes are started by Launchplane, not by a caller,
so they need no caller grant; the policy record's `scheduler` block is the
switch. A failing or lease-held target does not stop the others, mutate passes
deliver controller PR feedback, and each pass logs one JSON line per target.
GitHub runs scheduled workflows on a best-effort basis and fired the
five-minute workflow schedule only about every six hours, which is why the
clock moved into Launchplane.

Until it is removed, the GitHub Actions schedule in
`.github/workflows/merge-train-runner.yml` still reads
authorized policy targets from the native FastAPI
`GET /v1/work-graph/merge-train/policy-targets` route on every scheduled run.
Each target with `scheduler.enabled = true` gets its own run job in that pass
(at most four run at once), using that target's `runner_mode` and `mutate`.
Trains are independent per repository and base branch, so one target's failure
does not stop the others. Zero enabled targets make the scheduled pass a
successful no-op. Each run job uses the admission route before its worker call
and writes at most one Launchplane worker result per pass; the five-minute
schedule is the retry loop. Manual dispatch remains explicit and uses workflow inputs for
repository, base branch, runner mode, mutation, and phase-specific commands.
Controller-mode mutate runs and manually dispatched batch-candidate,
stack-collapse, or batch-landing phases render conservative PR feedback payloads
from their worker responses and post them through the managed feedback endpoint,
so queued PRs get one evolving Launchplane status comment as the train builds,
waits, blocks, or completes. A pull request awaiting current-head Client review
hears so even before any candidate exists. Controller-mode dry-runs do not
deliver feedback comments. Manual-phase feedback binds repository and base-branch identity to the
phase response's candidate, landing-plan, or stack-collapse-plan record and fails
closed if another identity-bearing phase result disagrees.

## Scheduler rollout runbook

Roll out controller-mode scheduling as an admin-controlled lane. The service
must already have an active merge-train policy for the target repository/base
branch, the deployed authz grants must allow `.github/workflows/merge-train-runner.yml`
to read admission and run the selected worker route, and the target repository
should have low-risk candidate pull requests whose checks and labels make the
expected train behavior easy to inspect.

To opt a repository into observation, import an active merge-train policy record
with the scheduler enabled on each repository policy to observe:

```toml
[policies.scheduler]
enabled = true
runner_mode = "controller"
mutate = false
```

Scheduled runs resolve the repository, base branch, runner mode, and mutation
flag from that DB-backed policy target through the deployed Launchplane API.
Manual workflow dispatches use explicit workflow inputs. Missing manual
repository or base branch input fails closed before any worker request.

Observe at least two scheduled dry-run passes before enabling mutation. A healthy
observation pass has a successful GitHub Actions run, an admitted or explicitly
deferred admission response, `runner_mode=controller`, `mutate=false`, and a
controller result whose mode is `dry-run`. Dry-run controller passes may render
feedback payloads for inspection, but they must report zero delivered feedback
comments and must not create or update Launchplane-managed PR comments. The
Launchplane UI controller status panel or the controller-status route should show
the same active records, stale-record reasons, latest run result, and next
controller action without requiring a GitHub read.

Enable mutation only after the Director explicitly chooses to promote the dry-run
lane. Import a replacement active policy record with `scheduler.mutate = true`
for that promotion and confirm that service deploy health, required checks,
runner capacity, and target PR heads are still current. A mutate run still
performs at most one controller action per scheduled pass. Managed PR feedback
comments are allowed in mutate mode and should summarize whether a PR is queued,
blocked, waiting, stale, or completed.

For rollback, import a replacement policy record or restore the previous record
with `scheduler.mutate = false`. To disarm the scheduled lane entirely, import a
replacement active policy record with no `scheduler.enabled = true` repository
policies; scheduled runs without an enabled DB target complete as no-ops before
the admission call. Changing `scheduler.runner_mode` to `level1` returns the
workflow to the Level 1 baseline after the next scheduled pass.

Every rollout or rollback should leave evidence in the planning issue: the
target repository/base branch, the scheduler policy settings, the relevant run
IDs, the latest controller/admission state, whether PR feedback was delivered,
and the post-merge Launchplane CI, Security, CodeQL, and deploy status that
carried
the rollout behavior.

The merge step is allowed only from a fresh dry-run result whose next action is
`merge`. The merge request must use the selected pull request's observed
`head_sha` as the GitHub merge `sha` guard and the repository policy's
`merge_method`. After a successful merge, the worker must re-read the train
before selecting another queued pull request.

The GitHub adapter maps Launchplane's domain fields to the REST API endpoints:
blocked labels use the issue labels endpoint, branch refresh uses
`expected_head_sha`, and merge uses `sha`. A GitHub `409 Conflict` from the
guarded merge call is treated as stale-head evidence and requires a fresh read
instead of a blind retry.

Live worker reads build the same `MergeTrainDryRunSnapshot` contract from
GitHub pull requests for the policy repository/base branch. The reader only uses
GET requests, preserves unknown mergeability or check evidence as `unknown` or
`pending`, and fails closed when required pull request fields are missing.
