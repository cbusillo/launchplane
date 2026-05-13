---
title: Coding Standards
---

- Optimize for durable ownership boundaries, not short-term convenience.
- Prefer fail-closed control-plane behavior.
- Avoid feature flags and dead transitional code.
- Keep any compatibility bridge explicit and removable; do not normalize it
  into a permanent abstraction.
- Do not parse logs when explicit records or typed contracts should exist.
- Do not hard-code real tenant, product, repository, branch, domain, or operator
  values as production defaults, smoke fixtures, fallback policy, or implicit
  runtime authority. Real-world examples may appear only in docs, tests, or
  explicit import material, and they must not be reachable by production code
  without a stored runtime record or operator-supplied input.
- Preserve minimal diffs and readable history.
- Update docs whenever behavior or repo ownership changes.
