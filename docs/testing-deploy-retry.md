---
title: Rebuild or retry Odoo testing without a product commit
---

A Launchplane fix does not require a new product commit or image build. An
external addon fix needs a new image, but can rebuild the existing product
commit. Neither operation releases production.

## Retry the recorded image

Read the product's `reconcile-requests-read` output using the installed
Launchplane skill. Take the exact desired artifact id and source commit from
the testing plan, and inspect its last failed operation. Resolve its cause
first. Check current staff/Client use and any recorded testing hold; an explicit
admin deploy is not an automatic reconcile and the worker treats it as a manual
operation. Do not run it during someone else's testing session. A hold appearing
after the client's preflight is not a worker interlock for an admin operation.

Use the repository client with the installed operator transport and existing
identity:

```bash
uv run python scripts/retry-testing-deploy.py \
  --operator-helper /path/to/installed/launchplane/scripts/launchplane-write-action.py \
  --product PRODUCT --context CONTEXT \
  --artifact-id ARTIFACT --source-commit COMMIT \
  --idempotency-key UNIQUE-KEY-FOR-THIS-RETRY --apply
```

Omit `--apply` to inspect only. The client checks the current hold and replacement
plan, then submits the exact artifact to the existing service replacement route.
It uses retained data, never an empty database or a restore. Existing artifact
validation, authorization, active-operation exclusion, worker checks, credential
compatibility and health checks still apply. Automatic reconciliation's three
failed-attempt budget is unchanged. This explicit deploy has its own stable key.

`accepted` means queued, not healthy. Read `target-replacement-operation-read`
with the returned operation id, then product environment/activity and path-check.
On an uncertain response, inspect activity before repeating anything and keep
the original key and arguments. Do not use a new key to escape a pending,
unknown or denied operation. A changed payload under that key is refused by the
service. A genuine HTTP authorization refusal stops the action; record its
exact code and trace on the owning issue.

## Rebuild current main and its tracked inputs

No workflow-dispatch trigger is needed. Read the product's current default-branch
commit and its `Build` workflow runs. Select the push run of
`.github/workflows/build.yml` for that exact commit in that repository, and rerun
**the entire run**, using the GitHub skill's automation identity:

```bash
/path/to/installed/github/scripts/gh-with-env-token run rerun RUN_ID --repo OWNER/REPO
```

Do not select an older commit, a PR run, only failed jobs, or the Launchplane
self-deploy workflow. GitHub reruns the same push event at the same commit with
a new run attempt. The product workflow checks out its own build inputs again
and uploads `artifact-manifest-<run_attempt>`. Launchplane verifies that attempt's
manifest and digest, gives it a distinct artifact identity and reconciles it to
testing. Same-commit movement uses the verified build attempt. Watch that exact
run with the GitHub skill, then inspect Launchplane's testing reconcile and
health. A successful build alone does not prove testing deployed.

A floating shared-addons ref is resolved again; an exact devkit pin remains
exact. A devkit fix outside the pinned commit still requires the site's approved
pin-update PR. This does not supersede Q196 on odoo-devkit#131 or remove pins.
An old successful attempt must never supply a new attempt's manifest.

## CM qualification

CM's retained-database credential repair (#3947) must settle before either live
proof. A retry/rebuild does not repair that refusal or authorize a new testing
generation. Record one exact-artifact retry and one same-main rebuild reaching
healthy CM testing on #3949 after that prerequisite. The issue stays open until
both are proven. No production promotion is part of either operation.
