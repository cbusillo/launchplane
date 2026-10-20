# Sync-cycle health observations

Existing service-owned public/private health probes retain an optional
`sync` projection. It contains `status: recorded` with typed `cycle` facts, or
`status: malformed` with no cycle. Products without a `sync` member keep their
existing response shape. This projection adds no endpoint, connection, monitoring
policy or sync execution.

Workflow-supplied stable-verification health payloads do not retain this
projection. They cannot supply the service-observed sync evidence.

The accepted cycle states are `idle`, `running`, `success`, `failed`, `unknown`
and `unavailable`; modes are `full` and `incremental`. Active/completed cycles
require a UUID identity, mode and timezone-aware start. Completed cycles require
an ordered finish. UUID hex and canonical hyphenated forms are accepted. A
heartbeat must not precede the start; the producer may record its final
heartbeat just after its finish with a separate clock read.

The projection retains the producer's stale/overdue flags, closed freshness
state and stale threshold, including a zero threshold. It validates their state
agreement without inventing a freshness policy. Errors, model names, business
rows/counts, endpoints, credentials and unrecognized properties are excluded.
Malformed input returns only the closed malformed state, without echoing input
or validation errors.

Monitor storage keeps the cycle beside its original observation time, check
and runtime-identity result, preserving existing material evidence when present.
Environment health-check readback uses
that same latest probe, including failures; it never borrows sync evidence from
an older passing record. Reading a record does not renew its time. Sync evidence
does not override HTTP, identity or dependency failures or introduce a new alert
rule.

`recorded` means that the product supplied well-formed facts. It does not prove
current freshness, checkpoint advancement, complete data, consumer queries,
successful cutover or retirement eligibility. A source can be running and ready
without a completed incremental cycle. The current source's successful cycle
witnesses its normal checkpoint-save path; the payload exposes no separate
checkpoint field or database-incarnation identity.

The retirement consumer must still verify exact current publication/runtime and
authority, derive the observation window from successful cutover, reject future
or stale evidence, distinguish a subsequent completed incremental cycle from
bootstrap/replay, and require actual consumer reads, accepted data validation,
current protection/recovery and exclusive resource custody. The monitor's
`observed_at` is the probe's service timestamp; it can precede a cycle that
finishes while the HTTP request is running. No clock tolerance or eligibility
decision is introduced by this observation contract.
