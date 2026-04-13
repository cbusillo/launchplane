---
title: Coding Standards
---

- Optimize for durable ownership boundaries, not short-term convenience.
- Prefer fail-closed control-plane behavior.
- Avoid feature flags and dead transitional code.
- Keep any compatibility bridge explicit and removable; do not normalize it
  into a permanent abstraction.
- Do not parse logs when explicit records or typed contracts should exist.
- Preserve minimal diffs and readable history.
- Update docs whenever behavior or repo ownership changes.
