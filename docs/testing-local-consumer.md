---
title: Testing bridge delivery to a local consumer
---

Launchplane owns custody, recipient encryption, the delivery receipt and the
local receiving helper. Discord Blue owns its bridge consumer and reads the
result through its existing `token_file` interface. This path changes no product
build or deploy workflow and installs no client or background process.

Only the declared `AGENT_SESSION_TOKEN` in `runtime_environment`, stored for one
exact `context_instance` testing lane with class `testing` and no sharing reason,
can be delivered. Production, preview, context-wide, shared, worker and service
credentials cannot use this path. The active key-safety evaluation still applies.

The signed-in administrator uses their existing exact-lane `product_config.plan`
and `product_config.apply` authority. Nonhuman identities cannot issue a delivery,
even with those actions. No reusable service credential, enrollment, session,
lease or authorization grant is created for the local consumer.

## Prepare before secret entry

Engineering first records and verifies the testing lane, exact secret declaration,
and an active private **HTTPS** health endpoint at `/health`. The testing bridge
must be reachable from Chris's machine through **WSS** on that same authority at
`/agent-session/connect`, with normal certificate verification. An HTTP-only
private endpoint is insufficient; an HTTPS/WSS route is a prerequisite, not a
reason to bypass transport checks. Real endpoint/placement facts remain service
records. The helper does not create a listener or an inbound workstation route.
For a no-website Compose service, use [private transport preparation](private-testing-transport.md)
to plan the dedicated testing listener and trust references. Activation and live
transport qualification remain separate from that planner's metadata/readback.

Chris runs the helper himself in a terminal using the qualified Launchplane
installation. The command contains only non-secret destination metadata:

```sh
launchplane local-consumer prepare \
  --directory /absolute/private-parent/testing-client-generation-1 \
  --service-origin https://LAUNCHPLANE_ORIGIN \
  --product PRODUCT --context CONTEXT \
  --endpoint-key TESTING_ENDPOINT_KEY \
  --server-url wss://TESTING_AUTHORITY/agent-session/connect
```

The parent must exist, belong to Chris and permit no group/other writes. The new
directory must not exist or traverse a symlink. It is mode 0700; its recipient
private key and request are mode 0600. Agents must not run this command for a
real transfer, inspect the real recipient directory, collect its key or token,
or include those files in task evidence. Ordinary same-user filesystem access
is not an isolation boundary against arbitrary code running as Chris. Custody
here means the supported flow never sends these values to an agent, browser
plaintext, chat, CLI argument or log. Use fake isolated data for engineering.

The output contains only `request_file` and a SHA-256 recipient fingerprint.
Preparation carries no credential or service authority and starts no delivery
timer. The service starts a 15-minute review/delivery window only when the
credential is ready and its first value-ready dry run is recorded. An expired
review needs a new private testing generation directory.

## Signed-in UI

1. Chris opens the product's **Testing → Managed secrets** page. In **Deliver
   to a testing client**, choose the helper's `request.json` using **Local
   recipient request**. Never upload `recipient.pem`, `bridge.token` or a token.
2. Check the product, testing environment, bridge address and private local
   directory. Select **Run delivery dry-run**. Compare **Recipient fingerprint**
   to the helper's output, character for character. The service verifies the
   declaration and exact testing endpoint even before a credential exists.
3. **Destination verified** with a credential-not-ready message is destination
   preparation only. The managed-secret input and isolated server delivery must
   still be prepared through their existing paths. Chris alone enters the real
   testing bridge value in that separate signed-in password field. No value is
   entered into this delivery panel or a terminal/file. Server synchronization
   remains the separate reviewed testing operation.
4. Select **Recheck original destination** after the testing value is ready.
   Review the current **Managed version**, compare the fingerprint again, tick
   **I compared the fingerprint and verified this private testing destination.**,
   and select **Deliver to this testing client**.
5. Select **Download encrypted delivery**. The downloaded JSON contains only
   public recipient metadata and authenticated recipient ciphertext, never the
   value. If the response is uncertain, use the same delivery button and original
   request; do not substitute another destination to recover it. A refreshed
   page can load that original request and dry-run again before exact replay.

The API is `POST /v1/local-consumer-deliveries` with `dry-run` or `apply` and a
matching stored plan/digest. The original request id and administrator identity
bind the receipt. Concurrent original applies converge on one immutable encrypted
package. Issuance does not rotate the stored credential, sync or deploy anything.

## Receive and use

Chris runs, without pasting a credential:

```sh
launchplane local-consumer receive \
  --directory /absolute/private-parent/testing-client-generation-1 \
  --package-file /absolute/path/to/testing-local-delivery.json
```

The receiver verifies the original request and pinned HTTPS service origin,
then asks `POST /v1/local-consumer-deliveries/verify` to compare the exact package
against its stored receipt and current source metadata. This unauthenticated
comparison requires the unguessable request id and exact ciphertext; it returns
only `current` and that package's digest, no credential, record or authorization.
There is no offline-install fallback.

For a private certificate issuer, Chris can select an already reviewed CA bundle
with `SSL_CERT_FILE=/absolute/path/to/ca-bundle.pem` for both the receiver and the
testing bridge process. The bundle must cover the Launchplane HTTPS service and
testing HTTPS/WSS issuer. Both HTTP and WebSocket verification use the same CA
bundle (the Requests bundle when no override is selected) and stay enabled;
the helper does not install a CA or offer an insecure mode.

After decrypting in memory, the receiver verifies healthy testing runtime
identity over HTTPS and performs one token-authenticated WebSocket upgrade at
the exact WSS endpoint. It sends no hello, session or Discord command. The
server must accept the same value that the current Launchplane version supplied;
this catches server synchronization lag without revealing or hashing the value.
HTTP redirects, WebSocket redirects and environment proxies are excluded.

After a second current-receipt verification, it creates `bridge.token`,
`codex-bridge.toml` and an encrypted `delivery.json` receipt in the private
directory, all mode 0600. Output contains only the version id and config path.
It never replaces an existing different file or follows a symlink/hard link.
An exact retry can finish an interrupted installation whose existing bytes match.
A torn/different file is preserved and requires another generation directory.

Start the existing Discord Blue client with that **separate testing config** and
an explicitly chosen testing daemon/home. Starting or changing an installed
bridge is a separate Chris-observed step; the receiver does not do it. Preserve
the production config, daemon, bot identity and launch service. A successful
delivery is not product UI acceptance or permission to release production.

## Rotation, revocation and recovery

Rotation or disabling the source record/binding, a changed classification,
profile or endpoint, key-safety failure, expired review, bad ciphertext, wrong
recipient, unavailable service, unhealthy/wrong-lane runtime or rejected bridge
authentication stops installation. No status is inferred from a failed request.
Keep the original request and package for an uncertain retry within its lifetime.

For rotation, use the existing signed-in testing secret change and reviewed
server sync, then prepare a **new generation directory**, review/deliver the new
version, verify it and switch only the testing client to that config. Old files
are never overwritten to recover a rotation. Stop the old testing client before
the switch; keep production untouched.

Disabling a Launchplane binding prevents new delivery/verification; revoking an
already installed credential also requires the product's supported server-side
rotation/revocation and synchronization, then stopping the old testing client.
An existing WebSocket may remain open until the product closes it. This helper
does not claim to revoke existing server sessions. After successful Chris
verification, the old testing generation may be removed by Chris; never
delete the only needed request/key while a transfer is uncertain.

If a needed transport, caller action or scope is refused, retain its trace and
return to the owning preparation/authorization issue. Do not broaden a grant,
copy production material or use another identity/provider/database path.
