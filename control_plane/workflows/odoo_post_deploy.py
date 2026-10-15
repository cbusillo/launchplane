from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Literal, Protocol, cast

import click
from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.durable_operation_authorization import DurableOperationAuthorizationDeniedError
from control_plane import odoo_instance_overrides as control_plane_odoo_instance_overrides
from control_plane.contracts.odoo_instance_override_record import (
    OdooInstanceOverrideRecord,
    OdooOverrideApplyPhase,
    OdooOverrideApplyResult,
    OdooOverrideApplyStatus,
)
from control_plane.contracts.odoo_post_deploy_payload import OdooPostDeployPayload
from control_plane.contracts.odoo_website_settings import OdooWebsiteSettingsIntent
from control_plane.contracts.public_hosts import resolve_public_base_url
from control_plane.contracts.odoo_post_deploy_payload import OdooPostDeployWorkflowIntent
from control_plane.contracts.odoo_target_replacement_failures import (
    OdooProviderEffectUncertainError,
    OdooProviderSafetyRefusedError,
)
from control_plane.workflows.ship import utc_now_timestamp
from control_plane.dokploy import api as dokploy_api
from control_plane.dokploy import source as dokploy_source
from control_plane.dokploy import post_deploy as dokploy_post_deploy
from control_plane.dokploy.connection_isolation import (
    ConnectionIsolationObservation,
    observe_connection_isolation_operation,
    run_connection_isolation,
)
from control_plane.odoo_connection_isolation import OdooConnectionIsolationMode
from control_plane.odoo_testing_restore import FreshOdooTestingGeneration
from control_plane.runtime_key_safety import runtime_key_safety_environment_class


class OdooPostDeployStore(Protocol):
    def read_odoo_instance_override_record(
        self, *, context_name: str, instance_name: str
    ) -> OdooInstanceOverrideRecord: ...

    def write_odoo_instance_override_record(self, record: OdooInstanceOverrideRecord) -> object: ...

    def update_odoo_instance_override_apply_result(
        self,
        *,
        context_name: str,
        instance_name: str,
        last_apply: OdooOverrideApplyResult,
        updated_at: str,
        source_label: str,
        preserve_website_settings: bool = False,
    ) -> OdooInstanceOverrideRecord: ...


class OdooPostDeployRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    context: str
    instance: str
    phase: OdooOverrideApplyPhase = "deploy"
    connection_isolation: OdooConnectionIsolationMode | None = None
    reviewed_dry_run: bool = False
    reviewed_plan_sha256: str = Field(default="", pattern=r"^(|[a-f0-9]{64})$")
    reviewed_resource_sha256: str = Field(default="", pattern=r"^(|[a-f0-9]{64})$")
    reason: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def _validate_request(self) -> "OdooPostDeployRequest":
        self.context = self.context.strip().lower()
        self.instance = self.instance.strip().lower()
        if not self.context:
            raise ValueError("Odoo post-deploy request requires context.")
        if not self.instance:
            raise ValueError("Odoo post-deploy request requires instance.")
        if self.connection_isolation is not None:
            if runtime_key_safety_environment_class(self.instance) == "prod":
                raise ValueError("Connection isolation refuses production.")
            if self.instance != "testing":
                raise ValueError("Connection isolation requires a testing lane.")
            if self.connection_isolation == "apply" and (
                not self.reviewed_dry_run
                or len(self.reviewed_plan_sha256) != 64
                or len(self.reviewed_resource_sha256) != 64
                or not self.reason.strip()
            ):
                raise ValueError("Connection isolation apply requires a reviewed dry-run.")
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
    override_payload_sha256: str = ""
    override_payload_schema_version: int | None = None
    override_count: int = 0
    website_bootstrap_included: bool = False
    workflow_intent: OdooPostDeployWorkflowIntent = "deploy"
    required_container_environment_keys: tuple[str, ...] = ()
    override_evidence: dict[str, str] = Field(default_factory=dict)
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
    applied_website_settings: OdooWebsiteSettingsIntent | None = None,
    settings_attempted: bool = False,
    website_settings_verified: bool | None = None,
) -> OdooInstanceOverrideRecord:
    now = utc_now_timestamp()
    return record_store.update_odoo_instance_override_apply_result(
        context_name=record.context,
        instance_name=record.instance,
        last_apply=OdooOverrideApplyResult(
            attempted=status in {"pending", "pass", "fail"},
            status=status,
            applied_at=now if status in {"pass", "fail"} else "",
            detail=detail,
            website_settings=applied_website_settings
            if settings_attempted
            else record.last_apply.website_settings,
            website_settings_verified=website_settings_verified
            if website_settings_verified is not None
            else record.last_apply.website_settings_verified,
        ),
        updated_at=now,
        source_label=source_label,
        preserve_website_settings=not settings_attempted,
    )


def _prefix_post_deploy_readback_evidence(markers: dict[str, str]) -> dict[str, str]:
    return {f"post_deploy_readback_{key}": value for key, value in markers.items()}


def _require_record_store(record_store: object) -> OdooPostDeployStore:
    required_methods = (
        "read_odoo_instance_override_record",
        "update_odoo_instance_override_apply_result",
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
) -> dokploy_source.DokployTargetDefinition:
    source_of_truth = dokploy_source.read_control_plane_dokploy_source_of_truth(
        control_plane_root=control_plane_root,
    )
    target_definition = dokploy_source.find_dokploy_target_definition(
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


def observe_odoo_connection_isolation(
    *,
    control_plane_root: Path,
    request: OdooPostDeployRequest,
    provider_operation_key: str,
    provider_effect_phase: str,
) -> ConnectionIsolationObservation:
    try:
        target = _resolve_compose_target_definition(
            control_plane_root=control_plane_root, request=request
        )
        host, token = dokploy_source.read_dokploy_config(control_plane_root=control_plane_root)
        return observe_connection_isolation_operation(
            host=host,
            token=token,
            target_definition=target,
            provider_operation_key=provider_operation_key,
            provider_effect_phase=provider_effect_phase,
            reviewed_plan_sha256=request.reviewed_plan_sha256,
            reviewed_resource_sha256=request.reviewed_resource_sha256,
        )
    except (FileNotFoundError, click.ClickException, ValueError):
        return ConnectionIsolationObservation("unknown", {})


def execute_odoo_post_deploy(
    *,
    control_plane_root: Path,
    record_store: object,
    request: OdooPostDeployRequest,
    env_file: Path | None = None,
    run_destructive_restore: bool = False,
    provider_effect_checkpoint: Callable[[str], None] | None = None,
    hold_uncertain_effects: bool = False,
    provider_operation_title: str = "",
    schedule_execution_timeout_seconds: int | None = None,
    allow_historical_sender_contract: bool = False,
    before_connection_isolation_mutation: Callable[[str], None] | None = None,
    connection_isolation_operation_key: str = "",
    connection_isolation_product: str = "",
    fresh_testing_generation: FreshOdooTestingGeneration | None = None,
    fresh_restore_source_environment: dict[str, str] | None = None,
    artifact_id: str = "",
    release_website_settings: OdooWebsiteSettingsIntent | None = None,
    release_operation: bool = False,
    rollback: bool = False,
) -> OdooPostDeployResult:
    if request.connection_isolation is not None:
        target = _resolve_compose_target_definition(
            control_plane_root=control_plane_root,
            request=request,
        )
        host, token = dokploy_source.read_dokploy_config(control_plane_root=control_plane_root)
        try:
            evidence = run_connection_isolation(
                host=host,
                token=token,
                target_definition=target,
                mode=request.connection_isolation,
                reviewed_plan_sha256=request.reviewed_plan_sha256,
                before_mutation=before_connection_isolation_mutation,
                reviewed_resource_sha256=request.reviewed_resource_sha256,
                provider_operation_key=connection_isolation_operation_key,
                expected_product=connection_isolation_product,
            )
        except dokploy_post_deploy.OdooPostDeployReadbackFailure as error:
            return OdooPostDeployResult(
                context=request.context,
                instance=request.instance,
                phase=request.phase,
                post_deploy_status="fail",
                override_evidence=error.evidence,
                error_message=str(error),
            )
        return OdooPostDeployResult(
            context=request.context,
            instance=request.instance,
            phase=request.phase,
            post_deploy_status="pass",
            override_evidence=evidence,
        )
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
    override_should_apply = override_phase_enabled or bool(
        run_destructive_restore
        and odoo_override_record is not None
        and "deploy" in odoo_override_record.apply_on
    )
    workflow_environment_overrides: dict[str, str] = {}
    required_workflow_environment_keys: tuple[str, ...] = ()
    workflow_intent: OdooPostDeployWorkflowIntent = (
        "restore" if run_destructive_restore and fresh_testing_generation is None else "deploy"
    )
    override_payload: OdooPostDeployPayload | None = None
    post_deploy_readback_markers: dict[str, str] = {}

    target_definition = _resolve_compose_target_definition(
        control_plane_root=control_plane_root,
        request=request,
    )
    protected_shopify_store_keys = (
        dokploy_source.protected_shopify_store_keys_for_target_definition(target_definition)
    )
    public_hosts = (
        target_definition.public_hosts
        if not run_destructive_restore and request.phase in {"deploy", "promotion"}
        else ()
    )
    public_base_url = resolve_public_base_url(instance=request.instance, public_hosts=public_hosts)
    rendering_record = control_plane_odoo_instance_overrides.record_with_public_base_url(
        odoo_override_record if override_should_apply else None,
        context=request.context,
        instance=request.instance,
        public_hosts=public_hosts,
        origin_domains=target_definition.domains
        if not run_destructive_restore and request.phase in {"deploy", "promotion"}
        else (),
        updated_at=utc_now_timestamp(),
    )

    try:
        accepted_settings: OdooWebsiteSettingsIntent | None = None
        if rendering_record is not None:
            if control_plane_odoo_instance_overrides.odoo_instance_is_production(request.instance):
                if not artifact_id and (
                    rendering_record.website_settings is not None
                    or rendering_record.last_apply.website_settings is not None
                    or release_website_settings is not None
                ):
                    release_reader = getattr(record_store, "read_release_tuple_record", None)
                    if not callable(release_reader):
                        raise click.ClickException(
                            "Website settings require recorded artifact identity."
                        )
                    try:
                        artifact_id = release_reader(
                            context_name=request.context, channel_name=request.instance
                        ).artifact_id
                    except FileNotFoundError as error:
                        raise click.ClickException(
                            "Website settings require the current production release record."
                        ) from error
                if release_operation:
                    if (
                        release_website_settings is not None
                        and release_website_settings.artifact_id != artifact_id
                    ):
                        raise click.ClickException(
                            "Website settings differ from the operation artifact."
                        )
                    accepted_settings = release_website_settings
                    if rollback and accepted_settings is None:
                        applied = rendering_record.last_apply.website_settings
                        if (
                            rendering_record.last_apply.website_settings_verified
                            and applied is not None
                            and applied.artifact_id == artifact_id
                        ):
                            accepted_settings = applied
                else:
                    applied = rendering_record.last_apply.website_settings
                    if (
                        not rendering_record.last_apply.website_settings_verified
                        and applied is not None
                        and applied.artifact_id != artifact_id
                    ):
                        raise click.ClickException(
                            "Unverified website settings require recovery through the recorded release or rollback operation; ordinary maintenance cannot change its artifact."
                        )
                    accepted_settings = (
                        applied
                        if applied is not None and applied.artifact_id == artifact_id
                        else None
                    )
            rendered_settings = accepted_settings
            if (
                rollback
                and rendered_settings is None
                and rendering_record.last_apply.website_settings is not None
            ):
                # This is an explicit historical rollback, never preparation or maintenance.
                rendered_settings = rendering_record.last_apply.website_settings.model_copy(
                    update={
                        "artifact_id": artifact_id,
                        "settings": rendering_record.last_apply.website_settings.settings.model_copy(
                            update={
                                "operation": "disable_collection",
                                "google_analytics_key": "",
                                "public_indexing_enabled": False,
                                "cookies_bar": None,
                                "block_third_party_domains": None,
                            }
                        ),
                    }
                )
            rendering_record = rendering_record.model_copy(
                update={"website_settings": rendered_settings}
            )
        if rendering_record is not None:
            post_deploy_environment = (
                control_plane_odoo_instance_overrides.build_post_deploy_environment(
                    rendering_record,
                    record_store=record_store,
                    workflow_intent=workflow_intent,
                    protected_shopify_store_keys=protected_shopify_store_keys,
                    selected_website_settings=rendering_record.website_settings.settings
                    if rendering_record.website_settings is not None
                    else None,
                )
            )
            override_payload = post_deploy_environment.payload
            workflow_environment_overrides = post_deploy_environment.inline_environment
            required_workflow_environment_keys = (
                post_deploy_environment.required_container_environment_keys
            )
    except click.ClickException as error:
        if odoo_override_record is not None and override_should_apply:
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
            override_status="fail" if override_should_apply else "skipped",
            override_record_found=override_record_found,
            workflow_intent=workflow_intent,
            required_container_environment_keys=required_workflow_environment_keys,
            error_message=str(error),
        )

    provider_effect_started = False

    def before_provider_effect(effect_name: str) -> None:
        nonlocal provider_effect_started
        if provider_effect_checkpoint is not None:
            provider_effect_checkpoint(effect_name)
        provider_effect_started = True

    try:
        host, token = dokploy_source.read_dokploy_config(control_plane_root=control_plane_root)
        post_deploy_readback_markers = (
            dokploy_post_deploy.run_compose_post_deploy_update(
                host=host,
                token=token,
                target_definition=target_definition,
                env_file=env_file,
                workflow_environment_overrides=workflow_environment_overrides,
                required_workflow_environment_keys=required_workflow_environment_keys,
                run_destructive_restore=run_destructive_restore,
                before_provider_mutation=before_provider_effect,
                deployment_title=provider_operation_title,
                schedule_execution_timeout_seconds=schedule_execution_timeout_seconds,
                allow_historical_sender_contract=allow_historical_sender_contract,
                fresh_testing_generation=fresh_testing_generation,
                fresh_restore_source_environment=fresh_restore_source_environment,
            )
            or {}
        )
        dokploy_post_deploy.require_odoo_module_update_readback_evidence(
            post_deploy_readback_markers
        )
        if override_payload is not None and override_payload.website_settings is not None:
            if (
                post_deploy_readback_markers.get("website_settings_sha256")
                != override_payload.website_settings.digest
            ):
                raise dokploy_post_deploy.OdooPostDeployReadbackFailure(
                    "Odoo website settings readback does not match the accepted release.",
                    evidence=post_deploy_readback_markers,
                )
        if (
            override_payload
            and override_payload.website_bootstrap
            and override_payload.website_bootstrap.company_email
        ):
            dokploy_post_deploy.require_odoo_company_email_readback_evidence(
                post_deploy_readback_markers,
                allow_historical_contract=allow_historical_sender_contract,
            )
    except (click.ClickException, OSError) as error:
        determinate_failure = isinstance(
            error,
            (
                dokploy_api.DokployDeploymentFailed,
                OdooProviderSafetyRefusedError,
                DurableOperationAuthorizationDeniedError,
                dokploy_post_deploy.OdooPostDeployReadbackFailure,
            ),
        ) or (
            isinstance(error, dokploy_api.DokployScheduleExecutionFailed)
            and error.cause in {"remote_command_exit", "trigger_rejected"}
        )
        uncertain = hold_uncertain_effects and provider_effect_started and not determinate_failure
        if isinstance(error, OSError) and not uncertain:
            raise
        if isinstance(error, dokploy_post_deploy.OdooPostDeployReadbackFailure):
            post_deploy_readback_markers = error.evidence
        if odoo_override_record is not None and override_should_apply:
            _write_odoo_instance_override_apply_result(
                record_store=typed_record_store,
                record=odoo_override_record,
                status="fail",
                detail=str(error),
                applied_website_settings=rendering_record.website_settings
                if rendering_record
                else None,
                settings_attempted=bool(override_payload and override_payload.website_settings),
                website_settings_verified=False
                if override_payload and override_payload.website_settings
                else None,
            )
        if isinstance(
            error, (OdooProviderSafetyRefusedError, DurableOperationAuthorizationDeniedError)
        ):
            raise
        if uncertain:
            raise OdooProviderEffectUncertainError(
                "Odoo post-deploy provider effect requires reconciliation."
            ) from error
        return OdooPostDeployResult(
            context=request.context,
            instance=request.instance,
            phase=request.phase,
            post_deploy_status="fail",
            override_status="fail" if override_should_apply else "skipped",
            override_record_found=override_record_found,
            override_payload_rendered=bool(
                workflow_environment_overrides or required_workflow_environment_keys
            ),
            override_payload_sha256=override_payload.wire_sha256 if override_payload else "",
            override_payload_schema_version=(
                override_payload.schema_version if override_payload else None
            ),
            override_count=override_payload.override_count if override_payload else 0,
            website_bootstrap_included=(
                override_payload.website_bootstrap_included if override_payload else False
            ),
            workflow_intent=workflow_intent,
            required_container_environment_keys=required_workflow_environment_keys,
            override_evidence=(
                {
                    **(override_payload.redacted_evidence() if override_payload else {}),
                    **_prefix_post_deploy_readback_evidence(post_deploy_readback_markers),
                }
            ),
            error_message=str(error),
        )

    override_status: OdooOverrideApplyStatus = "skipped"
    applied_at = ""
    if odoo_override_record is not None:
        if override_should_apply and (
            workflow_environment_overrides or required_workflow_environment_keys
        ):
            override_status = "pass"
            detail = (
                "Applied Odoo instance overrides through the Launchplane Odoo post-deploy driver."
            )
        elif override_should_apply:
            detail = "No Odoo instance overrides were rendered for this post-deploy run."
        else:
            detail = f"Odoo instance override record is not configured for phase {request.phase}."
        sender_skip_reason = post_deploy_readback_markers.get(
            "website_bootstrap_company_email_skip_reason", ""
        )
        if sender_skip_reason:
            detail += f" Website company sender was not verified: {sender_skip_reason}."
        updated_record = _write_odoo_instance_override_apply_result(
            record_store=typed_record_store,
            record=odoo_override_record,
            status=override_status,
            detail=detail,
            applied_website_settings=rendering_record.website_settings
            if rendering_record is not None
            else None,
            settings_attempted=bool(override_payload and override_payload.website_settings),
            website_settings_verified=True
            if override_payload and override_payload.website_settings
            else None,
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
        override_payload_sha256=override_payload.wire_sha256 if override_payload else "",
        override_payload_schema_version=(
            override_payload.schema_version if override_payload else None
        ),
        override_count=override_payload.override_count if override_payload else 0,
        website_bootstrap_included=(
            override_payload.website_bootstrap_included if override_payload else False
        ),
        workflow_intent=workflow_intent,
        required_container_environment_keys=required_workflow_environment_keys,
        override_evidence={
            **({"resolved_base_url": public_base_url} if public_base_url else {}),
            **(override_payload.redacted_evidence() if override_payload else {}),
            **_prefix_post_deploy_readback_evidence(post_deploy_readback_markers),
        },
        applied_at=applied_at,
    )
