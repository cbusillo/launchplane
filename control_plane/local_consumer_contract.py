"""Recipient-bound, testing-only bridge delivery. No plaintext response model."""

import hashlib
import json
import time
from typing import Literal
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import BaseModel, ConfigDict, Field, model_validator

DELIVERY_ROUTE = "/v1/local-consumer-deliveries"
VERIFY_ROUTE = DELIVERY_ROUTE + "/verify"
DELIVERY_SCOPE = "testing-local-consumer"
BRIDGE_BINDING = "AGENT_SESSION_TOKEN"
DELIVERY_LIFETIME_SECONDS = 900


def canonical(value: BaseModel | dict[str, object]) -> bytes:
    payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def digest(value: BaseModel | dict[str, object]) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def https_origin(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("The service requires an HTTPS origin without credentials or path.")
    hostname = parsed.hostname.encode("idna").decode().lower()
    hostname = f"[{hostname}]" if ":" in hostname else hostname
    port = parsed.port
    return f"https://{hostname}" + (f":{port}" if port not in {None, 443} else "")


class ConsumerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    kind: Literal["agent_session_token_file_v1"] = "agent_session_token_file_v1"
    request_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    product: str = Field(min_length=1, max_length=256)
    context: str = Field(min_length=1, max_length=256)
    environment: Literal["testing"] = "testing"
    service_origin: str = Field(max_length=2048)
    endpoint_key: str = Field(min_length=1, max_length=256)
    server_url: str = Field(max_length=2048)
    directory: str = Field(min_length=1, max_length=4096)
    public_key: str = Field(min_length=1, max_length=2048)

    @model_validator(mode="after")
    def validate_destination(self) -> "ConsumerRequest":
        if https_origin(self.service_origin) != self.service_origin:
            raise ValueError("Use the canonical HTTPS service origin.")
        parsed = urlsplit(self.server_url)
        if (
            parsed.scheme != "wss"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path != "/agent-session/connect"
        ):
            raise ValueError("The bridge requires a credential-free WSS connect endpoint.")
        if not self.directory.startswith("/") or "\x00" in self.directory:
            raise ValueError("The destination requires an absolute local directory.")
        key = serialization.load_pem_public_key(self.public_key.encode())
        if not isinstance(key, rsa.RSAPublicKey) or key.key_size != 3072:
            raise ValueError("The recipient requires an RSA-3072 key.")
        return self


class ConsumerPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_fingerprint: str
    secret_id: str
    version_id: str
    profile_digest: str
    endpoint_digest: str
    expires_at: int

    def require_fresh(self, now: int | None = None) -> None:
        current = now if now is not None else int(time.time())
        if not current < self.expires_at <= current + DELIVERY_LIFETIME_SECONDS:
            raise ValueError("The delivery review expired; prepare a new testing destination.")


class ConsumerPackage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request: ConsumerRequest
    plan: ConsumerPlan
    issued_by: str = Field(min_length=1, max_length=128)
    wrapped_key: str = Field(max_length=1024)
    nonce: str = Field(max_length=64)
    ciphertext: str = Field(max_length=16384)


class ConsumerDeliveryEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request: ConsumerRequest
    mode: Literal["dry-run", "apply"] = "dry-run"
    reviewed_plan_digest: str = Field(default="", max_length=64)


class ConsumerDeliveryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trace_id: str
    request_fingerprint: str
    ready_to_deliver: bool
    plan_digest: str = ""
    version_id: str = ""
    package: ConsumerPackage | None = None


class ConsumerVerificationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["current"] = "current"
    package_digest: str
