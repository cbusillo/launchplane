---
title: Private testing HTTPS and WSS preparation
---

A no-website generic-web service can prepare a dedicated Nginx TLS listener
inside its own testing Compose target. This reuses the existing Dokploy Compose
and private endpoint contracts; it does not route through the shared public
NPMplus host or require a public website/domain. The current preparation supports
an explicit RFC1918 IPv4 authority, not split DNS, tailnet names or public IPs.

`POST /v1/products/{product}/private-testing-transport/plan` accepts only
`dry-run` and `readback`. It uses existing exact-lane `product_config.plan`
authorization and browser CSRF when called from a browser. There is no apply
mode, resource creation, credential read, trust installation or deployment.

## Planning

Use the deployed service through the installed Launchplane CLI:

```sh
launchplane ingress private-testing-plan \
  --service-url https://LAUNCHPLANE_ORIGIN --product PRODUCT \
  --payload-file /absolute/private/transport-plan.json
```

Authentication uses the existing configured service-token environment; never
pass a token on the command line or collect it from Chris. The JSON payload
contains metadata only:

```json
{
  "context": "example-bot",
  "instance": "testing",
  "mode": "dry-run",
  "endpoint_key": "example-bot-testing-bridge",
  "private_ip": "10.77.0.9",
  "listener_port": 9443,
  "runtime_service": "bridge",
  "nginx_image": "nginx@sha256:REPLACE_WITH_REVIEWED_IMAGE_DIGEST",
  "ca_bundle_sha256": "REPLACE_WITH_REVIEWED_PUBLIC_CA_BUNDLE_DIGEST",
  "certificate_sha256": "REPLACE_WITH_REVIEWED_PUBLIC_CERTIFICATE_DIGEST",
  "monitor_ca_bundle_path": "/run/private-trust/example-bot-ca-bundle.pem",
  "receiver_ca_bundle_path": "/absolute/private-trust/example-bot-ca-bundle.pem",
  "trust_review": "Link to the exact issuer and per-purpose custody review"
}
```

Replace examples with freshly qualified records and reviewed metadata. These
values do not constitute runtime authority. No certificate private key, recipient
key or bridge/bot value is accepted by the schema. CA/certificate digests and
paths are reviewed intent; the planner does not inspect those files or prove
they exist, agree, are trusted, or are currently served.

The service reads its product/lane, tracked Dokploy id, provider-target record,
fresh provider placement and converted Compose source. It rechecks stored
bindings after the provider reads. Shared targets, public domains, absent source
or runtime service, host/external networking, published upstream ports, globally
named/external/bind storage and unqualified configuration are refused. The narrow
supported source uses its own default network and Compose-scoped named volumes.
An empty target must first receive a supported immutable runtime Compose source;
do not invent source or mark an empty target qualified to satisfy this planner.

The response provides a deterministic `plan_sha256`, target/source binding,
new listener overlay, private HTTPS endpoint metadata and WSS address on exactly
the same authority. The overlay carries no existing workload environment or
credential. It publishes only the selected private IPv4/TCP port. Nginx has no
public ingress labels, HTTP redirect listener, CA issuer, access logger or admin
listener. It forwards only `/health` and `/agent-session/connect`, including the
WebSocket upgrade and original authorization header. Other paths return 404.
See [Nginx's WebSocket contract](https://nginx.org/en/docs/http/websocket.html).

## Activation and trust boundary

This is preparation, not an apply-eligible live operation. A separate activation
brief must qualify the supported service-owned Compose update/deploy operation,
its review/idempotency/recovery contract and these exact prerequisites:

1. Fresh host evidence proves the selected address belongs to the testing host,
   the port is unused (including wildcard listeners), and private routing reaches
   both the receiver and monitor without public forwarding/NAT. The planner
   cannot infer a firewall or routing fact from an RFC1918 literal.
2. The reviewed immutable image supplies Nginx with TLS/proxy/rewrite support and
   can run as UID/GID 65534 with all capabilities dropped and a read-only root.
   Only its private namespace and tmpfs are new; production ports and storage
   stay unchanged. Preserve any testing hold, canonical artifact, bot state and
   existing credential-delivery contract. An overlay is not a replacement for
   the complete runtime source.
3. A separately reviewed private issuer supplies a server certificate with the
   exact IP subject alternative name. A public CA is not assumed to issue for
   a private IP. Chris or an explicitly authorized custody mechanism places the
   certificate and key in the generated target-local directory, with only the
   dedicated proxy UID able to read the key. Compose mounts them read-only as
   secrets; no agent reads or copies a real key and no shared production key is
   reused. Missing or unreadable files stop Nginx.
4. The selected per-purpose CA bundles cover the issuer, and the receiver bundle
   also covers Launchplane's HTTPS origin. The monitor's existing Python TLS
   path and the [receiver](testing-local-consumer.md) both use `SSL_CERT_FILE`
   with normal chain/hostname verification. Selecting/installing real bundles
   and restarting an installed monitor/client remain separately authorized.
   No insecure mode or global trust-store modification is supplied.

After separate activation, retain the original payload and set `mode=readback`
and `expected_plan_sha256` to the reviewed plan digest. The service reads current
provider Compose and compares every added service/config/secret reference, while
rebinding the unchanged baseline/placement/records. Missing, partial, different
or stale configuration is refused. This comparison does not inspect host port
bindings, mounted file bytes, running containers or certificates. A configuration
match still returns `transport_verified=false` and `certificate_verified=false`.

An uncertain **planning** response is safe to repeat: it writes nothing. A
changed digest requires fresh review. An uncertain **activation** is recovered
only by its original supported operation/key and readback; do not send another
apply, bypass a refusal, or treat this planner as a mutation receipt. No live
activation/recovery operation is introduced here.

## Endpoint, monitoring and recipient sequence

After supported activation/readback and actual verified HTTPS/WSS qualification,
use the existing private endpoint reviewed dry-run/apply to register the exact
testing `/health` endpoint. Read back that active record **before** planning an
enabled `private_http` check with `require_runtime_identity=true` and that endpoint
key. An absent endpoint is not an apply-eligible monitoring plan. Keep private
intent and the no-website declaration; never fabricate a public origin.

Then follow [testing local consumer preparation](testing-local-consumer.md):
Chris alone prepares a new private recipient generation, compares the fingerprint
and runs the signed-in destination dry-run before entering either testing value.
Endpoint metadata verification does not replace running HTTPS identity and
authenticated WSS verification. #3777/#3440 retain real destination, config,
token, deployment and transport proof; their existing person waits remain intact.

For rollback, stop only this testing listener through the separately qualified
activation operation, remove precisely its overlay additions, and read back the
original baseline. Disable only its endpoint/check records through existing
reviewed paths. Preserve the bot's isolated state, original operation/evidence,
uncertain outcomes, and any needed recipient generation. Never recover by
changing the shared public ingress, production bot, CA store or production client.

## Fake-data qualification

Unit tests exercise service authorization, no-website planning, stable replay,
partial/changed readback, source/provider failure and production/shared-resource
refusals. To test the generated proxy against a disposable fake HTTP/WS runtime
with a specifically selected Nginx artifact:

```sh
uv run --extra dev python scripts/qualify-private-testing-transport.py \
  --nginx-binary /absolute/task-artifact/nginx
```

The rehearsal creates temporary certificates, keys and loopback listeners, runs
the generated Nginx routes (relocating mount paths, pid and listener port into
the fixture namespace), uses the real private monitor and receiver, and checks
untrusted issuer, wrong hostname, wrong credential, production identity and
unrelated-path refusals. It sends no bridge command. It installs nothing and
does not qualify an installed image, actual routing, production or a live token.
