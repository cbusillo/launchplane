"""Fresh recovery of the published placement; bootstrap evidence remains historical."""

from datetime import datetime, timedelta, timezone
import re
from typing import TYPE_CHECKING
from typing import Any

from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.contracts.production_recipient import (
    ProductionRecipientRecord,
    RecipientRecoveryPlan,
)
from control_plane.integration_allowances import canonical_sha256
from control_plane.production_recipient import (
    ProductionRecipientAdapter,
    authority_snapshot,
    custody,
    qualified_render,
    runtime_values,
    same_recipient_authority,
)
from control_plane.production_recipient_publication import require_owned_publication_rows
from control_plane.production_recipient_resources import resource_spec

if TYPE_CHECKING:
    from control_plane.production_recipient_recovery import RecipientRecoveryAdapter

CAPTURE_PROTOCOL = "mariadb-owned-attempt-logical-roundtrip-v2"
CAPTURE_TIMEOUT_SECONDS = 120
CAPTURE_WITNESS_MAX_AGE_SECONDS = 15 * 60


def published_mounts(
    record: ProductionRecipientRecord, rendered: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    """Bind both exact destinations and access modes from qualified product source."""
    names = {key: value["name"] for key, value in rendered["volumes"].items()}
    result = {}
    for role, service in (
        ("database", record.request.database_service),
        ("config", record.request.application_service),
    ):
        mounts = rendered["services"][service].get("volumes", [])
        if len(mounts) != 1:
            raise ValueError("Published recovery requires one exact storage mount per service.")
        mount = mounts[0]
        if isinstance(mount, str):
            parts = mount.split(":")
            source, destination = parts[:2]
            read_only = len(parts) == 3 and parts[2] == "ro"
        else:
            source, destination = mount["source"], mount["target"]
            read_only = bool(mount.get("read_only"))
        if (
            names[source] != record.volumes[role]
            or role == "database"
            and (read_only or destination != "/var/lib/mysql")
        ):
            raise ValueError("Published storage does not match the MariaDB/config verifier.")
        result[role] = {"destination": destination, "read_only": read_only}
    return result


def published_proof_is_fresh(proof: dict[str, object]) -> bool:
    """Historical readback preserves its timestamp; it never renews a proof."""
    try:
        recovered_at = proof["recovered_at"]
        fresh_until = proof["recovery_fresh_until"]
        if not isinstance(recovered_at, str) or not isinstance(fresh_until, str):
            return False
        ended = datetime.fromisoformat(recovered_at.replace("Z", "+00:00"))
        until = datetime.fromisoformat(fresh_until.replace("Z", "+00:00"))
        return ended <= datetime.now(timezone.utc) <= until
    except (KeyError, ValueError, TypeError):
        return False


def published_current(
    adapter: "RecipientRecoveryAdapter",
) -> tuple[ProductionRecipientRecord, RecipientRecoveryPlan]:
    record = custody(adapter.store, adapter.product, adapter.request.context)
    if (
        record is None
        or record.phase != "published"
        or not record.qualified_at
        or (record.actor, record.scope, record.plan_sha256)
        != (adapter.actor, adapter.scope, adapter.request.preparation_plan_sha256)
        or not record.publication.get("published_at")
        or not record.publication.get("published_sha256")
    ):
        raise ValueError("Recovery requires the original caller and exact current publication.")
    require_owned_publication_rows(adapter.store, record)
    authority = authority_snapshot(adapter.store, adapter.product, record.context)
    # Verify the managed provider source/env without pretending the old authority
    # snapshot still owns this lane after its atomic publication.
    preparation = ProductionRecipientAdapter(
        adapter.root,
        adapter.store,
        adapter.product,
        record.request,
        adapter.actor,
        adapter.scope,
        adapter.trace_id,
        record.original_key,
    )
    preparation.verify(record.model_copy(update={"authority_snapshot": authority}), published=True)
    rendered = qualified_render(
        record, runtime_values(adapter.store, adapter.root, record, published=True)
    )
    mounts = published_mounts(record, rendered)
    authority_sha = canonical_sha256(authority)
    digest = canonical_sha256(
        {
            "purpose": "published",
            "protocol": CAPTURE_PROTOCOL,
            "capture_timeout_seconds": CAPTURE_TIMEOUT_SECONDS,
            "generation": adapter.request.generation,
            "actor": adapter.actor,
            "scope": adapter.scope,
            "preparation": record.plan_sha256,
            "publication": record.publication,
            "recipient": record.target_id,
            "destination": record.request.destination_server_id,
            "artifact": record.image_reference,
            "database_image": rendered["services"][record.request.database_service]["image"],
            "resources": resource_spec(record),
            "mounts": mounts,
            "authority": authority,
        }
    )
    owner = record.execution.get("published_recovery_owner:" + digest, {})
    if owner and adapter.original_key and owner.get("original_key") != adapter.original_key:
        raise ValueError("Retain the published recovery's original actor and key.")
    proof = record.published_recovery_proofs.get(digest, {})
    if proof and (
        proof.get("binding") != digest
        or proof.get("actor") != adapter.actor
        or proof.get("scope") != adapter.scope
        or adapter.original_key
        and proof.get("original_key") != adapter.original_key
    ):
        raise ValueError("Retain the published recovery's original actor and key.")
    return record, RecipientRecoveryPlan(
        product=adapter.product,
        context=record.context,
        preparation_plan_sha256=record.plan_sha256,
        plan_sha256=digest,
        purpose="published",
        generation=adapter.request.generation,
        publication_sha256=record.publication["published_sha256"],
        authority_sha256=authority_sha,
        capture_protocol=CAPTURE_PROTOCOL,
        recovered=bool(proof) and published_proof_is_fresh(proof),
        capture_sha256=str(proof.get("capture_sha256", "")),
        recovered_at=str(proof.get("recovered_at", "")),
        captured_database_empty=False if proof else None,
        capture_started_at=str(proof.get("capture_started_at", "")),
        capture_duration_ms=proof.get("capture_duration_ms"),
        recovery_fresh_until=str(proof.get("recovery_fresh_until", "")),
    )


def record_published_result(
    adapter: "RecipientRecoveryAdapter",
    record: ProductionRecipientRecord,
    result: RecipientRecoveryPlan,
    evidence: dict[str, str],
    reservation: LaunchplaneIdempotencyRecord | None,
) -> RecipientRecoveryPlan:
    if (
        evidence.get("recipient_recovery_ok") != "true"
        or evidence.get("recipient_recovery_empty") != "false"
        or evidence.get("recipient_recovery_binding") != result.plan_sha256
        or any(
            re.fullmatch(r"[a-f0-9]{64}", evidence.get(key, "")) is None
            for key in (
                "recipient_recovery_capture",
                "recipient_recovery_source",
                "recipient_recovery_data",
            )
        )
        or any(
            not evidence.get(key, "").isdigit()
            for key in (
                "recipient_recovery_epoch",
                "recipient_recovery_started",
                "recipient_recovery_duration_ms",
            )
        )
    ):
        raise ValueError("Only a service-owned populated isolated restore can qualify.")
    started = int(evidence["recipient_recovery_started"])
    ended = int(evidence["recipient_recovery_epoch"])
    duration = int(evidence["recipient_recovery_duration_ms"])
    owner = record.execution.get("published_recovery_owner:" + result.plan_sha256, {})
    if not owner or evidence.get("recipient_recovery_operation") != owner.get("operation"):
        raise ValueError("Recovery witness does not belong to the original operation.")
    published = datetime.fromisoformat(record.publication["published_at"].replace("Z", "+00:00"))
    if (
        started < int(published.timestamp() * 1000)
        or ended < started
        or ended > datetime.now(timezone.utc).timestamp() * 1000 + 1000
        or not 0 < duration <= CAPTURE_TIMEOUT_SECONDS * 1000
    ):
        raise PublishedWitnessUnqualified(
            "Recovery must follow publication and use a bounded capture window."
        )
    authority = authority_snapshot(adapter.store, adapter.product, record.context)
    if canonical_sha256(authority) != result.authority_sha256:
        raise PublishedAuthorityChanged("Published recovery authority changed before commit.")
    proof = {
        "binding": result.plan_sha256,
        "actor": adapter.actor,
        "scope": adapter.scope,
        "original_key": adapter.original_key,
        "generation": adapter.request.generation,
        "publication_sha256": result.publication_sha256,
        "authority_sha256": result.authority_sha256,
        "capture_sha256": evidence["recipient_recovery_capture"],
        "source_identity_sha256": evidence["recipient_recovery_source"],
        "logical_data_sha256": evidence["recipient_recovery_data"],
        "capture_started_at": datetime.fromtimestamp(started / 1000, timezone.utc).isoformat(),
        "recovered_at": datetime.fromtimestamp(ended / 1000, timezone.utc).isoformat(),
        "recovery_fresh_until": (
            datetime.fromtimestamp(ended / 1000, timezone.utc)
            + timedelta(seconds=CAPTURE_WITNESS_MAX_AGE_SECONDS)
        ).isoformat(),
        "capture_duration_ms": duration,
        "protocol": CAPTURE_PROTOCOL,
        "network": "none",
        "captured_database_empty": False,
        "recipient": record.target_id,
        "artifact": record.image_reference,
        "storage": resource_spec(record),
    }
    merge_published_entry(
        adapter, record, "published_recovery:" + result.plan_sha256, proof, authority, reservation
    )
    return adapter.current()[1]


class PublishedAuthorityChanged(ValueError):
    """An exact authority/publication comparison proved the capture outdated."""


class PublishedWitnessUnqualified(ValueError):
    """An authenticated terminal witness cannot satisfy capture-time eligibility."""


def merge_published_entry(
    adapter: "RecipientRecoveryAdapter",
    record: ProductionRecipientRecord,
    key: str,
    entry: dict[str, Any],
    authority: dict[str, Any],
    reservation: LaunchplaneIdempotencyRecord | None,
) -> ProductionRecipientRecord:
    """Merge an immutable entry after a custody collision, never an I/O failure."""
    for attempt in range(3):
        if not same_recipient_authority(
            authority_snapshot(adapter.store, adapter.product, record.context), authority
        ):
            raise PublishedAuthorityChanged("Published authority changed.")
        existing = record.execution.get(key)
        if existing is not None:
            if existing != entry:
                raise ValueError("Published operation identity or evidence changed.")
            return record
        replacement = record.model_copy(update={"execution": {**record.execution, key: entry}})
        try:
            adapter.store.write_production_recipient(
                expected=record.model_dump(mode="json"),
                replacement=replacement.model_dump(mode="json"),
                reservation=reservation,
                expected_authority=authority,
            )
            return replacement
        except ValueError as error:
            observed = custody(adapter.store, adapter.product, record.context)
            if observed is None:
                raise ValueError("Production recipient custody is unavailable.") from error
            if (observed.plan_sha256, observed.publication) != (
                record.plan_sha256,
                record.publication,
            ):
                raise PublishedAuthorityChanged("Published placement changed.")
            if not same_recipient_authority(
                authority_snapshot(adapter.store, adapter.product, record.context), authority
            ):
                raise PublishedAuthorityChanged("Published authority changed.")
            # Only a positively observed custody collision permits a bounded merge.
            if observed == record or attempt == 2:
                raise
            record = observed
    raise RuntimeError("Published custody merge exhausted.")


def own_published_attempt(
    adapter: "RecipientRecoveryAdapter",
    record: ProductionRecipientRecord,
    result: RecipientRecoveryPlan,
    operation_key: str,
    reservation: LaunchplaneIdempotencyRecord,
) -> ProductionRecipientRecord:
    operation = operation_key.removeprefix("provider-operation:")
    if not re.fullmatch(r"[a-f0-9]{64}", operation):
        raise ValueError("Exact durable operation identity required.")
    authority = authority_snapshot(adapter.store, adapter.product, record.context)
    if canonical_sha256(authority) != result.authority_sha256:
        raise PublishedAuthorityChanged("Published authority changed before attempt admission.")
    entry = {
        "operation": operation,
        "original_key": adapter.original_key,
        "actor": adapter.actor,
        "scope": adapter.scope,
        "binding": result.plan_sha256,
        "publication_sha256": result.publication_sha256,
        "authority_sha256": result.authority_sha256,
    }
    return merge_published_entry(
        adapter,
        record,
        "published_recovery_owner:" + result.plan_sha256,
        entry,
        authority,
        reservation,
    )
