from __future__ import annotations

from pathlib import Path
from typing import Literal, Protocol, cast

import click
from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane import dokploy as control_plane_dokploy
from control_plane import odoo_instance_overrides as control_plane_odoo_instance_overrides
from control_plane.contracts.odoo_instance_override_record import (
    OdooInstanceOverrideRecord,
    OdooOverrideApplyPhase,
    OdooOverrideApplyResult,
    OdooOverrideApplyStatus,
)
from control_plane.workflows.ship import utc_now_timestamp


class OdooPostDeployStore(Protocol):
    def read_odoo_instance_override_record(
        self, *, context_name: str, instance_name: str
    ) -> OdooInstanceOverrideRecord: ...

    def write_odoo_instance_override_record(self, record: OdooInstanceOverrideRecord) -> object: ...


class OdooPostDeployRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    context: str
    instance: str
    phase: OdooOverrideApplyPhase = "deploy"

    @model_validator(mode="after")
    def _validate_request(self) -> "OdooPostDeployRequest":
        self.context = self.context.strip().lower()
        self.instance = self.instance.strip().lower()
        if not self.context:
            raise ValueError("Odoo post-deploy request requires context.")
        if not self.instance:
            raise ValueError("Odoo post-deploy request requires instance.")
        return self


class OdooPostDeployResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context: str
    instance: str
    phase: OdooOverrideApplyPhase
    post_deploy_status: Literal["pass", "fail"]
    override_status: OdooOverrideApplyStatus = "skipped"
    override_record_found: bool = False
    override_payload_rendered: bool = False
    required_container_environment_keys: tuple[str, ...] = ()
    applied_at: str = ""
    error_message: str = ""


def _read_odoo_instance_override_record(
    *,
    record_store: OdooPostDeployStore,
    context_name: str,
    instance_name: str,
) -> OdooInstanceOverrideRecord | None:
    try:
        return record_store.read_odoo_instance_override_record(
            context_name=context_name,
            instance_name=instance_name,
        )
    except FileNotFoundError:
        return None


def _write_odoo_instance_override_apply_result(
    *,
    record_store: OdooPostDeployStore,
    record: OdooInstanceOverrideRecord,
    status: OdooOverrideApplyStatus,
    detail: str,
    source_label: str = "odoo-post-deploy-driver",
) -> OdooInstanceOverrideRecord:
    now = utc_now_timestamp()
    updated_record = record.model_copy(
        update={
            "last_apply": OdooOverrideApplyResult(
                attempted=status in {"pending", "pass", "fail"},
                status=status,
                applied_at=now if status in {"pass", "fail"} else "",
                detail=detail,
            ),
            "updated_at": now,
            "source_label": source_label,
        }
    )
    record_store.write_odoo_instance_override_record(updated_record)
    return updated_record


def _require_record_store(record_store: object) -> OdooPostDeployStore:
    required_methods = (
        "read_odoo_instance_override_record",
        "write_odoo_instance_override_record",
    )
    missing_methods = tuple(
        method_name
        for method_name in required_methods
        if not callable(getattr(record_store, method_name, None))
    )
    if missing_methods:
        raise click.ClickException(
            "Odoo post-deploy requires a Launchplane record store with override support. "
            f"Missing methods: {', '.join(missing_methods)}."
        )
    return cast(OdooPostDeployStore, record_store)


def _resolve_compose_target_definition(
    *,
    control_plane_root: Path,
    request: OdooPostDeployRequest,
) -> control_plane_dokploy.DokployTargetDefinition:
    source_of_truth = control_plane_dokploy.read_control_plane_dokploy_source_of_truth(
        control_plane_root=control_plane_root,
    )
    target_definition = control_plane_dokploy.find_dokploy_target_definition(
        source_of_truth,
        context_name=request.context,
        instance_name=request.instance,
    )
    if target_definition is None:
        raise click.ClickException(
            f"Odoo post-deploy target {request.context}/{request.instance} is missing from Launchplane Dokploy records."
        )
    if target_definition.target_type != "compose":
        raise click.ClickException(
            "Odoo post-deploy requires a compose target in Launchplane Dokploy records. "
            f"Configured={target_definition.target_type}."
        )
    return target_definition


def execute_odoo_post_deploy(
    *,
    control_plane_root: Path,
    record_store: object,
    request: OdooPostDeployRequest,
    env_file: Path | None = None,
) -> OdooPostDeployResult:
    typed_record_store = _require_record_store(record_store)
    odoo_override_record = _read_odoo_instance_override_record(
        record_store=typed_record_store,
        context_name=request.context,
        instance_name=request.instance,
    )
    override_record_found = odoo_override_record is not None
    override_phase_enabled = bool(
        odoo_override_record is not None and request.phase in odoo_override_record.apply_on
    )
    workflow_environment_overrides: dict[str, str] = {}
    required_workflow_environment_keys: tuple[str, ...] = ()

    target_definition = _resolve_compose_target_definition(
        control_plane_root=control_plane_root,
        request=request,
    )
    protected_shopify_store_keys = (
        control_plane_dokploy.protected_shopify_store_keys_for_target_definition(target_definition)
    )

    if odoo_override_record is not None and override_phase_enabled:
        try:
            post_deploy_environment = (
                control_plane_odoo_instance_overrides.build_post_deploy_environment(
                    odoo_override_record,
                    protected_shopify_store_keys=protected_shopify_store_keys,
                )
            )
            workflow_environment_overrides = post_deploy_environment.inline_environment
            required_workflow_environment_keys = (
                post_deploy_environment.required_container_environment_keys
            )
        except click.ClickException as error:
            _write_odoo_instance_override_apply_result(
                record_store=typed_record_store,
                record=odoo_override_record,
                status="fail",
                detail=str(error),
            )
            return OdooPostDeployResult(
                context=request.context,
                instance=request.instance,
                phase=request.phase,
                post_deploy_status="fail",
                override_status="fail",
                override_record_found=True,
                required_container_environment_keys=required_workflow_environment_keys,
                error_message=str(error),
            )

    try:
        host, token = control_plane_dokploy.read_dokploy_config(
            control_plane_root=control_plane_root
        )
        control_plane_dokploy.run_compose_post_deploy_update(
            host=host,
            token=token,
            target_definition=target_definition,
            env_file=env_file,
            workflow_environment_overrides=workflow_environment_overrides,
            required_workflow_environment_keys=required_workflow_environment_keys,
            protected_shopify_store_keys=protected_shopify_store_keys,
        )
    except click.ClickException as error:
        if odoo_override_record is not None and override_phase_enabled:
            _write_odoo_instance_override_apply_result(
                record_store=typed_record_store,
                record=odoo_override_record,
                status="fail",
                detail=str(error),
            )
        return OdooPostDeployResult(
            context=request.context,
            instance=request.instance,
            phase=request.phase,
            post_deploy_status="fail",
            override_status="fail" if override_phase_enabled else "skipped",
            override_record_found=override_record_found,
            override_payload_rendered=bool(
                workflow_environment_overrides or required_workflow_environment_keys
            ),
            required_container_environment_keys=required_workflow_environment_keys,
            error_message=str(error),
        )

    override_status: OdooOverrideApplyStatus = "skipped"
    applied_at = ""
    detail = "No Odoo instance override record matched this post-deploy request."
    if odoo_override_record is not None:
        if override_phase_enabled and (
            workflow_environment_overrides or required_workflow_environment_keys
        ):
            override_status = "pass"
            detail = (
                "Applied Odoo instance overrides through the Launchplane Odoo post-deploy driver."
            )
        elif override_phase_enabled:
            detail = "No Odoo instance overrides were rendered for this post-deploy run."
        else:
            detail = f"Odoo instance override record is not configured for phase {request.phase}."
        updated_record = _write_odoo_instance_override_apply_result(
            record_store=typed_record_store,
            record=odoo_override_record,
            status=override_status,
            detail=detail,
        )
        applied_at = updated_record.last_apply.applied_at

    return OdooPostDeployResult(
        context=request.context,
        instance=request.instance,
        phase=request.phase,
        post_deploy_status="pass",
        override_status=override_status,
        override_record_found=override_record_found,
        override_payload_rendered=bool(
            workflow_environment_overrides or required_workflow_environment_keys
        ),
        required_container_environment_keys=required_workflow_environment_keys,
        applied_at=applied_at,
    )
