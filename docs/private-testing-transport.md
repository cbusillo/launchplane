---
title: Private testing HTTPS and WSS through an existing edge
---

A no-website generic-web service can use an existing TLS edge, including NPMplus,
for its private testing HTTPS `/health` and WSS `/agent-session/connect` authority.
The edge keeps its existing certificate, private key and renewal. Launchplane
publishes the testing runtime's HTTP port on one RFC1918 interface; it generates
no TLS sidecar, certificate mounts, private issuer or client trust-store changes.
The product still owns its immutable runtime template and build.

## Endpoint and outbound boundary

A testing private endpoint record accepts an HTTP(S) DNS hostname, including a name
covered by an existing wildcard certificate. Other lane instances retain the existing internal-suffix/non-public-literal
record rule; their credential-bearing consumers do not gain new DNS authorities.
Registration validates URL syntax;
it does not resolve DNS or claim reachability. Existing non-public literal records remain readable, including loopback and
tailnet addresses; this preserves record readback, not permission to connect. At every check, the private HTTP client resolves the host again and accepts
only a nonempty answer set entirely within `10/8`, `172.16/12` or `192.168/16`.
Public, mixed, loopback, link-local, CGNAT and IPv6 answers fail closed. Every
redirect is checked again before opening its connection.

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
