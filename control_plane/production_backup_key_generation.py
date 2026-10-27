from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from control_plane.contracts.guest_capture_credential import (
    GuestCaptureBoundary,
    GuestCaptureCredential,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.secret_record import SecretBinding, SecretRecord
from control_plane.guest_capture_credentials import (
    CAPTURE_KEYS,
    plan_guest_capture_credential,
    validate_guest_capture_enrollment,
)
from control_plane.product_config import _plan_product_config_secret_write
from control_plane.production_backup_credentials import BackupCredentialBindingRequest
from control_plane.secrets import LAUNCHPLANE_WORKER_SECRET_INTEGRATION, expected_secret_id
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.storage.product_authority_bundle import ProductAuthorityBundle
from control_plane.contracts.production_backup_gate import ProductionBackupGateRequest
from control_plane.workflows.production_backup_gate import resolve_production_backup_binding
from control_plane.guest_capture_credentials import capture_boundary

GENERATION_SOURCE = "service:capture-key-generation"


def plan_capture_key_generation(
    store: PostgresRecordStore,
    profile: LaunchplaneProductProfileRecord,
    context: str,
    instance: str,
    records: tuple[SecretRecord, ...],
    bindings: tuple[SecretBinding, ...],
    request: BackupCredentialBindingRequest,
    capture_state: dict[str, list[dict[str, Any]]],
) -> tuple[dict[str, object], GuestCaptureBoundary]:
    worker = resolve_production_backup_binding(
        store,
        ProductionBackupGateRequest(
            product=profile.product,
            context=context,
            instance=instance,
            promotion_action=request.promotion_action,
            backup_record_id="key-enrollment-only",
        ),
    )
    boundary = capture_boundary(worker.policy, worker.source_target, worker.destination_target)
    lines = request.known_hosts.strip().splitlines()
    if len(lines) != 1:
        raise ValueError("Supply one independently verified public host key.")
    fields = lines[0].split()
    if len(fields) != 3 or fields[0] != boundary.host:
        raise ValueError("Host verification must name only the approved capture endpoint.")
    serialization.load_ssh_public_key(" ".join(fields[1:]).encode())
    if any(record.name in CAPTURE_KEYS for record in records) or any(
        binding.binding_key in CAPTURE_KEYS for binding in bindings
    ):
        raise ValueError("Initial generation cannot replace existing worker credentials.")
    candidate = GuestCaptureCredential(
        boundary=boundary,
        custody_context=context,
        custody_instance=instance,
        private_key_secret_id=expected_secret_id(
            integration=LAUNCHPLANE_WORKER_SECRET_INTEGRATION,
            name=CAPTURE_KEYS[0],
            context=context,
            instance=instance,
        ),
        known_hosts_secret_id=expected_secret_id(
            integration=LAUNCHPLANE_WORKER_SECRET_INTEGRATION,
            name=CAPTURE_KEYS[1],
            context=context,
            instance=instance,
        ),
    )
    validate_guest_capture_enrollment(candidate, capture_state)
    digest = hashlib.sha256(
        json.dumps(
            {
                "profile": profile.model_dump(mode="json"),
                "records": sorted(
                    (record.model_dump(mode="json") for record in records),
                    key=lambda item: item["secret_id"],
                ),
                "bindings": sorted(
                    (binding.model_dump(mode="json") for binding in bindings),
                    key=lambda item: item["binding_id"],
                ),
                "capture_state": capture_state,
                "request": request.model_dump(exclude={"mode", "reviewed_plan_sha256"}),
                "worker": worker.model_dump(mode="json"),
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    return {
        "plan_sha256": digest,
        "changed": True,
        "boundary_binding_id": boundary.event_id,
        "product": profile.product,
        "context": context,
        "instance": instance,
        "capture_started": False,
    }, boundary


def generate_capture_key_bundle(
    store: PostgresRecordStore,
    *,
    context: str,
    instance: str,
    boundary: GuestCaptureBoundary,
    known_hosts: str,
    actor: str,
) -> tuple[ProductAuthorityBundle, GuestCaptureCredential, dict[str, object]]:
    private = Ed25519PrivateKey.generate()
    public = (
        private.public_key()
        .public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
        .decode("ascii")
    )
    fingerprint = "SHA256:" + base64.b64encode(
        hashlib.sha256(base64.b64decode(public.split()[1])).digest()
    ).decode("ascii").rstrip("=")
    plans = [
        _plan_product_config_secret_write(
            record_store=store,
            scope="context_instance",
            integration=LAUNCHPLANE_WORKER_SECRET_INTEGRATION,
            name=key,
            binding_key=key,
            plaintext_value=value,
            context_name=context,
            instance_name=instance,
            actor=actor,
            source_label=GENERATION_SOURCE,
            value_source="generated",
        )
        for key, value in zip(
            CAPTURE_KEYS,
            (
                private.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.OpenSSH,
                    serialization.NoEncryption(),
                ).decode("ascii"),
                known_hosts.strip() + "\n",
            ),
            strict=True,
        )
    ]
    bundle = ProductAuthorityBundle(
        secret_records=tuple(record for plan in plans for record in plan["secret_records"]),
        secret_versions=tuple(version for plan in plans for version in plan["secret_versions"]),
        secret_bindings=tuple(binding for plan in plans for binding in plan["secret_bindings"]),
        secret_audit_events=tuple(event for plan in plans for event in plan["secret_audit_events"]),
        absent_secret_ids=tuple(str(plan["secret_id"]) for plan in plans),
    )
    enrollment = plan_guest_capture_credential(
        boundary, bundle.secret_records, bundle.secret_bindings, context, instance
    )
    public_metadata: dict[str, object] = {
        "public_key": public,
        "fingerprint": fingerprint,
        "boundary_binding_id": boundary.event_id,
        "private_key_binding": bundle.secret_bindings[0].binding_id,
        "known_hosts_binding": bundle.secret_bindings[1].binding_id,
    }
    event = bundle.secret_audit_events[0]
    bundle.secret_audit_events[0].metadata.update(
        {
            **{key: str(value) for key, value in public_metadata.items()},
            "version_id": bundle.secret_records[0].current_version_id,
        }
    )
    assert event.secret_id == enrollment.private_key_secret_id
    return bundle, enrollment, public_metadata


def generated_capture_keys(
    store: PostgresRecordStore, records: tuple[SecretRecord, ...]
) -> list[dict[str, str]]:
    result = []
    for record in records:
        if record.status != "configured" or record.name != CAPTURE_KEYS[0]:
            continue
        for event in store.list_secret_audit_events(secret_id=record.secret_id):
            if (
                event.metadata.get("source") != GENERATION_SOURCE
                or event.metadata.get("version_id") != record.current_version_id
            ):
                continue
            result.append(
                {
                    key: event.metadata[key]
                    for key in (
                        "public_key",
                        "fingerprint",
                        "boundary_binding_id",
                        "private_key_binding",
                        "known_hosts_binding",
                    )
                }
            )
    return result
