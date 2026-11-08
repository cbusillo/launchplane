---
title: Preview Memory Admission
---

# Preview memory admission

## Idle previews

Service-issued Odoo previews without upstream connections put their public route
on a small router from the same immutable image. It receives the preview generation
identity, with no application environment file, data mount or source credential.
The existing network layout stays the same. A private Unix socket lets Launchplane
read activity and coordinate sleep through its existing provider transport.
There is no public control endpoint or outbound wake callback.

A directory-only `DATA_WORKFLOW_SSH_DIR` setting does not establish a source
connection. Both binding and native runtime inspection use the same
source-connection predicate; upstream settings and other
SSH settings still refuse sleep. Native inspection continues to require exclusive
volumes, private application resources and no credential bind mounts.

The gateway keeps ordinary HTTP and Odoo websocket traffic on separate
listeners. The preview's host-scoped `/websocket` route uses Odoo's evented
listener when workers are enabled and the HTTP listener in threaded mode.
Both paths share the same activity and sleep accounting, so an open websocket
prevents idle sleep. Neither listener publishes a host port. Initial previews
that still need a source connection route websockets directly to their web service using the same
worker-mode selection; they retain their existing refusal of idle sleep.

After fifteen minutes without traffic and with no open connections or active
runner execution, Launchplane freezes forwarding and stops web, the runner and
PostgreSQL. It retains their containers, database, attachments and overlay source.
A visitor sees a page that checks again while Launchplane starts the preview.
A waiting non-GET request is forwarded once after wake, or receives a temporary
failure if startup exceeds its bounded wait. The router never replays it.

Sleep and wake use the existing preview executor and product locks plus the
host project lock. They are best-effort, idempotent operations without durable
provider reservations or retained intents. Each pass observes the current ready
generation, gateway receipt and service state. A partial stop finishes on a later
pass, after any failure backoff, before admitting a requested wake. This lets
the next wake measure headroom after the stop completes. An admitted partial
wake uses the gateway's persisted receipt as its idempotency key while a recent
visitor is asking. When visits stop for the idle interval, an unfinished wake
returns to sleep. A router or scheduler restart needs only another observation;
a lost successful completion response preserves the running services.
A failed stop/start backs off that preview for five minutes in memory, allowing
other previews and ordinary refresh/destroy operations to proceed; no retry
intent or budget is stored. Busy host locks and failed observations retry on the
next pass. Manual stops, staff testing holds, changed generations, other
provider reservations and unavailable custody still block idle effects.

Wakes serialize on the host and check the configured memory headroom threshold
and disk reserve before starting stopped services. PostgreSQL starts first,
followed by runner and web. A wake keeps the existing executor and host startup
locks for one attempt, with one shared five-minute health deadline. Docker
reporting a stopped, restarting or unhealthy service fails the attempt at once.
The gateway opens only after all services pass health and the exact runtime
identity agrees. Insufficient capacity leaves the preview asleep; a later pass
checks capacity again. The gateway's wake receipt identifies recovery; any
attempt that still needs to start stopped services checks current memory and
disk again. If admission refuses a partial wake, its running services stop so
it releases their memory before the next pass. It does not reserve capacity
across an interruption. A preview that
never becomes healthy can be refreshed or destroyed through the ordinary
preview path. Data-preserving refreshes also credit their own running services
after matching the current target's identity; cold starts still meet the memory
floor.

Gateway receipt IDs keep the existing wire format for routers already deployed;
they are opaque correlation IDs and no longer refer to database reservations.
Refresh and destroy use their own ordinary recovery paths and have no idle
supersession special case.
Quiet polling does not take the preview executor lock or perform a host-wide
storage inspection. Full qualification runs under exclusion before effects.

Product Ops shows each provider-observed preview's last sleep observation from
the existing inventory collector: awake, asleep, cannot sleep with a reason,
or unknown when observation is unavailable.
The collector includes previews without a successful lifecycle record and
drivers without idle support. It reads gateway status without visiting the
application or requesting a wake. The lock-free poll never takes the host's
preview operation lock. Provider reads and polls use bounded timeouts and an
aggregate per-product budget derived from the inventory cadence, with shutdown
checked between previews. The existing scheduler slot rotates the first preview
on consecutive scans so overflow does not always exclude the same tail; no
cursor is stored. Read
failures and exhausted budgets report unknown without provider exception text.
Missing or expired evidence cannot establish a sleep state. Individual observations require the
same `preview.read` authorization as individual preview identities.

Older Odoo previews acquire the gateway on their next ordinary, identity-bound
rebuild that preserves their data. Retained upstream settings still prevent
sleep and are shown as a source-connection reason even with a gateway. A first upstream restore still needs its
dedicated source mount and cannot use that gateway. No inventory scan retrofits
or replaces an existing preview. People using a preview and unique working-copy
custody remain protected; obtain their or the Director's word before a rebuild
or retirement. Closed changes use the existing guarded lifecycle retirement.

The coverage invariant is that every provider-observed preview has an explicit
sleep observation or an explanation of missing coverage. Serving-generation
health alone never proves idle coverage. Observation failure
does not block a deployment or trigger cleanup. Existing source-connection,
custody, ownership, memory-floor and runtime-inspection refusals continue to
block sleep or wake; rebuilds keep their normal deployment and health gates.

## New preview admission

The DB-backed runtime-environment record with `scope=context` and
`context=launchplane` may set
`LAUNCHPLANE_PREVIEW_HOST_MEMORY_FLOORS` to a JSON object mapping provider server
IDs to positive integer byte floors, for example
`{"synthetic-execution-host":5368709120}`. Configure real server IDs and values
through [service settings](service-runtime-settings.md). No process
environment, checked-in host list, or numeric default supplies this policy.
An absent key or an empty object selects no protected hosts; deploying this
source alone does not activate a floor. Unreadable records or malformed policy
refuse new allocation. Only this service scope supplies the host policy, so
product or instance overlays cannot lower it. Global and product records do not
activate the floor or distribute this policy to application runtimes.

For each selected host, the existing registered-server terminal reader runs a
fixed read-only memory observation immediately before provider creation. It
uses the smaller of Linux `MemAvailable` and the remaining bytes under every
visible cgroup-v2 memory limit, counting anonymous, shared and kernel memory
through `memory.current`. A guest can hide the parent limit behind its cgroup
namespace. When the visible root has memory controller files, even with a
numeric `memory.max`, the reader requires a
verified `fuse.lxcfs` mount for `/proc/meminfo` and also takes the smaller of
virtualized `MemFree` and `MemTotal` minus the root's entire `memory.current`.
It refuses an unverified unlimited namespace instead of treating physical-host
spare RAM as guest headroom. A physical host's real root lacks memory limit and
usage files and uses its own available RAM only when `cgroup.controllers`
confirms memory-controller availability. Missing controllers or partially
missing files refuse the read.

This follows the [kernel's cgroup accounting](https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html)
and [LXCFS's virtualized memory view](https://github.com/lxc/lxcfs/blob/main/src/proc_fuse.c).
The [retained guest observation](https://github.com/cbusillo/launchplane/issues/4468#issuecomment-6343109854)
compared the earlier probe with its enforced parent cap. Namespace and controller
refinements are covered by fixtures; those tests are source proof, not a fresh
live qualification. LXCFS must represent the selected guest's effective limit
and usage; its mount alone cannot qualify hidden shared-ancestor usage. Missing
or invalid observations refuse admission; a provider authorization denial
retains its original identity. No alternate transport or credential is selected.

At or above the floor, creation continues. Below it, the public refusal names the
configured floor and observed current headroom in bytes; an unavailable read
names the floor and marks headroom unavailable. A later request can retry after
headroom recovers. Refusal precedes provider allocation and never stops or
evicts running workloads. It does not change testing or production admission,
host limits, swap, or preview teardown.

The Odoo and generic-web HTTP APIs return `409` with error code
`preview_memory_admission_refused` and preserve the refusal's floor and headroom
details. An unused idempotency key remains retryable. Event-driven reconciliation
records the same code and numeric details without exposing provider text. Its
next attempt can retry the same verified build after headroom recovers.

The observation is a point-in-time admission check, not a memory reservation or
a measured preview peak, and does not establish a safe concurrency count for
heavy rehearsals.

The chosen budget counts total cgroup usage, including inactive file cache.
It does not assume that cache can be reclaimed. Cache-dominated usage can
therefore defer new previews conservatively.
