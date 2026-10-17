import hashlib
import json
import re
from typing import Literal
from urllib.parse import urlsplit

from cryptography import x509

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from control_plane.outbound_http import private_url_error


PrivateHealthEndpointStatus = Literal["active", "disabled"]


class PrivateHealthEndpointRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    endpoint_key: str
    product: str
    context: str
    instance: str
    url: str
    status: PrivateHealthEndpointStatus = "active"
    source_label: str = ""
    updated_at: str
    tls_ca_pem: str = Field(default="", max_length=65536, exclude_if=lambda value: not value)

    @field_validator(
        "endpoint_key",
        "product",
        "context",
        "instance",
        "url",
        "updated_at",
        mode="after",
    )
    @classmethod
    def _validate_required_text(cls, value: str) -> str:
        normalized_value = value.strip()
        if not normalized_value:
            raise ValueError("private health endpoint record requires non-empty text fields")
        return normalized_value

    @field_validator("source_label", mode="after")
    @classmethod
    def _normalize_optional_text(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def _validate_record(self) -> "PrivateHealthEndpointRecord":
        if self.schema_version != 1:
            raise ValueError("Unsupported private health endpoint record schema version")
        private_error = private_url_error(self.url)
        if private_error == "invalid_url":
            raise ValueError("private health endpoint url must be a valid HTTP URL")
        if private_error == "public_url":
            raise ValueError("private health endpoint url must not be public")
        if self.tls_ca_pem:
            if self.instance != "testing" or urlsplit(self.url).scheme != "https":
                raise ValueError("Scoped private CA trust is supported only on testing HTTPS.")
            blocks = re.findall(
                r"-----BEGIN CERTIFICATE-----\s+[A-Za-z0-9+/=\s]+?-----END CERTIFICATE-----",
                self.tls_ca_pem,
            )
            if not blocks or "".join(self.tls_ca_pem.split()) != "".join("".join(blocks).split()):
                raise ValueError("Scoped trust accepts public certificates only.")
            try:
                for certificate in x509.load_pem_x509_certificates(self.tls_ca_pem.encode("ascii")):
                    if not certificate.extensions.get_extension_for_class(
                        x509.BasicConstraints
                    ).value.ca:
                        raise ValueError("Scoped trust requires CA certificates.")
            except x509.ExtensionNotFound as error:
                raise ValueError("Scoped trust requires CA certificates.") from error
        return self


def private_health_endpoint_record_sha256(record: PrivateHealthEndpointRecord) -> str:
    canonical_payload = json.dumps(
        record.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()
