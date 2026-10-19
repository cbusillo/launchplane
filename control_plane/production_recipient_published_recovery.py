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
)
from control_plane.production_recipient_publication import require_owned_publication_rows
from control_plane.production_recipient_resources import resource_spec

if TYPE_CHECKING:
    from control_plane.production_recipient_recovery import RecipientRecoveryAdapter

CAPTURE_PROTOCOL = "mariadb-locked-logical-roundtrip-v1"
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
        ended = datetime.fromisoformat(str(proof["recovered_at"]).replace("Z", "+00:00"))
        until = datetime.fromisoformat(str(proof["recovery_fresh_until"]).replace("Z", "+00:00"))
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
        recovered=bool(proof),
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
    published = datetime.fromisoformat(record.publication["published_at"].replace("Z", "+00:00"))
    if (
        started < int(published.timestamp() * 1000)
        or ended < started
        or ended > datetime.now(timezone.utc).timestamp() * 1000 + 1000
        or not 0 < duration <= CAPTURE_TIMEOUT_SECONDS * 1000
    ):
        raise ValueError("Recovery must follow publication and use a bounded capture window.")
    _, current = adapter.current()
    if current.plan_sha256 != result.plan_sha256:
        raise ValueError("Published recovery authority changed during capture.")
    if current.recovered:
        return current
    authority = authority_snapshot(adapter.store, adapter.product, record.context)
    if canonical_sha256(authority) != result.authority_sha256:
        raise ValueError("Published recovery authority changed before commit.")
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
    replacement = record.model_copy(
        update={
            "published_recovery_proofs": {
                **record.published_recovery_proofs,
                result.plan_sha256: proof,
            },
        }
    )
    adapter.store.write_production_recipient(
        expected=record.model_dump(mode="json"),
        replacement=replacement.model_dump(mode="json"),
        reservation=reservation,
        expected_authority=authority,
    )
    return adapter.current()[1]
