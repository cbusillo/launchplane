---
title: Engineering Review Shadow Canary
---

This branch and its pull request are a non-production, non-authoritative
routine shadow canary for Launchplane's exact-head engineering-review flow.
They do not change runtime behavior, workflows, dependencies, policy, or
release authority.

The canary head may advance between rehearsals so each run proves fresh,
exact-head evidence rather than replaying an earlier assignment.
