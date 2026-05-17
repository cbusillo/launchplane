from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.workflows.odoo_verification import (
    OdooVerificationEvidence,
)


class OdooStableBootstrapRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    context: str = "cm"
    instance: str
    confirmation: str
    verify_health: bool = True
    verify_canonical: bool = True
    verify_logo: bool = True
    timeout_seconds: int | None = Field(default=None, ge=1)
    health_timeout_seconds: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _validate_request(self) -> "OdooStableBootstrapRequest":
        self.product = self.product.strip()
        self.context = self.context.strip().lower()
        self.instance = self.instance.strip().lower()
        self.confirmation = self.confirmation.strip().lower()
        if not self.product:
            raise ValueError("Odoo stable bootstrap requires product.")
        if not self.context:
            raise ValueError("Odoo stable bootstrap requires context.")
        if not self.instance:
            raise ValueError("Odoo stable bootstrap requires instance.")
        return self


class OdooStableBootstrapResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product: str
    context: str
    instance: str
    deployment_record_id: str = ""
    bootstrap_status: Literal["pass", "fail"]
    bootstrap_run_status: Literal["pass", "fail", "skipped"] = "skipped"
    readiness_status: Literal["pass", "fail", "verification_failed", "skipped"] = "skipped"
    post_deploy_status: Literal["pass", "fail", "skipped"] = "skipped"
    health_status: Literal["pass", "fail", "skipped"] = "skipped"
    canonical_status: Literal["pass", "fail", "skipped"] = "skipped"
    logo_status: Literal["pass", "fail", "skipped"] = "skipped"
    health_url: str = ""
    canonical_url: str = ""
    logo_urls: tuple[str, ...] = ()
    verification_evidence: OdooVerificationEvidence = Field(
        default_factory=OdooVerificationEvidence
    )
    target_id: str = ""
    target_name: str = ""
    artifact_id: str = ""
    source_git_ref: str = ""
    error_message: str = ""
