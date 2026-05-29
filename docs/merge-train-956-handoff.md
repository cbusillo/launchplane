# Merge Train 956 Handoff

This handoff is for the next Launchplane agent working on
`cbusillo/launchplane#956`.

## Current State

- Issue: <https://github.com/cbusillo/launchplane/issues/956>
- Affected repository: `cbusillo/codex-skills`
- Base branch: `main`
- Stale landing-plan record:
  `merge-train-batch-landing-plan-20260528T200702Z-3e6fde28dcb5bb7a`
- All planned PRs are already merged: #159, #160, #161, #162, #163, and #166.
- `codex-skills` main after that batch: `75081e01d3627ec2c372c3f0b3f2bd3151091392`
- New waiting PR blocked behind the stale plan:
  <https://github.com/cbusillo/codex-skills/pull/172>

The controller still advertises `land_batch` for the stale landing plan. A
mutating reconciliation pass still returns HTTP 502 with
`github_request_failed`, so the controller cannot admit newer ready work.

## Fresh Repro Evidence

Dry-run workflow:

- Run: <https://github.com/cbusillo/launchplane/actions/runs/26607634964>
- Result: success
- Admission action: `land_batch`
- Landing-plan record:
  `merge-train-batch-landing-plan-20260528T200702Z-3e6fde28dcb5bb7a`
- Admission trace: `launchplane_req_b2c6d4213c8a4e09a80c76f923e73dbc`
- Controller trace: `launchplane_req_a1dd991929dc49e28fbe5b8737289278`

Mutating reconciliation workflow:

- Run: <https://github.com/cbusillo/launchplane/actions/runs/26607700314>
- Result: failed
- HTTP status: 502
- Error code: `github_request_failed`
- Trace: `launchplane_req_f092f9592fc4493ba69232d8abeed46f`

The mutating run was intentionally not retried. Treat the trace above as the
latest point-in-time repro.

## Expected Behavior

When every entry in a landing plan is already merged with the recorded PR head
SHA, the controller should reconcile the landing plan into a terminal state and
return `batch_landed` or another accepted terminal equivalent. It should then
stop advertising `land_batch` for that old plan, allowing the next eligible
queue to proceed.

At minimum, failures in this path should identify which PR or GitHub response is
blocking reconciliation instead of returning only generic
`github_request_failed`.

## Likely Investigation Path

Start with the controller and batch landing code paths:

- `control_plane/service.py`: controller route and `land_batch` handling.
- `control_plane/merge_train_github.py`: PR-native landing and already-merged
  reconciliation.
- `control_plane/workflows/merge_train_controller.py`: latest landing-plan
  selection and terminal-state helpers.
- `tests/test_service.py` and `tests/test_merge_train_github.py`: add or extend
  coverage for the all-entries-already-merged reconciliation case.

Useful issue history:

- #952: original stale landing blocker.
- #955: restored enough partial-progress retry behavior to land the remaining
  PRs, but terminal all-merged reconciliation still fails.
- #956: current focused issue.

## Suggested Fix Shape

Add a test where the latest active landing plan has planned or in-progress
entries, GitHub reports every planned PR already merged, and each merged PR head
matches the recorded head SHA. The controller should write a completed landing
plan record and return a terminal action without making another merge attempt.

If the current implementation already attempts that, improve the GitHub error
classification so an already-merged PR with matching head is handled before any
request path that can raise a generic upstream failure.

After the fix lands and Deploy Launchplane succeeds, rerun the controller
against `cbusillo/codex-skills`:

```sh
gh workflow run merge-train-runner.yml \
  --repo cbusillo/launchplane \
  --ref main \
  -f repository=cbusillo/codex-skills \
  -f base_branch=main \
  -f mutate=false \
  -f runner_mode=controller \
  -f batch_candidate_mode=none \
  -f batch_landing_mode=none \
  -f stack_collapse_mode=none
```

Expected post-fix dry-run result: the old landing plan is terminal or ignored,
and the controller can proceed toward the next eligible queue item. At the time
of this handoff, the next ready item is `codex-skills` PR #172. PR #169 should
not be admitted yet.

## Do Not Accidentally Land PR 169

`cbusillo/codex-skills#169` is intentionally paused. It includes Launchplane
repo metadata work, but the plan needs to change so committed repo config does
not bake in this deployment's concrete Launchplane URL. Use env/local operator
config or another ignored/private source for the service URL, and keep committed
`.github/github.json` metadata public-safe and portable.

See <https://github.com/cbusillo/codex-skills/issues/168> for the updated plan.
