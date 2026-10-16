---
title: Opaque Odoo testing walkthroughs
---

Launchplane owns testing task accounts and the browser that uses them. The
password is generated inside the service, encrypted in an instance-scoped
`odoo_walkthrough` managed-secret record, and consumed only by the fixed Odoo
account driver and the isolated browser service. This integration is separate
from product runtime secrets: the password is never delivered as an application
environment variable or exposed through an admin/helper response. Restored
staff and admin passwords are preserved. Production and preview instances are
rejected before provider or secret resolution.

The API is `POST /v1/drivers/odoo/testing-walkthrough`. Its operations are
`provision`, `run`, and `cleanup`, each with `mode=dry-run` followed by
`mode=apply`, `result.plan_sha256` as `reviewed_plan_sha256`, and a distinct, stable
`Idempotency-Key`. Use the same `task_id` for the account's lifecycle; choose
a random UUID's 32 lowercase hex characters for each new task. No password,
cookie, browser handle, trace, HAR, HTML, session token, storage state, or
arbitrary script is accepted or returned. The service returns a trace ID,
task ID, plan digest, account ID, sign-in verdict, step count, and optional
masked PNG screenshots encoded as base64.

## Authority and prerequisites

Signed-in Launchplane administrators have the existing administrator authority.
Machine callers using the existing `local_operator` or `local_admin` transport
require `odoo_testing_walkthrough.execute` for the exact
product, context and `testing` instance. The inert dry run uses
`odoo_testing_walkthrough.plan`. Declaring these actions installs no policy
rules, grants or credentials. A machine denial remains `authorization_denied`;
record its trace and exact action under [denial handling](authorization-authority.md#denial-handling).
terminal-agent and GitHub Actions identities are not accepted on this mutation
surface. Use the existing `local_operator` transport selected by the Director.
Do not switch identities or use another operation to carry a denied call.
The Director decides any new grant through the existing policy approval path.
An exact `local_operator` subject/token-label rule may leave product and context
selectors empty and select only `instances=["testing"]` to cover current and
future testing lanes. The execution boundary, rather than a per-lane policy
list, enforces current connection isolation: the shared SQL readback must pass
before planning, in each native account transaction, and at each browser
checkpoint. Missing, failed or unreadable isolation refuses; no repair is
performed by the walkthrough. This restriction also covers cleanup. If a lane
loses isolation, use the supported
[connection-isolation repair and readback](operations.md#repair-copied-carrier-and-customer-sms-connections)
before retrying cleanup. Outcome reconciliation remains read-only and does not
require passing isolation to observe an already dispatched operation.
Dry runs take only a transient lane reservation and perform authenticated
provider/host reads; they create no account, secret or operative request.

The lane must have a completed qualified fresh Odoo testing generation with
independent credentials, storage and networks. The service checks current
product ownership, target aliases, HTTPS origin, exact runtime/image, copy
witness, real host isolation, staff hold and protected usage/custody. No other
stable lane may use the testing origin. Recent successful sign-ins by anyone
except natively witnessed task accounts on this exact copy refuse provisioning
and browser work. A closed task's history does not prevent the next task;
reserved-looking logins without a matching account/copy marker remain protected.
copied login history before preparation is excluded. An unreadable check
refuses. Launchplane consumes a task credential for at most four hours;
this does not expire the native Odoo account. Cleanup is required to revoke it,
and remains available after the service consumption deadline.
An existing staff login can never be adopted or reset.

Account groups are explicit native group XML IDs. The default is
`base.group_user`; supply only the business roles required by the checklist.
The native groups `base.group_system`, `base.group_erp_manager`,
`base.group_allow_export` and `base.group_sanitize_override`, including
application groups that imply them, are refused. This is a privilege denylist,
not a general audit of application ACLs; choose groups for the actual checklist.
Technical Features
cannot be requested explicitly; Odoo may imply it for an ordinary internal user.
This API cannot change the groups of an existing task account.
Run and cleanup use the account's stored groups; the caller need not repeat
the provisioned group selection.

## Admin and browser procedure

Use an authenticated request to the deployed service. A private curl config
may supply the existing admin authorization header; never put a token in
shell arguments, request JSON, logs or the receipt. For example, with a
private header config and the configured service origin:

```sh
curl --fail-with-body --silent --show-error \
  --config "$LP_PRIVATE_HEADER_CONFIG" \
  --header 'Content-Type: application/json' \
  --header 'Idempotency-Key: <task>-provision' \
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
`reviewed_plan_sha256`, keeping the shown provision idempotency key.
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
driver accepts no evaluated script, arbitrary URL, query string, dot-segment or
encoded-path escape, staff credentials or non-TEST input. It blocks external network requests, login and
password-reset pages, service workers, websockets, downloads and sensitive
controls. Screenshots are available only on the signed-in web client, with
inputs and occurrences of the task password masked. At most five screenshots
of two MiB each are returned per run. Contexts close on success and failure;
PNG bytes are returned only on the first successful HTTP response and are not
persisted in Launchplane. Durable receipts retain their SHA-256 digests; exact
replays omit the PNGs. The harness keeps needed screenshots in its task evidence.
Masks protect credential surfaces; screenshots may still show restored business
data or personal information. Keep them in the task's approved evidence location.
Each step rechecks runtime identity and staff usage, so a long checklist can
take several minutes. Use short checklist segments; an HTTP timeout does not
cancel the worker, and a same-key retry reports a live lease until it finishes.
If that HTTP response is lost, preserve the original transaction receipt and
use a new run containing only `open`, `visible` and `screenshot` to inspect the
current state. This does not reconstruct the lost historical screenshot, and
must not repeat the original business-write steps.
No reusable browser context is handed to the harness. Receipt step counts
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
Cleanup may revoke its own account while a staff hold or recent staff sign-in
protects the lane. Its separate read-only physical qualification still requires
the original current copy, isolated resources and exact runtime. It never lifts
the staff hold or reads login history to authorize cleanup. After a copy is
replaced, old accounts/secrets remain under that retained copy's custody;
this API does not redirect cleanup to an old database.

Every apply uses the existing durable provider reservation and Odoo lane fence.
An exact completed retry replays its redacted receipt and never repeats browser
transactions. After a disconnect or crash, retry only the original request/key.
A recovered interrupted dispatch returns `unknown` and releases that original
reservation only after its value-free host completion journal proves the native
consumer finished, or a read-only check proves it quiescent after the dispatch
deadline. The persisted admission deadline allows ten minutes for eligibility
reads and account dispatch; native execution has shorter timeouts. Expiry or
clock disagreement refuses safely rather than extending the original intent.
Definite pre-commit consumer refusals roll back and return a terminal `fail`
receipt; they do not strand the target. The native consumer and the read-only
probe share a task-specific PostgreSQL transaction advisory lock: after the
deadline, a future consumer refuses before account writes, and an already
running transaction prevents a quiescent observation. It
does not replan the current lane or repeat browser/account work. Recovery can
therefore settle the original receipt even after expiry or a staff hold.
Then run cleanup with a fresh reviewed digest and key. A live lease or changed
copy/target still refuses. Preserve the original receipt/key and use supported
operation recovery; do not reset fences or modify a retained old database.
An absent/incomplete journal can settle only after that deadline and positive
quiescence proof from the exact original testing receiver. An unreachable or
changed receiver remains unknown and keeps the fence. Unknown outcome is never
a passing walkthrough, and no account/browser dispatch is repeated. Tiny value-free
dispatch journals live in the original host's private
`/tmp/launchplane-walkthrough` directory. Preserve them until the original
receipt is settled; host temporary-file disposal can remove old recovery proof.
An ambiguous commit or failed post-commit readback stays `unknown`. Browser-step
errors also stay `unknown`: earlier business writes may already have completed.
Retrying the original key settles evidence without repeating those steps, then
cleanup remains available. Browser work holds the existing heartbeat lease and
Odoo lane reservation; recovery never replays browser commands.

Deploy compatible API and browser-service code together through normal
Launchplane delivery. No schema migration, product build setting, GitHub secret
or policy mutation is part of this source change. Before rolling back the API,
clean up task accounts through this route. Otherwise retain a compatible image
and its encrypted history for supported cleanup. Live account/sign-in proof
is a separate post-landing session; source tests and fixture browser evidence
do not claim any real testing walkthrough passed.
