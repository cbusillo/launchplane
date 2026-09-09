from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.ordinary_agent import PrincipalProfile
from control_plane.contracts.ordinary_agent_lifecycle import (
    OrdinaryAgentAuthenticationCredentialRecord,
    OrdinaryAgentCredentialCustodyRecord,
    OrdinaryAgentEnrollApplyEnvelope,
    OrdinaryAgentEnrollmentApplyEnvelope,
    OrdinaryAgentEnrollmentReceipt,
    OrdinaryAgentLifecycleAuditRecord,
    OrdinaryAgentPrincipalRecord,
    OrdinaryAgentRevokePrincipalApplyEnvelope,
    OrdinaryAgentRotateCredentialApplyEnvelope,
)


@dataclass(frozen=True)
class OrdinaryAgentLifecycleWriteSet:
    principal: OrdinaryAgentPrincipalRecord
    credential: OrdinaryAgentAuthenticationCredentialRecord | None
    custody: OrdinaryAgentCredentialCustodyRecord | None
    audit: OrdinaryAgentLifecycleAuditRecord
    receipt: OrdinaryAgentEnrollmentReceipt


def build_ordinary_agent_lifecycle_write_set(
    *,
    envelope: OrdinaryAgentEnrollmentApplyEnvelope,
    previous_principal: OrdinaryAgentPrincipalRecord | None,
    previous_credential: OrdinaryAgentAuthenticationCredentialRecord | None,
    execution_profile: PrincipalProfile,
    recorded_at: str,
) -> OrdinaryAgentLifecycleWriteSet:
    principal_revision = (
        1 if previous_principal is None else previous_principal.principal_revision + 1
    )

    if isinstance(envelope, OrdinaryAgentRevokePrincipalApplyEnvelope):
        if previous_principal is None:
            raise ValueError("principal revocation requires a current principal record")
        custody = None
        credential = (
            _record_with_status(
                previous_credential,
                status="revoked",
                revoked_at=max(previous_credential.valid_from, _timestamp_epoch(recorded_at)),
            )
            if previous_credential is not None
            else None
        )
        policy = previous_principal.policy
        credential_id = previous_principal.credential_id
        credential_version = previous_principal.credential_version
        credential_digest = previous_principal.credential_digest
        custody_record_id = previous_principal.custody_record_id
        custody_sha256 = previous_principal.custody_sha256
        principal_status = "revoked"
        outcome = "principal_revoked"
    else:
        candidate = envelope.authentication_credential
        credential_version = (
            1
            if isinstance(envelope, OrdinaryAgentEnrollApplyEnvelope)
            else envelope.credential_version + 1
        )
        credential_id = candidate.credential_id
        credential_digest = candidate.credential_digest
        policy = envelope.policy
        custody = _build_custody_record(
            envelope=envelope,
            credential_id=credential_id,
            credential_version=credential_version,
            recorded_at=recorded_at,
        )
        credential = _build_credential_record(
            envelope=envelope,
            credential_version=credential_version,
            recorded_at=recorded_at,
        )
        custody_record_id = custody.record_id
        custody_sha256 = custody.custody_sha256
        principal_status = "active"
        outcome = (
            "enrolled"
            if isinstance(envelope, OrdinaryAgentEnrollApplyEnvelope)
            else "credential_rotated"
        )

    principal_payload = {
        "schema_version": 1,
        "record_id": f"ordinary-agent-principal-{envelope.principal_id}-r{principal_revision}",
        "principal_id": envelope.principal_id,
        "principal_revision": principal_revision,
        "status": principal_status,
        "execution_profile": execution_profile,
        "credential_id": credential_id,
        "credential_version": credential_version,
        "credential_digest": credential_digest,
        "custody_record_id": custody_record_id,
        "custody_sha256": custody_sha256,
        "policy": policy.model_dump(mode="json"),
        "supersedes_record_id": previous_principal.record_id if previous_principal else None,
        "recorded_at": recorded_at,
    }
    principal = OrdinaryAgentPrincipalRecord.model_validate(
        {
            **principal_payload,
            "record_sha256": lifecycle_record_sha256_from_payload(principal_payload),
        }
    )

    audit_payload = {
        "schema_version": 1,
        "event_id": f"ordinary-agent-lifecycle-{envelope.operation_id}",
        "operation_id": envelope.operation_id,
        "action": envelope.action,
        "principal_id": envelope.principal_id,
        "administrator": envelope.administrator.model_dump(mode="json"),
        "request_sha256": envelope.request_sha256,
        "approval_sha256": envelope.approval_sha256,
        "evidence_sha256": envelope.evidence_sha256,
        "plan_sha256": envelope.plan_sha256,
        "previous_principal_record_id": previous_principal.record_id
        if previous_principal
        else None,
        "previous_principal_sha256": previous_principal.record_sha256
        if previous_principal
        else None,
        "previous_credential_record_id": (
            previous_credential.record_id if previous_credential else None
        ),
        "previous_credential_sha256": (
            previous_credential.record_sha256 if previous_credential else None
        ),
        "resulting_principal_record_id": principal.record_id,
        "resulting_principal_sha256": principal.record_sha256,
        "resulting_credential_record_id": credential.record_id if credential else None,
        "resulting_credential_sha256": credential.record_sha256 if credential else None,
        "custody_record_id": custody.record_id if custody else None,
        "custody_sha256": custody.custody_sha256 if custody else None,
        "outcome": outcome,
        "occurred_at": recorded_at,
    }
    audit = OrdinaryAgentLifecycleAuditRecord.model_validate(
        {
            **audit_payload,
            "audit_sha256": lifecycle_record_sha256_from_payload(audit_payload),
        }
    )
    receipt_payload = {
        "schema_version": 1,
        "operation_id": envelope.operation_id,
        "action": envelope.action,
        "principal_id": envelope.principal_id,
        "principal_record_id": principal.record_id,
        "principal_revision": principal.principal_revision,
        "principal_sha256": principal.record_sha256,
        "credential_id": principal.credential_id,
        "credential_version": principal.credential_version,
        "credential_sha256": credential.record_sha256 if credential else None,
        "custody_record_id": custody.record_id if custody else None,
        "custody_sha256": custody.custody_sha256 if custody else None,
        "audit_event_id": audit.event_id,
        "audit_sha256": audit.audit_sha256,
        "recorded_at": recorded_at,
    }
    receipt = OrdinaryAgentEnrollmentReceipt.model_validate(
        {
            **receipt_payload,
            "result_sha256": lifecycle_record_sha256_from_payload(receipt_payload),
        }
    )
    return OrdinaryAgentLifecycleWriteSet(
        principal=principal,
        credential=credential,
        custody=custody,
        audit=audit,
        receipt=receipt,
    )


def _build_credential_record(
    *,
    envelope: OrdinaryAgentEnrollApplyEnvelope | OrdinaryAgentRotateCredentialApplyEnvelope,
    credential_version: int,
    recorded_at: str,
) -> OrdinaryAgentAuthenticationCredentialRecord:
    candidate = envelope.authentication_credential
    payload = {
        "schema_version": 1,
        "record_id": (
            f"ordinary-agent-auth-credential-{candidate.credential_id}-v{credential_version}"
        ),
        "principal_id": envelope.principal_id,
        "credential_id": candidate.credential_id,
        "credential_version": credential_version,
        "status": "active",
        "credential_digest": candidate.credential_digest,
        "valid_from": candidate.valid_from,
        "expires_at": candidate.expires_at,
        "revoked_at": None,
        "issuance_evidence_sha256": candidate.issuance_evidence_sha256,
        "policy": envelope.policy.model_dump(mode="json"),
        "recorded_at": recorded_at,
    }
    return OrdinaryAgentAuthenticationCredentialRecord.model_validate(
        {**payload, "record_sha256": lifecycle_record_sha256_from_payload(payload)}
    )


def _build_custody_record(
    *,
    envelope: OrdinaryAgentEnrollApplyEnvelope | OrdinaryAgentRotateCredentialApplyEnvelope,
    credential_id: str,
    credential_version: int,
    recorded_at: str,
) -> OrdinaryAgentCredentialCustodyRecord:
    candidate = envelope.custody
    payload = {
        "schema_version": 1,
        "record_id": f"ordinary-agent-custody-{credential_id}-v{credential_version}",
        "principal_id": envelope.principal_id,
        "credential_id": credential_id,
        "credential_version": credential_version,
        "policy": candidate.policy.model_dump(mode="json"),
        "repository_inventory": candidate.repository_inventory.model_dump(mode="json"),
        "target": candidate.target.model_dump(mode="json"),
        "purpose": candidate.purpose,
        "github_app_id": candidate.github_app_id,
        "managed_secret": candidate.managed_secret.model_dump(mode="json"),
        "effect_profiles": candidate.effect_profiles,
        "permissions": tuple(item.model_dump(mode="json") for item in candidate.permissions),
        "valid_from": candidate.valid_from,
        "expires_at": candidate.expires_at,
        "provider_inspection_sha256": candidate.provider_inspection_sha256,
        "predecessor_record_id": candidate.predecessor_record_id,
        "predecessor_sha256": candidate.predecessor_sha256,
        "recorded_at": recorded_at,
    }
    return OrdinaryAgentCredentialCustodyRecord.model_validate(
        {**payload, "custody_sha256": lifecycle_record_sha256_from_payload(payload)}
    )


def _record_with_status(
    record: OrdinaryAgentAuthenticationCredentialRecord,
    *,
    status: Literal["superseded", "revoked"],
    revoked_at: int | None = None,
) -> OrdinaryAgentAuthenticationCredentialRecord:
    payload = record.model_dump(mode="json", exclude={"record_sha256"})
    payload["status"] = status
    payload["revoked_at"] = revoked_at
    return OrdinaryAgentAuthenticationCredentialRecord.model_validate(
        {**payload, "record_sha256": lifecycle_record_sha256_from_payload(payload)}
    )


def supersede_authentication_credential_record(
    record: OrdinaryAgentAuthenticationCredentialRecord,
) -> OrdinaryAgentAuthenticationCredentialRecord:
    return _record_with_status(record, status="superseded")


def lifecycle_record_sha256_from_payload(payload: dict[str, object]) -> str:
    return canonical_json_sha256(payload)


def _timestamp_epoch(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
