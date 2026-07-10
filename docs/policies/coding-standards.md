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
  values as production defaults, smoke fixtures, fallback policy, checked-in
  import catalogs, or implicit runtime authority. Real-world examples may
  appear only in docs or tests, and they must not be reachable by production
  code without a stored runtime record or operator-supplied input.
- Do not move real mutable configuration from code into checked-in config files
  as a workaround. Real product, tenant, repository, branch, domain, lane,
  provider-target, runtime-environment, authz, operator, or secret-binding
  identity belongs in Launchplane records or operator-supplied input, not in
  production code, workflow defaults, repo metadata, TOML, JSON, YAML, or local
  fallback catalogs.
- Code may define schemas, validators, typed contracts, generic disabled
  defaults, and fail-closed behavior. The only checked-in/process-level runtime
  config exception is Launchplane's minimal self-bootstrap/root-of-trust wiring
  needed to reach DB-backed records and managed secrets.
- Preserve minimal diffs and readable history.
- Update docs whenever behavior or repo ownership changes.

## Dependency Updates

- Group routine minor and patch updates when they share a validation surface.
- Keep semantic-version major updates independently reviewable. Where the
  package ecosystem supports cooldowns, delay newly released majors so they do
  not poison routine groups before adjacent tools declare compatibility.
- Keep security updates ungrouped and independently mergeable; version-update
  grouping and cooldown policy must not delay them.
- Fix compatibility findings in code or dependency constraints. Do not weaken
  type checks, peer-dependency resolution, tests, or vulnerability gates merely
  to make an automated update green.
