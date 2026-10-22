---
title: Preview Memory Admission
---

# Preview memory admission

The DB-backed runtime-environment record with `scope=context` and
`context=launchplane` may set
`LAUNCHPLANE_PREVIEW_HOST_MEMORY_FLOORS` to a JSON object mapping provider server
IDs to positive integer byte floors, for example
`{"synthetic-execution-host":5368709120}`. Configure real server IDs and values
through the existing service runtime-environment settings surface. No process
environment, checked-in host list, or numeric default supplies this policy.
An absent key or an empty object selects no protected hosts; deploying this
source alone does not activate a floor. Unreadable records or malformed policy
refuse new allocation. Only this service scope supplies the host policy, so product or instance
overlays cannot lower it. Global and product records do not activate the floor
or distribute this policy to application runtimes.

For each selected host, the existing registered-server terminal reader runs a
fixed read-only memory observation immediately before provider creation. It
uses the smaller of Linux `MemAvailable` and the remaining bytes under every
visible cgroup-v2 memory limit, counting anonymous, shared and kernel memory
through `memory.current`. A guest can hide the parent limit behind its cgroup
namespace. When the visible root has `memory.max=max`, the reader requires a
verified `fuse.lxcfs` mount for `/proc/meminfo` and also takes the smaller of
virtualized `MemFree` and `MemTotal` minus the root's entire `memory.current`.
It refuses an unverified unlimited namespace instead of treating physical-host
spare RAM as guest headroom. A physical host's real root lacks memory controller
files and uses its own available RAM; partially missing files refuse the read.

This follows the [kernel's cgroup accounting](https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html)
and [LXCFS's virtualized memory view](https://github.com/lxc/lxcfs/blob/main/src/proc_fuse.c).
The fixed probe was qualified read-only on the execution guest and compared
with its enforced parent cap; the observed guest namespace and virtualized
memory view are covered by fixtures. Missing or invalid observations refuse admission; a provider authorization
denial retains its original identity. No alternate transport or credential is
selected.

At or above the floor, creation continues. Below it, the refusal names the host,
configured floor and observed current headroom in bytes; an unavailable read
names the floor and marks headroom unavailable. A later request can retry after
headroom recovers. Refusal precedes provider allocation and never stops or
evicts running workloads. It does not change testing or production admission,
host limits, swap, or preview teardown.

The initial CT106 activation should record **5 GiB (5368709120 bytes)** against
its actual provider server ID, derived from
[incident accounting PR #629](https://github.com/cbusillo/shiny-infra-ops/pull/629):
19 GiB total cgroup current under the existing 24 GiB cap leaves 5 GiB.
[Director decision Q654](https://github.com/cbusillo/shiny-infra-ops/issues/628#issuecomment-6342021925)
approves this preview-start floor. Activation is a separate service-record
mutation; this source change performs none. The observation is a point-in-time
admission check, not a memory reservation or a measured preview peak, and does
not establish a safe concurrency count for heavy rehearsals.
