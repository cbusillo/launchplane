---
title: Private testing HTTPS and WSS through an existing edge
---

A no-website generic-web service can use an existing TLS edge, including NPMplus,
for its private testing HTTPS `/health` and WSS `/agent-session/connect` authority.
The edge keeps its existing certificate, private key and renewal. Application
lanes use their ordinary application edge. The Compose path publishes the testing
runtime's HTTP port on one RFC1918 interface; neither path generates a TLS
sidecar, certificate mounts, private issuer or client trust-store changes.
The product still owns its immutable runtime template and build.

## Application testing lanes

A conventional generic-web application lane uses its recorded non-production
server's existing HTTP edge. It does not publish a host port or use the Compose
activation below. `POST /v1/products/{product}/testing-private-ingress` supplies
`dry-run`, reviewed `apply`, and independent `readback` for the Director agent.
The request names only `context`, `instance=testing`, the active private health
`endpoint_key`, the existing `edge_endpoint_key`, positive `access_list_id` and
`certificate_id`, `operation=create|disable`, and `reason`. Apply/readback also
require `reviewed_plan_sha256`; apply retains its original Idempotency-Key.

The service derives the one hostname from that lane's active HTTPS `/health`
endpoint and the application/port from its product and target records. It checks
exclusive lane and application ownership, actual server placement against the
edge record, the existing wildcard certificate's coverage and expiry, and a
read of the named existing access list before effects. The access-list read
does not expand authentication entries; the plan digest covers its non-secret
metadata. It
creates the application's HTTP hostname and its own edge route; it does not
rewrite existing application domains or an unrelated edge route. A successful
receipt owns later disablement. The active private endpoint is the hostname's
recorded claim; the public-host write guard also checks that claim before any
provider effect for another lane. Successful creation or original-key adoption
also records it in the application's `domains`, under the existing name fences,
without making it a public website. The edge retains its certificate, key, renewal
and access list. The application owns its HTTP paths and bearer checks; the edge
preserves authorization and WebSocket headers. Provider readback retains the
access list, certificate and complete route configuration. No DNS operation is performed.
The existing scoped ingress planner also accepts an active testing private
endpoint hostname when an access list is supplied; public-route rules remain.

The edge-to-application hop uses the same plaintext LAN HTTP transport as other
application testing lanes. Q453 accepted this topology instead of the
Compose-only upstream binding and its host firewall requirement. It does not
change the restrictions on the Compose path below or any production record.

Independent application activation can preserve an existing private hostname
through its [native same-address route continuation](testing-activation.md).
The original edge marker, certificate, access list and provider domain remain
unchanged. The canonical replacement has no copied provider domain or managed
public route binding. Private health and a create/readback no-op verify the
original receipt, completed activation lineage and actual winning native route.
A second generation reuses that original template. An unhealthy predecessor does
not require hand repair: exact physical custody and independent candidate/private
edge identity are still required. Endpoint, edge and current receipt updates and
new private-ingress reservations share the existing publication authority fence.
A concurrent unresolved private operation stays unknown; activation never
re-enables an intentionally disabled edge. Actual authenticated WSS and product
qualification remain separate post-delivery proof.

### Continue a freshly allocated application

Keep the `isolation_staging` hold through endpoint and private-route preparation.
After testing inputs are configured, use the existing `testing-hold-dry-run` and
`testing-hold-apply` helpers with `--lift --purpose isolation_staging` and the same
product, context, testing instance and audit reason. Save the dry run, review its
digest, then apply with that evidence and a stable original idempotency key.
Put the helper's global `--timeout 360` before the apply subcommand so its
300-second bounded host inspection can finish and return its receipt.

The original Director agent can lift only its own staging interlock on the
fresh conventional application and lane-only volume it already allocated. The
service resolves that caller's completed allocation receipt itself and checks
current exclusive ownership, placement, empty source/environment, sole mount and
absence of deployment, protected use, needed data, aliases and uncertain effects.
The original environment and project IDs and their inherited environment inputs
must also match the empty allocation. The application must retain its vanilla
single-replica runtime: no custom Swarm options, networks, published ports,
execution, registry bindings, security or redirects. Nullable Swarm options must
stay null, since even empty objects override provider defaults. Supported testing
domain setup remains available: either no provider domain or the lane's one exact
private HTTP application origin at the runtime port, with root paths and no TLS,
middleware, authentication or entrypoint override. The hostname must remain
exclusive of other lanes' domains, public hosts and pending hostname effects.
Dockerfile/build-path settings skipped by
Docker-source deployment do not block continuation.
This boundary follows Dokploy's
[application defaults](https://github.com/Dokploy/dokploy/blob/e0c8d80cdea15a4ac3fe495a6ad38e043b4b80b5/packages/server/src/db/schema/application.ts)
and [runtime construction](https://github.com/Dokploy/dokploy/blob/e0c8d80cdea15a4ac3fe495a6ad38e043b4b80b5/packages/server/src/utils/builders/index.ts),
including the [domain defaults and API fields](https://github.com/Dokploy/dokploy/blob/e0c8d80cdea15a4ac3fe495a6ad38e043b4b80b5/packages/server/src/db/schema/domain.ts).
Staff, foreign, legacy and Compose interlocks keep their existing protections.

Dry-run remains an inert hold plan and does not qualify provider readiness.
Apply runs a fixed read-only
inspection through a disabled manual job on the recorded server before clearing
the hold. The allocated Docker volume and its backing directory must still be
absent, and no running or stopped container may consume the allocation or overlap
its storage through a writable path. Administrative read-only parent mounts are
allowed; exact allocation paths and children remain protected consumers.
Existing volumes, incomplete inspection and drift remain held for
separate recovery; this path creates or deletes no storage. The hold and replay
receipt commit atomically. Keep the original key after an uncertain reply.
The disabled inspection job reuses one per-lane slot on the recorded server.

A staging lift requests no reconcile and performs no workload deployment. The
normal testing event/sweep then delivers managed inputs and the verified build
through the existing deployment gates. Standalone runtime sync remains subject
to its normal qualification; do not bypass a refusal. Actual runtime identity,
health, monitor cadence and Chris's authenticated receiver proof remain separate.

For a generic-web application testing target, that ordinary deployment resolves
the site's context, exact lane and target settings with its site-scoped managed
secrets. Global settings, other lanes and Launchplane worker credentials do not
reach the application. Declared inputs, managed-secret provenance, key safety and
explicit provider-key retirements are checked before writing. The existing
provider operation writes the environment, verifies its readback, then installs
and deploys the verified image. Platform credentials left in provider-only keys
must be explicitly retired through product configuration before delivery.
Input preparation reads the provider environment and checks the complete merged
environment for platform credentials and provider-format fidelity before
schedules are quiesced. The same checks run again immediately before writing.
Dokploy environment text uses its consumer's dotenv grammar. Quoted multiline
values, literal hashes and quotes, surrounding whitespace, backslashes and
ordinary dollar signs are preserved, including provider-only keys retained by
the merge. Quoting does not disable Dokploy's own `${{...}}` substitutions;
literal managed inputs containing those references are refused before effects.
Supply the effective value through the site's managed inputs instead of a
provider template reference, or explicitly retire an unused provider key.
NUL and values with no lossless dotenv representation are also refused without
a write or a value in the error. For example, dotenv does not unescape quote
delimiters, so a multiline value containing all three quote delimiters may not
be representable. This is the shared provider storage codec. It qualifies
application delivery, where Dokploy passes the parsed values to the container.
Compose uses a separate two-stage wire codec, including for Odoo target
replacement and fresh testing candidates. It preserves new or changed literal
inputs through Dokploy's generated `.env` and final Compose consumption; it no
longer imposes the former single-line/no-trailing-whitespace restriction.
Untouched legacy values keep their existing effective meaning. See the
[environment wire contract](dokploy-service-deployments.md#environment-wire-formats)
for preservation, provider edits and actual-consumer qualification.
The application consumer follows Dokploy's
[dotenv parsing and service substitution](https://github.com/Dokploy/dokploy/blob/1c4414165d4aedbf6e28afab7a94c8fb7c053373/packages/server/src/utils/docker/utils.ts)
and the [dotenv value grammar](https://github.com/motdotla/dotenv/blob/v16.4.5/lib/main.js).
A later refusal restores or holds schedules using the original lifecycle
receipt and records the failed attempt. Failure publication retains the last
provider-effect phase. An interruption after input readback remains retryable
under the original operation once provider observation proves no deployment.
If its captured inputs have changed, or a newer verified image supersedes it,
the original lease settles the obsolete attempt as failed only with exact
pre-image proof on the original target. It restores its captured schedule
baseline through verified provider writes, or preserves an inherited hold on
an earlier unqualified image. A proved retry also retains that inherited hold;
only successful qualification or proved rollback may reactivate its schedules.
Settlement retains the old input receipt as
failed-attempt evidence. A durable settlement phase resumes cleanup after a
lost reply, even if inputs subsequently revert. The current image and inputs
then use a fresh operation. Returning to a settled operation's input identity
uses a deterministic successor bound to that terminal predecessor.
An ambiguous image-save phase, unknown provider observation, changed target,
missing baseline or lost lease keeps the original operation fenced. Environment
and registry preparation are journaled separately from image save and deploy;
an environment-only failure can restore schedules without treating it as an
unqualified image.
The existing generic-web deploy-recovery service supports application targets.
Its `retry_original_operation` action resumes the original controller, which
may restore and settle an obsolete pre-image attempt as failed instead of
deploying it. The reviewed recovery digest binds current input authority and
that disposition; changes between dry-run and apply require a fresh review.
The ordinary testing reconciler uses the same settlement automatically.
For in-place applications, image completion alone does not prove maintenance,
canary, activation or product-executor completion. Original execution receipts
must prove every dispatched effect terminal before a reservation can finish or
become eligible for another deployment. Lost replies, active or ambiguous
history, generic provider error rows and a later disabled schedule readback
leave the original operation fenced. A hold cannot prove that an earlier lost
enable request has stopped.

The native deploy-recovery API offers `settle_testing_failure` for attributable
terminal work that did not qualify, including a healthy image. After a fresh
digest review it reacquires the original lease, rechecks the captured evidence,
runs only the captured schedule hold and stores the original failed result.
The recovery envelope returns HTTP 202; the original deploy result remains
HTTP 502. Replay runs no effects. A deliberate normal request with a new key
can then proceed and inherits held originals. Unknown original work remains
held; neither commands nor product data cleanup are retried through settlement.

Schedule completion accepts an exact acknowledged job identifier with complete
matching history marked done, or an original successful synchronous reply with
nonempty complete new history all marked done. A lost reply plus a unique new
row is insufficient. This contract makes no claim about a live provider's
version or capabilities. Product verification records one executor-level
intent and completion. Its synchronous boundary waits for known dynamic page,
signup, server-action and maintenance responses, including response completion
and product assertions. Interrupted dispatched exchanges remain unknown and
skip data cleanup; browser teardown does not prove remote completion. These
boundaries do not promise termination of arbitrary asynchronous product jobs.
Actual browser request events open exchanges, so a local click failure that
dispatches nothing can remain a truthful failure. An observed QR server action
still requires its product success evidence; HTTP 200 alone is insufficient.
Production source admission during an unknown testing attempt is a separate
gate tracked in [#4259](https://github.com/cbusillo/launchplane/issues/4259).
It must account for the current original-operation fence before a fresh
production release; retaining old inventory does not qualify unknown work.

An exact original reservation with no effect checkpoint may settle before any
lifecycle receipt was captured. Its renewed lease verifies that no-effect
evidence again; it restores nothing and keeps the effect journal empty, even
if writing the failed record must retry. A missing baseline after an effect
remains fenced.

A successful deployment retains a keyed, value-free receipt for those captured
inputs. Missing legacy evidence or changed site inputs makes the same verified
image eligible for one normal testing deployment; an unchanged receipt settles
without another deployment. Staff/staging holds and unresolved operations still
prevent dispatch. Recovery preserves only the original readback-verified receipt
and never credits current inputs to an earlier attempt. An old operation without
that receipt can settle, then the next normal sweep repairs its inputs. Removing
a declaration or binding alone does not delete provider-only values; explicit
retirement remains the deletion contract. Production, preview and Compose
delivery paths are unchanged.
Rotating Launchplane's active master encryption key changes the delivery
fingerprint, so each managed-input testing lane receives one normal repair
deployment. Before image save, an in-flight attempt refuses changed input
authority. Once image save returns successfully in that same execution, the
trigger finishes only the captured values and earns only their original receipt;
the next sweep repairs any newer inputs. Current product activity, lane/target
binding and active key safety for the captured safety requirement set still
govern the trigger. Preparation uses the canonical site-delivery selection and
retains each actual supplier's binding identity and secret-record identity,
integration, scope, context, instance and configured status, without version or
audit timestamps or plaintext persistence. Unused global and shadowed bindings
do not become captured suppliers. Current policy checks those actual suppliers
during preparation and before dispatch; an unrelated higher-ranked binding
cannot supply their safety classification. Frozen safety requirements without
a delivered secret still receive their existing fail-closed policy check.
A later declaration or binding on a captured
plain setting does not expand that set. Original bindings must remain active
and bound to the same secrets and routes; deleting and recreating a binding
under its old key cannot authorize the captured payload. Version rotation may
finish the already captured version before the next sweep repairs it. Explicit
provider-key retirement added after image save likewise finishes the captured
dispatch and removes the key on the next operation; current retirement does
not change the saved operation's captured supplier set.

A typed safety refusal after the synchronous image-save reply and before the
trigger checkpoint records `testing_dispatch_refused`. Cleanup preserves this
proof and the original receipt. With current target authority, the renewed
original lease, exclusive product lock and fresh deployment absence, original
operation recovery settles it failed with schedules held. It never restores
schedules, saves an image or triggers a deployment. A fresh canonical operation
can replace the saved image using current inputs; a revoked key already saved
in provider env still needs explicit retirement, never a regrant. Failed cleanup
or record publication can resume this held settlement. Retirement and target
repointing keep the authority stop and permit no old-target cleanup writes.
A crash or uncertain image-save outcome earns no refusal proof and remains
fenced for exact recovery.

### Health probes with public wildcard DNS

An application testing endpoint may retain the existing public wildcard DNS.
The monitor does not follow those public answers or permit arbitrary public
private-health requests. For a testing hostname it verifies the active endpoint,
exclusive application and lane ownership, current provider placement and exact
application domain, an applied private-route receipt, and fresh NPMplus readback.
The enabled route must still have its reviewed access list, current wildcard
certificate and unchanged configuration, with no overlapping edge aliases or
held private-route provider operations. Historical pending receipts from a
completed replan do not hold a new route's probe. Only the latest applied receipt
for the current provider host supplies authority, so a later disable cannot be
overridden by replaying an older enabled receipt. Receipt timestamps preserve
subsecond ordering. Repeated plans preserve their prior applied receipt under a
content-addressed history ID before replacing the plan's pending/readback record;
the history retains its original timestamp. A failed pre-effect retry cannot
erase a later disable and resurrect older serving authority. If preserving that
history fails, the replacement and provider effects do not proceed.
Deleted historical hosts are not queried
as current candidates. Missing or changed current evidence fails the probe.

The health request connects to the existing edge's private HTTPS listener on
port 443. A directly addressed private API still identifies that machine. For
an API hostname on the standard HTTP/HTTPS listener, its authenticated provider
inventory may instead supply one exact, enabled, loaded proxy route with no
aliases, custom locations/configuration or delegated authentication. Its normal
HTTP(S) backend host identifies the private machine; its admin scheme and port
do not become the health scheme or port. Other hosts and wildcard routes never
supply that binding. The exact API route and its uniqueness are checked again
after the probe, along with existing endpoint, receipt and application authority.
Changed API configuration invalidates the result. No new record, credential or
provider write is needed.

The selected machine must resolve exclusively to the existing permitted private
address ranges (RFC1918 or RFC6598), and reach the same edge's HTTPS listener.
Socket addresses are validated and pinned before connecting, while
Host, SNI and ordinary certificate/name verification use the endpoint hostname.
Endpoint DNS is never used to select this socket, so wildcard DNS changes cannot
redirect it; a public or mixed answer for the selected listener refuses.
No DNS record or shared trust store changes. A provider API hosted separately
from its HTTPS listener does not qualify through this transport.

This continuation sends only an unauthenticated `GET /health`, with no request
body, query, API credentials, cookies or redirects. It requires the monitor's
runtime-identity check; HTTP 200 alone cannot qualify an absent or different
runtime. Non-testing endpoints and the generic private HTTP client retain their
existing network policy. Actual edge access-list denial, remote tailnet and WSS
qualification remain separate operational proof; route registration is not
transport proof.

Planning changes no provider state. Apply uses a dedicated lane-private-ingress
provider-operation fence, saves its pending receipt before effects, then records
independent provider readback. Active application-hostname creation also holds
the testing event executor lock, excluding a concurrent event deployment. A lost
final response can be adopted from the same key only when both effects and the
pending receipt match. That receipt binds stable lane/application/endpoint/edge,
runtime port, certificate and access-list identity/policy plus the exact desired
route. Certificate renewal and descriptive metadata updates do not change that
identity; current ownership, placement, certificate validity and access-list
existence are still checked again. Dokploy's absent nullable domain fields may
be null or empty, as in other domain readers.

For a partial create, retry the original apply request, reviewed plan and
Idempotency-Key. The service first observes both providers. If the matching
application domain is either absent or exactly matches the request, and
independent readback proves the edge route is absent, the existing durable
runner reacquires the original reservation and creates only the missing effects.
This also recovers a rejected or lost domain-create request when neither effect
landed. If both exact effects exist, it adopts their
receipt without provider writes. Different keys cannot take over. Unavailable,
foreign or materially changed evidence remains `reconcile_required` on the
private route, without holding the application's deployment reservation. Do not
clear a genuine unknown or repeat it with a new key. A busy executor, missing
provider record or unverified effect retains the prior pending operation's hold
even before that retry reaches its first new checkpoint. A fresh operation with
no pending receipt still rejects pre-effect failures.

A rejected or unlanded enable/disable toggle can also resume with the original
request and key. Readback must prove the exact receipt-owned route differs only
in `enabled`, and the pending receipt must match the stable authority and desired
route. Re-enable additionally requires the application's domain still present;
toggle recovery never recreates a domain removed after planning. The acquired
retry checks the same conditions and the immediate pre-effect snapshot before
repeating only the idempotent toggle. A toggle that already landed is adopted
without another provider write. Changed route configuration, wrong keys and
failed reads retain the hold.

If authority was corrected permanently, or a clean pre-effect rejection left a
pending receipt, retry the original key first. Before any new effects, the
service can complete that key with `applied=false`, `replan_required=true` and
`previous_effects_absent=true`. This requires fresh independent inventories
proving the requested hostname absent from both the edge and the receipt-bound
original application, while fencing that original application's executor. The
receipt retains its original application ID even after the lane binding changes;
legacy receipts without that identity cannot establish absence. A busy executor,
overlapping wildcard/regex or other edge host, existing domain, malformed evidence
or failed read keeps the hold. The acquired original-key retry repeats the proof
under its lease before recording the durable result. Same-key replay returns that
result without provider writes. Only after that result should the caller obtain
a fresh reviewed plan and new key. This uses the same provider read-after-write
assumption as missing-effect recovery; an empty inventory does not independently
prove a timed-out provider request can never land later. A late effect must still
pass strict ownership and payload checks in any subsequent plan.

If the original domain remains present after permanent authority drift, keep the
hold. Supported receipt-bound cleanup and private-endpoint retirement/evidence
invalidation are tracked separately in #4004. No direct provider deletion or
reservation clearing is a supported recovery path.
Legacy hostname spelling normalization across ownership comparisons and
PostgreSQL name fences is tracked separately in #4005.

Disable uses a fresh reviewed plan and the same lane's exact receipt-owned route,
retaining its original endpoint and edge keys. It only toggles that route even
after the old application or edge record disappears, and leaves the application
hostname and bot state in place. For disable only, a historical successful apply
receipt can establish ownership when its host ID and complete current route hash
match. This keeps service cleanup available after an out-of-band re-enable;
it does not authorize probes or create/re-enable, which still require the latest
applied receipt. Disabled routes need no loaded-nginx flag, but
retain the same hidden-config checks; enabled routes must be loaded.
Neither operation deploys a workload
or proves TLS, WSS, remote tailnet access or healthy runtime identity.

Disable and verify the edge route **before** retiring its private endpoint or
product, removing the lane declaration, or assigning the context elsewhere.
Disable requires that current lane authority even when the old application and
edge records have disappeared. It intentionally retains the application's
hostname and domain claim. First-testing setup remains limited to empty targets;
disabling the edge alone does not make an activated target empty or authorize
its replacement.
Private ingress receipts cannot supply managed route-binding evidence, whether
selected automatically or passed explicitly. An active testing private-endpoint
hostname cannot authorize a managed route binding through any receipt, including
an ordinary audit from a generic scoped no-op. Recording the domain does not
declare a public website. Older ship/promotion URL inference can derive HTTPS
health URLs from target domains; that inference is not private reachability
proof. Private probe support and access-list qualification remain prerequisites
for this lane (#3989 and #3777), rather than a reason to run a public probe or
change its access list.

The HTTP surface is projected into the agent/admin contract. Installed caller
support must include this operation before operational use; do not call a raw
provider API or use an arbitrary local CLI as a substitute. After source delivery,
qualify the actual route through its access list from the service and intended
clients, including a remote tailnet peer and public denial. For public wildcard
DNS use the health-only verified-edge continuation above. Do not change DNS,
use a public probe or weaken the generic private-address check as a workaround.

## Endpoint and outbound boundary

A testing private endpoint record accepts an HTTP(S) DNS hostname, including a name
covered by an existing wildcard certificate. Other lane instances retain the existing internal-suffix/non-public-literal
record rule; their credential-bearing consumers do not gain new DNS authorities.
Registration validates URL syntax;
it does not resolve DNS or claim reachability. Existing non-public literal records remain readable, including loopback and
tailnet addresses; record readback alone does not permit a connection. The generic
private HTTP client resolves the host at every request and accepts
only a nonempty answer set entirely within RFC1918 (`10/8`, `172.16/12`,
`192.168/16`) or RFC6598 shared address space (`100.64.0.0/10`, used by tailnets).
Answers mixing those allowed ranges are accepted; an answer set containing any
public, loopback, link-local, other reserved or IPv6 address fails closed. Every
redirect is checked again before opening its connection. Public HTTP probes
continue to reject tailnet addresses.

This restores probing for existing non-public literal endpoint records in
`100.64.0.0/10`, including Discord Blue's `discord-blue-prod-runtime`, without
rewriting their URL, scope or monitoring policy. Health and runtime identity
still have to pass through ordinary monitoring after normal deployment.

Connections use only the validated numeric socket addresses, without a second
DNS lookup. HTTP Host, TLS SNI and certificate name verification retain the
original hostname. Normal system certificate verification applies when the
endpoint has no `tls_ca_pem`; omit that field for the existing publicly trusted
edge certificate. The optional testing-only endpoint CA contract remains for
other existing private endpoints, without changing process-wide trust or
following redirects with that scoped CA.

New edge transport plans reject non-RFC1918 literals even though legacy endpoint
records remain readable. Private DNS alone does not prove private exposure. Before activation, qualify
edge listener/access enforcement and **public denial**, service/client private
reachability, normal certificate chain and hostname verification, and a
private testing-only upstream. Restrict the upstream TCP port to the edge host
using the supported host firewall/network controls; verify that an unrelated LAN
peer and public source cannot reach it. The edge-to-upstream HTTP hop carries
credentials in plaintext inside that restricted network, so private addressing
alone is insufficient isolation. Path restrictions exist at the edge, not at the
runtime bind. Proxy only `/health` and `/agent-session/connect`,
preserving authorization and WebSocket upgrade. Do not fabricate a public website
declaration to get past public-ingress scoping. Use the supported reviewed edge
operation; if it cannot enforce the private route, record that missing capability
on the operational item before changing the edge. This source capability does
not configure or qualify an edge route.

## Inert upstream planner

`POST /v1/products/{product}/private-testing-transport/plan` accepts `dry-run`
and `readback` with exact-lane `product_config.plan` authority and browser CSRF.
The metadata-only payload contains:

- `context`, `instance=testing`, `endpoint_key`, and `mode`;
- `endpoint_url`: the edge's exact HTTPS `/health` URL, without credentials,
  query or fragment;
- `private_ip`: the testing host's RFC1918 bind address;
- `listener_port`: its unused dedicated HTTP upstream port (1024–65535);
- `runtime_service`: the product template's existing service name.

The edge's hostname and HTTPS port are independent of the upstream bind.

```sh
launchplane ingress private-testing-plan \
  --service-url https://LAUNCHPLANE_ORIGIN --product PRODUCT \
  --payload-file /absolute/private/transport-plan.json
```

The planner checks tracked/provider lane ownership, placement and converted
Compose. It proposes only a `ports` addition to the selected runtime service,
never another service or secret. It binds the input and unchanged baseline into
`plan_sha256`. Readback requires that digest, the exact port binding and baseline;
an absent, partial, public or changed binding refuses. A fresh dry-run can
re-review an exact binding after a normal runtime redeploy. The planner writes
nothing, leaves transport/certificate verification false, and does not activate
an endpoint record or claim a healthy workload.

Only an isolated no-website testing target is eligible. The baseline has only
`services` and Compose-scoped named `volumes`, uses its own default network and
has no published ports, host mounts, shared namespaces, external networks,
privileged fields, build directives or public routing labels. Exact Dokploy
metadata labels and an unused root network annotation are normalized; actual
shared attachments remain refused. Provider evidence must explicitly identify
Docker Compose mode, `isolatedDeployment=false`, `randomize=false`, and empty
`serviceNetworks`, `mounts` and `domains`.

## Reviewed source installation and recovery

`POST /v1/products/{product}/private-testing-transport/activation` uses
`mode=dry-run|apply|readback` and `operation=install|refresh|remove`. Add
`source_commit`, `source_path`, `host_review` and `reason` to the planner fields.
`host_review` records the independent host/port, edge access, edge-only upstream
network restriction, routing and TLS qualification. These are reviewed inputs, never measured live proof.

```sh
launchplane ingress private-testing-activate \
  --service-url https://LAUNCHPLANE_ORIGIN --product PRODUCT \
  --payload-file /absolute/private/source-install.json
```

Tracked/provider Git source must agree on the repository, forty-character commit
and Compose path with automatic deployment off. The existing source reader
fetches that immutable template and the build verifier checks artifact provenance.
The runtime consumes `DOCKER_IMAGE_REFERENCE` or `LAUNCHPLANE_IMAGE_REFERENCE`;
auxiliary images are immutable. Structural interpolation and credential
literals/default values refuse. Managed inputs remain references; planning reads
no runtime token and never persists converted secret-bearing environment values.

Retain and review the complete source plan, then apply its
`expected_plan_sha256` with the original idempotency key. Existing Director-agent
execution, physical-target reservation, testing executor lock, complete provider
quiescence and current runtime-input binding still apply. Initial inert planning
uses exact-lane `product_config.plan`; continuation and recovery keep the original
identity and key. No grant, deployment or hold change is added.

Installation changes only source settings: the immutable template with the
selected service's private HTTP port binding. It preserves environment, placement,
bot state and holds. Independent readback compares the entire source and
placement. `runtime_inputs_match_review` separately reports environment drift;
`certificate_verified`, `host_binding_verified`, `transport_verified` and
`deployment_performed` stay false. There is no certificate custody directory.

After a lost response, retain the original payload/key. The durable runner can
adopt an exact source match without repeating the update. Missing, partial,
foreign, changed or unavailable evidence remains `reconcile_required`; another
key or identity cannot bypass the physical-target fence. Readback never releases
an uncertain operation.

For maintenance, plan `refresh` with `original_activation_key` naming the completed
installation/refresh. It first matches its source and placement, then verifies
the new immutable template. Apply the new reviewed plan with a new operation key.
Ordinary artifact upgrades still use the existing deploy driver and preserve the
private bind. Edge certificate renewal stays with the existing edge custodian.

For rollback, plan `remove` against the original completed install/refresh and
its exact transport/source inputs. It refuses drift and restores the preceding
source settings, preserving environment and state. Review/apply with a distinct
removal key, read back, then use normal testing deployment to remove the upstream
binding. Disable the matching edge route and endpoint/check through supported
operations. Keep original operation receipts until rollback is no longer needed.
Old sidecar request fields are rejected, and refresh/remove refuse a legacy
installation missing current transport fields. No compatibility path creates
private keys or restarts the deleted product TLS proxy. Preserve such original
operation evidence for separately supported source recovery; do not bypass its fence.

## Operational sequence

For an existing empty testing Compose, the Director-agent transport may complete
its source through `POST /v1/dokploy-targets/setup` with
`operation=complete-compose-source`. Use the installed
`dokploy-target-complete-compose-source-dry-run` and `-apply` helpers, with the
saved plan, reviewed digest and original idempotency key. Supply a forty-character
commit and its relative Compose path; the service derives the repository from
the exclusive active product profile. This writes only source settings, leaves
automatic deployment off and starts no deployment. The later activation still
fetches and qualifies the immutable template and artifact provenance.

Director-agent completion requires current, binding-matched disposable-lane
evidence for usage, data and isolation. Unknown, stale, in-use, only-copy or
shared evidence refuses; do not turn an observer's unknown result into a manual
positive assertion. Identify the missing service evidence producer on the
operational issue if that qualification is unavailable. The service also holds
the testing executor lock, refuses unresolved physical-target reservations,
checks complete provider deployment/queue/schedule inventories and isolated
placement, and repeats these checks immediately before the source update inside
the existing ownership/binding transaction. A configured/partial source, foreign
or shared target, public route, provider drift or active/unknown work refuses.
Existing scoped workflow source-completion authority is unchanged; all source
completions now wait for unresolved lane operations and retained custody. A partial
provider result retains the existing reconciliation requirement; never retry
under a new key or identity.
The source transaction also excludes active or unresolved lane operations and
retained-target custody. With no source, the provider's automatic-deploy default
is inert; the source update disables it atomically and verifies it off afterward.
Completion changes the safety binding, so collect fresh usage/data/isolation
evidence before any later operation that requires it. Provider requests have
bounded timeouts; the final exclusion reads hold the product-authority lock and
can delay other authority writers while a slow provider responds.

1. Verify the deployed service includes this source. Resolve the independent
   immutable-source/hold prerequisites and recheck actual testing isolation.
2. Select an edge-certified hostname with only RFC1918 DNS answers. Qualify its
   private listener/access rules, public denial, testing-only upstream, TLS
   chain/name, HTTP authorization and WebSocket upgrade through supported paths.
   Verify the chosen upstream IP belongs to the testing host and its port is
   unused, including wildcard listeners. Restrict that port to the edge and
   verify denial from other peers before activating the source; if the supported
   host/network operation is missing, record that prerequisite rather than
   publishing an unrestricted upstream. Keep production resources unchanged.
3. Prepare/review/apply/read back the private HTTP upstream source under the
   operational task's authority; this source session authorizes no live changes.
4. Register/read back the exact HTTPS `/health` private endpoint without a custom
   CA, then plan/apply/read back its identity-required `private_http` check.
5. Qualify the maintained local helper and give Chris the actual metadata-only
   recipient command. Chris alone creates a private recipient generation, uploads
   `request.json`, compares its fingerprint and runs signed-in destination dry-run
   before first token entry. No agent handles real recipient/key/token material.
6. After normal testing sync/deploy, verify actual TLS, runtime identity, health,
   monitor cadence and authenticated WSS. Endpoint/destination metadata can precede
   the token-backed deploy; it does not prove those runtime facts.

The operational parent retains the exact service-backed coordinates and any
missing edge capability. No source-only plan qualifies a real destination.
