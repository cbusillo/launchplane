# Merge Train Policy Contract

Launchplane merge trains use an explicit repository policy before any worker is
allowed to enqueue, update, or merge pull requests. The policy is a bootstrap
contract for the sequential merge train work in #410; it is not a live tracked
`config/*.toml` authority.

The first live smoke target is SellYourOutboard `main`. The policy is bundled in
code for CLI and worker dry-runs so the next implementation slice can discover a
stable contract without adding repo-local runtime config.

## Fields

Each repository policy contains:

- `repository`: GitHub `owner/name` repository.
- `base_branch`: Branch the train protects and merges into.
- `enqueue_label`: Label required before a pull request can enter the train.
- `blocked_label`: Label Launchplane applies when a queued pull request blocks.
- `merge_method`: GitHub merge strategy, one of `merge`, `squash`, or `rebase`.
- `failure_policy`: Whether Launchplane pauses the whole train or continues
  after marking the blocked pull request.
- `enqueue`: Requirements for who may enqueue.
- `merge_identity`: Token or workload identity allowed to update branches and
  merge pull requests.
- `service_authz`: Launchplane authz action/product/context required before the
  service endpoint may run the policy.
- `github_token`: Launchplane service-host token source used for live GitHub
  API calls.

The initial enqueue policy is intentionally narrow: the enqueue label must be
present and the enqueue action must come from a repo owner or repo admin. That
keeps the runner fail-closed until #410 wires live GitHub role checks.

## Failure Semantics

The default SellYourOutboard policy uses `pause_train`. If any pull request in
the train cannot update, pass checks, or merge, Launchplane marks that pull
request with `blocked_label` and stops processing later queued pull requests.

`continue_after_blocking_pr` is reserved for repositories that explicitly choose
higher throughput over strict ordering. A worker must still mark the failed pull
request with `blocked_label` before considering later entries.

## Initial Smoke Policy

```toml
schema_version = 1

[[policies]]
repository = "cbusillo/sellyouroutboard"
base_branch = "main"
enqueue_label = "ready-to-merge"
blocked_label = "merge-blocked"
merge_method = "merge"
failure_policy = "pause_train"

[policies.enqueue]
label_required = true
allowed_actor_roles = ["repo_owner", "repo_admin"]

[policies.merge_identity]
kind = "github_actions_oidc"
name = "launchplane-merge-train"

[policies.service_authz]
action = "merge_train.run_once"
product = "launchplane"
context = "launchplane"

[policies.github_token]
env_var = "GITHUB_TOKEN"
```

## Discoverability

The contract is available to dry-run tooling with:

```sh
uv run launchplane work-graph merge-train-policy \
  --repository cbusillo/sellyouroutboard \
  --base-branch main
```

Operators can validate an external bootstrap TOML without treating it as live
authority:

```sh
uv run launchplane work-graph merge-train-policy \
  --policy-file path/to/merge-train-policy.toml
```

Workers should load the same typed contract before enqueuing or merging. A
missing policy for a repository/base branch is a hard failure, not a fallback to
implicit behavior.

The sequential train dry-run accepts a JSON snapshot of candidate pull requests
and reports queue order plus the next intended action without mutating GitHub:

```sh
uv run launchplane work-graph merge-train-dry-run \
  --snapshot-file path/to/merge-train-snapshot.json
```

The smoke-target run-once command reads a live GitHub snapshot for the selected
repository/base branch and reports the same worker-step intent without mutating
by default:

```sh
GITHUB_TOKEN=... uv run launchplane work-graph merge-train-run-once
```

Passing `--mutate` applies exactly one worker transition from that fresh
snapshot. Use it only from the intended operator environment for the smoke
target; the command is a narrow bootstrap surface, not a long-running train
scheduler.

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

A worker pass applies at most one transition from one fresh snapshot. It may add
the block label, request a branch refresh, record a wait boundary, perform one
guarded merge, or report an idle queue; it must not chain follow-up reads or
mutations in the same pass.

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
