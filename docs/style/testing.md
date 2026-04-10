---
title: Testing Style
---

- Add targeted unit tests for storage, contracts, and workflow mapping.
- Prefer deterministic file-system tests using `TemporaryDirectory`.
- Test fail-closed behavior explicitly.
- Keep fixtures small and inline unless they are reused heavily.
- Default test entrypoint is `uv run python -m unittest`.
