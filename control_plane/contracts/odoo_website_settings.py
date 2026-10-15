"""Native website settings; values are supplied by runtime records, never code."""

from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import urlsplit
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator


class OdooWebsiteSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    operation: Literal["apply", "disable_collection"] = "apply"
    website_id: int = Field(ge=1, strict=True)
    public_origin: str
    public_indexing_enabled: StrictBool = False
    google_analytics_key: str = ""
    cookies_bar: StrictBool | None = True
    block_third_party_domains: StrictBool | None = True

    @model_validator(mode="after")
    def validate_settings(self) -> OdooWebsiteSettings:
        if self.operation == "disable_collection":
            if (
                self.google_analytics_key
                or self.public_indexing_enabled
                or self.cookies_bar is not None
                or self.block_third_party_domains is not None
            ):
                raise ValueError(
                    "Collection-only rollback must disable collection and preserve consent controls."
                )
        elif self.cookies_bar is None or self.block_third_party_domains is None:
            raise ValueError("Website apply requires explicit consent choices.")
        origin = urlsplit(self.public_origin)
        if (
            origin.scheme != "https"
            or not origin.hostname
            or origin.username
            or origin.password
            or origin.path not in {"", "/"}
            or origin.query
            or origin.fragment
        ):
            raise ValueError("Website settings require a public HTTPS origin.")
        if self.google_analytics_key and not re.fullmatch(
            r"G-[A-Z0-9]+", self.google_analytics_key
        ):
            raise ValueError("Website analytics requires a GA4 Measurement ID or an empty key.")
        if self.google_analytics_key and not (self.cookies_bar and self.block_third_party_domains):
            raise ValueError("Website analytics requires the cookie bar and third-party blocking.")
        return self

    @property
    def digest(self) -> str:
        return hashlib.sha256(
            json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


class OdooWebsiteSettingsIntent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: str = Field(min_length=1)
    settings: OdooWebsiteSettings
