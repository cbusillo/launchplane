from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
from dataclasses import dataclass, field
from typing import Callable

from control_plane.contracts.ordinary_agent_lifecycle import (
    OrdinaryAgentAuthenticationCredentialCandidate,
    OrdinaryAgentEnrollApplyEnvelope,
    OrdinaryAgentRotateCredentialApplyEnvelope,
)

# Public wire-format version; the secret is a separate randomly generated component.
_WIRE_PREFIX = "lp_ordinary_v1"
_TOKEN_SECRET_BYTES = 32
_MAX_TOKEN_LENGTH = 512
_MAX_CIPHERTEXT_LENGTH = 64 * 1024
_IDENTIFIER_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{2,127}$")
_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_TOKEN_DIGEST_DOMAIN = b"launchplane:ordinary-agent-token:v1\0"
_CLAIM_DIGEST_DOMAIN = b"launchplane:ordinary-agent-claim:v1\0"
_ISSUANCE_DIGEST_DOMAIN = b"launchplane:ordinary-agent-issuance:v1\0"
_CIPHERTEXT_DIGEST_DOMAIN = b"launchplane:ordinary-agent-ciphertext:v1\0"

Encrypt = Callable[[str], tuple[str, str]]
Decrypt = Callable[[str, str], str]
RandomBytes = Callable[[int], bytes]


@dataclass(frozen=True)
class OrdinaryAgentToken:
    value: str = field(repr=False)


@dataclass(frozen=True)
class OrdinaryAgentClaimSecret:
    value: str = field(repr=False)


@dataclass(frozen=True)
class OrdinaryAgentTokenProof:
    credential_id: str
    credential_version: int
    credential_digest: str = field(repr=False)


@dataclass(frozen=True)
class OrdinaryAgentIdentity:
    principal_id: str
    credential_id: str
    credential_version: int


@dataclass(frozen=True)
class OrdinaryAgentIssuanceBundle:
    candidate: OrdinaryAgentAuthenticationCredentialCandidate = field(repr=False)
    credential_version: int
    operation_id: str
    receiver_claim_sha256: str = field(repr=False)
    delivery_expires_at: int
    intent_sha256: str
    ciphertext: str = field(repr=False)
    key_id: str = field(repr=False)
    ciphertext_sha256: str
    token: OrdinaryAgentToken = field(repr=False)


def _digest(domain: bytes, value: bytes) -> str:
    return hashlib.sha256(domain + value).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _encoded_random(random_bytes: RandomBytes) -> str:
    material = random_bytes(_TOKEN_SECRET_BYTES)
    if len(material) != _TOKEN_SECRET_BYTES:
        raise ValueError("ordinary-agent randomness must produce exactly 32 bytes")
    return base64.urlsafe_b64encode(material).decode("ascii").rstrip("=")


def _validate_digest(value: str, *, label: str) -> None:
    if _DIGEST_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")


def _token_digest(token: str) -> str:
    return _digest(_TOKEN_DIGEST_DOMAIN, token.encode("ascii"))


def ordinary_agent_ciphertext_sha256(ciphertext: str) -> str:
    if not ciphertext or len(ciphertext) > _MAX_CIPHERTEXT_LENGTH:
        raise ValueError("ordinary-agent issuance ciphertext is invalid")
    return _digest(_CIPHERTEXT_DIGEST_DOMAIN, ciphertext.encode("utf-8"))


def generate_receiver_claim_secret(
    *, random_bytes: RandomBytes = secrets.token_bytes
) -> OrdinaryAgentClaimSecret:
    return OrdinaryAgentClaimSecret(_encoded_random(random_bytes))


def receiver_claim_sha256(secret: OrdinaryAgentClaimSecret | str) -> str:
    value = secret.value if isinstance(secret, OrdinaryAgentClaimSecret) else secret
    if (
        not isinstance(value, str)
        or len(value) != 43
        or re.fullmatch(r"[A-Za-z0-9_-]{43}", value) is None
    ):
        raise ValueError("ordinary-agent receiver claim secret is invalid")
    decoded = base64.urlsafe_b64decode(value + "=")
    if (
        len(decoded) != _TOKEN_SECRET_BYTES
        or base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=") != value
    ):
        raise ValueError("ordinary-agent receiver claim secret is invalid")
    return _digest(_CLAIM_DIGEST_DOMAIN, value.encode("utf-8"))


def verify_receiver_claim(secret: OrdinaryAgentClaimSecret | str, expected_sha256: str) -> bool:
    _validate_digest(expected_sha256, label="receiver claim digest")
    try:
        actual = receiver_claim_sha256(secret)
    except (TypeError, ValueError):
        actual = "0" * 64
        valid = False
    else:
        valid = True
    return valid and hmac.compare_digest(actual, expected_sha256)


def parse_ordinary_agent_token(token: str) -> OrdinaryAgentTokenProof:
    if not isinstance(token, str) or not token or len(token) > _MAX_TOKEN_LENGTH:
        raise ValueError("ordinary-agent credential is malformed")
    parts = token.split(".")
    if len(parts) != 4 or parts[0] != _WIRE_PREFIX:
        raise ValueError("ordinary-agent credential is malformed")
    _, credential_id, version_text, secret_text = parts
    if _IDENTIFIER_PATTERN.fullmatch(credential_id) is None:
        raise ValueError("ordinary-agent credential is malformed")
    if not version_text.isascii() or not version_text.isdecimal() or len(version_text) > 19:
        raise ValueError("ordinary-agent credential is malformed")
    if version_text != str(int(version_text)):
        raise ValueError("ordinary-agent credential is malformed")
    credential_version = int(version_text)
    if not 1 <= credential_version <= 2**63 - 1:
        raise ValueError("ordinary-agent credential is malformed")
    if len(secret_text) != 43 or re.fullmatch(r"[A-Za-z0-9_-]{43}", secret_text) is None:
        raise ValueError("ordinary-agent credential is malformed")
    try:
        decoded = base64.urlsafe_b64decode(secret_text + "=")
    except ValueError as error:
        raise ValueError("ordinary-agent credential is malformed") from error
    if (
        len(decoded) != _TOKEN_SECRET_BYTES
        or base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=") != secret_text
    ):
        raise ValueError("ordinary-agent credential is malformed")
    return OrdinaryAgentTokenProof(
        credential_id=credential_id,
        credential_version=credential_version,
        credential_digest=_token_digest(token),
    )


def _issuance_payload(
    *,
    candidate_fields: dict[str, object],
    credential_version: int,
    operation_id: str,
    receiver_claim_sha256: str,
    delivery_expires_at: int,
    intent_sha256: str,
) -> dict[str, object]:
    return {
        "candidate": candidate_fields,
        "credential_version": credential_version,
        "operation_id": operation_id,
        "receiver_claim_sha256": receiver_claim_sha256,
        "delivery_expires_at": delivery_expires_at,
        "intent_sha256": intent_sha256,
    }


def _issuance_evidence_sha256(payload: dict[str, object]) -> str:
    return _digest(_ISSUANCE_DIGEST_DOMAIN, _canonical_json(payload).encode("utf-8"))


def issue_ordinary_agent_credential(
    *,
    principal_id: str,
    credential_id: str,
    credential_version: int,
    valid_from: int,
    expires_at: int,
    operation_id: str,
    receiver_claim_sha256: str,
    delivery_expires_at: int,
    intent_sha256: str,
    random_bytes: RandomBytes = secrets.token_bytes,
    encrypt: Encrypt | None = None,
) -> OrdinaryAgentIssuanceBundle:
    if credential_version < 1 or credential_version > 2**63 - 1:
        raise ValueError("ordinary-agent credential version is invalid")
    if expires_at <= valid_from:
        raise ValueError("ordinary-agent credential expiry must follow valid_from")
    if delivery_expires_at <= valid_from or delivery_expires_at > expires_at:
        raise ValueError("ordinary-agent delivery expiry must fit within credential lifetime")
    _validate_digest(receiver_claim_sha256, label="receiver claim digest")
    _validate_digest(intent_sha256, label="ordinary-agent issuance intent")

    secret_text = _encoded_random(random_bytes)
    token = OrdinaryAgentToken(
        f"{_WIRE_PREFIX}.{credential_id}.{credential_version}.{secret_text}"
    )
    proof = parse_ordinary_agent_token(token.value)
    candidate_fields: dict[str, object] = {
        "candidate_kind": "service_issued",
        "principal_id": principal_id,
        "credential_id": credential_id,
        "credential_digest": proof.credential_digest,
        "valid_from": valid_from,
        "expires_at": expires_at,
    }
    evidence_payload = _issuance_payload(
        candidate_fields=candidate_fields,
        credential_version=credential_version,
        operation_id=operation_id,
        receiver_claim_sha256=receiver_claim_sha256,
        delivery_expires_at=delivery_expires_at,
        intent_sha256=intent_sha256,
    )
    candidate = OrdinaryAgentAuthenticationCredentialCandidate.model_validate(
        {
            **candidate_fields,
            "issuance_evidence_sha256": _issuance_evidence_sha256(evidence_payload),
        }
    )
    capsule = _canonical_json({**evidence_payload, "token": token.value})
    if encrypt is None:
        from control_plane import secrets as control_plane_secrets

        encrypt = control_plane_secrets._encrypt_secret_value
    ciphertext, key_id = encrypt(capsule)
    return OrdinaryAgentIssuanceBundle(
        candidate=candidate,
        credential_version=credential_version,
        operation_id=operation_id,
        receiver_claim_sha256=receiver_claim_sha256,
        delivery_expires_at=delivery_expires_at,
        intent_sha256=intent_sha256,
        ciphertext=ciphertext,
        key_id=key_id,
        ciphertext_sha256=ordinary_agent_ciphertext_sha256(ciphertext),
        token=token,
    )


def validate_ordinary_agent_issuance(
    bundle: OrdinaryAgentIssuanceBundle,
    envelope: OrdinaryAgentEnrollApplyEnvelope | OrdinaryAgentRotateCredentialApplyEnvelope,
    *,
    intent_sha256: str,
) -> None:
    expected_version = (
        1
        if isinstance(envelope, OrdinaryAgentEnrollApplyEnvelope)
        else envelope.credential_version + 1
    )
    expected = (
        envelope.authentication_credential,
        expected_version,
        envelope.operation_id,
        envelope.delivery.receiver_claim_sha256,
        envelope.delivery.expires_at,
        intent_sha256,
    )
    actual = (
        bundle.candidate,
        bundle.credential_version,
        bundle.operation_id,
        bundle.receiver_claim_sha256,
        bundle.delivery_expires_at,
        bundle.intent_sha256,
    )
    if actual != expected:
        raise ValueError("ordinary-agent issuance does not match the reviewed envelope")
    if bundle.candidate.principal_id != envelope.principal_id:
        raise ValueError("ordinary-agent issuance principal does not match the reviewed envelope")
    proof = parse_ordinary_agent_token(bundle.token.value)
    if (
        proof.credential_id != bundle.candidate.credential_id
        or proof.credential_version != bundle.credential_version
        or not hmac.compare_digest(proof.credential_digest, bundle.candidate.credential_digest)
    ):
        raise ValueError("ordinary-agent issuance token does not match its candidate")
    candidate_fields = bundle.candidate.model_dump(
        mode="json", exclude={"issuance_evidence_sha256"}
    )
    payload = _issuance_payload(
        candidate_fields=candidate_fields,
        credential_version=bundle.credential_version,
        operation_id=bundle.operation_id,
        receiver_claim_sha256=bundle.receiver_claim_sha256,
        delivery_expires_at=bundle.delivery_expires_at,
        intent_sha256=bundle.intent_sha256,
    )
    if not hmac.compare_digest(
        _issuance_evidence_sha256(payload), bundle.candidate.issuance_evidence_sha256
    ):
        raise ValueError("ordinary-agent issuance evidence does not match its bindings")
    if not hmac.compare_digest(
        ordinary_agent_ciphertext_sha256(bundle.ciphertext), bundle.ciphertext_sha256
    ):
        raise ValueError("ordinary-agent issuance ciphertext digest does not match")


def decrypt_ordinary_agent_issuance(
    *,
    ciphertext: str,
    key_id: str,
    expected_candidate: OrdinaryAgentAuthenticationCredentialCandidate,
    expected_credential_version: int,
    expected_operation_id: str,
    expected_receiver_claim_sha256: str,
    expected_delivery_expires_at: int,
    expected_intent_sha256: str,
    expected_ciphertext_sha256: str,
    decrypt: Decrypt | None = None,
) -> OrdinaryAgentToken:
    if not hmac.compare_digest(
        ordinary_agent_ciphertext_sha256(ciphertext), expected_ciphertext_sha256
    ):
        raise ValueError("ordinary-agent issuance ciphertext digest does not match")
    if decrypt is None:
        from control_plane import secrets as control_plane_secrets

        decrypt = control_plane_secrets._decrypt_secret_value
    plaintext = decrypt(ciphertext, key_id)
    try:
        payload = json.loads(plaintext)
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError("ordinary-agent issuance capsule is invalid") from error
    if not isinstance(payload, dict) or set(payload) != {
        "candidate",
        "credential_version",
        "operation_id",
        "receiver_claim_sha256",
        "delivery_expires_at",
        "intent_sha256",
        "token",
    }:
        raise ValueError("ordinary-agent issuance capsule is invalid")
    expected_fields = _issuance_payload(
        candidate_fields=expected_candidate.model_dump(
            mode="json", exclude={"issuance_evidence_sha256"}
        ),
        credential_version=expected_credential_version,
        operation_id=expected_operation_id,
        receiver_claim_sha256=expected_receiver_claim_sha256,
        delivery_expires_at=expected_delivery_expires_at,
        intent_sha256=expected_intent_sha256,
    )
    if any(payload.get(key) != value for key, value in expected_fields.items()):
        raise ValueError("ordinary-agent issuance capsule does not match its committed bindings")
    token_value = payload.get("token")
    if not isinstance(token_value, str):
        raise ValueError("ordinary-agent issuance capsule is invalid")
    proof = parse_ordinary_agent_token(token_value)
    if (
        proof.credential_id != expected_candidate.credential_id
        or proof.credential_version != expected_credential_version
        or not hmac.compare_digest(proof.credential_digest, expected_candidate.credential_digest)
        or not hmac.compare_digest(
            _issuance_evidence_sha256(expected_fields),
            expected_candidate.issuance_evidence_sha256,
        )
    ):
        raise ValueError("ordinary-agent issuance capsule does not match its credential")
    return OrdinaryAgentToken(token_value)
