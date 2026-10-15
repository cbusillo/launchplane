from __future__ import annotations

from ipaddress import ip_address
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def public_dns_addresses(values: tuple[str, ...]) -> tuple[str, ...]:
    addresses = tuple(sorted({str(ip_address(value.strip())) for value in values}))
    if any(
        not ip_address(value).is_global or ip_address(value).is_multicast for value in addresses
    ):
        raise ValueError("DNS expectations require public unicast IP addresses")
    return addresses


class PublicDnsExpectedTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    public_name: str
    addresses: tuple[str, ...] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def _validate_target(self) -> PublicDnsExpectedTarget:
        self.public_name = self.public_name.strip().rstrip(".").lower().encode("idna").decode()
        if len(self.public_name) > 253 or not re.fullmatch(
            r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+",
            self.public_name,
        ):
            raise ValueError("DNS expectations require an exact public DNS name")
        try:
            ip_address(self.public_name)
        except ValueError:
            pass
        else:
            raise ValueError("DNS expectations require a DNS name, not an IP literal")
        self.addresses = public_dns_addresses(self.addresses)
        return self


class PublicDnsObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    public_name: str
    status: Literal["match", "mismatch", "unavailable"]
    expected_addresses: tuple[str, ...]
    observed_addresses: tuple[str, ...] = ()
    failure_code: Literal["", "dns_failure", "invalid_url", "private_url", "unknown_error"] = ""

    @model_validator(mode="after")
    def _validate_observation(self) -> PublicDnsObservation:
        expectation = PublicDnsExpectedTarget(
            public_name=self.public_name, addresses=self.expected_addresses
        )
        self.public_name = expectation.public_name
        self.expected_addresses = expectation.addresses
        self.observed_addresses = public_dns_addresses(self.observed_addresses)
        if self.status == "unavailable":
            if not self.failure_code or self.observed_addresses:
                raise ValueError("Unavailable DNS evidence requires a failure and no answers")
        elif self.failure_code or not self.observed_addresses:
            raise ValueError("DNS comparison requires successful public resolution")
        elif (self.observed_addresses == self.expected_addresses) != (self.status == "match"):
            raise ValueError("DNS comparison status disagrees with the observed answer set")
        return self
