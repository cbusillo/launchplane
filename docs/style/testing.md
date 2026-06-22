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
uv run launchplane ci unittest-shard plan --shard-count 6
uv run launchplane ci unittest-shard run --shard-count 6 --shard-index 0 --timings-output tmp/shard-0.json
```

Shard planning discovers `tests/test*.py` dynamically. Timing files are
balancing hints only; discovered test modules remain the source of truth.
