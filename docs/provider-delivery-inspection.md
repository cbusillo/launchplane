---
title: Provider Delivery Inspection
---

Ordinary guarded delivery requires a current, independently observed provider
protection result. Launchplane refreshes that evidence on demand during ordinary
admission and finite-job continuation, using the same engineering delegation.
There is no separate client preflight, public refresh route or inspection daemon.
Owner preview acceptance remains a separate gate and grants no operating power.

This document describes the source contract approved in issue #2369 under
#2240. Installing source does not configure an inspection App, change a provider
protection, enable an ordinary rule, or start a worker. Runtime qualification
and activation remain separate from source and deployment evidence.

## Independent inspection authority

The inspection App is separate from the ordinary delivery App. Resolve its App
identity from Launchplane's DB-backed service runtime environment and its exact
current private-key binding/version from managed secrets. There is no ambient
GitHub token, owner key, workflow secret or ordinary-custody fallback. Missing or
ambiguous durable configuration stops before inspection reservation or charge;
a transient database failure does not establish that a binding was removed.

The installation token covers exactly one repository and requests
`administration:write`, `contents:read` and implicit `metadata:read`. GitHub
requires ruleset-write visibility to return complete bypass actors. This is a
write-capable credential even though the inspection adapter permits only its
fixed diagnostic GETs. Giving that permission to the constrained delivery App
would let it alter the protections that constrain it, so its existing permission
ceiling stays unchanged.

The adapter verifies the App, installation, owner, repository, permissions and
token expiry before using a token. It bounds evaluated rules, applicable ruleset
reads, inherited visibility and classic branch-protection reads. Missing fields,
redacted bypass actors, unsupported rule semantics and incomplete pagination
never mean that protections are absent or satisfied. Provider payloads, keys
and token values are not ordinary-client diagnostics or receipt content.

## Governed protection expectation

`MergeTrainRepositoryPolicy.provider_delivery_protection_expectation` is an
optional strict versioned contract. When omitted, existing policy serialization
and digests are unchanged and the ordinary inspection capability is unavailable.
The field is separate from A5's authorization-policy schema transition.

Only the existing `managed-merge-train-policy-import` lifecycle may change the
effective active expectation: proposal, human approval, worker reauthorization,
locked apply and readback. Raw writers and bare compare-and-swap cannot add,
replace or remove it, including a same-ID active-to-superseded overwrite. The
storage transaction must prove the actual stored executing operation, current
outer worker reservation, exact candidate/predecessor and request fingerprints;
caller-supplied route strings are insufficient. Filesystem rehearsal cannot
supply this governed mutation proof.

The expectation names exact status checks and their positive App IDs, an
explicit code-scanning tool list, exact native pull-request review semantics and
allowed merge methods. An empty scanning list or `pull_request: null` requires
complete provider proof of absence. Native approval counts may be zero through
six; this contract invents no additional human review requirement. Allowed merge
methods remain explicit when native review is absent and must include `merge`.
The existing Launchplane engineering and Owner gates remain independent.

The first supported writer restriction has exactly one applicable ruleset
containing `update`. It contains only that rule and has exactly one Integration
bypass: the ordinary delivery App, in `pull_request` mode. Every gate-bearing
ruleset has an explicitly empty bypass set. Required technical, deletion and
non-fast-forward protections must also hold. Classic protection can contribute
compatible checks or reviews, but does not establish exclusive writer identity.
Unknown rules, incompatible merge modes or another update-bearing ruleset cannot
produce ready evidence.

The observation certifies the selected base branch. It does not certify the
candidate-ref namespace or substitute for actual proof that the configured
update bypass permits Launchplane's PR-native merge operation.

## Automatic demand, budgets and pacing

Authenticated exact admission replay returns the stored job before checking
freshness. A new request first undergoes a pure non-provider authority, scope
and budget precheck. Only a typed refresh-needed result can invoke inspection
outside the admission transaction. Inspection independently rechecks those
prerequisites and resolves the profile and expectation before reserving work.
Admission then reenters once with the same immutable intent and current locks.

A winning generation needs two units of current action headroom and atomically
consumes one existing delegation action, with its lease revision, and zero PRs.
This leaves headroom for the requested guarded action; it does not reserve that
future capacity against concurrent jobs. A concurrent consumer can still cause
the later admission to return `budget_exhausted`. Cache hits, followers, exact
replay and retries in one generation add no charge. Inspection generations do
not consume or enlarge the separate PR-head-refresh allowance.

Worker continuation honors history, cancellation, terminal state, custody,
provider waits and pending-read deadlines before inspection. An observation of
an already-dispatched effect needs no refresh. New work ensures readiness after
policy/inventory validation and before controller work; head-refresh rebind has
its own ensure point before controller acquisition. No manual ID or reason is
needed to renew an expired observation.

Each charged generation permits at most three provider/custody attempts.
Provider `Retry-After` is interpreted against server time, bounded and preserved
as pacing. The ordinary caller receives only closed, redacted same-target reason
codes and a bounded retry interval. Provider capability or permission failures
are HTTP 503; denial of the caller's own authority remains HTTP 403.

## Custody, publication and currentness

The logical inspection flight is repository-wide, across branches and key/App
rotation. It begins at reservation and ends only with atomic terminal history
publication and release. Revoking or expiring the token alone does not release
that flight. Another branch waits and then obtains evidence for its own target.
Network work never runs while database locks are held.

One database reservation/start instant anchors 45 seconds of provider work,
cleanup ending by 55 seconds and a 60-second publication-eligibility fence. A
finalizer checks fresh database time after acquiring its locks. After the fence
it may record capability failure and settle custody, but cannot publish ready.
These are work and publication bounds, not a global database timeout or a
whole-handler latency guarantee.

Live or unknown custody remains fenced through confirmed revocation or its
conservative token-lifetime/skew bound, even beyond 60 seconds. Once custody is
closed or known expired, a later demand can recover an abandoned flight through
an expected-state/revision CAS. Terminal history and flight release commit
together. A stale finisher cannot replace the recovered result or overwrite a
newer observation. Recovery cannot mint a token under expired authority.

Receipts use a 300-second window anchored before the first provider fact read;
cleanup and delayed finalization do not extend it. Completed observations have
two classes:

- `protection_conclusive`: ready, semantic negative/drift and semantic
  inconclusive provider observations. The latest repository completion sequence
  shadows earlier conclusive evidence, so older ready evidence cannot hide a
  newer negative or unknown protection fact.
- `capability_unavailable`: transport, permission, deadline, pacing, custody,
  exhaustion, recovery and late-positive failures. These pace and audit work;
  they do not shadow an unexpired conclusive ready observation.

The consumer independently resolves current inventory, immutable installed
setup, delivery identity, inspection profile, secret binding, selected governed
expectation and merge-policy semantics. Caller/session/rule provenance and
unrelated containing-policy history do not become receipt authority. Positive
ordinary qualification remains a separate current gate. S2 retains inspection
audit history and adds no automatic deletion.

## Guarded effect boundary

Fresh readiness is checked at admission, controller acquisition, semantic
dispatch checkpoint, completion without dispatch and new-effect/rebind
reauthorization. Each check precedes its caller's writes. A 30-second residual
margin helps new orchestration make progress but cannot eliminate a scheduling
pause or expiry race.

A refresh deferral with an acquired controller returns paced waiting only after
durable controller yield succeeds. Yield failure propagates. A rejected
semantic checkpoint creates no child and sends no provider mutation. Once a
child has committed under current readiness, execution follows the existing
send/unknown-outcome contract; no inspection is inserted between that checkpoint
and the send and no unconfirmed effect is abandoned because the receipt expired.

Only actual negative/drift evidence or durable selected-profile/expectation loss
can project `readiness_lost`, atomically with its activation event. Capability
failure, expiry, wait, custody uncertainty, semantic inconclusive evidence and
caller-scoped budget/authority denial do not project protection loss.

Before runtime activation, prove the exact Apps and bindings, governed
expectation and applied/read-back protections, repository feature support,
candidate-ref compatibility, positive qualification, A5 policy enablement,
guarded derivation and revoke/readback rollback. Qualify the real client's
timeout and deployed synchronous capacity against provider/cleanup budgets plus
measured database/service overhead. Starting the ordinary worker requires its
separate reviewed no-claim check and explicit activation authority.
