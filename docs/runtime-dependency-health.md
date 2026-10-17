# Runtime dependency health

Launchplane derives each lane's database, email and cache requirements from its
deployment metadata. It does not connect to an application's database or decrypt
application credentials to monitor it.

The Odoo driver implies a database. For web lanes, Launchplane examines the
context and exact-lane runtime records, tracked target overrides and the managed
secret bindings selected by the same selector as runtime delivery. Global and
other-site credentials, disabled bindings, other integrations and retired keys
do not imply dependencies. Standard database keys (`DATABASE_URL`, `DIRECT_URL`,
`DB_HOST`, `DB_NAME`, `DB_PASSWORD`, `POSTGRES_*`, `ODOO_DB_VOLUME`), email keys
(`SMTP_*`, `RESEND_*`, `SENDGRID_*`, `MAILGUN_*`) and cache keys (`REDIS_URL`,
`REDIS_HOST`, `MEMCACHED_URL`, `MEMCACHED_HOST`) imply their dependency kind.
These are setting names, never credential values. An unrecognized service or
setting needs an explicit override; Launchplane cannot infer an arbitrary
application's semantics from a secret's value.

The lane's `health_monitoring.dependency_health.override`, when present, replaces
the derivation. It requires `dependencies` and a nonblank `reason`; an empty list
means **none**. Removing the override restores derivation. Use overrides for
nonstandard keys or documented exceptions, rather than maintaining a duplicate
list for every product. Current expectations and their source are exposed in
the environment's health-monitoring read model. Missing metadata capabilities
produce `unavailable`, not a claim that the product has no dependencies.

## Public response

One JSON member carries the contract:

```json
{"dependencies":{"database":"pass","email":"fail"}}
```

Each dependency reports only the literal `pass` or `fail`. Never include errors,
addresses, versions, credentials or application data in this member. The existing
runtime-identity contract can remain beside it. A dependency result must come
from a real bounded operation: for example a read-only query on the selected
application database, or authenticated provider readiness for email. Configured
credentials alone do not prove readiness; a readiness check also does not prove
end-to-end email delivery. Health checks must not send messages or write data.
An application with none returns `{"dependencies":{}}`.

The probe retains only results for expected dependency kinds. A reported failure
makes the probe fail even on HTTP 200. An absent, malformed or unsupported result
is **not covered**, never a dependency pass. Failed HTTP and identity checks stay
failed independently of dependency results. Raw endpoint exceptions and extra
properties are not stored in dependency evidence.

## Move products individually

Existing lanes default to `mode: "informational"`. Their dependency evidence
still says **not covered** when missing; existing HTTP behavior and alerts are
preserved. A reported dependency failure is a real failure in either mode.

1. Deliver a conforming product health route through that product's normal
   release process. Verify the real operation, failure and timeout behavior.
2. Review that lane's current derived requirements or an explained override.
3. In the environment's Health monitoring editor, select **Require dependency
   coverage**, review and apply the change with a reason. The service
   `/v1/product-profiles/health-monitoring/apply` dry-run/apply flow accepts
   `dependency_health: {"mode":"required"}` in its request, alongside the current
   check configuration. Adoption participates in the plan digest and existing
   authorization and concurrency checks; this feature creates no new grant.
   The editor preserves any existing override; explicit overrides can be supplied
   through this same reviewed service API.
4. Read back the lane policy and a natural probe. Missing coverage now fails with
   `dependency_not_covered`; reported failures use `dependency_failed`. Before a
   conforming observation exists, the read model cannot reuse an older HTTP pass
   as required dependency coverage.

Do not bulk-enroll products when deploying Launchplane. VeriReel conformance is
separate product work; reuse its existing database check, rather than adding a
second one. Other products follow through their own issues and releases. To undo
adoption, review/apply `mode: "informational"` through the same service flow;
the coverage gap remains visible. These are live monitoring configuration
changes and retain the normal authorization and Director boundaries.

## Shared Node web helper

[`helpers/generic-web/dependency-health.mjs`](../helpers/generic-web/dependency-health.mjs)
works with standard Web Responses, including Next.js route handlers. Vendor the
helper into the product's own source at adoption; products build from their own
files and do not call Launchplane or fetch build inputs from it.

```js
import { dependencyHealthResponse } from "./dependency-health.mjs";

export async function GET() {
  return dependencyHealthResponse({
    database: async signal => {
      await queryApplicationDatabase({ signal, timeoutMs: 800 });
      return true;
    },
  }, { timeoutMs: 1000 });
}
```

`queryApplicationDatabase` represents the product's existing client operation,
not a helper-provided client. Callbacks return exactly `true` for success.
Exceptions, other values and timeout become `fail`. Checks run concurrently;
the helper aborts the signal and bounds the response wait. Clients must also
honor cancellation and configure their own query/network deadlines: JavaScript
cannot forcibly terminate a callback that ignores its signal. Responses use
HTTP 503 on failure and `Cache-Control: no-store`. Existing handlers needing
runtime identity can use `dependencyHealth()` and attach that contract beside
their existing identity. Do not cache the route in the framework or proxy.

## Odoo built-in route: limited database-server check

Upstream [Odoo 19](https://github.com/odoo/odoo/blob/19.0/addons/web/controllers/home.py#L152-L167)
and [Odoo 18](https://github.com/odoo/odoo/blob/18.0/addons/web/controllers/home.py)
implement `/web/health`. By default it returns a pass without opening a
database connection. `/web/health?db_server_status=1` opens and closes a cursor
on the PostgreSQL **postgres maintenance database** and returns HTTP 500 on a
connection error, exposing only status and a boolean.

That is server connectivity, **not a query against the selected application
database**, its registry, schema or permissions. It also uses
`db_server_status`, not the dependency contract above. Launchplane therefore
does not translate a plain built-in route's HTTP 200 into database coverage.
It remains not covered for required application-database readiness. Existing
shared Odoo database-bound readiness work (odoo-docker#113 and Launchplane#3268)
owns the product-database route and its activation; this change does not add
another Odoo addon or enable it on any lane. At Odoo adoption, verify that
route's database target and adapt its safe response to this contract in the
existing shared implementation.
