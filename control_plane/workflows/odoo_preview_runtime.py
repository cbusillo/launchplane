from __future__ import annotations

import time
from pathlib import Path
from typing import Literal, Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import click
from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane import dokploy as control_plane_dokploy
from control_plane import runtime_environments as control_plane_runtime_environments
from control_plane.dokploy import JsonObject
from control_plane.contracts.artifact_identity import ArtifactIdentityManifest
from control_plane.contracts.odoo_preview_runtime_plan import (
    OdooPreviewProviderCapabilities,
    OdooPreviewRuntimeBindingEvidence,
    OdooPreviewRuntimeBlocker,
    OdooPreviewRuntimeOperation,
    OdooPreviewRuntimePlan,
    OdooPreviewRuntimePlanRequest,
    OdooPreviewRuntimeTargetEvidence,
    OdooPreviewRuntimePlanStatus,
    plan_odoo_preview_runtime,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.secret_record import SecretBinding
from control_plane.secrets import RUNTIME_ENVIRONMENT_SECRET_INTEGRATION
from control_plane.workflows.generic_web_preview import (
    GenericWebPreviewRefreshRequest,
    effective_preview_app_name_prefix,
    preview_application_name,
    resolve_generic_web_preview_url,
)
from control_plane.workflows.preview_resource_destroy import (
    destroy_dokploy_preview_resource,
)


OdooPreviewDokployDryRunStatus = OdooPreviewRuntimePlanStatus
OdooPreviewDokployDryRunBlockerCode = Literal[
    "endpoint_path_missing",
    "environment_id_missing",
    "preview_url_invalid",
    "runtime_plan_not_ready",
    "template_compose_id_missing",
]
OdooPreviewDokployApplyStatus = Literal["pass", "blocked", "fail"]
OdooPreviewDokployOperationName = Literal[
    "compose_create",
    "compose_update_raw_source",
    "compose_update_env",
    "domain_lookup",
    "domain_create_or_update",
    "compose_deploy",
    "smoke_check",
    "domain_delete",
    "compose_delete",
]

ODOO_PREVIEW_REQUIRED_ENV_KEYS = (
    "ODOO_DB_NAME",
    "ODOO_DB_USER",
    "ODOO_DB_PASSWORD",
    "ODOO_DATA_VOLUME",
    "ODOO_LOG_VOLUME",
    "ODOO_DB_VOLUME",
    "ODOO_MASTER_PASSWORD",
    "ODOO_ADMIN_PASSWORD",
)


class OdooPreviewApplyInputsStore(Protocol):
    def list_runtime_environment_records(
        self, *, context_name: str = "", instance_name: str = ""
    ) -> tuple[RuntimeEnvironmentRecord, ...]: ...

    def list_secret_bindings(
        self,
        *,
        integration: str = "",
        context_name: str = "",
        instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[SecretBinding, ...]: ...


class OdooPreviewApplyInputsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    operation: OdooPreviewRuntimeOperation = "refresh"
    pr_number: int = Field(ge=1)
    preview_slug: str = ""
    preview_url: str = ""
    image_reference: str = ""
    manifest: ArtifactIdentityManifest | None = None
    source_git_ref: str = ""
    source: str = "odoo-preview-apply-inputs"
    timeout_seconds: int = Field(default=300, ge=1)
    no_cache: bool = False

    @model_validator(mode="after")
    def _normalize_request(self) -> "OdooPreviewApplyInputsRequest":
        self.product = _required_text(self.product, "Odoo preview apply inputs requires product")
        if self.preview_slug.strip():
            self.preview_slug = self.preview_slug.strip()
        self.preview_url = self.preview_url.strip()
        self.image_reference = self.image_reference.strip()
        if self.manifest is not None or self.image_reference or self.operation == "refresh":
            self.image_reference = _resolve_manifest_image_reference(
                image_reference=self.image_reference,
                manifest=self.manifest,
                label="Odoo preview apply inputs",
            )
        self.source_git_ref = self.source_git_ref.strip()
        if self.manifest is not None and not self.source_git_ref:
            self.source_git_ref = self.manifest.source_commit
        self.source = _required_text(self.source, "Odoo preview apply inputs requires source")
        return self


class OdooPreviewApplyInputsResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ready", "blocked"]
    product: str
    context: str
    template_instance: str
    operation: OdooPreviewRuntimeOperation
    preview_slug: str
    preview_url: str
    repository: str
    runtime_plan: OdooPreviewRuntimePlan
    dry_run_plan: OdooPreviewDokployDryRunPlan
    source: str
    error_message: str = ""

    @model_validator(mode="after")
    def _normalize_result(self) -> "OdooPreviewApplyInputsResult":
        self.product = _required_text(
            self.product, "Odoo preview apply inputs result requires product"
        )
        self.context = _required_text(
            self.context, "Odoo preview apply inputs result requires context"
        )
        self.template_instance = _required_text(
            self.template_instance, "Odoo preview apply inputs result requires template_instance"
        )
        self.preview_slug = _required_text(
            self.preview_slug, "Odoo preview apply inputs result requires preview_slug"
        )
        self.repository = _required_text(
            self.repository, "Odoo preview apply inputs result requires repository"
        )
        self.error_message = self.error_message.strip()
        return self


def build_odoo_preview_apply_inputs(
    *,
    control_plane_root: Path,
    record_store: OdooPreviewApplyInputsStore,
    profile: LaunchplaneProductProfileRecord,
    request: OdooPreviewApplyInputsRequest,
    database_url: str | None = None,
) -> OdooPreviewApplyInputsResult:
    preview_profile = profile.preview
    if not preview_profile.enabled or not preview_profile.context.strip():
        raise click.ClickException(
            f"Product {profile.product!r} does not have Odoo previews enabled."
        )

    preview_slug = _preview_slug(profile=profile, request=request)
    preview_url_error = ""
    try:
        preview_url = resolve_odoo_preview_url(
            control_plane_root=control_plane_root,
            profile=profile,
            preview_slug=preview_slug,
            preview_url=request.preview_url,
            database_url=database_url,
        )
    except click.ClickException as exc:
        preview_url = request.preview_url.strip()
        preview_url_error = str(exc)
    template_instance = preview_profile.template_instance.strip()
    runtime_bindings = _preview_runtime_bindings(
        record_store=record_store,
        context_name=preview_profile.context,
        instance_name=template_instance,
    )
    compose_name = preview_application_name(
        app_name_prefix=effective_preview_app_name_prefix(profile=profile),
        preview_slug=preview_slug,
    )
    target: OdooPreviewRuntimeTargetEvidence | None = None
    discovery_error = ""
    try:
        target = _discover_odoo_preview_target(
            control_plane_root=control_plane_root,
            context_name=preview_profile.context,
            preview_slug=preview_slug,
            preview_url=preview_url,
            compose_name=compose_name,
            database_url=database_url,
        )
    except click.ClickException as exc:
        discovery_error = str(exc)
    runtime_plan = plan_odoo_preview_runtime(
        request=OdooPreviewRuntimePlanRequest(
            operation=request.operation,
            product=profile.product,
            repository=profile.repository,
            pr_number=request.pr_number,
            preview_slug=preview_slug,
            preview_url=preview_url,
            strategy="isolated_dokploy_compose",
            image_reference=request.image_reference,
            source_git_ref=request.source_git_ref,
            target=target,
            provider_capabilities=_odoo_preview_provider_capabilities(),
            runtime_bindings=runtime_bindings,
            required_runtime_keys=ODOO_PREVIEW_REQUIRED_ENV_KEYS,
        )
    )
    if preview_url_error:
        runtime_plan = _with_runtime_blocker(
            runtime_plan=runtime_plan,
            blocker=OdooPreviewRuntimeBlocker(
                code="preview_url_missing",
                message=preview_url_error,
            ),
        )
    if discovery_error and request.operation == "destroy":
        runtime_plan = _with_runtime_blocker(
            runtime_plan=runtime_plan,
            blocker=OdooPreviewRuntimeBlocker(
                code="runtime_target_discovery_failed",
                message=discovery_error,
            ),
        )
    template_compose_id = _preview_template_compose_id(
        control_plane_root=control_plane_root,
        context_name=preview_profile.context,
        instance_name=template_instance,
    )
    environment_id, environment_error = _preview_environment_id(
        control_plane_root=control_plane_root,
        context_name=preview_profile.context,
        instance_name=template_instance,
        database_url=database_url,
    )
    dry_run_plan = build_odoo_preview_dokploy_dry_run(
        request=OdooPreviewDokployDryRunRequest(
            runtime_plan=runtime_plan,
            no_cache=request.no_cache,
            runtime_port=profile.runtime_port,
            compose_name=compose_name,
            environment_id=environment_id,
            template_compose_id=template_compose_id,
        )
    )
    if environment_error and _needs_preview_environment_id(runtime_plan):
        dry_run_plan = _with_dry_run_blocker(
            dry_run_plan=dry_run_plan,
            blocker=OdooPreviewDokployDryRunBlocker(
                code="environment_id_missing",
                message=environment_error,
            ),
        )
    status: Literal["ready", "blocked"] = (
        "ready" if runtime_plan.status == "ready" and dry_run_plan.status == "ready" else "blocked"
    )
    return OdooPreviewApplyInputsResult(
        status=status,
        product=profile.product,
        context=preview_profile.context,
        template_instance=template_instance,
        operation=request.operation,
        preview_slug=preview_slug,
        preview_url=preview_url,
        repository=profile.repository,
        runtime_plan=runtime_plan,
        dry_run_plan=dry_run_plan,
        source=request.source,
        error_message=(
            "" if status == "ready" else _blocked_inputs_message(runtime_plan, dry_run_plan)
        ),
    )


def _needs_preview_environment_id(runtime_plan: OdooPreviewRuntimePlan) -> bool:
    return runtime_plan.operation == "refresh" and runtime_plan.target is None


def _preview_slug(
    *, profile: LaunchplaneProductProfileRecord, request: OdooPreviewApplyInputsRequest
) -> str:
    if request.preview_slug.strip():
        return request.preview_slug.strip()
    return profile.preview.slug_template.strip().replace("{number}", str(request.pr_number))


def resolve_odoo_preview_url(
    *,
    control_plane_root: Path,
    profile: LaunchplaneProductProfileRecord,
    preview_slug: str,
    preview_url: str,
    database_url: str | None = None,
) -> str:
    preview_refresh_request = GenericWebPreviewRefreshRequest(
        product=profile.product,
        preview_slug=preview_slug,
        preview_url=preview_url,
        image_reference="__launchplane_url_resolution__",
    )
    return resolve_generic_web_preview_url(
        control_plane_root=control_plane_root,
        profile=profile,
        request=preview_refresh_request,
        database_url=database_url,
    )


def _preview_runtime_bindings(
    *, record_store: OdooPreviewApplyInputsStore, context_name: str, instance_name: str
) -> tuple[OdooPreviewRuntimeBindingEvidence, ...]:
    bindings: list[OdooPreviewRuntimeBindingEvidence] = []
    definition = (
        control_plane_runtime_environments.load_optional_runtime_environment_definition_from_store(
            record_store=record_store
        )
    )
    if definition is not None:
        merged_values = _preview_merged_runtime_environment_keys(
            definition=definition,
            context_name=context_name,
            instance_name=instance_name,
        )
        bindings.extend(
            OdooPreviewRuntimeBindingEvidence(key=key, source="runtime_environment")
            for key in sorted(merged_values)
        )
    secret_bindings = record_store.list_secret_bindings(
        integration=RUNTIME_ENVIRONMENT_SECRET_INTEGRATION,
        context_name=context_name,
        instance_name=instance_name,
    )
    bindings.extend(
        OdooPreviewRuntimeBindingEvidence(key=binding.binding_key, source="managed_secret")
        for binding in secret_bindings
        if binding.status == "configured"
    )
    bindings.extend(
        OdooPreviewRuntimeBindingEvidence(key=key, source="generated")
        for key in (
            "ODOO_DB_NAME",
            "ODOO_DATA_VOLUME",
            "ODOO_LOG_VOLUME",
            "ODOO_DB_VOLUME",
        )
    )
    return tuple({binding.key: binding for binding in bindings}.values())


def _preview_merged_runtime_environment_keys(
    *,
    definition: control_plane_runtime_environments.RuntimeEnvironmentDefinition,
    context_name: str,
    instance_name: str,
) -> dict[str, str]:
    merged_values: dict[str, str] = {
        key: str(value) for key, value in definition.shared_env.items()
    }
    context_definition = definition.contexts.get(context_name)
    if context_definition is None:
        return merged_values
    merged_values.update({key: str(value) for key, value in context_definition.shared_env.items()})
    instance_definition = context_definition.instances.get(instance_name)
    if instance_definition is not None:
        merged_values.update({key: str(value) for key, value in instance_definition.env.items()})
    return {key: value for key, value in merged_values.items() if value.strip()}


def _odoo_preview_provider_capabilities() -> OdooPreviewProviderCapabilities:
    return OdooPreviewProviderCapabilities(
        can_create_compose=True,
        can_update_compose_env=True,
        can_deploy_compose=True,
        can_bind_domain=True,
        can_delete_compose=True,
        can_delete_domain=True,
    )


def _preview_template_compose_id(
    *, control_plane_root: Path, context_name: str, instance_name: str
) -> str:
    target_definition = _preview_template_target_definition(
        control_plane_root=control_plane_root,
        context_name=context_name,
        instance_name=instance_name,
    )
    if target_definition is None or target_definition.target_type != "compose":
        return ""
    return target_definition.target_id.strip()


def _preview_environment_id(
    *,
    control_plane_root: Path,
    context_name: str,
    instance_name: str,
    database_url: str | None,
) -> tuple[str, str]:
    try:
        environment_values = control_plane_runtime_environments.resolve_runtime_environment_values(
            control_plane_root=control_plane_root,
            context_name=context_name,
            instance_name=instance_name,
            database_url=database_url,
        )
    except click.ClickException as exc:
        return "", str(exc)
    return environment_values.get("DOKPLOY_ENVIRONMENT_ID", "").strip(), ""


def _preview_template_target_definition(
    *, control_plane_root: Path, context_name: str, instance_name: str
) -> control_plane_dokploy.DokployTargetDefinition | None:
    try:
        source_of_truth = control_plane_dokploy.read_control_plane_dokploy_source_of_truth(
            control_plane_root=control_plane_root,
            allow_incomplete_target_ids=True,
            allowed_incomplete_target_routes=((context_name, instance_name),),
        )
    except click.ClickException:
        return None
    return control_plane_dokploy.find_dokploy_target_definition(
        source_of_truth,
        context_name=context_name,
        instance_name=instance_name,
    )


def _discover_odoo_preview_target(
    *,
    control_plane_root: Path,
    context_name: str,
    preview_slug: str,
    preview_url: str,
    compose_name: str,
    database_url: str | None,
) -> OdooPreviewRuntimeTargetEvidence | None:
    domain_host = _domain_host(preview_url)
    host, token = control_plane_dokploy.read_dokploy_config(
        control_plane_root=control_plane_root,
        database_url=database_url,
    )
    raw_projects = control_plane_dokploy.dokploy_request(
        host=host,
        token=token,
        path="/api/project.all",
    )
    matches: list[OdooPreviewRuntimeTargetEvidence] = []
    for compose in _iter_dokploy_composes(raw_projects):
        _append_preview_target_match(
            matches=matches,
            compose=compose,
            compose_name=compose_name,
            context_name=context_name,
            preview_slug=preview_slug,
            domain_host=domain_host,
            host=host,
            token=token,
        )
    if not matches:
        raw_search_matches = control_plane_dokploy.dokploy_request(
            host=host,
            token=token,
            path="/api/compose.search",
            query={"q": compose_name, "limit": 25},
        )
        for compose in _iter_dokploy_search_composes(raw_search_matches):
            _append_preview_target_match(
                matches=matches,
                compose=compose,
                compose_name=compose_name,
                context_name=context_name,
                preview_slug=preview_slug,
                domain_host=domain_host,
                host=host,
                token=token,
            )
    if len(matches) > 1:
        raise click.ClickException(
            f"Discovered multiple Odoo preview composes named {compose_name!r}; refusing to plan mutation."
        )
    if matches:
        return matches[0]
    return None


def _append_preview_target_match(
    *,
    matches: list[OdooPreviewRuntimeTargetEvidence],
    compose: JsonObject,
    compose_name: str,
    context_name: str,
    preview_slug: str,
    domain_host: str,
    host: str,
    token: str,
) -> None:
    discovered_name = str(compose.get("name") or "").strip()
    if discovered_name != compose_name:
        return
    compose_id = str(compose.get("composeId") or compose.get("id") or "").strip()
    if not compose_id:
        return
    if domain_host and not _compose_has_domain(
        host=host,
        token=token,
        compose_id=compose_id,
        domain_host=domain_host,
    ):
        return
    if any(match.target_id == compose_id for match in matches):
        return
    matches.append(
        OdooPreviewRuntimeTargetEvidence(
            target_id=compose_id,
            target_name=discovered_name,
            context=context_name,
            instance=preview_slug,
            environment_kind="preview",
            domain=domain_host,
        )
    )


def _with_runtime_blocker(
    *, runtime_plan: OdooPreviewRuntimePlan, blocker: OdooPreviewRuntimeBlocker
) -> OdooPreviewRuntimePlan:
    return runtime_plan.model_copy(
        update={
            "status": "blocked",
            "blockers": (*runtime_plan.blockers, blocker),
            "planned_actions": (),
            "rollback_actions": (),
            "summary": "Odoo preview runtime plan is blocked",
        }
    )


def _with_dry_run_blocker(
    *,
    dry_run_plan: OdooPreviewDokployDryRunPlan,
    blocker: OdooPreviewDokployDryRunBlocker,
) -> OdooPreviewDokployDryRunPlan:
    return dry_run_plan.model_copy(
        update={
            "status": "blocked",
            "blockers": (
                *(existing for existing in dry_run_plan.blockers if existing.code != blocker.code),
                blocker,
            ),
            "operations": (),
            "rollback_operations": (),
            "summary": "Odoo preview Dokploy dry-run plan is blocked",
        }
    )


def _iter_dokploy_composes(raw_projects: object) -> tuple[JsonObject, ...]:
    if not isinstance(raw_projects, list):
        raise click.ClickException(
            "Dokploy project inventory returned an invalid response payload."
        )
    composes: list[JsonObject] = []
    for raw_project in raw_projects:
        project = control_plane_dokploy.as_json_object(raw_project)
        if project is None:
            continue
        raw_environments = project.get("environments")
        if not isinstance(raw_environments, list):
            continue
        for raw_environment in raw_environments:
            environment = control_plane_dokploy.as_json_object(raw_environment)
            if environment is None:
                continue
            raw_composes = environment.get("composes")
            if not isinstance(raw_composes, list):
                continue
            for raw_compose in raw_composes:
                compose = control_plane_dokploy.as_json_object(raw_compose)
                if compose is not None:
                    composes.append(compose)
    return tuple(composes)


def _iter_dokploy_search_composes(raw_search_matches: object) -> tuple[JsonObject, ...]:
    if isinstance(raw_search_matches, list):
        search_items = raw_search_matches
    elif isinstance(raw_search_matches, dict):
        search_items = []
        for key in ("composes", "items", "results", "data"):
            value = raw_search_matches.get(key)
            if isinstance(value, list):
                search_items.extend(value)
    else:
        raise click.ClickException(
            "Dokploy compose search returned an invalid response payload."
        )

    composes: list[JsonObject] = []
    for raw_compose in search_items:
        compose = control_plane_dokploy.as_json_object(raw_compose)
        if compose is not None:
            composes.append(compose)
    return tuple(composes)


def _compose_has_domain(*, host: str, token: str, compose_id: str, domain_host: str) -> bool:
    for domain in _compose_domains(host=host, token=token, compose_id=compose_id):
        if str(domain.get("host") or "").strip().lower() == domain_host:
            return True
    return False


def _blocked_inputs_message(
    runtime_plan: OdooPreviewRuntimePlan, dry_run_plan: OdooPreviewDokployDryRunPlan
) -> str:
    messages = [
        *[blocker.message for blocker in runtime_plan.blockers],
        *[blocker.message for blocker in dry_run_plan.blockers],
    ]
    return "; ".join(messages) or "Odoo preview apply inputs are blocked."


def _blocked_apply_inputs_result(
    *,
    profile: LaunchplaneProductProfileRecord,
    request: OdooPreviewApplyInputsRequest,
    preview_slug: str,
    preview_url: str,
    runtime_plan: OdooPreviewRuntimePlan | None,
    dry_run_plan: OdooPreviewDokployDryRunPlan | None,
    message: str,
) -> OdooPreviewApplyInputsResult:
    resolved_runtime_plan = runtime_plan or OdooPreviewRuntimePlan(
        status="blocked",
        operation=request.operation,
        product=profile.product,
        repository=profile.repository,
        pr_number=request.pr_number,
        preview_slug=preview_slug,
        preview_url=preview_url,
        strategy="unknown",
        blockers=(
            OdooPreviewRuntimeBlocker(code="runtime_strategy_not_isolated", message=message),
        ),
        summary="Odoo preview runtime plan is blocked",
    )
    resolved_dry_run_plan = dry_run_plan or OdooPreviewDokployDryRunPlan(
        status="blocked",
        operation=request.operation,
        product=profile.product,
        repository=profile.repository,
        preview_slug=preview_slug,
        preview_url=preview_url,
        compose_ref=f"{profile.product}-{preview_slug}",
        compose_name=preview_application_name(
            app_name_prefix=effective_preview_app_name_prefix(profile=profile),
            preview_slug=preview_slug,
        ),
        blockers=(OdooPreviewDokployDryRunBlocker(code="runtime_plan_not_ready", message=message),),
        summary="Odoo preview Dokploy dry-run plan is blocked",
    )
    return OdooPreviewApplyInputsResult(
        status="blocked",
        product=profile.product,
        context=profile.preview.context,
        template_instance=profile.preview.template_instance,
        operation=request.operation,
        preview_slug=preview_slug,
        preview_url=preview_url,
        repository=profile.repository,
        runtime_plan=resolved_runtime_plan,
        dry_run_plan=resolved_dry_run_plan,
        source=request.source,
        error_message=message,
    )


class OdooPreviewDokployEndpointSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    compose_create_path: str = "/api/compose.create"
    compose_update_path: str = "/api/compose.update"
    compose_deploy_path: str = "/api/compose.deploy"
    compose_redeploy_path: str = "/api/compose.redeploy"
    compose_delete_path: str = "/api/compose.delete"
    domain_by_compose_path: str = "/api/domain.byComposeId"
    domain_create_path: str = "/api/domain.create"
    domain_update_path: str = "/api/domain.update"
    domain_delete_path: str = "/api/domain.delete"

    @model_validator(mode="after")
    def _normalize_paths(self) -> "OdooPreviewDokployEndpointSpec":
        for field_name in type(self).model_fields:
            raw_value = getattr(self, field_name)
            value = raw_value.strip()
            if value and not value.startswith("/api/"):
                raise ValueError(f"Dokploy endpoint path must start with /api/: {field_name}")
            setattr(self, field_name, value)
        return self


class OdooPreviewDokployDryRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runtime_plan: OdooPreviewRuntimePlan
    endpoint_spec: OdooPreviewDokployEndpointSpec = Field(
        default_factory=OdooPreviewDokployEndpointSpec
    )
    no_cache: bool = False
    delete_volumes: bool = True
    runtime_port: int = Field(default=8069, ge=1)
    compose_name: str = ""
    environment_id: str = ""
    template_compose_id: str = ""

    @model_validator(mode="after")
    def _normalize_request(self) -> "OdooPreviewDokployDryRunRequest":
        self.compose_name = self.compose_name.strip()
        self.environment_id = self.environment_id.strip()
        self.template_compose_id = self.template_compose_id.strip()
        return self


class OdooPreviewDokployDryRunBlocker(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: OdooPreviewDokployDryRunBlockerCode
    message: str

    @model_validator(mode="after")
    def _normalize_blocker(self) -> "OdooPreviewDokployDryRunBlocker":
        self.message = _required_text(self.message, "Odoo preview dry-run blocker requires message")
        return self


class OdooPreviewDokployOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: OdooPreviewDokployOperationName
    method: Literal["GET", "POST", "LOCAL"]
    path: str = ""
    alternate_paths: tuple[str, ...] = ()
    target: str
    payload_keys: tuple[str, ...] = ()
    secret_payload: bool = False

    @model_validator(mode="after")
    def _normalize_operation(self) -> "OdooPreviewDokployOperation":
        self.path = self.path.strip()
        if self.method != "LOCAL" and not self.path:
            raise ValueError("Dokploy API operation requires path")
        self.alternate_paths = _normalized_optional_paths(self.alternate_paths)
        self.target = _required_text(self.target, "Dokploy operation requires target")
        self.payload_keys = _normalized_unique_texts(self.payload_keys)
        return self


class OdooPreviewDokployDryRunPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: OdooPreviewDokployDryRunStatus
    dry_run: Literal[True] = True
    operation: OdooPreviewRuntimeOperation
    product: str
    repository: str
    preview_slug: str
    preview_url: str
    domain_host: str = ""
    compose_ref: str
    compose_name: str
    environment_id: str = ""
    template_compose_id: str = ""
    no_cache: bool = False
    delete_volumes: bool = True
    runtime_port: int = Field(default=8069, ge=1)
    blockers: tuple[OdooPreviewDokployDryRunBlocker, ...] = ()
    operations: tuple[OdooPreviewDokployOperation, ...] = ()
    rollback_operations: tuple[OdooPreviewDokployOperation, ...] = ()
    summary: str

    @model_validator(mode="after")
    def _normalize_plan(self) -> "OdooPreviewDokployDryRunPlan":
        self.product = _required_text(self.product, "Odoo preview dry-run plan requires product")
        self.repository = _required_text(
            self.repository, "Odoo preview dry-run plan requires repository"
        )
        self.preview_slug = _required_text(
            self.preview_slug, "Odoo preview dry-run plan requires preview_slug"
        )
        self.preview_url = self.preview_url.strip()
        self.domain_host = self.domain_host.strip().lower()
        self.compose_ref = _required_text(
            self.compose_ref, "Odoo preview dry-run plan requires compose_ref"
        )
        self.compose_name = _required_text(
            self.compose_name, "Odoo preview dry-run plan requires compose_name"
        )
        self.environment_id = self.environment_id.strip()
        self.template_compose_id = self.template_compose_id.strip()
        self.blockers = tuple(sorted(self.blockers, key=lambda blocker: blocker.code))
        self.summary = _required_text(self.summary, "Odoo preview dry-run plan requires summary")
        if self.status == "ready" and self.blockers:
            raise ValueError("ready Odoo preview dry-run plan cannot include blockers")
        if self.status == "blocked" and not self.blockers:
            raise ValueError("blocked Odoo preview dry-run plan requires blockers")
        if self.status == "blocked" and (self.operations or self.rollback_operations):
            raise ValueError("blocked Odoo preview dry-run plan cannot include operations")
        return self


class OdooPreviewDokployApplyStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: OdooPreviewDokployOperationName
    target: str
    status: Literal["pass", "skipped"] = "pass"
    detail: str = ""

    @model_validator(mode="after")
    def _normalize_step(self) -> "OdooPreviewDokployApplyStep":
        self.target = _required_text(self.target, "Odoo preview apply step requires target")
        self.detail = self.detail.strip()
        return self


class OdooPreviewDokployApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dry_run_plan: OdooPreviewDokployDryRunPlan
    image_reference: str = ""
    manifest: ArtifactIdentityManifest | None = None
    environment_values: dict[str, str] = Field(default_factory=dict)
    compose_file: str = ""
    health_path: str = "/web/health"
    timeout_seconds: int = Field(default=300, ge=1)
    wait_for_deploy: bool = True
    smoke_check: bool = True

    @model_validator(mode="after")
    def _normalize_request(self) -> "OdooPreviewDokployApplyRequest":
        self.image_reference = self.image_reference.strip()
        if (
            self.manifest is not None
            or self.image_reference
            or self.dry_run_plan.operation == "refresh"
        ):
            self.image_reference = _resolve_manifest_image_reference(
                image_reference=self.image_reference,
                manifest=self.manifest,
                label="Odoo preview apply",
            )
        self.compose_file = self.compose_file.strip()
        self.health_path = self.health_path.strip() or "/web/health"
        if not self.health_path.startswith("/"):
            self.health_path = f"/{self.health_path}"
        self.environment_values = {
            key.strip(): value for key, value in self.environment_values.items() if key.strip()
        }
        return self


class OdooPreviewDokployApplyResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: OdooPreviewDokployApplyStatus
    operation: OdooPreviewRuntimeOperation
    product: str
    repository: str
    preview_slug: str
    preview_url: str
    domain_host: str
    compose_id: str = ""
    compose_name: str
    created_compose: bool = False
    domain_id: str = ""
    steps: tuple[OdooPreviewDokployApplyStep, ...] = ()
    rollback_errors: tuple[str, ...] = ()
    error_message: str = ""

    @model_validator(mode="after")
    def _normalize_result(self) -> "OdooPreviewDokployApplyResult":
        self.error_message = self.error_message.strip()
        self.rollback_errors = tuple(
            error.strip() for error in self.rollback_errors if error.strip()
        )
        if self.status in {"blocked", "fail"} and not self.error_message:
            raise ValueError("blocked or failed Odoo preview apply result requires error_message")
        return self


def build_odoo_preview_dokploy_dry_run(
    *, request: OdooPreviewDokployDryRunRequest
) -> OdooPreviewDokployDryRunPlan:
    runtime_plan = request.runtime_plan
    compose_ref = _compose_ref(runtime_plan=runtime_plan)
    compose_name = _compose_name(runtime_plan=runtime_plan, requested_name=request.compose_name)
    blockers: list[OdooPreviewDokployDryRunBlocker] = []

    if runtime_plan.status != "ready":
        blockers.append(
            _blocker(
                "runtime_plan_not_ready",
                "Odoo preview Dokploy dry-run requires a ready runtime plan.",
            )
        )
    domain_host = _domain_host(runtime_plan.preview_url)
    if not domain_host:
        blockers.append(
            _blocker(
                "preview_url_invalid",
                "Odoo preview Dokploy dry-run requires a preview URL with a hostname.",
            )
        )
    if runtime_plan.operation == "refresh" and runtime_plan.target is None:
        if not request.environment_id:
            blockers.append(
                _blocker(
                    "environment_id_missing",
                    "Odoo preview Dokploy dry-run requires environment_id before creating an isolated compose.",
                )
            )
        if not request.template_compose_id:
            blockers.append(
                _blocker(
                    "template_compose_id_missing",
                    "Odoo preview Dokploy dry-run requires template_compose_id before creating an isolated compose.",
                )
            )

    missing_paths = _missing_endpoint_paths(request=request)
    if missing_paths:
        blockers.append(
            _blocker(
                "endpoint_path_missing",
                "Odoo preview Dokploy dry-run is missing explicit endpoint paths: "
                + ", ".join(missing_paths),
            )
        )

    status: OdooPreviewDokployDryRunStatus = "blocked" if blockers else "ready"
    operations: tuple[OdooPreviewDokployOperation, ...] = ()
    rollback_operations: tuple[OdooPreviewDokployOperation, ...] = ()
    if status == "ready":
        operations = _operations(request=request, compose_ref=compose_ref, domain_host=domain_host)
        rollback_operations = _rollback_operations(
            request=request,
            compose_ref=compose_ref,
            domain_host=domain_host,
        )

    return OdooPreviewDokployDryRunPlan(
        status=status,
        operation=runtime_plan.operation,
        product=runtime_plan.product,
        repository=runtime_plan.repository,
        preview_slug=runtime_plan.preview_slug,
        preview_url=runtime_plan.preview_url,
        domain_host=domain_host,
        compose_ref=compose_ref,
        compose_name=compose_name,
        environment_id=request.environment_id,
        template_compose_id=request.template_compose_id,
        no_cache=request.no_cache,
        delete_volumes=request.delete_volumes,
        runtime_port=request.runtime_port,
        blockers=tuple(blockers),
        operations=operations,
        rollback_operations=rollback_operations,
        summary=(
            "Odoo preview Dokploy dry-run plan is ready"
            if status == "ready"
            else "Odoo preview Dokploy dry-run plan is blocked"
        ),
    )


def execute_odoo_preview_dokploy_apply(
    *,
    control_plane_root: Path,
    request: OdooPreviewDokployApplyRequest,
    database_url: str | None = None,
) -> OdooPreviewDokployApplyResult:
    plan = request.dry_run_plan
    if plan.status != "ready":
        return _apply_result(
            request=request,
            status="blocked",
            error_message="Odoo preview Dokploy apply requires a ready dry-run plan.",
        )

    missing_env_keys = tuple(
        key
        for key in ODOO_PREVIEW_REQUIRED_ENV_KEYS
        if not request.environment_values.get(key, "").strip()
    )
    if plan.operation != "destroy" and missing_env_keys:
        return _apply_result(
            request=request,
            status="blocked",
            error_message="Odoo preview Dokploy apply is missing runtime env keys: "
            + ", ".join(missing_env_keys),
        )

    host, token = control_plane_dokploy.read_dokploy_config(
        control_plane_root=control_plane_root,
        database_url=database_url,
    )
    if plan.operation == "destroy":
        return _execute_destroy(host=host, token=token, request=request)
    return _execute_refresh(host=host, token=token, request=request)


def _missing_endpoint_paths(*, request: OdooPreviewDokployDryRunRequest) -> tuple[str, ...]:
    spec = request.endpoint_spec
    runtime_plan = request.runtime_plan
    missing: list[str] = []
    if runtime_plan.operation == "refresh":
        if runtime_plan.target is None and not spec.compose_create_path:
            missing.append("compose_create_path")
        if not spec.compose_update_path:
            missing.append("compose_update_path")
        if not spec.domain_by_compose_path:
            missing.append("domain_by_compose_path")
        if not spec.domain_create_path:
            missing.append("domain_create_path")
        if not spec.domain_update_path:
            missing.append("domain_update_path")
        deploy_path = spec.compose_redeploy_path if request.no_cache else spec.compose_deploy_path
        if not deploy_path:
            missing.append("compose_redeploy_path" if request.no_cache else "compose_deploy_path")
        if runtime_plan.target is None and not spec.domain_delete_path:
            missing.append("domain_delete_path")
        if runtime_plan.target is None and not spec.compose_delete_path:
            missing.append("compose_delete_path")
    else:
        if not spec.domain_by_compose_path:
            missing.append("domain_by_compose_path")
        if not spec.domain_delete_path:
            missing.append("domain_delete_path")
        if not spec.compose_delete_path:
            missing.append("compose_delete_path")
    return tuple(missing)


def _execute_refresh(
    *, host: str, token: str, request: OdooPreviewDokployApplyRequest
) -> OdooPreviewDokployApplyResult:
    plan = request.dry_run_plan
    creating_compose = plan.compose_ref.startswith("${created.composeId:")
    created_compose_id = ""
    resolved_compose_id = ""
    domain_id = ""
    steps: list[OdooPreviewDokployApplyStep] = []
    try:
        if creating_compose and not plan.template_compose_id:
            raise click.ClickException("Odoo preview compose create requires template_compose_id.")
        source_compose_id = plan.template_compose_id if creating_compose else plan.compose_ref
        target_payload = control_plane_dokploy.fetch_dokploy_target_payload(
            host=host,
            token=token,
            target_type="compose",
            target_id=source_compose_id,
        )
        compose_id = _resolve_or_create_compose(
            host=host,
            token=token,
            plan=plan,
            template_payload=target_payload,
            steps=steps,
        )
        resolved_compose_id = compose_id
        created_compose_id = compose_id if creating_compose else ""
        if creating_compose:
            target_payload = control_plane_dokploy.fetch_dokploy_target_payload(
                host=host,
                token=token,
                target_type="compose",
                target_id=compose_id,
            )
        compose_file = request.compose_file or control_plane_dokploy.render_odoo_raw_compose_file(
            image_reference=request.image_reference,
            domain_hosts=(plan.domain_host,),
            runtime_port=plan.runtime_port,
            publish_host_ports=False,
        )
        control_plane_dokploy.sync_dokploy_compose_raw_source(
            host=host,
            token=token,
            compose_id=compose_id,
            compose_name=plan.compose_name,
            target_payload=target_payload,
            compose_file=compose_file,
        )
        steps.append(_step("compose_update_raw_source", compose_id))

        env_text = control_plane_dokploy.serialize_dokploy_env_text(request.environment_values)
        control_plane_dokploy.update_dokploy_target_env(
            host=host,
            token=token,
            target_type="compose",
            target_id=compose_id,
            target_payload=target_payload,
            env_text=env_text,
        )
        steps.append(_step("compose_update_env", compose_id))

        domain_id = control_plane_dokploy.ensure_compose_web_domain_route(
            host=host,
            token=token,
            compose_id=compose_id,
            domain_host=plan.domain_host,
            runtime_port=plan.runtime_port,
        )
        steps.append(_step("domain_create_or_update", plan.domain_host))

        latest_before = control_plane_dokploy.latest_deployment_for_target(
            host=host,
            token=token,
            target_type="compose",
            target_id=compose_id,
        )
        control_plane_dokploy.trigger_deployment(
            host=host,
            token=token,
            target_type="compose",
            target_id=compose_id,
            no_cache=plan.no_cache,
        )
        steps.append(_step("compose_deploy", compose_id))
        if request.wait_for_deploy:
            control_plane_dokploy.wait_for_target_deployment(
                host=host,
                token=token,
                target_type="compose",
                target_id=compose_id,
                before_key=control_plane_dokploy.deployment_key(latest_before),
                timeout_seconds=request.timeout_seconds,
            )
        if request.smoke_check:
            _wait_for_smoke_check(
                preview_url=plan.preview_url,
                health_path=request.health_path,
                timeout_seconds=request.timeout_seconds,
            )
            steps.append(_step("smoke_check", plan.preview_url))
        return _apply_result(
            request=request,
            status="pass",
            compose_id=compose_id,
            domain_id=domain_id,
            created_compose=bool(created_compose_id),
            steps=tuple(steps),
        )
    except click.ClickException as exc:
        rollback_errors = _rollback_created_runtime(
            host=host,
            token=token,
            domain_id=domain_id if created_compose_id else "",
            compose_id=created_compose_id,
            delete_volumes=plan.delete_volumes,
        )
        return _apply_result(
            request=request,
            status="fail",
            error_message=str(exc),
            domain_id=domain_id,
            compose_id=resolved_compose_id,
            created_compose=bool(created_compose_id),
            steps=tuple(steps),
            rollback_errors=rollback_errors,
        )


def _execute_destroy(
    *, host: str, token: str, request: OdooPreviewDokployApplyRequest
) -> OdooPreviewDokployApplyResult:
    plan = request.dry_run_plan
    compose_id = plan.compose_ref
    destroy_result = destroy_dokploy_preview_resource(
        host=host,
        token=token,
        resource_type="compose",
        resource_id=compose_id,
        domain_host=plan.domain_host,
        delete_volumes=plan.delete_volumes,
        continue_after_domain_cleanup_error=False,
        missing_resource_is_clean=True,
    )
    steps = tuple(
        _step(cast("OdooPreviewDokployOperationName", step.name), step.target)
        for step in destroy_result.steps
    )
    domain_id = destroy_result.domain_ids[0] if destroy_result.domain_ids else ""
    if destroy_result.status == "pass":
        return _apply_result(
            request=request,
            status="pass",
            compose_id=compose_id,
            domain_id=domain_id,
            steps=steps,
        )
    return _apply_result(
        request=request,
        status="fail",
        error_message="; ".join(destroy_result.cleanup_errors),
        compose_id=compose_id,
        domain_id=domain_id,
        steps=steps,
    )


def _resolve_or_create_compose(
    *,
    host: str,
    token: str,
    plan: OdooPreviewDokployDryRunPlan,
    template_payload: JsonObject,
    steps: list[OdooPreviewDokployApplyStep],
) -> str:
    if not plan.compose_ref.startswith("${created.composeId:"):
        return plan.compose_ref
    if not plan.environment_id:
        raise click.ClickException("Odoo preview compose create requires environment_id.")
    server_id = str(template_payload.get("serverId") or "").strip()
    if not server_id:
        raise click.ClickException(
            "Odoo preview compose create requires the template compose serverId."
        )
    created = control_plane_dokploy.dokploy_request(
        host=host,
        token=token,
        path="/api/compose.create",
        method="POST",
        payload={
            "name": plan.compose_name,
            "appName": plan.compose_name,
            "description": f"Launchplane Odoo preview {plan.preview_slug}",
            "environmentId": plan.environment_id,
            "serverId": server_id,
            "composeType": "docker-compose",
        },
    )
    created_compose = control_plane_dokploy.as_json_object(created)
    compose_id = str((created_compose or {}).get("composeId") or "").strip()
    if not compose_id:
        raise click.ClickException(
            f"Dokploy did not return composeId for Odoo preview compose {plan.compose_name!r}."
        )
    steps.append(_step("compose_create", plan.compose_name))
    return compose_id


def _compose_domains(*, host: str, token: str, compose_id: str) -> tuple[JsonObject, ...]:
    raw_domains = control_plane_dokploy.dokploy_request(
        host=host,
        token=token,
        path="/api/domain.byComposeId",
        query={"composeId": compose_id},
    )
    if not isinstance(raw_domains, list):
        return ()
    domains: list[JsonObject] = []
    for raw_domain in raw_domains:
        domain = control_plane_dokploy.as_json_object(raw_domain)
        if domain is not None:
            domains.append(domain)
    return tuple(domains)


def _delete_domain(*, host: str, token: str, domain_id: str) -> None:
    control_plane_dokploy.dokploy_request(
        host=host,
        token=token,
        path="/api/domain.delete",
        method="POST",
        payload={"domainId": domain_id},
    )


def _delete_compose(*, host: str, token: str, compose_id: str, delete_volumes: bool) -> None:
    control_plane_dokploy.dokploy_request(
        host=host,
        token=token,
        path="/api/compose.delete",
        method="POST",
        payload={"composeId": compose_id, "deleteVolumes": delete_volumes},
    )


def _is_compose_delete_not_found(exc: click.ClickException) -> bool:
    message = str(exc).lower()
    return "/api/compose.delete" in message and "404" in message


def _rollback_created_runtime(
    *, host: str, token: str, domain_id: str, compose_id: str, delete_volumes: bool
) -> tuple[str, ...]:
    errors: list[str] = []
    if domain_id:
        try:
            _delete_domain(host=host, token=token, domain_id=domain_id)
        except click.ClickException as exc:
            errors.append(f"domain rollback failed: {exc}")
    if compose_id:
        try:
            _delete_compose(
                host=host,
                token=token,
                compose_id=compose_id,
                delete_volumes=delete_volumes,
            )
        except click.ClickException as exc:
            errors.append(f"compose rollback failed: {exc}")
    return tuple(errors)


def _wait_for_smoke_check(*, preview_url: str, health_path: str, timeout_seconds: int) -> None:
    parsed = urlparse(preview_url.rstrip("/"))
    smoke_url = parsed._replace(path=health_path, params="", query="", fragment="").geturl()
    request = Request(
        smoke_url,
        headers={"Accept": "application/json, text/plain, */*", "Cache-Control": "no-store"},
    )
    deadline = timeout_seconds
    last_http_status: int | None = None
    while deadline > 0:
        try:
            with urlopen(request, timeout=min(15, deadline)) as response:
                response.read()
            if 200 <= response.status < 400:
                return
        except HTTPError as exc:
            last_http_status = exc.code
        except (TimeoutError, URLError, ValueError):
            pass
        sleep_seconds = min(5, deadline)
        time.sleep(sleep_seconds)
        deadline -= sleep_seconds
    if last_http_status is not None:
        raise click.ClickException(f"Odoo preview smoke check returned HTTP {last_http_status}.")
    raise click.ClickException(f"Timed out waiting for Odoo preview smoke check {smoke_url}.")


def _step(name: OdooPreviewDokployOperationName, target: str) -> OdooPreviewDokployApplyStep:
    return OdooPreviewDokployApplyStep(name=name, target=target)


def _apply_result(
    *,
    request: OdooPreviewDokployApplyRequest,
    status: OdooPreviewDokployApplyStatus,
    compose_id: str = "",
    domain_id: str = "",
    created_compose: bool = False,
    steps: tuple[OdooPreviewDokployApplyStep, ...] = (),
    rollback_errors: tuple[str, ...] = (),
    error_message: str = "",
) -> OdooPreviewDokployApplyResult:
    plan = request.dry_run_plan
    return OdooPreviewDokployApplyResult(
        status=status,
        operation=plan.operation,
        product=plan.product,
        repository=plan.repository,
        preview_slug=plan.preview_slug,
        preview_url=plan.preview_url,
        domain_host=plan.domain_host,
        compose_id=compose_id,
        compose_name=plan.compose_name,
        created_compose=created_compose,
        domain_id=domain_id,
        steps=steps,
        rollback_errors=rollback_errors,
        error_message=error_message,
    )


def _operations(
    *, request: OdooPreviewDokployDryRunRequest, compose_ref: str, domain_host: str
) -> tuple[OdooPreviewDokployOperation, ...]:
    runtime_plan = request.runtime_plan
    spec = request.endpoint_spec
    if runtime_plan.operation == "destroy":
        return (
            OdooPreviewDokployOperation(
                name="domain_lookup",
                method="GET",
                path=spec.domain_by_compose_path,
                target=compose_ref,
                payload_keys=("composeId",),
            ),
            OdooPreviewDokployOperation(
                name="domain_delete",
                method="POST",
                path=spec.domain_delete_path,
                target=domain_host,
                payload_keys=("domainId",),
            ),
            OdooPreviewDokployOperation(
                name="compose_delete",
                method="POST",
                path=spec.compose_delete_path,
                target=compose_ref,
                payload_keys=("composeId", "deleteVolumes"),
            ),
        )

    operations: list[OdooPreviewDokployOperation] = []
    if runtime_plan.target is None:
        operations.append(
            OdooPreviewDokployOperation(
                name="compose_create",
                method="POST",
                path=spec.compose_create_path,
                target=_compose_name(
                    runtime_plan=runtime_plan, requested_name=request.compose_name
                ),
                payload_keys=("name", "appName", "environmentId", "serverId", "composeType"),
            )
        )
    operations.extend(
        (
            OdooPreviewDokployOperation(
                name="compose_update_raw_source",
                method="POST",
                path=spec.compose_update_path,
                target=compose_ref,
                payload_keys=(
                    "composeId",
                    "name",
                    "environmentId",
                    "sourceType",
                    "composePath",
                    "composeFile",
                ),
            ),
            OdooPreviewDokployOperation(
                name="compose_update_env",
                method="POST",
                path=spec.compose_update_path,
                target=compose_ref,
                payload_keys=("composeId", "env"),
                secret_payload=True,
            ),
            OdooPreviewDokployOperation(
                name="domain_lookup",
                method="GET",
                path=spec.domain_by_compose_path,
                target=compose_ref,
                payload_keys=("composeId",),
            ),
            OdooPreviewDokployOperation(
                name="domain_create_or_update",
                method="POST",
                path=spec.domain_create_path,
                alternate_paths=(spec.domain_update_path,),
                target=domain_host,
                payload_keys=("host", "port", "composeId", "serviceName", "domainType"),
            ),
            OdooPreviewDokployOperation(
                name="compose_deploy",
                method="POST",
                path=spec.compose_redeploy_path if request.no_cache else spec.compose_deploy_path,
                target=compose_ref,
                payload_keys=("composeId",),
            ),
            OdooPreviewDokployOperation(
                name="smoke_check",
                method="LOCAL",
                target=runtime_plan.preview_url,
            ),
        )
    )
    return tuple(operations)


def _rollback_operations(
    *, request: OdooPreviewDokployDryRunRequest, compose_ref: str, domain_host: str
) -> tuple[OdooPreviewDokployOperation, ...]:
    if request.runtime_plan.operation == "destroy" or request.runtime_plan.target is not None:
        return ()
    spec = request.endpoint_spec
    return (
        OdooPreviewDokployOperation(
            name="domain_delete",
            method="POST",
            path=spec.domain_delete_path,
            target=domain_host,
            payload_keys=("domainId",),
        ),
        OdooPreviewDokployOperation(
            name="compose_delete",
            method="POST",
            path=spec.compose_delete_path,
            target=compose_ref,
            payload_keys=("composeId", "deleteVolumes"),
        ),
    )


def _compose_ref(*, runtime_plan: OdooPreviewRuntimePlan) -> str:
    if runtime_plan.target is not None:
        return runtime_plan.target.target_id
    return f"${{created.composeId:{runtime_plan.product}-{runtime_plan.preview_slug}}}"


def _compose_name(*, runtime_plan: OdooPreviewRuntimePlan, requested_name: str) -> str:
    if requested_name.strip():
        return requested_name.strip()
    if runtime_plan.target is not None:
        return runtime_plan.target.target_name
    return f"{runtime_plan.product}-{runtime_plan.preview_slug}"


def _domain_host(preview_url: str) -> str:
    parsed = urlparse(preview_url.strip())
    return (parsed.hostname or "").strip().lower()


def _blocker(
    code: OdooPreviewDokployDryRunBlockerCode, message: str
) -> OdooPreviewDokployDryRunBlocker:
    return OdooPreviewDokployDryRunBlocker(code=code, message=message)


def _required_text(value: str, message: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(message)
    return normalized


def _artifact_image_reference(manifest: ArtifactIdentityManifest) -> str:
    return f"{manifest.image.repository}@{manifest.image.digest}"


def _resolve_manifest_image_reference(
    *,
    image_reference: str,
    manifest: ArtifactIdentityManifest | None,
    label: str,
) -> str:
    normalized_image_reference = image_reference.strip()
    if manifest is None:
        return _required_text(
            normalized_image_reference,
            f"{label} requires image_reference or manifest.",
        )
    manifest_image_reference = _artifact_image_reference(manifest)
    if normalized_image_reference and normalized_image_reference != manifest_image_reference:
        raise ValueError(
            f"{label} image_reference does not match manifest image reference. "
            f"Request={normalized_image_reference} manifest={manifest_image_reference}."
        )
    return manifest_image_reference


def _normalized_unique_texts(values: tuple[str, ...]) -> tuple[str, ...]:
    normalized: list[str] = []
    for raw_value in values:
        value = raw_value.strip()
        if not value:
            raise ValueError("Odoo preview Dokploy operation payload keys must be non-empty")
        if value not in normalized:
            normalized.append(value)
    return tuple(normalized)


def _normalized_optional_paths(values: tuple[str, ...]) -> tuple[str, ...]:
    normalized: list[str] = []
    for raw_value in values:
        value = raw_value.strip()
        if not value:
            continue
        if not value.startswith("/api/"):
            raise ValueError("Dokploy alternate operation paths must start with /api/")
        if value not in normalized:
            normalized.append(value)
    return tuple(normalized)
