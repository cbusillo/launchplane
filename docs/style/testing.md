---
title: Testing Style
---

- Add targeted unit tests for storage, contracts, and workflow mapping.
- Prefer deterministic file-system tests using `TemporaryDirectory`.
- Test fail-closed behavior explicitly.
- Keep fixtures small and inline unless they are reused heavily.
- Default test entrypoint is `uv run python -m unittest`.

## Local test loop

Run a focused unittest module or test method before the full suite:

```bash
uv run python -m unittest tests.test_module_name
uv run python -m unittest tests.test_module_name.TestCaseName.test_behavior
```

CI may shard same-repo unittest runs through Launchplane's helper while keeping
the canonical framework as stdlib `unittest`:

```bash
uv run launchplane ci unittest-shard plan --shard-count 12 --timings-file .ci-cache/unittest-timings/history.json --max-tests-per-target 20 --max-seconds-per-target 30
uv run launchplane ci unittest-shard run --shard-count 12 --shard-index 0 --timings-file .ci-cache/unittest-timings/history.json --max-tests-per-target 20 --max-seconds-per-target 30 --timings-output tmp/shard-0.json
```

Shard planning discovers `tests/test*.py` dynamically. Small files run as whole
modules. Large or timing-known-slow files may be split into unittest target IDs,
preferably `tests.test_module.TestCase` and only down to
`tests.test_module.TestCase.test_behavior` when a single test case is still too
large. This lets hot modules distribute across shards without physical file
moves. Timing files are balancing hints only; discovered tests remain the source
of truth.

Same-repo CI currently uses 12 unittest shards with a 20-test/30-second split
threshold to keep large app and service targets under the tool wall-clock
ceiling. CI restores timing history once per workflow run and distributes that
immutable snapshot to every shard and the aggregate job. Shards must not restore
timing caches independently because timing-dependent target splitting requires
one shared plan input. Snapshot and shard artifacts remain available for the
full workflow rerun window and may be overwritten safely by rerun jobs. The
shard plan includes per-target timing-source diagnostics:

- `exact`: the timing file has a direct record for that unittest target.
- `parent`: the estimate is derived from the parent module timing and spread
  across discovered child targets.
- `default`: no timing history exists yet, so the planner uses the conservative
  default estimate until a shard timing artifact teaches it better.
