# Merge Train Policy Contract

## Terminology

Launchplane currently ships a Level 1 ordered merge queue baseline. It reads a
fresh GitHub snapshot, orders eligible pull requests, selects the first eligible
entry, and applies at most one worker transition per service call. That baseline
is useful for fail-closed ordering, but it is not the full batch merge train
target.

The full Launchplane merge train target is a batch-validating train:

1. Collect eligible queued pull requests for one repository/base branch.
2. Build one combined batch candidate from the base branch plus queued pull
   requests in train order.
3. Run required checks against that exact candidate commit.
4. If the candidate passes, land the original pull requests in train order using
   GitHub's normal pull request merge path so repository UI and audit hints stay
   attached to each PR.
5. If the candidate fails or cannot be built, split or reduce the batch to
   isolate blockers, then mark or requeue entries according to policy.

Until the batch candidate and landing records exist, docs and operators should
describe the live implementation as the ordered merge queue baseline.

Launchplane merge trains use an explicit repository policy before any worker is
allowed to enqueue, update, or merge pull requests. Live service routes resolve
the active `launchplane_merge_train_policies` record from Launchplane storage.
If no active record exists, service routes fail closed with
`merge_train_policy_not_configured`. Operators change live policy by writing a
new DB-backed policy record, not by relying on checked-in config files,
service-host env, or generic service-code conditionals.

## Separation From Tenant Repository Admission

Scheduler merge train admission (`merge_train_admission`) governs pull request queueing, batch candidate construction, and landing order under active `launchplane_merge_train_policies` records.

Tenant merge eligibility (`evaluate_tenant_merge_eligibility`) and repository classification records (`launchplane_tenant_repository_classifications`) operate independently under their own DB authority:
- Repository classifications explicitly categorize repositories as `engineering` (taking the normal engineering fast path) or `tenant_ui` (requiring one exact-head manager-preview, technical-human-waiver, or trusted-maintenance path).
- Repository classifications use exact immutable identity and CAS operator recovery without heuristics or PR label fallback.
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
controller lease record. One controller owner may actively mutate one
`repository/base_branch` pair at a time; a second owner must fail closed while
the current lease is still valid. The lease record stores owner, expiry,
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
operator-required conflicts. Retryable states retain the exact active phase and
may be adopted by the next mutating controller pass. Deterministic policy,
expected-SHA, or provider rejection states use an `operator_required:` detail
so operators can repair the repository or provider condition before retrying
through the service. The next mutating controller pass adopts that exact phase
and re-observes provider state; no out-of-band record edit is required or
supported. Read-only controller calls report either state without acquiring or
mutating the lease record.

Lease expiry semantics are fail closed. PostgreSQL observes acquisition,
heartbeat, and expiry time inside the advisory-locked transaction rather than
trusting a caller timestamp. Once a lease expires and another controller
acquires the same repository/base fence, the stale owner cannot heartbeat,
checkpoint, or release because every transition compares both owner and
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
- `github_token`: Launchplane service-host token source used for live GitHub
  API calls.
- `scheduler`: Optional DB-backed scheduler intent for the GitHub Actions
  scheduler. When enabled, the scheduler reads this target from the deployed
  Launchplane API; checked-in workflows and GitHub variables are not target
  authority.

The default enqueue policy is intentionally narrow: the enqueue label must be
present and the PR author must be a repo owner or repo admin. A repository may
explicitly opt a known automation account in by storing its immutable GitHub
numeric user id in `trusted_automation_github_user_ids`. Matching identities are
reported as `trusted_automation` in controller dry-run output. The default list
is empty, so existing owner/admin-only policies remain fail-closed and unchanged.
Logins are diagnostic labels, not policy identity, because logins can be renamed.

## Failure Semantics

Policies using `pause_train` stop processing later queued pull requests after a
selected pull request cannot update, pass checks, or merge. Launchplane marks
the blocking pull request with `blocked_label` before stopping.

`continue_after_blocking_pr` is reserved for repositories that explicitly choose
higher throughput over strict ordering. A worker must still mark the failed pull
request with `blocked_label` before considering later entries.

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

The candidate is built in deterministic queue order. If any pull request cannot
be applied cleanly, candidate construction stops at that pull request and the
worker records a blocker. The first implementation should use an explicit
temporary candidate ref or branch so GitHub Actions can run checks against a
real commit SHA. The exact ref naming and cleanup policy are part of the batch
train implementation, not the repository policy TOML.

### Candidate Validation

Required checks must pass on the candidate commit that includes the queued PRs
being considered for the batch. Checks on each PR's own head are useful
screening evidence, but they do not prove the combined tree is safe to land.
Launchplane must fail closed when candidate check evidence is missing, pending,
failed, stale, or attached to a different commit SHA.

### PR-Native Landing

After a batch candidate passes, Launchplane lands the original pull requests in
queue order using GitHub's pull request merge API and the configured
`merge_method`. Before each PR merge, Launchplane must verify that the PR still
matches the candidate evidence it is about to rely on. At minimum, the PR head
SHA, exact target base ref and current base SHA, queue position, policy digest,
and candidate batch identity
must match the recorded landing plan. If GitHub state changes, Launchplane must
stop, re-read, and rebuild or requeue rather than continuing from stale batch
evidence.

Stack-collapse records participate in landing only when their repository, base
branch, policy digest, root PR, and expected root head match the landing plan.
Older waiting stack-collapse records stay visible as status evidence, but they
must not block or annotate an unrelated unstacked batch landing.

Directly merging the candidate branch into the protected base branch is not the
preferred first implementation because it hides the normal PR-by-PR merge UX and
can make repository history and GitHub review state harder to inspect. It should
remain a separate, explicit future decision if ever needed.

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
train. The root PR's `enqueue_label` is the operator/agent intent: it means
"land this work through Launchplane," including any required same-repository
linear stack collapse before train admission. Launchplane records a stack
collapse plan before mutating branches so the root PR, child order, expected
SHAs, mutation sequence, policy digest, and idempotency evidence remain
auditable. Ambiguous, forked, cyclic, or unsupported branch-protection cases
must fail closed with operator-visible reasons.

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

## Operator Changes

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
support, store it outside the repo or generate it from operator automation, then
import it through the deployed service API. GitHub Actions operator workflows
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
- uses: cbusillo/launchplane/.github/actions/launchplane-request@main
  with:
    launchplane-url: ${{ vars.LAUNCHPLANE_PUBLIC_URL }}
    route-path: /v1/merge-train/policies/import
    payload-file: merge-train-policy-import-request.json
    idempotency-key: merge-train-policy-import:${{ github.run_id }}
```

For local operator terminals, the compatibility CLI import path still reads the
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

Operators can validate an external TOML before importing it as a policy record:

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
imported policies normally reference that same service-host token source.

Passing `--mutate` applies exactly one ordered-queue worker transition from that
fresh snapshot. Use it only from the intended operator environment for the smoke
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

When the selected pull request needs a branch refresh, Launchplane updates that
pull request using the observed head SHA as the compare point. The worker must
then re-read mergeability and required checks before any later merge decision;
pre-update check results are stale after a branch refresh.

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
chain follow-up reads or mutations in the same pass. The full batch train will
use separate batch candidate and landing-plan records instead of treating a
single selected PR as the whole train state.

The service endpoint `POST /v1/work-graph/merge-train/run-once` uses the same
policy. Request payloads name `repository`, `base_branch`, and optional
`mutate`; the service finds the repository/base policy before any GitHub call,
authorizes the caller through `service_authz`, resolves the GitHub token from
`github_token.env_var`, reads a fresh snapshot, and either returns the dry-run
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
`POST /v1/work-graph/merge-train/controller/run-once` is the preferred operator
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
- `wait_for_root_checks`: The collapsed root PR needs fresh required checks.
  Stop and poll later; do not call phase endpoints.
- `admit_collapsed_root`: The collapsed root PR is ready to enter the batch
  candidate path. Mutate once, then call again.
- `stack_unsupported`: A stack exists but is not a supported same-repo linear
  stack. Stop and surface the redacted stack discovery details.
- `plan_candidate`: The queue can produce the next batch candidate. Mutate once,
  then call again.
- `build_candidate`: A stored candidate needs its train ref built or refreshed.
  Mutate once, then call again.
- `observe_candidate`: A built candidate needs check observation. Mutate or
  dry-run later until checks pass, fail, or remain pending.
- `candidate_failed`: Candidate checks failed and still matches the current
  eligible queue. Stop and surface the candidate record id and failed check
  evidence. If the eligible queue/base changes, the next controller action may
  become `plan_candidate` for a superseding candidate.
- `plan_landing`: A passed candidate is ready for PR-native landing-plan
  creation. Mutate once, then call again.
- `land_batch`: A landing plan with planned or in-progress merge entries is
  ready to merge or resume the original PRs in order. Mutate once only after
  operator intent; call again to verify terminal state.
- `batch_landed`: The batch already landed. Stop; the train phase is complete
  for that batch.
- `block`: The selected PR is blocked by conflicts or failed checks. Stop and
  surface `dry_run_result.next_action_detail`.
- `update_branch`: The selected PR needs a branch update before it can be
  checked. Stop or use the lower-level recovery workflow deliberately.
- `wait_for_checks`: Required PR checks are pending. Stop and poll later.
- `idle`: No eligible queued work exists. Stop.

All controller calls are one-action calls. A caller that wants to drive the
train should repeat `run-once` only after reading the returned action and should
stop on terminal or attention states: `batch_landed`, `candidate_failed`,
`stack_unsupported`, `block`, `update_branch`, `wait_for_checks`,
`wait_for_root_checks`, and `idle`. A failed HTTP response with `status:
"rejected"` is also terminal for that attempt. Public-safe helper summaries
should include `error.code`, `trace_id`, and the retry/stop recommendation, not
the original request body.

Schedulers and operators report train progress through
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
the cleanup phase and candidate ref intact. The next owner resumes that exact
phase before planning new train work.

### Guarded Level 3 landing

Controller landing and direct batch landing use the same per-entry guarded
boundary documented in [merge-admission.md](merge-admission.md). Immediately
before each provider merge, Launchplane re-resolves current Owner,
change-impact, engineering-review, technical-check, policy, candidate, queue,
rolling-base, head/tree, lease, and expected-effect evidence. It persists one
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
The route is policy-backed and authorized through the repository policy's
`service_authz`, but it is store-only: it does not require a GitHub token, does
not read GitHub, and does not write run records.

Operator views can read the broader stored controller state from the native
FastAPI route
`GET /v1/work-graph/merge-train/controller/status?repository=owner/name&base_branch=main`.
That route returns the same admission decision plus the latest Level 1 run record,
controller owner, active action and phase, lease and heartbeat age, reconciliation
state, and compact summaries for active batch candidates, landing plans, and
stack collapse plans. When the latest run is dry-run evidence, the response also
includes a compact queue summary with the intended next action, selected PR,
eligible count, queued count, and visible ineligible reasons from the persisted
dry-run payload. Stored controller records only influence the advertised
controller action when their policy key and digest match the active repository
policy; stale records remain visible in the summaries with a stale reason. It is
also store-only, so it can power dashboards and status summaries without
consuming GitHub API capacity or advancing the train.

The GitHub Actions scheduler in `.github/workflows/merge-train-runner.yml` reads
authorized policy targets from the native FastAPI
`GET /v1/work-graph/merge-train/policy-targets` route on every scheduled run.
Exactly one target may have `scheduler.enabled = true`;
zero enabled targets make the scheduled pass a successful no-op, and multiple
enabled targets fail closed until an operator narrows the DB-backed scheduler
intent. The scheduler then uses the admission route before every worker call and
writes at most one Launchplane worker result per pass; the five-minute schedule
is the retry loop. Manual dispatch remains explicit and uses workflow inputs for
repository, base branch, runner mode, mutation, and phase-specific commands.
Controller-mode mutate runs and manually dispatched batch-candidate,
stack-collapse, or batch-landing phases render conservative PR feedback payloads
from their worker responses and post them through the managed feedback endpoint,
so queued PRs get one evolving Launchplane status comment as the train builds,
waits, blocks, or completes. Controller-mode dry-runs do not deliver feedback
comments.

## Scheduler rollout runbook

Roll out controller-mode scheduling as an operator-controlled lane. The service
must already have an active merge-train policy for the target repository/base
branch, the deployed authz grants must allow `.github/workflows/merge-train-runner.yml`
to read admission and run the selected worker route, and the target repository
should have low-risk candidate pull requests whose checks and labels make the
expected train behavior easy to inspect.

To opt a repository into observation, import an active merge-train policy record
with exactly one repository policy whose scheduler is enabled:

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
operator UI controller status panel or the controller-status route should show
the same active records, stale-record reasons, latest run result, and next
controller action without requiring a GitHub read.

Enable mutation only after an operator explicitly chooses to promote the dry-run
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
