---
title: Private testing HTTPS and WSS preparation and activation
---

A no-website generic-web service can use a dedicated Nginx TLS listener inside
its isolated testing Compose target. Launchplane owns the overlay and runtime
source operation; the product owns its runtime template and immutable build.
This does not require a public website, public domain or shared ingress change.
The supported authority is an explicit RFC1918 IPv4 address, not split DNS,
tailnet names or public IPs.

## Inert overlay planner

`POST /v1/products/{product}/private-testing-transport/plan` accepts `dry-run`
and `readback` only, using exact-lane `product_config.plan` and browser CSRF.
It reads fresh tracked/provider placement and converted Compose, rechecks the
records and binds the baseline, overlay, provider options and trust intent into
`plan_sha256`. It never applies, deploys, reads a key or installs trust.

```sh
launchplane ingress private-testing-plan \
  --service-url https://LAUNCHPLANE_ORIGIN --product PRODUCT \
  --payload-file /absolute/private/transport-plan.json
```

The metadata-only payload contains `context`, `instance=testing`, `mode`,
`endpoint_key`, `private_ip`, `listener_port`, `runtime_service`, immutable
`nginx_image`, public `ca_bundle_sha256` and `certificate_sha256`, absolute
`monitor_ca_bundle_path` and `receiver_ca_bundle_path`, and `trust_review`.
Do not substitute fabricated hashes for actual reviewed public material.
For example, an isolated fixture may select `10.77.0.9:9443`, service `bridge`
and its own disposable certificate; those values are not runtime authority.

The source must have only `services` and Compose-scoped named `volumes`, its own
default network, and the planner's supported service fields. It excludes builds,
global container/volume names, host/external networking, published upstream ports,
host mounts/env files, shared namespaces, capabilities and security overrides.
Dokploy's exact target/service metadata labels and unused root-only network
annotation are normalized; foreign labels, actual attachments and public routes
remain refused. Provider evidence must explicitly name Docker Compose mode,
`isolatedDeployment=false`, `randomize=false`, and empty `serviceNetworks`,
`mounts` and `domains`. Swarm and unknown provider options are unqualified.

The generated unprivileged, read-only Nginx sidecar publishes only the reviewed
private IP/TCP port. It forwards precisely `/health` and `/agent-session/connect`,
including WebSocket upgrade and authorization, and returns 404 elsewhere.
Docker DNS is refreshed after runtime container changes. Missing workloads fail
the route. There is no HTTP redirect/admin listener, access logger or public
routing label. See [Nginx WebSockets](https://nginx.org/en/docs/http/websocket.html).

The original planner's overlay uses relative certificate references. Its readback
requires the original intent and `expected_plan_sha256`, compares the exact
installed overlay and unchanged baseline, and still reports certificate and
transport verification false. A fresh dry-run can rebind an exact overlay after
baseline drift; partial/different additions refuse. Uncertain inert plans are safe
to repeat; provider read failures return 503, unqualified/stale inputs 409.

## Reviewed source installation and recovery

`POST /v1/products/{product}/private-testing-transport/activation` fills the source
installation gap. `mode=dry-run|apply|readback` and
`operation=install|refresh|remove` operate on source configuration only. Existing
immutable-artifact deployment starts the resulting services. This operation never
changes provider env, deploys, creates a resource, reads a real key, installs trust,
lifts a hold or claims that a workload is healthy.

Initial tracked/provider Git source must agree on the product's repository,
exact forty-character commit and Compose path, with automatic deployment off.
The existing source reader fetches that immutable tree and the existing build
verifier proves its artifact provenance; SHA-shaped branch acceptance is not
source delivery. Only the narrow isolated source shape above is supported.
The runtime image consumes `DOCKER_IMAGE_REFERENCE`; auxiliary images are also
immutable. Structural interpolation and credential literals/default values
refuse. Managed inputs stay template references, so source planning needs no real
token and never persists converted secret-bearing env into raw Compose.

Use the installed client, configured service identity and a private payload:

```sh
launchplane ingress private-testing-activate \
  --service-url https://LAUNCHPLANE_ORIGIN --product PRODUCT \
  --payload-file /absolute/private/source-install.json
```

Add `source_commit`, `source_path`, `certificate_root`, `host_review`,
`custody_review`, `reason` and `operation` to the planner intent fields.
`certificate_root` is an absolute purpose-specific parent directory. The service
appends its generated lane namespace and returns the exact `custody.directory`;
use that returned directory for the Chris-only file step. No secret
value, certificate private key or recipient file is accepted. Review references
attest to the exact host/port, private routing, issuer and custody review; they
are not measured file/host evidence. The source plan binds those references,
public CA/leaf digests, immutable source, placement, provider options and current
runtime-input digest. Its `plan_sha256` is distinct from the inert overlay digest.

Retain and review that complete plan. Set `mode=apply`, its
`expected_plan_sha256`, and supply the original `--idempotency-key`. Apply requires
the authenticated Director agent, as existing independent testing activation
does; other callers retain exact-lane `product_config.plan` for inert planning
and readback. PostgreSQL, the physical-target reservation, existing testing
executor lock and complete quiescent provider deployment/queue/schedule evidence
protect execution. No grant or workflow is added.

Installation writes only source settings: the immutable product template plus
generated TLS additions with the reviewed absolute certificate paths. It preserves
provider env, placement, bot state, records and holds. `mode=readback` with the
same payload/key independently fetches and compares the whole provider source
and placement. This readback owns the absolute-custody overlay; the original
relative-file planner readback is not its receipt. `runtime_inputs_match_review`
separately describes env drift after normal image/identity delivery.
`certificate_verified`, `host_binding_verified`, `transport_verified` and
`deployment_performed` stay false even when configuration matches.

After a lost response, retain the original payload/key. An apply retry uses the
durable runner to adopt an exact source match without repeating the update.
Missing/partial/foreign/changed/unavailable evidence remains `reconcile_required`;
a different key cannot bypass the physical-target fence. Readback diagnoses source
but never releases an uncertain operation. Do not recreate resources, overwrite
foreign source or switch identity to recover.

For source maintenance, plan `operation=refresh` with `original_activation_key`
naming the completed install/refresh. It first matches that operation's current
source and placement, then fetches/verifies the newly selected immutable source.
Review and apply with a new operation key. Ordinary image upgrades already use
the existing deployment driver: it updates `DOCKER_IMAGE_REFERENCE` and runtime
identity without rewriting the installed template/overlay. Host/certificate/trust
changes still need fresh operational qualification and review. Preserve testing
holds; use the existing supported hold operation if one must be lifted.

For rollback, plan `operation=remove` naming the original completed installation
or refresh and its source commit/path. It refuses drift and restores exactly the
previous source settings, retaining env/state. Review/apply under a distinct
removal key, independently read back, then use the existing testing deploy and
its original-key recovery to activate the restored source and stop the listener.
Removal never deletes state, certificates, endpoint records or uncertain history.
Retain operation receipts, public certificate metadata and original recovery keys
until rollback is no longer needed.

## Host activation, certificate custody and scoped trust

Before the existing service-owned testing deployment, the operational session
qualifies these facts independently. Source installation does not prove them:

1. The selected private address belongs to the testing host; the TCP port is free,
   including wildcard listeners; both receiver and monitor can reach it, with no
   public forwarding/NAT. RFC1918 syntax alone proves none of this.
2. The selected immutable Nginx image supports the generated TLS/proxy/rewrite
   configuration as UID/GID 65534, with all capabilities dropped, read-only root
   and only its own tmpfs/network. No production port or storage is changed.
3. Chris or an explicitly authorized existing custody mechanism supplies a private
   issuer's server certificate with the exact IP SAN. Public CAs are not assumed
   to issue for private IPs. Chris places `server.crt` and `server.key` in the exact
   dedicated directory returned in `custody.directory`, with no symlinks/shared
   production material. The engine needs traversal/read access; the mounted key
   is readable only by proxy UID/GID 65534 (for example UID 65534, mode 0400).
   Verify CA/leaf public digests, chain, SAN, validity and key/certificate agreement
   in the custodian's trusted environment. Do not print or transmit key bytes.
   Compose mounts both files read-only as secrets. Missing/unreadable files stop
   Nginx. Agents never generate, read, copy or upload real key material.
4. Prefer the exact testing endpoint's optional `tls_ca_pem`: reviewed **public CA
   certificates**, stored with that endpoint through its existing reviewed
   dry-run/apply path. Its authority digest includes those bytes. The schema
   refuses private keys, server-leaf-only trust, HTTP and production records.
   The monitor creates a fresh normal-verification TLS context for this probe,
   adds that CA and follows no redirects using it. Other probes retain their own
   trust; no CA store, process env or restart changes. Compare the public CA digest
   with the source review before registration.
5. The receiver separately selects an Director-reviewed private CA bundle covering
   the private issuer **and Launchplane's HTTPS origin**, with normal hostname and
   chain verification. The original planner's legacy `SSL_CERT_FILE` monitor intent
   is process-wide, including production checks. Do not apply that option under
   testing-only scope. Use endpoint-scoped public CA trust above; any shared-monitor
   bundle/restart or new real issuer/trust setup needs its separate Director decision.

Renew before expiry through the same custodian and directory, preserve the
old certificate/key until rollback is no longer needed, re-review public hashes
and source intent, then use supported testing redeploy and live qualification.
No service operation issues a CA or silently installs trust. A new trust/custody
choice goes to Chris as one concrete question with a recommendation.

## Endpoint, monitoring and recipient sequence

After reviewed source/custody preparation, use the existing private endpoint
reviewed dry-run/apply to record the exact testing `/health` authority, optionally
with its public `tls_ca_pem`. Read that active record back before planning an
identity-required `private_http` check with its endpoint key. Preserve private
intent and the no-website declaration; do not fabricate a public origin or a
healthy check against an absent workload.

Endpoint and [local-consumer destination verification](testing-local-consumer.md)
validate metadata and may precede the first token-backed deployment. Chris alone
prepares a new private recipient generation, compares its fingerprint and runs
signed-in destination dry-run before first token entry. No agent reads that
directory or issues a real package during preparation. Actual host bindings,
mounted bytes, TLS, identity, authenticated WSS and fresh monitor observations
qualify after the existing testing deployment. There is no deployment-before-token
cycle. The operational parent retains those facts and Chris's real handoff.

After a rollback, disable only this listener's endpoint/check through their
existing reviewed paths. Keep required recipient generations and original
operation evidence. Never recover through shared ingress, production credentials,
CA stores or the production client.

## Fake-data qualification

Behavior tests cover actual source operations, immutable-source/template binding,
replay, lost-response adoption, foreign/partial state and production/shared-resource
refusals. Separate real HTTPS tests prove endpoint-scoped trust and hostname/issuer
refusal without process trust changes. The generated proxy rehearsal uses a
specifically selected disposable Nginx binary:

```sh
uv run --extra dev python scripts/qualify-private-testing-transport.py \
  --nginx-binary /absolute/task-artifact/nginx
```

It creates its own CA/leaf/keys and fake HTTP/WS runtime, exercises real monitor
HTTPS and receiver authentication/WSS, and rejects unknown issuer, wrong hostname,
wrong fictional credential, production identity and unrelated paths. It sends no
bridge command, installs nothing and qualifies no real destination or token.
