from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import base64
import hashlib
import json
from pathlib import Path
import re
import secrets
import subprocess
import tempfile
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.authz_grant_service import (
    AuthzManagedPolicyDiff,
    AuthzManagedPolicyReconcileEnvelope,
    authz_managed_policy_reconcile_audit_payload,
    plan_managed_authz_policy_reconcile,
)
from control_plane.contracts.authz_policy_record import (
    LaunchplaneAuthzPolicyRecord,
    authz_policy_sha256,
    build_authz_policy_record_id,
)
from control_plane.contracts.outbox_delivery import (
    OutboxDeliveryRecord,
    build_outbox_dedupe_key,
    build_outbox_delivery_id,
)
from control_plane.service_auth import (
    GitHubHumanIdentity,
    GitHubHumanPolicyRule,
    LaunchplaneAuthzPolicy,
)


AUTHORIZATION_RECOVERY_NAMESPACE = "launchplane.authorization-recovery.v1"
AUTHORIZATION_RECOVERY_SIGNER_IDENTITY = "launchplane-recovery"
AUTHORIZATION_RECOVERY_MANAGED_SET_ID = "authorization-recovery-root"
AUTHORIZATION_RECOVERY_MANAGED_RULE_ID = "recovered-github-policy-admin"
AUTHORIZATION_RECOVERY_ACTION = "authz_policy_grant.write"
AUTHORIZATION_RECOVERY_PRODUCT = "launchplane"
AUTHORIZATION_RECOVERY_CONTEXT = "launchplane"
AUTHORIZATION_RECOVERY_KEY_TYPES = frozenset({"sk-ssh-ed25519@openssh.com"})
AUTHORIZATION_RECOVERY_CHALLENGE_TTL = timedelta(minutes=10)
AUTHORIZATION_RECOVERY_POP_TTL = timedelta(minutes=10)
AUTHORIZATION_RECOVERY_MAX_OPEN_CHALLENGES = 8
AUTHORIZATION_RECOVERY_SIGNATURE_MAX_BYTES = 16 * 1024
AUTHORIZATION_RECOVERY_VERIFY_TIMEOUT_SECONDS = 5
_AUTHORIZATION_RECOVERY_SOURCE = "service:authorization-recovery"
_HEX_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_RECORD_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,159}$")
_OPENSSH_PUBLIC_KEY_PATTERN = re.compile(
    r"^(?P<key_type>[^\s]+)\s+(?P<key_data>[A-Za-z0-9+/]+={0,2})(?:\s+[^\r\n]+)?$"
)

RecoveryOperation = Literal[
    "initial_bootstrap",
    "restore_known_administrator",
    "replace_recovery_key",
]
RecoveryKeyStatus = Literal["pending", "active", "revoked"]
AuthorizationRecoveryApplyStatus = Literal[
    "applied",
    "adopted",
    "challenge_unavailable",
    "challenge_expired",
    "replayed",
    "signing_key_unavailable",
    "signing_key_inactive",
    "signing_key_mismatch",
    "custody_not_independent",
    "active_policy_missing",
    "active_policy_ambiguous",
    "active_policy_stale",
    "candidate_digest_mismatch",
    "operation_invariant_failed",
    "bootstrap_already_complete",
    "replacement_key_unavailable",
    "replacement_key_not_pending",
    "replacement_key_slot_mismatch",
    "compromised_key_unavailable",
    "compromised_key_not_active",
    "compromised_key_signed",
    "conflict",
    "reconciliation_required",
]
RandomTokenProvider = Callable[[int], str]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str, *, field_name: str = "timestamp") -> datetime:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"Authorization recovery {field_name} is required.")
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(
            f"Authorization recovery {field_name} must be an ISO timestamp."
        ) from error
    if parsed.tzinfo is None:
        raise ValueError(f"Authorization recovery {field_name} must include a timezone.")
    return parsed.astimezone(timezone.utc)


def _canonical_bytes(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "utf-8"
    )


def _new_token(byte_count: int = 32) -> str:
    return secrets.token_urlsafe(byte_count)


def _require_record_token(value: str, *, field_name: str) -> str:
    normalized = value.strip()
    if _RECORD_ID_PATTERN.fullmatch(normalized) is None:
        raise ValueError(f"Authorization recovery {field_name} is invalid.")
    return normalized


def _require_sha256(value: str, *, field_name: str) -> str:
    normalized = value.strip().lower()
    if _HEX_SHA256_PATTERN.fullmatch(normalized) is None:
        raise ValueError(f"Authorization recovery {field_name} must be a SHA-256 hex digest.")
    return normalized


def _openssh_key_blob_type(key_blob: bytes) -> str:
    if len(key_blob) < 4:
        raise ValueError("Recovery public key has malformed OpenSSH key material.")
    key_type_size = int.from_bytes(key_blob[:4], "big")
    key_type_end = 4 + key_type_size
    if key_type_size <= 0 or key_type_end > len(key_blob):
        raise ValueError("Recovery public key has malformed OpenSSH key material.")
    try:
        return key_blob[4:key_type_end].decode("ascii")
    except UnicodeDecodeError as error:
        raise ValueError("Recovery public key has malformed OpenSSH key material.") from error


def _canonicalize_openssh_public_key(
    public_key: str,
    *,
    allowed_key_types: frozenset[str],
) -> tuple[str, str, bytes]:
    stripped = public_key.strip()
    if "\n" in stripped or "\r" in stripped:
        raise ValueError("Recovery public key must be a single OpenSSH public-key line.")
    normalized_line = " ".join(stripped.split())
    match = _OPENSSH_PUBLIC_KEY_PATTERN.fullmatch(normalized_line)
    if match is None:
        raise ValueError("Recovery public key must be a single OpenSSH public-key line.")
    key_type = match.group("key_type")
    if key_type not in allowed_key_types:
        raise ValueError(
            "Recovery public key type is not an allowed hardware-backed security-key type."
        )
    try:
        decoded = base64.b64decode(match.group("key_data"), validate=True)
    except ValueError as error:
        raise ValueError("Recovery public key has invalid OpenSSH base64 material.") from error
    if not decoded:
        raise ValueError("Recovery public key has empty OpenSSH key material.")
    if _openssh_key_blob_type(decoded) != key_type:
        raise ValueError("Recovery public key type does not match its OpenSSH key material.")
    canonical_key = f"{key_type} {base64.b64encode(decoded).decode('ascii')}"
    return canonical_key, key_type, decoded


def canonicalize_recovery_public_key(public_key: str) -> tuple[str, str, bytes]:
    return _canonicalize_openssh_public_key(
        public_key,
        allowed_key_types=AUTHORIZATION_RECOVERY_KEY_TYPES,
    )


def recovery_key_fingerprint(public_key: str) -> str:
    _, _, key_blob = canonicalize_recovery_public_key(public_key)
    return hashlib.sha256(key_blob).hexdigest()


def validate_recovery_public_key(public_key: str) -> tuple[str, str]:
    canonical_key, key_type, _ = canonicalize_recovery_public_key(public_key)
    return canonical_key, key_type


class AuthorizationRecoveryKey(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key_id: str = Field(min_length=1, max_length=160)
    custody_slot: str = Field(min_length=1, max_length=120)
    public_key: str = Field(min_length=1, max_length=8192)
    fingerprint_sha256: str = Field(min_length=64, max_length=64)
    key_type: str = Field(min_length=1, max_length=64)
    status: RecoveryKeyStatus = "pending"
    enrolled_at: str = Field(min_length=1, max_length=40)
    activated_at: str = Field(default="", max_length=40)
    revoked_at: str = Field(default="", max_length=40)
    proof_challenge: str = Field(default="", max_length=128)
    proof_expires_at: str = Field(default="", max_length=40)

    @model_validator(mode="after")
    def _validate_key(self) -> "AuthorizationRecoveryKey":
        canonical_key, key_type = validate_recovery_public_key(self.public_key)
        fingerprint = _require_sha256(self.fingerprint_sha256, field_name="fingerprint_sha256")
        if fingerprint != recovery_key_fingerprint(canonical_key):
            raise ValueError("Recovery key fingerprint does not match its public key blob.")
        _parse_timestamp(self.enrolled_at, field_name="enrolled_at")
        if self.activated_at:
            _parse_timestamp(self.activated_at, field_name="activated_at")
        if self.revoked_at:
            _parse_timestamp(self.revoked_at, field_name="revoked_at")
        if self.proof_expires_at:
            _parse_timestamp(self.proof_expires_at, field_name="proof_expires_at")
        if self.status == "pending" and (not self.proof_challenge or not self.proof_expires_at):
            raise ValueError("Pending recovery key requires a proof-of-possession challenge.")
        if self.status != "pending" and (self.proof_challenge or self.proof_expires_at):
            raise ValueError(
                "Non-pending recovery key cannot retain a proof-of-possession challenge."
            )
        if self.status == "active" and not self.activated_at:
            raise ValueError("Active recovery key requires activation evidence.")
        if self.status == "revoked" and not self.revoked_at:
            raise ValueError("Revoked recovery key requires revocation evidence.")
        if self.key_type != key_type or self.public_key != canonical_key:
            raise ValueError("Recovery key must store the canonical public key and key type.")
        _require_record_token(self.key_id, field_name="key_id")
        _require_record_token(self.custody_slot, field_name="custody_slot")
        return self

    def redacted(self) -> dict[str, str]:
        return {
            "key_id": self.key_id,
            "custody_slot": self.custody_slot,
            "fingerprint_sha256": self.fingerprint_sha256,
            "key_type": self.key_type,
            "status": self.status,
            "enrolled_at": self.enrolled_at,
            "activated_at": self.activated_at,
            "revoked_at": self.revoked_at,
        }


class AuthorizationBootstrapState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    record_id: Literal["authorization-recovery-bootstrap"] = "authorization-recovery-bootstrap"
    status: Literal["complete"] = "complete"
    completed_at: str = Field(min_length=1, max_length=40)
    completed_by_github_id: int = Field(ge=1)
    completion_challenge_id: str = Field(min_length=1, max_length=160)

    @model_validator(mode="after")
    def _validate_state(self) -> "AuthorizationBootstrapState":
        _parse_timestamp(self.completed_at, field_name="completed_at")
        _require_record_token(self.completion_challenge_id, field_name="completion_challenge_id")
        return self


class AuthorizationRecoveryChallenge(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    challenge_id: str = Field(min_length=1, max_length=160)
    operation: RecoveryOperation
    intended_github_id: int = Field(ge=1)
    nonce: str = Field(min_length=24, max_length=160)
    issued_at: str = Field(min_length=1, max_length=40)
    expires_at: str = Field(min_length=1, max_length=40)
    active_policy_record_id: str = Field(min_length=1, max_length=180)
    active_policy_revision: int = Field(ge=1)
    active_policy_sha256: str = Field(min_length=64, max_length=64)
    candidate_policy_sha256: str = Field(min_length=64, max_length=64)
    candidate_record_id: str = Field(min_length=1, max_length=180)
    plan_sha256: str = Field(min_length=64, max_length=64)
    signing_key_id: str = Field(min_length=1, max_length=160)
    signing_key_fingerprint_sha256: str = Field(min_length=64, max_length=64)
    compromised_key_id: str = Field(default="", max_length=160)
    replacement_key_id: str = Field(default="", max_length=160)
    replacement_custody_slot: str = Field(default="", max_length=120)
    replacement_key_fingerprint_sha256: str = Field(default="", max_length=64)
    replacement_public_key: str = Field(default="", max_length=8192)
    used_at: str = Field(default="", max_length=40)

    @model_validator(mode="after")
    def _validate_challenge(self) -> "AuthorizationRecoveryChallenge":
        _require_record_token(self.challenge_id, field_name="challenge_id")
        _parse_timestamp(self.issued_at, field_name="issued_at")
        _parse_timestamp(self.expires_at, field_name="expires_at")
        if _parse_timestamp(self.expires_at, field_name="expires_at") <= _parse_timestamp(
            self.issued_at, field_name="issued_at"
        ):
            raise ValueError("Authorization recovery challenge expires_at must be after issued_at.")
        if self.used_at:
            _parse_timestamp(self.used_at, field_name="used_at")
        _require_sha256(self.active_policy_sha256, field_name="active_policy_sha256")
        _require_sha256(self.candidate_policy_sha256, field_name="candidate_policy_sha256")
        _require_sha256(self.plan_sha256, field_name="plan_sha256")
        _require_sha256(
            self.signing_key_fingerprint_sha256,
            field_name="signing_key_fingerprint_sha256",
        )
        _require_record_token(self.signing_key_id, field_name="signing_key_id")
        if self.operation == "replace_recovery_key":
            for field_name, value in (
                ("compromised_key_id", self.compromised_key_id),
                ("replacement_key_id", self.replacement_key_id),
                ("replacement_custody_slot", self.replacement_custody_slot),
            ):
                _require_record_token(value, field_name=field_name)
            _require_sha256(
                self.replacement_key_fingerprint_sha256,
                field_name="replacement_key_fingerprint_sha256",
            )
            canonical_key, _ = validate_recovery_public_key(self.replacement_public_key)
            if self.replacement_public_key != canonical_key:
                raise ValueError("Replacement recovery key must be canonicalized.")
            if self.signing_key_id in {self.compromised_key_id, self.replacement_key_id}:
                raise ValueError("Recovery key rotation must be signed by a different active key.")
        elif any(
            (
                self.compromised_key_id,
                self.replacement_key_id,
                self.replacement_custody_slot,
                self.replacement_key_fingerprint_sha256,
                self.replacement_public_key,
            )
        ):
            raise ValueError("Non-rotation recovery challenges cannot retain rotation fields.")
        return self

    def canonical_payload(self, *, service_identity: str) -> dict[str, object]:
        payload: dict[str, object] = {
            "service_identity": service_identity,
            "namespace": AUTHORIZATION_RECOVERY_NAMESPACE,
            "operation": self.operation,
            "challenge_id": self.challenge_id,
            "nonce": self.nonce,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "active_policy_record_id": self.active_policy_record_id,
            "active_policy_revision": self.active_policy_revision,
            "active_policy_sha256": self.active_policy_sha256,
            "candidate_policy_sha256": self.candidate_policy_sha256,
            "candidate_record_id": self.candidate_record_id,
            "intended_github_id": self.intended_github_id,
            "managed_set_id": AUTHORIZATION_RECOVERY_MANAGED_SET_ID,
            "managed_rule_id": AUTHORIZATION_RECOVERY_MANAGED_RULE_ID,
            "plan_sha256": self.plan_sha256,
            "signing_key_id": self.signing_key_id,
            "signing_key_fingerprint_sha256": self.signing_key_fingerprint_sha256,
        }
        if self.operation == "replace_recovery_key":
            payload.update(
                {
                    "compromised_key_id": self.compromised_key_id,
                    "replacement_key_id": self.replacement_key_id,
                    "replacement_custody_slot": self.replacement_custody_slot,
                    "replacement_key_fingerprint_sha256": self.replacement_key_fingerprint_sha256,
                    "replacement_public_key": self.replacement_public_key,
                }
            )
        return payload

    def canonical_bytes(self, *, service_identity: str) -> bytes:
        return _canonical_bytes(self.canonical_payload(service_identity=service_identity))


class AuthorizationRecoveryAudit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    audit_id: str = Field(min_length=1, max_length=160)
    event: str = Field(min_length=1, max_length=80)
    status: Literal["accepted", "rejected", "completed"]
    recorded_at: str = Field(min_length=1, max_length=40)
    challenge_id: str = Field(default="", max_length=160)
    operation: RecoveryOperation | Literal[""] = ""
    key_id: str = Field(default="", max_length=160)
    key_fingerprint_sha256: str = Field(default="", max_length=64)
    intended_github_id: int | None = Field(default=None, ge=1)
    reason_code: str = Field(default="", max_length=120)
    payload: dict[str, object] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_audit(self) -> "AuthorizationRecoveryAudit":
        _parse_timestamp(self.recorded_at, field_name="recorded_at")
        if self.key_fingerprint_sha256:
            _require_sha256(self.key_fingerprint_sha256, field_name="key_fingerprint_sha256")
        return self


class AuthorizationRecoveryApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    challenge: AuthorizationRecoveryChallenge
    signing_key_id: str = Field(min_length=1, max_length=160)
    signing_key_fingerprint_sha256: str = Field(min_length=64, max_length=64)
    candidate_record: LaunchplaneAuthzPolicyRecord
    completed_at: str = Field(min_length=1, max_length=40)
    audit: AuthorizationRecoveryAudit
    outbox_delivery: OutboxDeliveryRecord

    @model_validator(mode="after")
    def _validate_request(self) -> "AuthorizationRecoveryApplyRequest":
        if self.signing_key_id != self.challenge.signing_key_id:
            raise ValueError("Authorization recovery signing key ID does not match the challenge.")
        if self.signing_key_fingerprint_sha256 != self.challenge.signing_key_fingerprint_sha256:
            raise ValueError(
                "Authorization recovery signing key fingerprint does not match the challenge."
            )
        if self.candidate_record.status != "active":
            raise ValueError("Authorization recovery candidate policy must be active.")
        if self.candidate_record.policy_sha256 != self.challenge.candidate_policy_sha256:
            raise ValueError(
                "Authorization recovery candidate digest does not match the challenge."
            )
        if self.candidate_record.record_id != self.challenge.candidate_record_id:
            raise ValueError(
                "Authorization recovery candidate record ID does not match the challenge."
            )
        _parse_timestamp(self.completed_at, field_name="completed_at")
        return self


class AuthorizationRecoveryApplyResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: AuthorizationRecoveryApplyStatus
    authz_policy_record: LaunchplaneAuthzPolicyRecord | None = None
    bootstrap_state: AuthorizationBootstrapState | None = None
    challenge: AuthorizationRecoveryChallenge | None = None
    audit: AuthorizationRecoveryAudit | None = None
    outbox_delivery: OutboxDeliveryRecord | None = None
    current_record: LaunchplaneAuthzPolicyRecord | None = None
    reason_code: str = ""


class AuthorizationRecoveryStore(Protocol):
    def read_authorization_bootstrap_state(self) -> AuthorizationBootstrapState | None: ...

    def complete_authorization_bootstrap_once(self, state: AuthorizationBootstrapState) -> bool: ...

    def list_authorization_recovery_keys(self) -> tuple[AuthorizationRecoveryKey, ...]: ...

    def read_authorization_recovery_key(self, key_id: str) -> AuthorizationRecoveryKey | None: ...

    def write_authorization_recovery_key(self, key: AuthorizationRecoveryKey) -> None: ...

    def read_authorization_recovery_challenge(
        self, challenge_id: str
    ) -> AuthorizationRecoveryChallenge | None: ...

    def list_authorization_recovery_challenges(
        self,
    ) -> tuple[AuthorizationRecoveryChallenge, ...]: ...

    def write_authorization_recovery_challenge(
        self, challenge: AuthorizationRecoveryChallenge
    ) -> None: ...

    def write_authorization_recovery_audit(self, audit: AuthorizationRecoveryAudit) -> None: ...

    def list_authz_policy_records(
        self,
        *,
        status: str = "",
        limit: int | None = None,
    ) -> tuple[LaunchplaneAuthzPolicyRecord, ...]: ...

    def apply_authorization_recovery(
        self, request: AuthorizationRecoveryApplyRequest
    ) -> AuthorizationRecoveryApplyResult: ...


@dataclass(frozen=True)
class RecoveryPrepareResult:
    challenge: AuthorizationRecoveryChallenge
    canonical_request: bytes
    plan: AuthzManagedPolicyReconcileEnvelope


class AuthorizationRecoveryService:
    def __init__(
        self,
        *,
        record_store: AuthorizationRecoveryStore,
        service_identity: str,
        now: Callable[[], datetime] = _utc_now,
        random_token: RandomTokenProvider = _new_token,
    ) -> None:
        self._record_store = record_store
        self._service_identity = service_identity.strip()
        self._now = now
        self._random_token = random_token
        if not self._service_identity:
            raise ValueError("Authorization recovery requires a stable service identity.")

    def bootstrap_state(self) -> Literal["pending", "complete"]:
        return "complete" if self._record_store.read_authorization_bootstrap_state() else "pending"

    def readiness(self) -> dict[str, object]:
        keys = self._record_store.list_authorization_recovery_keys()
        active = [key for key in keys if key.status == "active"]
        return {
            "bootstrap_status": self.bootstrap_state(),
            "active_key_count": len(active),
            "independent_custody_slot_count": len({key.custody_slot for key in active}),
            "ready": _active_custody_slot_count(active) >= 2,
            "keys": [key.redacted() for key in keys],
        }

    def enroll_key(
        self, *, key_id: str, custody_slot: str, public_key: str
    ) -> AuthorizationRecoveryKey:
        if self.bootstrap_state() == "complete":
            raise PermissionError(
                "Recovery-key enrollment after bootstrap completion requires browser administration."
            )
        normalized_key_id = _require_record_token(key_id, field_name="key_id")
        if self._record_store.read_authorization_recovery_key(normalized_key_id) is not None:
            raise ValueError("Recovery key ID already exists.")
        normalized_key, key_type = validate_recovery_public_key(public_key)
        normalized_slot = _require_record_token(custody_slot, field_name="custody_slot")
        for existing_key in self._record_store.list_authorization_recovery_keys():
            if existing_key.custody_slot == normalized_slot and existing_key.status != "revoked":
                raise ValueError("Recovery key custody slot already has a non-revoked key.")
        self._require_prepare_capacity()
        now = self._now()
        record = AuthorizationRecoveryKey(
            key_id=normalized_key_id,
            custody_slot=normalized_slot,
            public_key=normalized_key,
            fingerprint_sha256=recovery_key_fingerprint(normalized_key),
            key_type=key_type,
            enrolled_at=_timestamp(now),
            proof_challenge=self._random_token(32),
            proof_expires_at=_timestamp(now + AUTHORIZATION_RECOVERY_POP_TTL),
        )
        self._record_store.write_authorization_recovery_key(record)
        self._audit(event="key_enrolled", status="accepted", key=record)
        return record

    def proof_bytes(self, *, key_id: str) -> bytes:
        key = self._require_key(key_id)
        if key.status != "pending" or _parse_timestamp(key.proof_expires_at) <= self._now():
            raise ValueError("Recovery key proof-of-possession challenge is unavailable.")
        return _canonical_bytes(
            {
                "service_identity": self._service_identity,
                "namespace": AUTHORIZATION_RECOVERY_NAMESPACE,
                "operation": "recovery_key_proof_of_possession",
                "key_id": key.key_id,
                "custody_slot": key.custody_slot,
                "key_type": key.key_type,
                "fingerprint_sha256": key.fingerprint_sha256,
                "challenge": key.proof_challenge,
                "expires_at": key.proof_expires_at,
            }
        )

    def verify_key_proof(self, *, key_id: str, signature: bytes) -> AuthorizationRecoveryKey:
        key = self._require_key(key_id)
        if key.status != "pending" or _parse_timestamp(key.proof_expires_at) <= self._now():
            self._audit(event="key_proof", status="rejected", key=key, reason_code="proof_expired")
            raise ValueError("Recovery key proof-of-possession challenge is unavailable.")
        _verify_sshsig(
            public_key=key.public_key, payload=self.proof_bytes(key_id=key_id), signature=signature
        )
        activated = key.model_copy(
            update={
                "status": "active",
                "activated_at": _timestamp(self._now()),
                "proof_challenge": "",
                "proof_expires_at": "",
            }
        )
        self._record_store.write_authorization_recovery_key(activated)
        self._audit(event="key_proof", status="completed", key=activated)
        return activated

    def revoke_key(self, *, key_id: str) -> AuthorizationRecoveryKey:
        key = self._require_key(key_id)
        if key.status == "revoked":
            return key
        active = [
            candidate
            for candidate in self._record_store.list_authorization_recovery_keys()
            if candidate.status == "active"
        ]
        remaining = [candidate for candidate in active if candidate.key_id != key.key_id]
        if key.status == "active" and _active_custody_slot_count(remaining) < 2:
            raise ValueError(
                "Recovery key revocation cannot leave fewer than two independent active keys."
            )
        revoked = key.model_copy(
            update={
                "status": "revoked",
                "revoked_at": _timestamp(self._now()),
                "proof_challenge": "",
                "proof_expires_at": "",
            }
        )
        self._record_store.write_authorization_recovery_key(revoked)
        self._audit(event="key_revoked", status="completed", key=revoked)
        return revoked

    def prepare(
        self,
        *,
        operation: RecoveryOperation,
        intended_github_id: int,
        signing_key_id: str,
        compromised_key_id: str = "",
        replacement_key_id: str = "",
    ) -> RecoveryPrepareResult:
        self._require_operation_allowed(operation)
        self._require_recovery_readiness()
        self._require_prepare_capacity()
        signing_key = self._require_active_key(signing_key_id)
        replacement_key: AuthorizationRecoveryKey | None = None
        compromised_key: AuthorizationRecoveryKey | None = None
        if operation == "replace_recovery_key":
            replacement_key = self._require_key(replacement_key_id)
            compromised_key = self._require_active_key(compromised_key_id)
            if replacement_key.status != "pending":
                raise ValueError("Recovery-key replacement target must be pending.")
            if signing_key.key_id in {replacement_key.key_id, compromised_key.key_id}:
                raise ValueError("Recovery key rotation must be signed by a different active key.")
            active_after_rotation = [
                key
                for key in self._record_store.list_authorization_recovery_keys()
                if key.status == "active" and key.key_id != compromised_key.key_id
            ] + [
                replacement_key.model_copy(
                    update={
                        "status": "active",
                        "activated_at": _timestamp(self._now()),
                        "proof_challenge": "",
                        "proof_expires_at": "",
                    }
                )
            ]
            if _active_custody_slot_count(active_after_rotation) < 2:
                raise ValueError(
                    "Recovery key rotation cannot leave fewer than two independent active keys."
                )
        elif compromised_key_id or replacement_key_id:
            raise ValueError("Only recovery-key rotation accepts key replacement fields.")
        active_record = self._require_single_active_policy()
        plan, candidate_record, diff = self._candidate_record(
            active_record=active_record,
            intended_github_id=intended_github_id,
            trace_id="prepare",
        )
        self._validate_fixed_recovery_policy_diff(diff)
        now = self._now()
        challenge = AuthorizationRecoveryChallenge(
            challenge_id=f"authz-recovery-{self._random_token(24)}",
            operation=operation,
            intended_github_id=intended_github_id,
            nonce=self._random_token(32),
            issued_at=_timestamp(now),
            expires_at=_timestamp(now + AUTHORIZATION_RECOVERY_CHALLENGE_TTL),
            active_policy_record_id=active_record.record_id,
            active_policy_revision=active_record.revision,
            active_policy_sha256=active_record.policy_sha256,
            candidate_policy_sha256=candidate_record.policy_sha256,
            candidate_record_id=candidate_record.record_id,
            plan_sha256=diff.plan_sha256,
            signing_key_id=signing_key.key_id,
            signing_key_fingerprint_sha256=signing_key.fingerprint_sha256,
            compromised_key_id=compromised_key.key_id if compromised_key else "",
            replacement_key_id=replacement_key.key_id if replacement_key else "",
            replacement_custody_slot=replacement_key.custody_slot if replacement_key else "",
            replacement_key_fingerprint_sha256=replacement_key.fingerprint_sha256
            if replacement_key
            else "",
            replacement_public_key=replacement_key.public_key if replacement_key else "",
        )
        self._record_store.write_authorization_recovery_challenge(challenge)
        self._audit(event="prepare", status="accepted", challenge=challenge, key=signing_key)
        return RecoveryPrepareResult(
            challenge=challenge,
            canonical_request=challenge.canonical_bytes(service_identity=self._service_identity),
            plan=plan,
        )

    def apply(
        self,
        *,
        challenge_id: str,
        key_id: str,
        signature: bytes,
        trace_id: str,
    ) -> AuthorizationRecoveryApplyResult:
        challenge = self._record_store.read_authorization_recovery_challenge(challenge_id.strip())
        if challenge is None or _parse_timestamp(challenge.expires_at) <= self._now():
            self._audit(event="apply", status="rejected", reason_code="challenge_unavailable")
            raise ValueError("Recovery challenge is unavailable.")
        if challenge.used_at:
            self._audit(
                event="apply", status="rejected", challenge=challenge, reason_code="replayed"
            )
            raise ValueError("Recovery challenge has already been consumed.")
        if key_id.strip() != challenge.signing_key_id:
            self._audit(
                event="apply",
                status="rejected",
                challenge=challenge,
                reason_code="signing_key_mismatch",
            )
            raise ValueError("Recovery signing key does not match the prepared challenge.")
        key = self._require_active_key(key_id)
        if key.fingerprint_sha256 != challenge.signing_key_fingerprint_sha256:
            self._audit(
                event="apply",
                status="rejected",
                challenge=challenge,
                key=key,
                reason_code="signing_key_mismatch",
            )
            raise ValueError(
                "Recovery signing key fingerprint does not match the prepared challenge."
            )
        self._require_recovery_readiness()
        _verify_sshsig(
            public_key=key.public_key,
            payload=challenge.canonical_bytes(service_identity=self._service_identity),
            signature=signature,
        )
        active_record = self._require_single_active_policy()
        plan, candidate_record, diff = self._candidate_record(
            active_record=active_record,
            intended_github_id=challenge.intended_github_id,
            trace_id=trace_id,
        )
        self._validate_fixed_recovery_policy_diff(diff)
        if diff.plan_sha256 != challenge.plan_sha256:
            self._audit(
                event="apply",
                status="rejected",
                challenge=challenge,
                key=key,
                reason_code="plan_drift",
            )
            raise ValueError("Recovery plan drifted after challenge preparation.")
        if candidate_record.policy_sha256 != challenge.candidate_policy_sha256:
            self._audit(
                event="apply",
                status="rejected",
                challenge=challenge,
                key=key,
                reason_code="candidate_drift",
            )
            raise ValueError("Recovery candidate drifted after challenge preparation.")
        completed_at = _timestamp(self._now())
        audit = self._audit_record(
            event="apply",
            status="completed",
            challenge=challenge,
            key=key,
            recorded_at=completed_at,
            payload={
                "candidate_record_id": candidate_record.record_id,
                "candidate_policy_sha256": candidate_record.policy_sha256,
                "trace_id": trace_id.strip(),
            },
        )
        request = AuthorizationRecoveryApplyRequest(
            challenge=challenge,
            signing_key_id=key.key_id,
            signing_key_fingerprint_sha256=key.fingerprint_sha256,
            candidate_record=candidate_record,
            completed_at=completed_at,
            audit=audit,
            outbox_delivery=_operator_alert_delivery(
                challenge=challenge,
                key=key,
                candidate_record=candidate_record,
                created_at=completed_at,
            ),
        )
        result = self._record_store.apply_authorization_recovery(request)
        if result.status not in {"applied", "adopted"}:
            self._record_store.write_authorization_recovery_audit(
                self._audit_record(
                    event="apply",
                    status="rejected",
                    challenge=challenge,
                    key=key,
                    reason_code=result.status,
                )
            )
            raise ValueError(f"Recovery apply did not complete safely: {result.status}.")
        return result

    def _candidate_record(
        self,
        *,
        active_record: LaunchplaneAuthzPolicyRecord,
        intended_github_id: int,
        trace_id: str,
    ) -> tuple[
        AuthzManagedPolicyReconcileEnvelope, LaunchplaneAuthzPolicyRecord, AuthzManagedPolicyDiff
    ]:
        preview = self._recovery_plan(
            active_policy=active_record.policy,
            intended_github_id=intended_github_id,
            mode="dry_run",
        )
        _, observed_record, updated_policy, diff = plan_managed_authz_policy_reconcile(
            record_store=self._record_store,
            request=preview,
        )
        if (
            observed_record.record_id != active_record.record_id
            or observed_record.revision != active_record.revision
            or observed_record.policy_sha256 != active_record.policy_sha256
        ):
            raise ValueError("Recovery active authorization policy changed while planning.")
        plan = self._recovery_plan(
            active_policy=active_record.policy,
            intended_github_id=intended_github_id,
            mode="apply",
            reviewed_plan_sha256=diff.plan_sha256,
        )
        policy_sha256 = authz_policy_sha256(updated_policy)
        candidate_record = LaunchplaneAuthzPolicyRecord(
            record_id=build_authz_policy_record_id(
                revision=diff.candidate_revision,
                policy_sha256=policy_sha256,
            ),
            revision=diff.candidate_revision,
            status="active",
            source=_AUTHORIZATION_RECOVERY_SOURCE,
            updated_at=_timestamp(self._now()),
            policy_sha256=policy_sha256,
            policy=updated_policy,
            audit=authz_managed_policy_reconcile_audit_payload(
                request=plan,
                identity=_recovered_identity(intended_github_id),
                previous_record=active_record,
                new_record=None,
                diff=diff,
                trace_id=trace_id,
                now_timestamp=lambda: _timestamp(self._now()),
            ),
        )
        return plan, candidate_record, diff

    def _recovery_plan(
        self,
        *,
        active_policy: LaunchplaneAuthzPolicy,
        intended_github_id: int,
        mode: Literal["dry_run", "apply"],
        reviewed_plan_sha256: str = "",
    ) -> AuthzManagedPolicyReconcileEnvelope:
        desired_policy = LaunchplaneAuthzPolicy(
            schema_version=2,
            github_humans=(
                GitHubHumanPolicyRule(
                    github_ids=(intended_github_id,),
                    roles=("admin",),
                    products=(AUTHORIZATION_RECOVERY_PRODUCT,),
                    contexts=(AUTHORIZATION_RECOVERY_CONTEXT,),
                    actions=(AUTHORIZATION_RECOVERY_ACTION,),
                    managed_set_id=AUTHORIZATION_RECOVERY_MANAGED_SET_ID,
                    managed_rule_id=AUTHORIZATION_RECOVERY_MANAGED_RULE_ID,
                ),
            ),
        )
        return AuthzManagedPolicyReconcileEnvelope(
            product=AUTHORIZATION_RECOVERY_PRODUCT,
            mode=mode,
            managed_set_id=AUTHORIZATION_RECOVERY_MANAGED_SET_ID,
            schema_migration="migrate_v1_to_v2" if active_policy.schema_version != 2 else "reject",
            unmanaged_adoption="reject",
            reason="hardware-backed authorization recovery",
            related_issue="https://github.com/cbusillo/launchplane/issues/2239",
            reviewed_plan_sha256=reviewed_plan_sha256,
            desired_policy=desired_policy,
        )

    def _validate_fixed_recovery_policy_diff(self, diff: AuthzManagedPolicyDiff) -> None:
        if diff.managed_set_id != AUTHORIZATION_RECOVERY_MANAGED_SET_ID:
            raise ValueError("Recovery plan targeted the wrong managed authz set.")
        if diff.adopted_rule_count or diff.updated_rule_count or diff.removed_rule_count:
            raise ValueError("Recovery plan may only add or preserve the fixed managed rule.")
        if diff.added_rule_count not in {0, 1}:
            raise ValueError("Recovery plan must add at most one managed rule.")
        changed_rule_ids = {change.managed_rule_id for change in diff.changes}
        if changed_rule_ids and changed_rule_ids != {AUTHORIZATION_RECOVERY_MANAGED_RULE_ID}:
            raise ValueError("Recovery plan changed unrelated managed rules.")
        if diff.operational_readiness_blocked_rule_count:
            raise ValueError("Recovery plan has operational-readiness blockers.")
        permitted_blockers = {
            "authz_policy_admin_unreachable",
            "authz_policy_independent_admin_unreachable",
        }
        if any(blocker.code not in permitted_blockers for blocker in diff.policy_safety_blockers):
            raise ValueError("Recovery plan has unexpected policy-safety blockers.")

    def _require_operation_allowed(self, operation: RecoveryOperation) -> None:
        state = self.bootstrap_state()
        if operation == "initial_bootstrap" and state != "pending":
            raise ValueError("Initial authorization bootstrap is already complete.")
        if operation != "initial_bootstrap" and state != "complete":
            raise ValueError(
                "Recovery cannot run before initial authorization bootstrap completes."
            )

    def _require_prepare_capacity(self) -> None:
        now = self._now()
        open_challenge_count = sum(
            challenge.used_at == "" and _parse_timestamp(challenge.expires_at) > now
            for challenge in self._record_store.list_authorization_recovery_challenges()
        )
        if open_challenge_count >= AUTHORIZATION_RECOVERY_MAX_OPEN_CHALLENGES:
            self._audit(event="prepare", status="rejected", reason_code="challenge_rate_limited")
            raise ValueError("Recovery challenge rate limit exceeded.")

    def _require_single_active_policy(self) -> LaunchplaneAuthzPolicyRecord:
        active_records = self._record_store.list_authz_policy_records(status="active", limit=2)
        if len(active_records) != 1:
            raise ValueError("Recovery requires exactly one active authorization policy record.")
        return active_records[0]

    def _require_key(self, key_id: str) -> AuthorizationRecoveryKey:
        key = self._record_store.read_authorization_recovery_key(key_id.strip())
        if key is None:
            raise ValueError("Recovery key was not found.")
        return key

    def _require_active_key(self, key_id: str) -> AuthorizationRecoveryKey:
        key = self._require_key(key_id)
        if key.status != "active":
            raise ValueError("Recovery key is not active.")
        return key

    def _require_recovery_readiness(self) -> None:
        if not self.readiness()["ready"]:
            raise ValueError("Recovery requires two active independently custodied public keys.")

    def _audit(
        self,
        *,
        event: str,
        status: Literal["accepted", "rejected", "completed"],
        challenge: AuthorizationRecoveryChallenge | None = None,
        key: AuthorizationRecoveryKey | None = None,
        reason_code: str = "",
    ) -> None:
        self._record_store.write_authorization_recovery_audit(
            self._audit_record(
                event=event,
                status=status,
                challenge=challenge,
                key=key,
                reason_code=reason_code,
            )
        )

    def _audit_record(
        self,
        *,
        event: str,
        status: Literal["accepted", "rejected", "completed"],
        recorded_at: str | None = None,
        challenge: AuthorizationRecoveryChallenge | None = None,
        key: AuthorizationRecoveryKey | None = None,
        reason_code: str = "",
        payload: dict[str, object] | None = None,
    ) -> AuthorizationRecoveryAudit:
        now = recorded_at or _timestamp(self._now())
        audit_material: dict[str, object] = {
            "event": event,
            "status": status,
            "recorded_at": now,
            "challenge_id": challenge.challenge_id if challenge else "",
            "reason_code": reason_code,
            "nonce": self._random_token(16),
        }
        return AuthorizationRecoveryAudit(
            audit_id=f"authz-recovery-audit-{hashlib.sha256(_canonical_bytes(audit_material)).hexdigest()[:32]}",
            event=event,
            status=status,
            recorded_at=now,
            challenge_id=challenge.challenge_id if challenge else "",
            operation=challenge.operation if challenge else "",
            key_id=key.key_id if key else "",
            key_fingerprint_sha256=key.fingerprint_sha256 if key else "",
            intended_github_id=challenge.intended_github_id if challenge else None,
            reason_code=reason_code,
            payload=payload or {},
        )


def _active_custody_slot_count(keys: Sequence[AuthorizationRecoveryKey]) -> int:
    return len({key.custody_slot for key in keys if key.status == "active"})


def _recovered_identity(github_id: int) -> GitHubHumanIdentity:
    return GitHubHumanIdentity(
        login=f"github-id-{github_id}",
        github_id=github_id,
        name="Recovered administrator",
        email="",
        organizations=frozenset(),
        teams=frozenset(),
        role="admin",
    )


def _operator_alert_delivery(
    *,
    challenge: AuthorizationRecoveryChallenge,
    key: AuthorizationRecoveryKey,
    candidate_record: LaunchplaneAuthzPolicyRecord,
    created_at: str,
) -> OutboxDeliveryRecord:
    dedupe_key = build_outbox_dedupe_key(
        kind="operator_authorization_recovery_alert",
        parts=(challenge.challenge_id, candidate_record.record_id),
    )
    return OutboxDeliveryRecord(
        delivery_id=build_outbox_delivery_id(
            kind="operator_authorization_recovery_alert", dedupe_key=dedupe_key
        ),
        kind="operator_authorization_recovery_alert",
        aggregate_type="authorization_recovery",
        aggregate_id=challenge.challenge_id,
        dedupe_key=dedupe_key,
        created_at=created_at,
        updated_at=created_at,
        next_attempt_at=created_at,
        max_attempts=12,
        payload={
            "event": "authorization_recovery_applied",
            "challenge_id": challenge.challenge_id,
            "operation": challenge.operation,
            "signing_key_id": key.key_id,
            "signing_key_fingerprint_sha256": key.fingerprint_sha256,
            "intended_github_id": challenge.intended_github_id,
            "candidate_record_id": candidate_record.record_id,
            "candidate_policy_sha256": candidate_record.policy_sha256,
        },
    )


def _verify_sshsig(
    *,
    public_key: str,
    payload: bytes,
    signature: bytes,
) -> None:
    canonical_key, _, _ = _canonicalize_openssh_public_key(
        public_key,
        allowed_key_types=AUTHORIZATION_RECOVERY_KEY_TYPES,
    )
    if not signature.startswith(b"-----BEGIN SSH SIGNATURE-----"):
        raise ValueError("Recovery signature is not an OpenSSH SSHSIG envelope.")
    if len(signature) > AUTHORIZATION_RECOVERY_SIGNATURE_MAX_BYTES:
        raise ValueError("Recovery SSHSIG envelope exceeds the maximum accepted size.")
    with tempfile.TemporaryDirectory(prefix="launchplane-sshsig-") as directory:
        base = Path(directory)
        allowed_signers = base / "allowed_signers"
        payload_file = base / "payload"
        signature_file = base / "signature"
        allowed_signers.write_text(
            f"{AUTHORIZATION_RECOVERY_SIGNER_IDENTITY} {canonical_key}\n",
            encoding="utf-8",
        )
        payload_file.write_bytes(payload)
        signature_file.write_bytes(signature)
        try:
            completed = subprocess.run(
                [
                    "ssh-keygen",
                    "-Y",
                    "verify",
                    "-f",
                    str(allowed_signers),
                    "-I",
                    AUTHORIZATION_RECOVERY_SIGNER_IDENTITY,
                    "-n",
                    AUTHORIZATION_RECOVERY_NAMESPACE,
                    "-s",
                    str(signature_file),
                ],
                input=payload,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=AUTHORIZATION_RECOVERY_VERIFY_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as error:
            raise ValueError("Recovery SSHSIG verification timed out.") from error
    if completed.returncode != 0:
        raise ValueError("Recovery SSHSIG verification failed.")
