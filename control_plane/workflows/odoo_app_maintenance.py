from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.odoo_instance_override_record import OdooOverrideApplyPhase
from control_plane.workflows.odoo_post_deploy import (
    OdooPostDeployRequest,
    execute_odoo_post_deploy,
)
from control_plane.workflows.ship import utc_now_timestamp


OdooAppMaintenanceAction = Literal["post-deploy"]
OdooAppMaintenanceIntent = Literal["stable-post-deploy"]


class OdooAppMaintenanceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    context: str
    instance: str = "testing"
    action: OdooAppMaintenanceAction
    intent: OdooAppMaintenanceIntent
    phase: OdooOverrideApplyPhase = "deploy"
    email: str = ""
    application_name: str = ""
    preview_slug: str = ""
    timeout_seconds: int = Field(default=300, ge=1)

    @model_validator(mode="after")
    def _validate_request(self) -> "OdooAppMaintenanceRequest":
        self.context = self.context.strip().lower()
        self.instance = self.instance.strip().lower()
        self.email = self.email.strip()
        self.application_name = self.application_name.strip()
        self.preview_slug = self.preview_slug.strip()
        if not self.context:
            raise ValueError("Odoo app maintenance requires context.")
        if not self.instance:
            raise ValueError("Odoo app maintenance requires instance.")
        if self.email or self.application_name or self.preview_slug:
            raise ValueError("Odoo app maintenance does not accept user or preview-scoped fields.")
        if self.action != "post-deploy" or self.intent != "stable-post-deploy":
            raise ValueError(
                "Odoo app maintenance currently supports only post-deploy / stable-post-deploy."
            )
        if self.phase != "deploy":
            raise ValueError("Odoo app maintenance currently supports only the deploy phase.")
        return self


class OdooAppMaintenanceResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    maintenance_status: Literal["pass", "fail"]
    action: OdooAppMaintenanceAction
    intent: str
    context: str
    instance: str
    application_name: str = ""
    application_id: str = ""
    schedule_name: str = "odoo-post-deploy"
    post_deploy_status: Literal["pass", "fail"]
    override_status: str = "skipped"
    override_record_found: bool = False
    override_payload_rendered: bool = False
    website_bootstrap_included: bool = False
    applied_at: str = ""
    started_at: str = ""
    finished_at: str = ""
    error_message: str = ""


def execute_odoo_app_maintenance(
    *,
    control_plane_root: Path,
    record_store: object,
    request: OdooAppMaintenanceRequest,
) -> OdooAppMaintenanceResult:
    started_at = utc_now_timestamp()
    post_deploy_result = execute_odoo_post_deploy(
        control_plane_root=control_plane_root,
        record_store=record_store,
        request=OdooPostDeployRequest(
            context=request.context,
            instance=request.instance,
            phase=request.phase,
        ),
    )
    finished_at = utc_now_timestamp()
    return OdooAppMaintenanceResult(
        maintenance_status=post_deploy_result.post_deploy_status,
        action=request.action,
        intent=request.intent,
        context=post_deploy_result.context,
        instance=post_deploy_result.instance,
        post_deploy_status=post_deploy_result.post_deploy_status,
        override_status=post_deploy_result.override_status,
        override_record_found=post_deploy_result.override_record_found,
        override_payload_rendered=post_deploy_result.override_payload_rendered,
        website_bootstrap_included=post_deploy_result.website_bootstrap_included,
        applied_at=post_deploy_result.applied_at,
        started_at=started_at,
        finished_at=finished_at,
        error_message=post_deploy_result.error_message,
    )
