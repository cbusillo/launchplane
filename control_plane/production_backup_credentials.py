"""Metadata-only rebinding of an existing lane's shared-backup credentials."""

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.secret_record import SecretBinding, SecretRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.secrets import LAUNCHPLANE_WORKER_SECRET_INTEGRATION
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.production_backup_gate import SSH_KNOWN_HOSTS, SSH_PRIVATE_KEY
from control_plane.workflows.ship import utc_now_timestamp

BACKUP_CREDENTIAL_KEYS = (SSH_PRIVATE_KEY, SSH_KNOWN_HOSTS)


class BackupCredentialBindingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["dry-run", "apply"] = "dry-run"
    bindings: dict[str, str]
    reason: str = Field(min_length=1, max_length=500)
    reviewed_plan_sha256: str = ""

    @model_validator(mode="after")
    def validate_bindings(self) -> "BackupCredentialBindingRequest":
        if set(self.bindings) != set(BACKUP_CREDENTIAL_KEYS):
            raise ValueError("Select both shared backup credential bindings.")
        if len(set(self.bindings.values())) != len(BACKUP_CREDENTIAL_KEYS):
            raise ValueError("The private key and host-key binding must be distinct.")
        if not self.reason.strip() or any(not value.strip() for value in self.bindings.values()):
            raise ValueError("Binding references and reason must be nonempty.")
        if self.mode == "apply" and len(self.reviewed_plan_sha256) != 64:
            raise ValueError("Apply requires the reviewed plan digest.")
        return self


def credential_metadata(
    store: PostgresRecordStore, *, context: str, instance: str
) -> tuple[tuple[SecretRecord, ...], tuple[SecretBinding, ...]]:
    records = tuple(
        record
        for record in store.list_secret_records(
            integration=LAUNCHPLANE_WORKER_SECRET_INTEGRATION,
            context_name=context,
            instance_name=instance,
        )
        if record.scope == "context_instance"
        and (record.context, record.instance) == (context, instance)
    )
    bindings = store.list_secret_bindings(
        integration=LAUNCHPLANE_WORKER_SECRET_INTEGRATION,
        context_name=context,
        instance_name=instance,
    )
    return records, bindings


def credential_options(
    records: tuple[SecretRecord, ...], bindings: tuple[SecretBinding, ...]
) -> list[dict[str, str]]:
    configured = {r.secret_id: r for r in records if r.status == "configured"}
    return [
        {
            "binding_id": b.binding_id,
            "binding_key": b.binding_key,
            "version_id": configured[b.secret_id].current_version_id,
        }
        for b in bindings
        if b.status == "configured" and b.secret_id in configured
    ]


def plan_credential_bindings(
    profile: LaunchplaneProductProfileRecord,
    records: tuple[SecretRecord, ...],
    bindings: tuple[SecretBinding, ...],
    request: BackupCredentialBindingRequest,
) -> tuple[dict[str, object], tuple[SecretBinding, ...]]:
    available = {item["binding_id"] for item in credential_options(records, bindings)}
    by_id = {b.binding_id: b for b in bindings}
    selected = set(request.bindings.values())
    if not selected <= available:
        raise ValueError("Select configured worker credentials stored exactly for this lane.")
    selected_secrets = {by_id[identifier].secret_id for identifier in selected}
    if len(selected_secrets) != len(selected):
        raise ValueError("Select two distinct credential records.")
    for key, identifier in request.bindings.items():
        if any(
            b.binding_key == key and b.status == "configured" and b.binding_id != identifier
            for b in bindings
        ):
            raise ValueError("A different credential already supplies a shared backup key.")
        source = by_id[identifier]
        if any(b.secret_id == source.secret_id and b.binding_id != identifier for b in bindings):
            raise ValueError("The source credential has ambiguous consumers.")
    changes = [
        {"binding_id": identifier, "from_key": by_id[identifier].binding_key, "to_key": key}
        for key, identifier in sorted(request.bindings.items())
    ]
    digest = hashlib.sha256(
        json.dumps(
            {
                "profile": profile.model_dump(mode="json"),
                "records": [
                    r.model_dump(mode="json") for r in sorted(records, key=lambda r: r.secret_id)
                ],
                "bindings": [
                    b.model_dump(mode="json") for b in sorted(bindings, key=lambda b: b.binding_id)
                ],
                "request": {"bindings": request.bindings, "reason": request.reason},
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    replacements = tuple(
        by_id[item["binding_id"]].model_copy(
            update={
                "binding_key": item["to_key"],
                "updated_at": utc_now_timestamp(),
            }
        )
        for item in changes
        if item["from_key"] != item["to_key"]
    )
    return {"plan_sha256": digest, "changes": changes, "changed": bool(replacements)}, replacements
