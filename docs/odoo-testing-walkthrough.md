---
title: Opaque Odoo testing walkthroughs
---

Launchplane owns testing task accounts and the browser that uses them. The
password is generated inside the service, encrypted in an instance-scoped
`odoo_walkthrough` managed-secret record, and consumed only by the fixed Odoo
account driver and the isolated browser service. This integration is separate
from product runtime secrets: the password is never delivered as an application
environment variable or exposed through an operator/helper response. Restored
staff and admin passwords are preserved. Production and preview instances are
rejected before provider or secret resolution.

The API is `POST /v1/drivers/odoo/testing-walkthrough`. Its operations are
`provision`, `run`, and `cleanup`, each with `mode=dry-run` followed by
`mode=apply`, the returned `reviewed_plan_sha256`, and a distinct, stable
`Idempotency-Key`. Use the same `task_id` for the account's lifecycle; choose
a random UUID's 32 lowercase hex characters for each new task. No password,
cookie, browser handle, trace, HAR, HTML, session token, storage state, or
arbitrary script is accepted or returned. The service returns a trace ID,
task ID, plan digest, account ID, sign-in verdict, step count, and optional
masked PNG screenshots encoded as base64.

## Authority and prerequisites

Signed-in Launchplane administrators have the existing administrator authority.
Machine callers require `odoo_testing_walkthrough.execute` for the exact
product, context and `testing` instance. The inert dry run uses
`odoo_testing_walkthrough.plan`. Declaring these actions installs no policy
rules, grants or credentials. A machine denial remains `authorization_denied`;
record its trace and exact action under [denial handling](authorization-authority.md#denial-handling).
Do not switch identities or use another operation to carry a denied call.
The Director decides any new grant through the existing policy approval path.

The lane must have a completed qualified fresh Odoo testing generation with
independent credentials, storage and networks. The service checks current
product ownership, target aliases, HTTPS origin, exact runtime/image, copy
witness, real host isolation, staff hold and protected usage/custody. No other
stable lane may use the testing origin. Recent successful sign-ins by anyone
except this exact witnessed task account refuse provisioning and browser work;
copied login history before preparation is excluded. An unreadable check
refuses. A task is usable for four hours; expiration still permits cleanup.
An existing staff login can never be adopted or reset.

Account groups are explicit native business group XML IDs. The default is
`base.group_user`; supply only the business roles required by the checklist.
Odoo administration groups and business groups that imply administration are
refused. This API cannot change the groups of an existing task account.

## Operator and browser procedure

Use an authenticated request to the deployed service. A private curl config
may supply the existing operator authorization header; never put a token in
shell arguments, request JSON, logs or the receipt. For example, with a
private header config and the configured service origin:

```sh
curl --fail-with-body --silent --show-error \
  --config "$LP_PRIVATE_HEADER_CONFIG" \
  --header 'Content-Type: application/json' \
  --data-binary @task-request.json \
  "$LAUNCHPLANE_OPERATOR_URL/v1/drivers/odoo/testing-walkthrough" \
  --output task-receipt.json
```

The first request contains only nonsecret intent:

```json
{
  "product": "example-odoo",
  "context": "example",
  "instance": "testing",
  "task_id": "0123456789abcdef0123456789abcdef",
  "operation": "provision",
  "mode": "dry-run",
  "groups": ["base.group_user", "stock.group_stock_user"]
}
```

Read the receipt, then repeat with `mode=apply`, the returned digest as
`reviewed_plan_sha256`, and `--header 'Idempotency-Key: <task>-provision'`.
The receipt contains no credential to copy. Inspect `result.status`, including
when HTTP status is 200: `unknown` is never a passing operation.

For browser work, dry-run and apply the same task with `operation=run` and
a new key. A run always signs in independently as the task account and verifies
the returned user ID before opening the web client. Supply bounded `steps`:

```json
[
  {"action": "open", "value": "/web#action=example_business_action"},
  {"action": "visible", "selector": ".o_web_client"},
  {"action": "fill", "selector": "input[name=name]", "value": "TEST walkthrough"},
  {"action": "click", "selector": "button[name=save]"},
  {"action": "screenshot"}
]
```

Use the product's actual checklist selectors; these are illustrative. The
driver accepts no evaluated script, arbitrary URL, query string, staff
credentials or non-TEST input. It blocks external network requests, login and
password-reset pages, service workers, websockets, downloads and sensitive
controls. Screenshots are available only on the signed-in web client, with
inputs and occurrences of the task password masked. At most five screenshots
of two MiB each are returned per run. Contexts close on success and failure;
no reusable browser context is handed to the harness. Receipt step counts
prove completed interactions, not business correctness: the product checklist
still owns its pass/fail observations and screenshot interpretation.

## Cleanup, interruption and maintenance

After browser work, dry-run and apply `operation=cleanup` for the same task.
Cleanup verifies its native ownership marker and the current testing copy,
archives only that account, replaces its password, removes its API keys, and
then disables its managed-secret record and binding. This invalidates its Odoo
sessions. The account marker and encrypted secret history remain audit evidence;
they cannot be used to provision the same task again. Use a new task ID.
Business records created by a walkthrough remain TEST-marked and are removed
under that product's supported testing-data procedure; cleanup does not erase
the walkthrough's business evidence or staff data.

Every apply uses the existing durable provider reservation and Odoo lane fence.
An exact completed retry replays its redacted receipt and never repeats browser
transactions. After a disconnect or crash, retry only the original request/key.
A recovered interrupted dispatch returns `unknown` and releases that original
reservation for independent task cleanup; it does not repeat the dispatch.
Then run cleanup with a fresh reviewed digest and key. A live lease or changed
copy/target still refuses. Preserve the original receipt/key and use supported
operation recovery; do not reset fences or modify a retained old database.

Deploy compatible API and browser-service code together through normal
Launchplane delivery. No schema migration, product build setting, GitHub secret
or policy mutation is part of this source change. Before rolling back the API,
clean up task accounts through this route. Otherwise retain a compatible image
and its encrypted history for supported cleanup. Live account/sign-in proof
is a separate post-landing session; source tests and fixture browser evidence
do not claim any real testing walkthrough passed.
