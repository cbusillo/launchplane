from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import base64
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.authz_grant_service import (
    AuthzManagedPolicyReconcileEnvelope,
    execute_managed_authz_policy_reconcile,
    plan_managed_authz_policy_reconcile,
)
from control_plane.contracts.authz_policy_record import (
    AuthzPolicyCompareWriteResult,
    LaunchplaneAuthzPolicyRecord,
)
from control_plane.service_auth import GitHubHumanIdentity, GitHubHumanPolicyRule, LaunchplaneAuthzPolicy


AUTHORIZATION_RECOVERY_NAMESPACE = "launchplane.authorization-recovery.v1"
AUTHORIZATION_RECOVERY_MANAGED_SET_ID = "authorization-recovery-root"
AUTHORIZATION_RECOVERY_MANAGED_RULE_ID = "recovered-github-policy-admin"
AUTHORIZATION_RECOVERY_ACTION = "authz_policy_grant.write"
AUTHORIZATION_RECOVERY_PRODUCT = "launchplane"
AUTHORIZATION_RECOVERY_CONTEXT = "launchplane"
AUTHORIZATION_RECOVERY_KEY_TYPES = frozenset({"sk-ssh-ed25519@openssh.com"})
AUTHORIZATION_RECOVERY_CHALLENGE_TTL = timedelta(minutes=10)
AUTHORIZATION_RECOVERY_POP_TTL = timedelta(minutes=10)
AUTHORIZATION_RECOVERY_MAX_OPEN_CHALLENGES = 8
_PUBLIC_KEY_PATTERN = re.compile(r"^(?P<key_type>[^\s]+)\s+(?P<key_data>[A-Za-z0-9+/]+={0,2})(?:\s+[^\r\n]+)?$")

RecoveryOperation = Literal["initial_bootstrap", "restore_known_administrator", "replace_recovery_key"]
RecoveryKeyStatus = Literal["pending", "active", "revoked"]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _canonical_bytes(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def recovery_key_fingerprint(public_key: str) -> str:
    return hashlib.sha256(public_key.strip().encode("utf-8")).hexdigest()


def validate_recovery_public_key(public_key: str) -> tuple[str, str]:
    normalized = " ".join(public_key.strip().split())
    match = _PUBLIC_KEY_PATTERN.fullmatch(normalized)
    if match is None:
        raise ValueError("Recovery public key must be a single OpenSSH public-key line.")
    key_type = match.group("key_type")
    if key_type not in AUTHORIZATION_RECOVERY_KEY_TYPES:
        raise ValueError("Recovery public key type is not an allowed hardware-backed security-key type.")
    try:
        decoded = base64.b64decode(match.group("key_data"), validate=True)
    except ValueError as error:
        raise ValueError("Recovery public key has invalid OpenSSH base64 material.") from error
    if not decoded:
        raise ValueError("Recovery public key has empty OpenSSH key material.")
    return normalized, key_type


class AuthorizationRecoveryKey(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key_id: str
    custody_slot: str
    public_key: str
    fingerprint_sha256: str
    key_type: str
    status: RecoveryKeyStatus = "pending"
    enrolled_at: str
    activated_at: str = ""
    revoked_at: str = ""
    proof_challenge: str = ""
    proof_expires_at: str = ""

    @model_validator(mode="after")
    def _validate_key(self) -> "AuthorizationRecoveryKey":
        self.key_id = self.key_id.strip()
        self.custody_slot = self.custody_slot.strip()
        if not self.key_id or not self.custody_slot:
            raise ValueError("Recovery key requires a stable key ID and custody slot.")
        self.public_key, self.key_type = validate_recovery_public_key(self.public_key)
        if self.fingerprint_sha256 != recovery_key_fingerprint(self.public_key):
            raise ValueError("Recovery key fingerprint does not match its public key.")
        if self.status == "pending" and (not self.proof_challenge or not self.proof_expires_at):
            raise ValueError("Pending recovery key requires a proof-of-possession challenge.")
        if self.status != "pending" and (self.proof_challenge or self.proof_expires_at):
            raise ValueError("Non-pending recovery key cannot retain a proof-of-possession challenge.")
        if self.status == "active" and not self.activated_at:
            raise ValueError("Active recovery key requires activation evidence.")
        if self.status == "revoked" and not self.revoked_at:
            raise ValueError("Revoked recovery key requires revocation evidence.")
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
    model_config = ConfigDict(extra="forbid")

    record_id: Literal["authorization-recovery-bootstrap"] = "authorization-recovery-bootstrap"
    status: Literal["pending", "complete"] = "pending"
    completed_at: str = ""
    completed_by_github_id: int | None = Field(default=None, ge=1)
    completion_challenge_id: str = ""

    @model_validator(mode="after")
    def _validate_state(self) -> "AuthorizationBootstrapState":
        if self.status == "complete":
            if not self.completed_at or self.completed_by_github_id is None or not self.completion_challenge_id:
                raise ValueError("Completed bootstrap state requires immutable completion evidence.")
        elif self.completed_at or self.completed_by_github_id is not None or self.completion_challenge_id:
            raise ValueError("Pending bootstrap state cannot retain completion evidence.")
        return self


class AuthorizationRecoveryChallenge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    challenge_id: str
    operation: RecoveryOperation
    intended_github_id: int = Field(ge=1)
    nonce: str
    issued_at: str
    expires_at: str
    active_policy_record_id: str
    active_policy_revision: int = Field(ge=1)
    active_policy_sha256: str
    plan_sha256: str
    key_id: str = ""
    used_at: str = ""

    def canonical_payload(self, *, service_identity: str) -> dict[str, object]:
        return {
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
            "intended_github_id": self.intended_github_id,
            "managed_set_id": AUTHORIZATION_RECOVERY_MANAGED_SET_ID,
            "managed_rule_id": AUTHORIZATION_RECOVERY_MANAGED_RULE_ID,
            "plan_sha256": self.plan_sha256,
        }

    def canonical_bytes(self, *, service_identity: str) -> bytes:
        return _canonical_bytes(self.canonical_payload(service_identity=service_identity))


class AuthorizationRecoveryAudit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    audit_id: str
    event: str
    status: Literal["accepted", "rejected", "completed"]
    recorded_at: str
    challenge_id: str = ""
    key_fingerprint_sha256: str = ""
    intended_github_id: int | None = None
    reason_code: str = ""


class AuthorizationRecoveryStore(Protocol):
    def read_authorization_bootstrap_state(self) -> AuthorizationBootstrapState | None: ...

    def write_authorization_bootstrap_state(self, state: AuthorizationBootstrapState) -> None: ...

    def list_authorization_recovery_keys(self) -> tuple[AuthorizationRecoveryKey, ...]: ...

    def read_authorization_recovery_key(self, key_id: str) -> AuthorizationRecoveryKey | None: ...

    def write_authorization_recovery_key(self, key: AuthorizationRecoveryKey) -> None: ...

    def read_authorization_recovery_challenge(
        self, challenge_id: str
    ) -> AuthorizationRecoveryChallenge | None: ...

    def list_authorization_recovery_challenges(self) -> tuple[AuthorizationRecoveryChallenge, ...]: ...

    def write_authorization_recovery_challenge(self, challenge: AuthorizationRecoveryChallenge) -> None: ...

    def consume_authorization_recovery_challenge(self, *, challenge_id: str, used_at: str) -> bool: ...

    def write_authorization_recovery_audit(self, audit: AuthorizationRecoveryAudit) -> None: ...

    def list_authz_policy_records(
        self, *, status: str = "", limit: int | None = None
    ) -> tuple[LaunchplaneAuthzPolicyRecord, ...]: ...

    def compare_and_write_authz_policy_record(
        self,
        *,
        expected_record: LaunchplaneAuthzPolicyRecord,
        replacement_record: LaunchplaneAuthzPolicyRecord | None,
        mutation: object | None,
    ) -> AuthzPolicyCompareWriteResult: ...


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
    ) -> None:
        self._record_store = record_store
        self._service_identity = service_identity.strip()
        self._now = now
        if not self._service_identity:
            raise ValueError("Authorization recovery requires a stable service identity.")

    def bootstrap_state(self) -> AuthorizationBootstrapState:
        return self._record_store.read_authorization_bootstrap_state() or AuthorizationBootstrapState()

    def readiness(self) -> dict[str, object]:
        keys = self._record_store.list_authorization_recovery_keys()
        active = [key for key in keys if key.status == "active"]
        return {
            "bootstrap_status": self.bootstrap_state().status,
            "active_key_count": len(active),
            "independent_custody_slot_count": len({key.custody_slot for key in active}),
            "ready": len(active) >= 2 and len({key.custody_slot for key in active}) >= 2,
            "keys": [key.redacted() for key in keys],
        }

    def enroll_key(self, *, key_id: str, custody_slot: str, public_key: str) -> AuthorizationRecoveryKey:
        state = self.bootstrap_state()
        if state.status == "complete":
            raise PermissionError("Recovery-key enrollment after bootstrap completion requires browser administration.")
        if self._record_store.read_authorization_recovery_key(key_id.strip()) is not None:
            raise ValueError("Recovery key ID already exists.")
        normalized_key, key_type = validate_recovery_public_key(public_key)
        normalized_slot = custody_slot.strip()
        if not normalized_slot:
            raise ValueError("Recovery key custody slot is required.")
        for existing_key in self._record_store.list_authorization_recovery_keys():
            if existing_key.custody_slot == normalized_slot and existing_key.status != "revoked":
                raise ValueError("Recovery key custody slot already has a non-revoked key.")
        now = self._now()
        open_challenge_count = sum(
            challenge.used_at == "" and _parse_timestamp(challenge.expires_at) > now
            for challenge in self._record_store.list_authorization_recovery_challenges()
        )
        if open_challenge_count >= AUTHORIZATION_RECOVERY_MAX_OPEN_CHALLENGES:
            self._audit(event="prepare", status="rejected", reason_code="challenge_rate_limited")
            raise ValueError("Recovery challenge rate limit exceeded.")
        record = AuthorizationRecoveryKey(
            key_id=key_id.strip(),
            custody_slot=normalized_slot,
            public_key=normalized_key,
            fingerprint_sha256=recovery_key_fingerprint(normalized_key),
            key_type=key_type,
            enrolled_at=_timestamp(now),
            proof_challenge=hashlib.sha256(f"{key_id}:{now.timestamp()}".encode()).hexdigest(),
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
        _verify_sshsig(public_key=key.public_key, payload=self.proof_bytes(key_id=key_id), signature=signature)
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
        active = [candidate for candidate in self._record_store.list_authorization_recovery_keys() if candidate.status == "active"]
        remaining = [candidate for candidate in active if candidate.key_id != key.key_id]
        if key.status == "active" and (
            len(remaining) < 2 or len({candidate.custody_slot for candidate in remaining}) < 2
        ):
            raise ValueError("Recovery key revocation cannot leave fewer than two independent active keys.")
        revoked = key.model_copy(
            update={"status": "revoked", "revoked_at": _timestamp(self._now()), "proof_challenge": "", "proof_expires_at": ""}
        )
        self._record_store.write_authorization_recovery_key(revoked)
        self._audit(event="key_revoked", status="completed", key=revoked)
        return revoked

    def prepare(self, *, operation: RecoveryOperation, intended_github_id: int, key_id: str = "") -> RecoveryPrepareResult:
        state = self.bootstrap_state()
        self._require_recovery_readiness()
        if operation == "initial_bootstrap" and state.status != "pending":
            raise ValueError("Initial authorization bootstrap is already complete.")
        if operation != "initial_bootstrap" and state.status != "complete":
            raise ValueError("Recovery cannot run before initial authorization bootstrap completes.")
        if operation == "replace_recovery_key" and not key_id.strip():
            raise ValueError("Recovery-key replacement requires the key being replaced.")
        active_records = self._record_store.list_authz_policy_records(status="active", limit=2)
        if len(active_records) != 1:
            raise ValueError("Recovery requires exactly one active authorization policy record.")
        active_record = active_records[0]
        now = self._now()
        preview = self._recovery_plan(
            active_policy=active_record.policy,
            intended_github_id=intended_github_id,
            mode="dry_run",
        )
        _, _, _, diff = plan_managed_authz_policy_reconcile(record_store=self._record_store, request=preview)
        plan = self._recovery_plan(
            active_policy=active_record.policy,
            intended_github_id=intended_github_id,
            mode="apply",
            reviewed_plan_sha256=diff.plan_sha256,
        )
        challenge = AuthorizationRecoveryChallenge(
            challenge_id=hashlib.sha256(f"{operation}:{intended_github_id}:{now.timestamp()}".encode()).hexdigest()[:32],
            operation=operation,
            intended_github_id=intended_github_id,
            nonce=hashlib.sha256(f"nonce:{operation}:{now.timestamp()}".encode()).hexdigest(),
            issued_at=_timestamp(now),
            expires_at=_timestamp(now + AUTHORIZATION_RECOVERY_CHALLENGE_TTL),
            active_policy_record_id=active_record.record_id,
            active_policy_revision=active_record.revision,
            active_policy_sha256=active_record.policy_sha256,
            plan_sha256=diff.plan_sha256,
            key_id=key_id.strip(),
        )
        self._record_store.write_authorization_recovery_challenge(challenge)
        self._audit(event="prepare", status="accepted", challenge=challenge)
        return RecoveryPrepareResult(challenge=challenge, canonical_request=challenge.canonical_bytes(service_identity=self._service_identity), plan=plan)

    def apply(self, *, challenge_id: str, key_id: str, signature: bytes, trace_id: str) -> LaunchplaneAuthzPolicyRecord:
        challenge = self._record_store.read_authorization_recovery_challenge(challenge_id.strip())
        if challenge is None or _parse_timestamp(challenge.expires_at) <= self._now():
            self._audit(event="apply", status="rejected", reason_code="challenge_unavailable")
            raise ValueError("Recovery challenge is unavailable.")
        if challenge.used_at:
            self._audit(event="apply", status="rejected", challenge=challenge, reason_code="replayed")
            raise ValueError("Recovery challenge has already been consumed.")
        key = self._require_key(key_id)
        if key.status != "active":
            self._audit(event="apply", status="rejected", challenge=challenge, key=key, reason_code="key_not_active")
            raise ValueError("Recovery key is not active.")
        self._require_recovery_readiness()
        _verify_sshsig(
            public_key=key.public_key,
            payload=challenge.canonical_bytes(service_identity=self._service_identity),
            signature=signature,
        )
        if not self._record_store.consume_authorization_recovery_challenge(
            challenge_id=challenge.challenge_id, used_at=_timestamp(self._now())
        ):
            self._audit(event="apply", status="rejected", challenge=challenge, key=key, reason_code="replayed")
            raise ValueError("Recovery challenge has already been consumed.")
        active_records = self._record_store.list_authz_policy_records(status="active", limit=2)
        if len(active_records) != 1:
            raise ValueError("Recovery requires exactly one active authorization policy record.")
        active_record = active_records[0]
        if (
            active_record.record_id != challenge.active_policy_record_id
            or active_record.revision != challenge.active_policy_revision
            or active_record.policy_sha256 != challenge.active_policy_sha256
        ):
            raise ValueError("Recovery challenge is stale against the active authorization policy.")
        preview = self._recovery_plan(
            active_policy=active_record.policy,
            intended_github_id=challenge.intended_github_id,
            mode="dry_run",
        )
        _, _, _, diff = plan_managed_authz_policy_reconcile(record_store=self._record_store, request=preview)
        if diff.plan_sha256 != challenge.plan_sha256:
            raise ValueError("Recovery plan drifted after challenge preparation.")
        plan = self._recovery_plan(
            active_policy=active_record.policy,
            intended_github_id=challenge.intended_github_id,
            mode="apply",
            reviewed_plan_sha256=diff.plan_sha256,
        )
        identity = GitHubHumanIdentity(
            login=f"github-id-{challenge.intended_github_id}",
            github_id=challenge.intended_github_id,
            name="Recovered administrator",
            email="",
            organizations=frozenset(),
            teams=frozenset(),
            role="admin",
        )
        result = execute_managed_authz_policy_reconcile(
            record_store=self._record_store,
            request=plan,
            identity=identity,
            trace_id=trace_id,
            now_timestamp=lambda: _timestamp(self._now()),
            authorized_policy_sha256=challenge.active_policy_sha256,
            allow_recovery_single_admin=True,
        )
        write_result = self._record_store.compare_and_write_authz_policy_record(
            expected_record=result.previous_authz_policy_record,
            replacement_record=result.authz_policy_record if result.changed else None,
            mutation=None,
        )
        if write_result.status not in {"written", "unchanged"}:
            raise ValueError("Recovery authorization policy write did not complete safely.")
        if challenge.operation == "initial_bootstrap":
            current_state = self.bootstrap_state()
            if current_state.status != "pending":
                raise ValueError("Bootstrap completion state changed unexpectedly.")
            self._record_store.write_authorization_bootstrap_state(
                AuthorizationBootstrapState(
                    status="complete",
                    completed_at=_timestamp(self._now()),
                    completed_by_github_id=challenge.intended_github_id,
                    completion_challenge_id=challenge.challenge_id,
                )
            )
        self._audit(event="apply", status="completed", challenge=challenge, key=key)
        return result.authz_policy_record

    def _recovery_plan(
        self,
        *,
        active_policy: LaunchplaneAuthzPolicy,
        intended_github_id: int,
        mode: Literal["dry_run", "apply"],
        reviewed_plan_sha256: str = "",
    ) -> AuthzManagedPolicyReconcileEnvelope:
        desired_policy = active_policy.model_copy(
            update={
                "schema_version": 2,
                "github_humans": tuple(
                    rule
                    for rule in active_policy.github_humans
                    if rule.managed_set_id != AUTHORIZATION_RECOVERY_MANAGED_SET_ID
                )
                + (
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
            }
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

    def _require_key(self, key_id: str) -> AuthorizationRecoveryKey:
        key = self._record_store.read_authorization_recovery_key(key_id.strip())
        if key is None:
            raise ValueError("Recovery key was not found.")
        return key

    def _require_recovery_readiness(self) -> None:
        ready = self.readiness()
        if not ready["ready"]:
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
        now = _timestamp(self._now())
        audit = AuthorizationRecoveryAudit(
            audit_id=hashlib.sha256(f"{event}:{status}:{now}:{challenge.challenge_id if challenge else ''}".encode()).hexdigest()[:32],
            event=event,
            status=status,
            recorded_at=now,
            challenge_id=challenge.challenge_id if challenge else "",
            key_fingerprint_sha256=key.fingerprint_sha256 if key else "",
            intended_github_id=challenge.intended_github_id if challenge else None,
            reason_code=reason_code,
        )
        self._record_store.write_authorization_recovery_audit(audit)


def _verify_sshsig(*, public_key: str, payload: bytes, signature: bytes) -> None:
    validate_recovery_public_key(public_key)
    if not signature.startswith(b"-----BEGIN SSH SIGNATURE-----"):
        raise ValueError("Recovery signature is not an OpenSSH SSHSIG envelope.")
    with tempfile.TemporaryDirectory(prefix="launchplane-sshsig-") as directory:
        base = Path(directory)
        allowed_signers = base / "allowed_signers"
        payload_file = base / "payload"
        signature_file = base / "signature"
        allowed_signers.write_text(f"launchplane-recovery {public_key}\n", encoding="utf-8")
        payload_file.write_bytes(payload)
        signature_file.write_bytes(signature)
        completed = subprocess.run(
            [
                "ssh-keygen",
                "-Y",
                "verify",
                "-f",
                str(allowed_signers),
                "-I",
                "launchplane-recovery",
                "-n",
                AUTHORIZATION_RECOVERY_NAMESPACE,
                "-s",
                str(signature_file),
            ],
            input=payload,
            capture_output=True,
            check=False,
        )
    if completed.returncode != 0:
        raise ValueError("Recovery SSHSIG verification failed.")
