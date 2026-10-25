---
title: Preview Memory Admission
---

# Preview memory admission

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

The initial CT106 activation should record **5 GiB (5368709120 bytes)** against
its actual provider server ID, derived from
[incident accounting PR #629](https://github.com/cbusillo/shiny-infra-ops/pull/629):
19 GiB total cgroup current under the existing 24 GiB cap leaves 5 GiB.
[Director decision Q654](https://github.com/cbusillo/shiny-infra-ops/issues/628#issuecomment-6342021925)
approves this preview-start floor. Activation is a separate service-record
mutation; this source change performs none. The observation is a point-in-time
admission check, not a memory reservation or a measured preview peak, and does
not establish a safe concurrency count for heavy rehearsals.

The chosen budget counts total cgroup usage, including inactive file cache.
It does not assume that cache can be reclaimed. Cache-dominated usage can
therefore defer new previews conservatively; live qualification must distinguish
that availability tradeoff from the incident's mostly shared-memory pressure.
