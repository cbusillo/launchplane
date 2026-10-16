# Artifact scanning and image retention

Launchplane owns these capabilities (#3858 / Q397). Product builds still supply
only their artifact and provenance; they do not call Launchplane for either.

## Verified artifact scanning

Before the event reconciler queues a testing deployment or applies a preview,
it scans the image repository and digest from the verified build manifest.
Provenance verification and forward-movement checks happen first. A tag is
never passed to the scanner. Already-serving images and preview destruction do
not require another scan. This gate does not change the accepted-release,
backup, promotion, rollback, or restore paths.

The service image includes Trivy. It reads the remote image without executing
it, scans OS and language packages, and blocks on fixable HIGH/CRITICAL findings.
Each run has an isolated directory, explicit empty config/ignore files, and no
host registry login, Docker socket, product configuration, plugins or inherited
`TRIVY_*` environment. A missing binary, timeout, inaccessible private image,
invalid report, missing package inventory or wrong image is unavailable, never
clean. Only bounded counts, the image reference, timestamp and reason enter the
reconcile plan; provider stderr and report contents do not.

The existing product reconcile-request reader exposes `last_plan.artifact_scan`.
A refused scan sets `action=wait`, `held=true`, and
`reason=artifact_scan_refused` before deploy effects. The normal sweep retries;
fixing the image and publishing its next build also requests reconciliation.
The scan covers the platform Trivy selects on the service host. It is not proof
for every architecture in a multi-platform index.

Scanning is enabled by default. An administrator can set the stored product
profile's `image.scan_enabled` to false through the existing product-profile
write path (`POST /v1/product-profiles`); disabled scans are explicit in the
plan. Keep the rest of the current profile intact. Default serialization omits
this field so existing profile fingerprints do not change. No product source
file or service-host environment variable controls it. A private registry that
needs new credentials remains unavailable until its access is resolved through
the Director's normal access boundary; there is no token fallback here.

## Retention dry run

`GET /v1/artifacts/retention?product=<product>` requires the existing
`artifact_protection.read` authority for that product and all contexts. It
uses that product's existing read-only build-provenance App path. It reads the
GHCR package named by the stored profile, verifies its linked repository ID
against tracked inventory, and reads a bounded complete version inventory.
Missing access, ambiguous metadata, duplicate/truncated pages or protection
warnings yield an unavailable plan with no candidates.

Protection comes from the existing whole-store protected-artifact inventory,
including stable lanes, previous passing rollback deployments, release tuples,
and active preview generations/feedback. Whole-store protection covers another
product sharing the package. Protected digests and tags stay. The planner also
keeps the newest 20 SHA-tagged versions, images younger than 30 days, unknown
or floating tags, and every untagged manifest (including architecture children
and attestations). Only old, unprotected build-tagged roots are candidates.

Every result has `mode=dry-run` and `permission=unconfirmed`. There is no DELETE
transport, apply endpoint, scheduled deletion, or credential/grant mutation.
A successful package GET does not prove package admin authority. The live
capability audit and effective package ACL must establish the supported service
identity before apply is enabled; any new access is a separate Owner question.
A future apply must re-read protection and in-flight deployment/preview/release
reservations immediately before each deletion, protect reachable OCI child
manifests, bind a reviewed plan to current identity, and record results. This dry
run cannot be replayed as deletion authority.

## Rollout and recovery

Ship and verify the Launchplane replacement before removing any product job.
Keep the five cleanup jobs while pruning is report-only. Follow-up PRs in each
product repository remove its copies only after the replacement covers their
behavior, including private-image access and the deployed target architecture.
VeriReel and SellYourOutboard's causal dependency comparison is distinct from
this absolute deployed-image scan; record its disposition explicitly when
retiring their inline Trivy setup. Retire the frozen action after its last
consumer goes. Plain product CI remains product-owned.

Rolling back this source change restores the previous deploy behavior. Existing
protected-artifact records and product cleanup jobs provide the retention
fallback; the new path has deleted nothing. Removing an explicit scan opt-out
restores the gate. No live profile, registry ACL or production lane is changed
by the source PR.
