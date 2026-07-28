from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.artifact_identity import ArtifactIdentityManifest
from control_plane.workflows.odoo_verification import OdooVerificationEvidence


LAUNCHPLANE_REQUIRED_ODOO_MODULES = ("launchplane_settings", "disable_odoo_online")


def missing_required_odoo_modules_from_artifact(
    artifact_manifest: ArtifactIdentityManifest,
) -> tuple[str, ...]:
    declared_modules = {
        str(module).strip()
        for module in artifact_manifest.odoo_install_modules
        if str(module).strip()
    }
    return tuple(
        module for module in LAUNCHPLANE_REQUIRED_ODOO_MODULES if module not in declared_modules
    )


class OdooStableTargetReplacementRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    instance: str
    strategy: Literal["recreate-in-place", "replace-and-cutover"] = "recreate-in-place"
    allow_empty_data: bool = False
    data_source_mode: Literal["existing", "empty", "upstream_restore"] = "existing"
    confirmation: str = ""
    artifact_id: str = ""
    source_git_ref: str = ""
    expected_current_artifact_id: str = ""

    @model_validator(mode="after")
    def _validate_request(self) -> "OdooStableTargetReplacementRequest":
        self.product = self.product.strip()
        self.instance = self.instance.strip().lower()
        self.confirmation = self.confirmation.strip().lower()
        self.artifact_id = self.artifact_id.strip()
        self.source_git_ref = self.source_git_ref.strip()
        self.expected_current_artifact_id = self.expected_current_artifact_id.strip()
        if not self.product:
            raise ValueError("Odoo stable target replacement requires product.")
        if not self.instance:
            raise ValueError("Odoo stable target replacement requires instance.")
        if bool(self.artifact_id) != bool(self.source_git_ref):
            raise ValueError(
                "Odoo target replacement requires artifact_id and source_git_ref together."
            )
        return self


class OdooStableTargetReplacementApplyRequest(OdooStableTargetReplacementRequest):
    verify_health: bool = True
    verify_canonical: bool = True
    verify_logo: bool = True
    no_cache: bool = False
    timeout_seconds: int | None = Field(default=None, ge=1)
    health_timeout_seconds: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _validate_apply_request(self) -> "OdooStableTargetReplacementApplyRequest":
        if self.strategy != "recreate-in-place":
            raise ValueError(
                "Odoo target replacement apply currently supports recreate-in-place only."
            )
        return self


class OdooStableTargetReplacementApplyResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product: str
    context: str
    instance: str
    strategy: Literal["recreate-in-place"]
    deployment_record_id: str = ""
    release_tuple_id: str = ""
    deploy_status: Literal["pass", "fail"]
    post_deploy_status: Literal["pass", "fail", "skipped"] = "skipped"
    post_deploy_override_status: Literal["pending", "pass", "fail", "skipped"] = "skipped"
    post_deploy_override_record_found: bool = False
    post_deploy_override_payload_rendered: bool = False
    post_deploy_override_count: int = 0
    post_deploy_website_bootstrap_included: bool = False
    post_deploy_override_evidence: dict[str, str] = Field(default_factory=dict)
    health_status: Literal["pass", "fail", "skipped"] = "skipped"
    canonical_status: Literal["pass", "fail", "skipped"] = "skipped"
    logo_status: Literal["pass", "fail", "skipped"] = "skipped"
    health_url: str = ""
    canonical_url: str = ""
    logo_urls: tuple[str, ...] = ()
    verification_evidence: OdooVerificationEvidence = Field(
        default_factory=OdooVerificationEvidence
    )
    runtime_identity_injected: bool = False
    target_id: str = ""
    target_name: str = ""
    artifact_id: str = ""
    image_reference: str = ""
    runtime_source: dict[str, str] = Field(default_factory=dict)
    error_message: str = ""
