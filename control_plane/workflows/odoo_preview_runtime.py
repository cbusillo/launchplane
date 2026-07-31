from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import click
from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane import runtime_environments as control_plane_runtime_environments
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
from control_plane.contracts.odoo_runtime_environment import (
    merge_required_odoo_addons_path,
)
from control_plane.contracts.odoo_stable_target_replacement import (
    LAUNCHPLANE_REQUIRED_ODOO_MODULES,
    merge_odoo_install_modules,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.runtime_identity import (
    RuntimeIdentity,
    health_payload_runtime_identity_status,
)
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
from control_plane.workflows.odoo_verification import DEFAULT_ODOO_RUNTIME_HEALTH_PATH
from control_plane.dokploy import api as dokploy_api
from control_plane.dokploy import source as dokploy_source
from control_plane.dokploy import compose as dokploy_compose
from control_plane.dokploy import post_deploy as dokploy_post_deploy
from control_plane.dokploy.api import JsonObject, JsonValue


OdooPreviewDokployDryRunStatus = OdooPreviewRuntimePlanStatus
OdooPreviewDomainCertificateType = Literal["none", "letsencrypt"]


_IMMUTABLE_IMAGE_REFERENCE_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._:/-]*@sha256:[A-Za-z0-9._-]+$"
)
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
    "module_install_update",
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
ODOO_PREVIEW_ARTIFACT_PUBLISH_INPUTS_ROUTE = "/v1/drivers/odoo/artifact-publish-inputs"
ODOO_PREVIEW_APPLY_INPUTS_ROUTE = "/v1/drivers/odoo/preview-apply-inputs"
ODOO_PREVIEW_APPLY_ROUTE = "/v1/drivers/odoo/preview-apply"
ODOO_PREVIEW_GENERATED_ENV_KEYS = (
    "ODOO_DB_NAME",
    "ODOO_DATA_VOLUME",
    "ODOO_LOG_VOLUME",
    "ODOO_DB_VOLUME",
)
ODOO_PREVIEW_MAX_APPLY_TIMEOUT_SECONDS = 600
ODOO_PREVIEW_DESTROY_SUPERSESSION_GRACE_SECONDS = ODOO_PREVIEW_MAX_APPLY_TIMEOUT_SECONDS + 300


class OdooPreviewTargetDiscoveryAmbiguousError(click.ClickException):
    pass


class OdooPreviewWorkflowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    route_path: str
    payload: dict[str, object]
    idempotency_key: str
    payload_json_files: dict[str, str] = Field(default_factory=dict)
    response_output_path: str = ""
    fail_result_paths: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_request(self) -> "OdooPreviewWorkflowRequest":
        self.route_path = _required_text(
            self.route_path, "Odoo preview workflow request requires route_path"
        )
        self.idempotency_key = _required_text(
            self.idempotency_key,
            "Odoo preview workflow request requires idempotency_key",
        )
        self.payload_json_files = {
            _required_text(path, "Odoo preview payload file binding requires path"): _required_text(
                file_path, "Odoo preview payload file binding requires file_path"
            )
            for path, file_path in self.payload_json_files.items()
        }
        self.response_output_path = self.response_output_path.strip()
        self.fail_result_paths = tuple(
            _required_text(path, "Odoo preview fail-result path cannot be blank")
            for path in self.fail_result_paths
        )
        return self


class OdooPreviewWorkflowRequestFacts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product: str
    context: str
    instance: str = "testing"
    operation: OdooPreviewRuntimeOperation = "refresh"
    pr_number: int = Field(ge=1)
    preview_slug: str = ""
    source_git_ref: str = ""
    run_id: str
    run_attempt: str
    source: str = ""

    @model_validator(mode="after")
    def _validate_facts(self) -> "OdooPreviewWorkflowRequestFacts":
        self.product = _required_text(self.product, "Odoo preview request facts require product")
        self.context = _required_text(self.context, "Odoo preview request facts require context")
        self.instance = _required_text(self.instance, "Odoo preview request facts require instance")
        self.preview_slug = self.preview_slug.strip()
        self.source_git_ref = self.source_git_ref.strip()
        if self.operation == "refresh":
            self.source_git_ref = _required_text(
                self.source_git_ref,
                "Odoo preview refresh request facts require source_git_ref",
            )
        self.run_id = _required_text(self.run_id, "Odoo preview request facts require run_id")
        self.run_attempt = _required_text(
            self.run_attempt, "Odoo preview request facts require run_attempt"
        )
        if not self.source.strip():
            self.source = f"{self.product}-preview-apply-inputs"
        else:
            self.source = self.source.strip()
        return self


def build_odoo_preview_artifact_publish_inputs_workflow_request(
    *, facts: OdooPreviewWorkflowRequestFacts
) -> OdooPreviewWorkflowRequest:
    if facts.operation != "refresh":
        raise ValueError("Odoo preview artifact publish inputs request requires refresh operation.")
    source_git_ref = _required_text(
        facts.source_git_ref,
        "Odoo preview artifact publish inputs request requires source_git_ref",
    )
    return OdooPreviewWorkflowRequest(
        route_path=ODOO_PREVIEW_ARTIFACT_PUBLISH_INPUTS_ROUTE,
        payload={
            "schema_version": 1,
            "product": facts.product,
            "inputs": {
                "schema_version": 1,
                "context": facts.context,
                "instance": facts.instance,
                "pr_number": facts.pr_number,
                "isolated": True,
                "source_git_ref": source_git_ref,
            },
        },
        idempotency_key=(
            "odoo-artifact-publish-inputs:"
            f"{facts.product}:{facts.context}:{facts.instance}:"
            f"preview-{facts.pr_number}-run-{facts.run_id}-attempt-{facts.run_attempt}"
        ),
        fail_result_paths=("result.input_status",),
        response_output_path="result",
    )


def build_odoo_preview_apply_inputs_workflow_request(
    *, facts: OdooPreviewWorkflowRequestFacts, manifest_file: str = ""
) -> OdooPreviewWorkflowRequest:
    manifest_file = manifest_file.strip()
    if facts.operation == "refresh" and not manifest_file:
        raise ValueError("Odoo preview refresh apply-inputs request requires manifest_file.")
    return OdooPreviewWorkflowRequest(
        route_path=ODOO_PREVIEW_APPLY_INPUTS_ROUTE,
        payload={
            "schema_version": 1,
            "product": facts.product,
            "inputs": {
                "schema_version": 1,
                "product": facts.product,
                "operation": facts.operation,
                "pr_number": facts.pr_number,
                "source_git_ref": facts.source_git_ref,
                "source": facts.source,
                "timeout_seconds": 300,
                "no_cache": False,
            },
        },
        payload_json_files={"inputs.manifest": manifest_file} if manifest_file else {},
        idempotency_key=(
            "odoo-preview-apply-inputs:"
            f"{facts.product}:{_preview_request_token(facts)}:{_preview_source_token(facts)}:"
            f"inputs-run-{facts.run_id}-attempt-{facts.run_attempt}"
        ),
        fail_result_paths=("result.status",),
        response_output_path="result.dry_run_plan",
    )


def build_odoo_preview_apply_workflow_request(
    *,
    facts: OdooPreviewWorkflowRequestFacts,
    dry_run_plan_file: str,
    plan_id: str,
    manifest_file: str = "",
    wait_for_deploy: bool = True,
    smoke_check: bool | None = None,
) -> OdooPreviewWorkflowRequest:
    dry_run_plan_file = _required_text(
        dry_run_plan_file,
        "Odoo preview apply request requires dry_run_plan_file.",
    )
    plan_id = _required_text(plan_id, "Odoo preview apply request requires plan_id.")
    if facts.operation == "refresh" and not manifest_file.strip():
        raise ValueError("Odoo preview refresh apply request requires manifest_file.")
    payload_json_files = {"apply.dry_run_plan": dry_run_plan_file}
    if manifest_file.strip():
        payload_json_files["apply.manifest"] = manifest_file.strip()
    apply_payload: dict[str, object] = {
        "timeout_seconds": 600,
        "wait_for_deploy": wait_for_deploy,
    }
    if smoke_check is not None:
        apply_payload["smoke_check"] = smoke_check
    return OdooPreviewWorkflowRequest(
        route_path=ODOO_PREVIEW_APPLY_ROUTE,
        payload={
            "schema_version": 1,
            "product": facts.product,
            "apply": apply_payload,
        },
        payload_json_files=payload_json_files,
        idempotency_key=plan_id,
        fail_result_paths=("result.status",),
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


class OdooPreviewApplyPlanProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    plan_id: str
    plan_sha256: str
    issued_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def _normalize_provenance(self) -> "OdooPreviewApplyPlanProvenance":
        self.plan_id = _required_text(self.plan_id, "Odoo preview plan provenance requires plan_id")
        self.plan_sha256 = self.plan_sha256.strip().lower()
        if len(self.plan_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.plan_sha256
        ):
            raise ValueError("Odoo preview plan provenance requires a SHA-256 fingerprint")
        if self.issued_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("Odoo preview plan provenance timestamps require UTC offsets")
        self.issued_at = self.issued_at.astimezone(timezone.utc)
        self.expires_at = self.expires_at.astimezone(timezone.utc)
        if self.expires_at <= self.issued_at:
            raise ValueError("Odoo preview plan provenance must expire after issuance")
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
    plan_request: OdooPreviewApplyInputsRequest
    runtime_plan: OdooPreviewRuntimePlan
    dry_run_plan: OdooPreviewDokployDryRunPlan
    plan_provenance: OdooPreviewApplyPlanProvenance | None = None
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
        if self.plan_request.product != self.product:
            raise ValueError("Odoo preview apply inputs result requires matching plan product")
        if self.plan_request.operation != self.operation:
            raise ValueError("Odoo preview apply inputs result requires matching plan operation")
        if self.dry_run_plan.product != self.product:
            raise ValueError("Odoo preview apply inputs result requires matching dry-run product")
        if self.dry_run_plan.operation != self.operation:
            raise ValueError("Odoo preview apply inputs result requires matching dry-run operation")
        if self.status == "blocked" and self.plan_provenance is not None:
            raise ValueError("Blocked Odoo preview plans cannot include apply provenance")
        self.error_message = self.error_message.strip()
        return self


def odoo_preview_apply_plan_sha256(result: OdooPreviewApplyInputsResult) -> str:
    payload = result.model_dump(mode="json", exclude={"plan_provenance"})
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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
    block_discovery_error = False
    try:
        target = _discover_odoo_preview_target(
            control_plane_root=control_plane_root,
            context_name=preview_profile.context,
            preview_slug=preview_slug,
            preview_url=preview_url,
            compose_name=compose_name,
            database_url=database_url,
        )
    except OdooPreviewTargetDiscoveryAmbiguousError as exc:
        discovery_error = str(exc)
        block_discovery_error = True
    except click.ClickException as exc:
        discovery_error = str(exc)
        block_discovery_error = request.operation == "destroy"
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
    if discovery_error and block_discovery_error:
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
            domain_certificate_type=preview_profile.domain_certificate_type,
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
        plan_request=request.model_copy(deep=True),
        runtime_plan=runtime_plan,
        dry_run_plan=dry_run_plan,
        source=request.source,
        error_message=(
            "" if status == "ready" else _blocked_inputs_message(runtime_plan, dry_run_plan)
        ),
    )


def _needs_preview_environment_id(runtime_plan: OdooPreviewRuntimePlan) -> bool:
    return (
        runtime_plan.status == "ready"
        and runtime_plan.operation == "refresh"
        and runtime_plan.target is None
    )


def _preview_slug(
    *, profile: LaunchplaneProductProfileRecord, request: OdooPreviewApplyInputsRequest
) -> str:
    if request.preview_slug.strip():
        return request.preview_slug.strip()
    return profile.preview.slug_template.strip().replace("{number}", str(request.pr_number))


def _preview_request_token(facts: OdooPreviewWorkflowRequestFacts) -> str:
    return facts.preview_slug or f"pr-{facts.pr_number}"


def _preview_source_token(facts: OdooPreviewWorkflowRequestFacts) -> str:
    if facts.operation == "destroy":
        return "destroy"
    return facts.source_git_ref


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
        for key in ODOO_PREVIEW_GENERATED_ENV_KEYS
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
) -> dokploy_source.DokployTargetDefinition | None:
    try:
        source_of_truth = dokploy_source.read_control_plane_dokploy_source_of_truth(
            control_plane_root=control_plane_root,
            allow_incomplete_target_ids=True,
            allowed_incomplete_target_routes=((context_name, instance_name),),
        )
    except click.ClickException:
        return None
    return dokploy_source.find_dokploy_target_definition(
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
    host, token = dokploy_source.read_dokploy_config(
        control_plane_root=control_plane_root,
        database_url=database_url,
    )
    raw_projects = dokploy_api.dokploy_request(
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
        raw_search_matches = dokploy_api.dokploy_request(
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
        raise OdooPreviewTargetDiscoveryAmbiguousError(
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
        project = dokploy_api.as_json_object(raw_project)
        if project is None:
            continue
        raw_environments = project.get("environments")
        if not isinstance(raw_environments, list):
            continue
        for raw_environment in raw_environments:
            environment = dokploy_api.as_json_object(raw_environment)
            if environment is None:
                continue
            raw_composes = environment.get("composes")
            if not isinstance(raw_composes, list):
                continue
            for raw_compose in raw_composes:
                compose = dokploy_api.as_json_object(raw_compose)
                if compose is not None:
                    composes.append(compose)
    return tuple(composes)


def _iter_dokploy_search_composes(raw_search_matches: object) -> tuple[JsonObject, ...]:
    search_items: list[JsonValue]
    if isinstance(raw_search_matches, list):
        search_items = cast(list[JsonValue], raw_search_matches)
    elif isinstance(raw_search_matches, dict):
        search_items = []
        for key in ("composes", "items", "results", "data"):
            value = raw_search_matches.get(key)
            if isinstance(value, list) and value:
                json_values = cast(list[JsonValue], value)
                if search_items and json_values != search_items:
                    raise OdooPreviewTargetDiscoveryAmbiguousError(
                        "Dokploy compose search returned ambiguous result envelopes."
                    )
                search_items = json_values
    else:
        raise click.ClickException("Dokploy compose search returned an invalid response payload.")

    composes: list[JsonObject] = []
    for raw_compose in search_items:
        compose = dokploy_api.as_json_object(raw_compose)
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
    domain_certificate_type: OdooPreviewDomainCertificateType = "none"
    smoke_check: bool = True
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
    domain_certificate_type: OdooPreviewDomainCertificateType = "none"
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
    health_path: str = DEFAULT_ODOO_RUNTIME_HEALTH_PATH
    timeout_seconds: int = Field(
        default=300,
        ge=1,
        le=ODOO_PREVIEW_MAX_APPLY_TIMEOUT_SECONDS,
    )
    wait_for_deploy: bool = True
    smoke_check: bool | None = None

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
        self.health_path = self.health_path.strip() or DEFAULT_ODOO_RUNTIME_HEALTH_PATH
        if not self.health_path.startswith("/"):
            self.health_path = f"/{self.health_path}"
        self.environment_values = {
            key.strip(): value for key, value in self.environment_values.items() if key.strip()
        }
        if self.smoke_check is None:
            self.smoke_check = any(
                operation.name == "smoke_check" for operation in self.dry_run_plan.operations
            )
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
    provider_effect_attempted: bool = False
    domain_id: str = ""
    module_install_update_status: Literal["pass", "fail", "skipped"] = "skipped"
    module_install_update_evidence: dict[str, str] = Field(default_factory=dict)
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


class OdooPreviewDokployObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: Literal["present", "absent", "unknown"]
    result: OdooPreviewDokployApplyResult | None = None
    retry_safe: bool = False


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
    if _needs_preview_environment_id(runtime_plan):
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
        domain_certificate_type=request.domain_certificate_type,
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
    provider_operation_title: str = "",
    provider_effect_checkpoint: Callable[[str], None] | None = None,
    provider_lease_check: Callable[[], None] | None = None,
    expected_runtime_identity: RuntimeIdentity | None = None,
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

    host, token = dokploy_source.read_dokploy_config(
        control_plane_root=control_plane_root,
        database_url=database_url,
    )
    if plan.operation == "destroy":
        return _execute_destroy(
            host=host,
            token=token,
            request=request,
            provider_effect_checkpoint=provider_effect_checkpoint,
        )
    return _execute_refresh(
        host=host,
        token=token,
        request=request,
        provider_operation_title=provider_operation_title,
        provider_effect_checkpoint=provider_effect_checkpoint,
        provider_lease_check=provider_lease_check,
        expected_runtime_identity=expected_runtime_identity,
    )


def observe_odoo_preview_dokploy_apply(
    *,
    control_plane_root: Path,
    context_name: str,
    request: OdooPreviewDokployApplyRequest,
    database_url: str | None,
    provider_operation_title: str,
    provider_effect_phase: str = "",
) -> OdooPreviewDokployObservation:
    plan = request.dry_run_plan
    try:
        host, token = dokploy_source.read_dokploy_config(
            control_plane_root=control_plane_root,
            database_url=database_url,
        )
        if plan.operation == "destroy":
            compose_still_exists = _compose_exists_by_id(
                host=host,
                token=token,
                compose_id=plan.compose_ref,
            )
            if compose_still_exists:
                retry_safe_phases = {"", "domain_delete"}
                return OdooPreviewDokployObservation(
                    outcome=("absent" if provider_effect_phase in retry_safe_phases else "unknown"),
                    retry_safe=provider_effect_phase in retry_safe_phases,
                )
            return OdooPreviewDokployObservation(
                outcome="present",
                result=_apply_result(request=request, status="pass", compose_id=plan.compose_ref),
            )
        target = _discover_odoo_preview_target(
            control_plane_root=control_plane_root,
            context_name=context_name,
            preview_slug=plan.preview_slug,
            preview_url=plan.preview_url,
            compose_name=plan.compose_name,
            database_url=database_url,
        )
        if target is None:
            retry_safe_phases = {"", "rollback_complete"}
            return OdooPreviewDokployObservation(
                outcome=("absent" if provider_effect_phase in retry_safe_phases else "unknown"),
                retry_safe=provider_effect_phase in retry_safe_phases,
            )
        deployment = dokploy_api.deployment_for_target_by_title(
            host=host,
            token=token,
            target_type="compose",
            target_id=target.target_id,
            title=provider_operation_title,
        )
        status = dokploy_api.deployment_status(deployment)
        if deployment is None:
            retry_safe_phases = {"", "compose_create", "compose_update", "domain_ensure"}
            return OdooPreviewDokployObservation(
                outcome=("absent" if provider_effect_phase in retry_safe_phases else "unknown"),
                retry_safe=provider_effect_phase in retry_safe_phases,
            )
        if status in {"", "pending", "queued", "running"}:
            return OdooPreviewDokployObservation(outcome="unknown")
        if status in {"success", "succeeded", "done", "completed", "healthy", "finished"}:
            return OdooPreviewDokployObservation(outcome="unknown")
        if status in {
            "failed",
            "error",
            "canceled",
            "cancelled",
            "killed",
            "unhealthy",
            "timeout",
        }:
            error_message = str(
                (deployment or {}).get("errorMessage")
                or (deployment or {}).get("error_message")
                or f"Dokploy deployment finished with status {status}."
            ).strip()
            return OdooPreviewDokployObservation(
                outcome="present",
                result=_apply_result(
                    request=request,
                    status="fail",
                    compose_id=target.target_id,
                    error_message=error_message,
                ),
            )
    except click.ClickException:
        return OdooPreviewDokployObservation(outcome="unknown")
    return OdooPreviewDokployObservation(outcome="unknown")


def odoo_preview_destroy_target_is_quiescent(
    *,
    control_plane_root: Path,
    request: OdooPreviewDokployApplyRequest,
    database_url: str | None,
) -> bool:
    plan = request.dry_run_plan
    if plan.operation != "destroy" or not plan.compose_ref:
        return False
    try:
        host, token = dokploy_source.read_dokploy_config(
            control_plane_root=control_plane_root,
            database_url=database_url,
        )
        if not _compose_exists_by_id(
            host=host,
            token=token,
            compose_id=plan.compose_ref,
        ):
            return True
        if any(
            dokploy_api.deployment_status(deployment)
            in dokploy_post_deploy.DOKPLOY_RUNNING_DEPLOYMENT_STATUSES
            for deployment in dokploy_api.list_deployments_for_target(
                host=host,
                token=token,
                target_type="compose",
                target_id=plan.compose_ref,
            )
        ):
            return False
        return dokploy_post_deploy.compose_data_workflow_is_quiescent(
            host=host,
            token=token,
            target_definition=dokploy_source.DokployTargetDefinition(
                context=plan.product,
                instance=plan.preview_slug,
                target_id=plan.compose_ref,
                target_name=plan.compose_name,
            ),
        )
    except click.ClickException:
        return False


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


def _preview_refresh_environment_values(
    *, request: OdooPreviewDokployApplyRequest
) -> dict[str, str]:
    environment_values = dict(request.environment_values)
    addons_path = merge_required_odoo_addons_path(environment_values.get("ODOO_ADDONS_PATH", ""))
    if addons_path:
        environment_values["ODOO_ADDONS_PATH"] = addons_path
    else:
        environment_values.pop("ODOO_ADDONS_PATH", None)
    manifest_modules = request.manifest.odoo_install_modules if request.manifest is not None else ()
    update_modules = merge_odoo_install_modules(
        LAUNCHPLANE_REQUIRED_ODOO_MODULES,
        manifest_modules,
        environment_values.get("ODOO_INSTALL_MODULES", ""),
    )
    environment_values["ODOO_INSTALL_MODULES"] = update_modules
    environment_values["ODOO_UPDATE_MODULES"] = update_modules
    return environment_values


def _execute_refresh(
    *,
    host: str,
    token: str,
    request: OdooPreviewDokployApplyRequest,
    provider_operation_title: str,
    provider_effect_checkpoint: Callable[[str], None] | None,
    provider_lease_check: Callable[[], None] | None,
    expected_runtime_identity: RuntimeIdentity | None,
) -> OdooPreviewDokployApplyResult:
    plan = request.dry_run_plan
    creating_compose = plan.compose_ref.startswith("${created.composeId:")
    created_compose_id = ""
    resolved_compose_id = ""
    domain_id = ""
    deploy_triggered = False
    provider_effect_attempted = False
    module_install_update_status: Literal["pass", "fail", "skipped"] = "skipped"
    module_install_update_evidence: dict[str, str] = {}
    steps: list[OdooPreviewDokployApplyStep] = []

    def checkpoint_provider_effect(phase: str) -> None:
        nonlocal provider_effect_attempted
        if provider_effect_checkpoint is not None:
            provider_effect_checkpoint(phase)
        provider_effect_attempted = True

    try:
        if creating_compose and not plan.template_compose_id:
            raise click.ClickException("Odoo preview compose create requires template_compose_id.")
        source_compose_id = plan.template_compose_id if creating_compose else plan.compose_ref
        target_payload = dokploy_api.fetch_dokploy_target_payload(
            host=host,
            token=token,
            target_type="compose",
            target_id=source_compose_id,
        )
        compose_id, created_compose = _resolve_or_create_compose(
            host=host,
            token=token,
            plan=plan,
            template_payload=target_payload,
            steps=steps,
            provider_effect_checkpoint=checkpoint_provider_effect,
        )
        resolved_compose_id = compose_id
        created_compose_id = compose_id if created_compose else ""
        if creating_compose:
            target_payload = dokploy_api.fetch_dokploy_target_payload(
                host=host,
                token=token,
                target_type="compose",
                target_id=compose_id,
            )
        compose_file = dokploy_compose.render_odoo_raw_compose_file(
            image_reference=request.image_reference,
            domain_hosts=(plan.domain_host,),
            runtime_port=plan.runtime_port,
            publish_host_ports=False,
            domain_certificate_type=plan.domain_certificate_type,
        )
        checkpoint_provider_effect("compose_update")
        dokploy_compose.sync_dokploy_compose_raw_source(
            host=host,
            token=token,
            compose_id=compose_id,
            compose_name=plan.compose_name,
            target_payload=target_payload,
            compose_file=compose_file,
        )
        steps.append(_step("compose_update_raw_source", compose_id))

        environment_values = _preview_refresh_environment_values(request=request)
        env_text = dokploy_api.serialize_dokploy_env_text(environment_values)
        checkpoint_provider_effect("compose_update")
        dokploy_api.update_dokploy_target_env(
            host=host,
            token=token,
            target_type="compose",
            target_id=compose_id,
            target_payload=target_payload,
            env_text=env_text,
        )
        steps.append(_step("compose_update_env", compose_id))

        checkpoint_provider_effect("domain_ensure")
        domain_id = dokploy_compose.ensure_compose_web_domain_route(
            host=host,
            token=token,
            compose_id=compose_id,
            domain_host=plan.domain_host,
            runtime_port=plan.runtime_port,
            certificate_type=plan.domain_certificate_type,
        )
        steps.append(_step("domain_create_or_update", plan.domain_host))

        latest_before = dokploy_api.latest_deployment_for_target(
            host=host,
            token=token,
            target_type="compose",
            target_id=compose_id,
        )
        checkpoint_provider_effect("deploy_trigger")
        deploy_triggered = True
        if provider_operation_title:
            dokploy_api.trigger_deployment(
                host=host,
                token=token,
                target_type="compose",
                target_id=compose_id,
                no_cache=plan.no_cache,
                title=provider_operation_title,
            )
        else:
            dokploy_api.trigger_deployment(
                host=host,
                token=token,
                target_type="compose",
                target_id=compose_id,
                no_cache=plan.no_cache,
            )
        steps.append(_step("compose_deploy", compose_id))
        if request.wait_for_deploy:
            if provider_operation_title:
                dokploy_api.wait_for_target_deployment(
                    host=host,
                    token=token,
                    target_type="compose",
                    target_id=compose_id,
                    before_key=dokploy_api.deployment_key(latest_before),
                    timeout_seconds=request.timeout_seconds,
                    deployment_title=provider_operation_title,
                )
            else:
                dokploy_api.wait_for_target_deployment(
                    host=host,
                    token=token,
                    target_type="compose",
                    target_id=compose_id,
                    before_key=dokploy_api.deployment_key(latest_before),
                    timeout_seconds=request.timeout_seconds,
                )
        module_install_update_status = "fail"
        module_install_update_evidence = dokploy_post_deploy.run_compose_post_deploy_update(
            host=host,
            token=token,
            target_definition=dokploy_source.DokployTargetDefinition(
                context=plan.product,
                instance=plan.preview_slug,
                target_id=compose_id,
                target_name=plan.compose_name,
                deploy_timeout_seconds=request.timeout_seconds,
            ),
            env_file=None,
            before_provider_mutation=checkpoint_provider_effect,
            deployment_title=provider_operation_title,
        )
        dokploy_post_deploy.require_odoo_module_update_readback_evidence(
            module_install_update_evidence
        )
        module_install_update_status = "pass"
        steps.append(_step("module_install_update", compose_id))
        if request.smoke_check:
            _wait_for_smoke_check(
                preview_url=plan.preview_url,
                health_path=request.health_path,
                timeout_seconds=request.timeout_seconds,
                expected_runtime_identity=expected_runtime_identity,
            )
            steps.append(_step("smoke_check", plan.preview_url))
        return _apply_result(
            request=request,
            status="pass",
            compose_id=compose_id,
            domain_id=domain_id,
            created_compose=bool(created_compose_id),
            provider_effect_attempted=provider_effect_attempted,
            module_install_update_status=module_install_update_status,
            module_install_update_evidence=module_install_update_evidence,
            steps=tuple(steps),
        )
    except click.ClickException as exc:
        rollback_errors: tuple[str, ...] = ()
        if not deploy_triggered:
            if created_compose_id and provider_effect_checkpoint is not None:
                provider_effect_checkpoint("rollback_started")
            elif provider_lease_check is not None:
                provider_lease_check()
            rollback_errors = _rollback_created_runtime(
                host=host,
                token=token,
                domain_id=domain_id if created_compose_id else "",
                compose_id=created_compose_id,
                delete_volumes=plan.delete_volumes,
                provider_effect_checkpoint=provider_effect_checkpoint,
            )
            if (
                created_compose_id
                and not rollback_errors
                and provider_effect_checkpoint is not None
            ):
                provider_effect_checkpoint("rollback_complete")
        return _apply_result(
            request=request,
            status="fail",
            error_message=str(exc),
            domain_id=domain_id,
            compose_id=resolved_compose_id,
            created_compose=bool(created_compose_id),
            provider_effect_attempted=provider_effect_attempted,
            module_install_update_status=module_install_update_status,
            module_install_update_evidence=module_install_update_evidence,
            steps=tuple(steps),
            rollback_errors=rollback_errors,
        )


def _execute_destroy(
    *,
    host: str,
    token: str,
    request: OdooPreviewDokployApplyRequest,
    provider_effect_checkpoint: Callable[[str], None] | None,
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
        before_provider_mutation=provider_effect_checkpoint,
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
            provider_effect_attempted=True,
            steps=steps,
        )
    return _apply_result(
        request=request,
        status="fail",
        error_message="; ".join(destroy_result.cleanup_errors),
        compose_id=compose_id,
        domain_id=domain_id,
        provider_effect_attempted=True,
        steps=steps,
    )


def _resolve_or_create_compose(
    *,
    host: str,
    token: str,
    plan: OdooPreviewDokployDryRunPlan,
    template_payload: JsonObject,
    steps: list[OdooPreviewDokployApplyStep],
    provider_effect_checkpoint: Callable[[str], None],
) -> tuple[str, bool]:
    if not plan.compose_ref.startswith("${created.composeId:"):
        return plan.compose_ref, False
    if not plan.environment_id:
        raise click.ClickException("Odoo preview compose create requires environment_id.")
    existing_compose_id = _find_compose_id_by_environment_and_name(
        host=host,
        token=token,
        environment_id=plan.environment_id,
        compose_name=plan.compose_name,
    )
    if existing_compose_id:
        return existing_compose_id, False
    server_id = str(template_payload.get("serverId") or "").strip()
    if not server_id:
        raise click.ClickException(
            "Odoo preview compose create requires the template compose serverId."
        )
    provider_effect_checkpoint("compose_create")
    created = dokploy_api.dokploy_request(
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
    created_compose = dokploy_api.as_json_object(created)
    compose_id = str((created_compose or {}).get("composeId") or "").strip()
    if not compose_id:
        raise click.ClickException(
            f"Dokploy did not return composeId for Odoo preview compose {plan.compose_name!r}."
        )
    steps.append(_step("compose_create", plan.compose_name))
    return compose_id, True


def _find_compose_id_by_environment_and_name(
    *,
    host: str,
    token: str,
    environment_id: str,
    compose_name: str,
) -> str:
    raw_projects = dokploy_api.dokploy_request(
        host=host,
        token=token,
        path="/api/project.all",
    )
    if not isinstance(raw_projects, list):
        raise click.ClickException(
            "Dokploy project inventory returned an invalid response payload."
        )
    matches: list[str] = []
    for raw_project in raw_projects:
        project = dokploy_api.as_json_object(raw_project)
        if project is None:
            continue
        raw_environments = project.get("environments")
        if not isinstance(raw_environments, list):
            continue
        for raw_environment in raw_environments:
            environment = dokploy_api.as_json_object(raw_environment)
            if environment is None:
                continue
            observed_environment_id = str(
                environment.get("environmentId") or environment.get("id") or ""
            ).strip()
            if observed_environment_id != environment_id:
                continue
            raw_composes = environment.get("composes")
            if not isinstance(raw_composes, list):
                continue
            for raw_compose in raw_composes:
                compose = dokploy_api.as_json_object(raw_compose)
                if compose is None or str(compose.get("name") or "").strip() != compose_name:
                    continue
                compose_id = str(compose.get("composeId") or compose.get("id") or "").strip()
                if compose_id and compose_id not in matches:
                    matches.append(compose_id)
    if not matches:
        raw_search_matches = dokploy_api.dokploy_request(
            host=host,
            token=token,
            path="/api/compose.search",
            query={"q": compose_name, "limit": 25},
        )
        for compose in _iter_dokploy_search_composes(raw_search_matches):
            if str(compose.get("name") or "").strip() != compose_name:
                continue
            compose_id = str(compose.get("composeId") or compose.get("id") or "").strip()
            if not compose_id:
                continue
            observed_environment_id = _compose_environment_id(compose)
            if not observed_environment_id:
                compose_payload = dokploy_api.fetch_dokploy_target_payload(
                    host=host,
                    token=token,
                    target_type="compose",
                    target_id=compose_id,
                )
                observed_environment_id = _compose_environment_id(compose_payload)
            if observed_environment_id == environment_id and compose_id not in matches:
                matches.append(compose_id)
    if len(matches) > 1:
        raise OdooPreviewTargetDiscoveryAmbiguousError(
            f"Discovered multiple Odoo preview composes named {compose_name!r}."
        )
    return matches[0] if matches else ""


def _compose_environment_id(compose: JsonObject) -> str:
    environment = dokploy_api.as_json_object(compose.get("environment"))
    return str(
        compose.get("environmentId")
        or (environment or {}).get("environmentId")
        or (environment or {}).get("id")
        or ""
    ).strip()


def _compose_exists_by_id(*, host: str, token: str, compose_id: str) -> bool:
    try:
        payload = dokploy_api.fetch_dokploy_target_payload(
            host=host,
            token=token,
            target_type="compose",
            target_id=compose_id,
        )
    except click.ClickException as error:
        if "failed (404):" in str(error):
            return False
        raise
    observed_compose_id = str(payload.get("composeId") or payload.get("id") or "").strip()
    if observed_compose_id != compose_id:
        raise click.ClickException("Dokploy compose lookup returned the wrong compose.")
    return True


def _compose_domains(*, host: str, token: str, compose_id: str) -> tuple[JsonObject, ...]:
    raw_domains = dokploy_api.dokploy_request(
        host=host,
        token=token,
        path="/api/domain.byComposeId",
        query={"composeId": compose_id},
    )
    if not isinstance(raw_domains, list):
        return ()
    domains: list[JsonObject] = []
    for raw_domain in raw_domains:
        domain = dokploy_api.as_json_object(raw_domain)
        if domain is not None:
            domains.append(domain)
    return tuple(domains)


def _delete_domain(*, host: str, token: str, domain_id: str) -> None:
    dokploy_api.dokploy_request(
        host=host,
        token=token,
        path="/api/domain.delete",
        method="POST",
        payload={"domainId": domain_id},
    )


def _delete_compose(*, host: str, token: str, compose_id: str, delete_volumes: bool) -> None:
    dokploy_api.dokploy_request(
        host=host,
        token=token,
        path="/api/compose.delete",
        method="POST",
        payload={"composeId": compose_id, "deleteVolumes": delete_volumes},
    )


def _rollback_created_runtime(
    *,
    host: str,
    token: str,
    domain_id: str,
    compose_id: str,
    delete_volumes: bool,
    provider_effect_checkpoint: Callable[[str], None] | None = None,
) -> tuple[str, ...]:
    errors: list[str] = []
    if domain_id:
        try:
            if provider_effect_checkpoint is not None:
                provider_effect_checkpoint("rollback_domain_delete")
            _delete_domain(host=host, token=token, domain_id=domain_id)
        except click.ClickException as exc:
            errors.append(f"domain rollback failed: {exc}")
    if compose_id:
        try:
            if provider_effect_checkpoint is not None:
                provider_effect_checkpoint("rollback_compose_delete")
            _delete_compose(
                host=host,
                token=token,
                compose_id=compose_id,
                delete_volumes=delete_volumes,
            )
        except click.ClickException as exc:
            errors.append(f"compose rollback failed: {exc}")
    return tuple(errors)


def _wait_for_smoke_check(
    *,
    preview_url: str,
    health_path: str,
    timeout_seconds: int,
    expected_runtime_identity: RuntimeIdentity | None = None,
) -> None:
    parsed = urlparse(preview_url.rstrip("/"))
    smoke_url = parsed._replace(path=health_path, params="", query="", fragment="").geturl()
    request = Request(
        smoke_url,
        headers={"Accept": "application/json, text/plain, */*", "Cache-Control": "no-store"},
    )
    deadline = time.monotonic() + timeout_seconds
    last_http_status: int | None = None
    last_transport_error = ""
    last_identity_error = ""
    while True:
        remaining_seconds = deadline - time.monotonic()
        if remaining_seconds <= 0:
            break
        try:
            with urlopen(request, timeout=min(15, remaining_seconds)) as response:
                body = response.read().decode("utf-8")
                status = response.status
            if 200 <= status < 400:
                last_http_status = None
                last_transport_error = ""
                if expected_runtime_identity is None:
                    return
                try:
                    payload = json.loads(body)
                except json.JSONDecodeError:
                    last_identity_error = (
                        "health endpoint did not return parseable JSON runtime identity evidence"
                    )
                else:
                    if isinstance(payload, dict) and payload.get("ok") is False:
                        last_identity_error = "health endpoint reported ok=false"
                    else:
                        runtime_status, detail, _observed = health_payload_runtime_identity_status(
                            expected=expected_runtime_identity,
                            payload=payload,
                        )
                        if runtime_status == "match":
                            strict_mismatches = _runtime_identity_mismatched_fields(
                                expected=expected_runtime_identity,
                                observed=_observed,
                            )
                            if not strict_mismatches:
                                return
                            last_identity_error = (
                                "Runtime identity mismatched fields: "
                                + ", ".join(strict_mismatches)
                            )
                        else:
                            last_identity_error = detail
        except HTTPError as exc:
            last_http_status = exc.code
            last_transport_error = ""
            last_identity_error = ""
        except TimeoutError as exc:
            last_http_status = None
            last_transport_error = str(exc).strip() or "request timed out"
            last_identity_error = ""
        except URLError as exc:
            last_http_status = None
            last_transport_error = str(exc.reason).strip() or str(exc).strip()
            last_identity_error = ""
        except ValueError as exc:
            last_http_status = None
            last_transport_error = str(exc).strip()
            last_identity_error = ""
        remaining_seconds = deadline - time.monotonic()
        if remaining_seconds <= 0:
            break
        sleep_seconds = min(5, remaining_seconds)
        time.sleep(sleep_seconds)
    if last_http_status is not None:
        raise click.ClickException(f"Odoo preview smoke check returned HTTP {last_http_status}.")
    if last_identity_error:
        raise click.ClickException(
            "Odoo preview runtime identity verification failed: " + last_identity_error
        )
    if last_transport_error:
        raise click.ClickException(
            f"Timed out waiting for Odoo preview smoke check {smoke_url}. "
            f"Last transport error: {last_transport_error}."
        )
    raise click.ClickException(f"Timed out waiting for Odoo preview smoke check {smoke_url}.")


def _runtime_identity_mismatched_fields(
    *,
    expected: RuntimeIdentity,
    observed: RuntimeIdentity | None,
) -> tuple[str, ...]:
    if observed is None:
        return ("runtime_identity",)
    return tuple(
        field_name
        for field_name in RuntimeIdentity.model_fields
        if getattr(expected, field_name) != getattr(observed, field_name)
    )


def _step(name: OdooPreviewDokployOperationName, target: str) -> OdooPreviewDokployApplyStep:
    return OdooPreviewDokployApplyStep(name=name, target=target)


def _apply_result(
    *,
    request: OdooPreviewDokployApplyRequest,
    status: OdooPreviewDokployApplyStatus,
    compose_id: str = "",
    domain_id: str = "",
    created_compose: bool = False,
    provider_effect_attempted: bool = False,
    module_install_update_status: Literal["pass", "fail", "skipped"] = "skipped",
    module_install_update_evidence: dict[str, str] | None = None,
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
        provider_effect_attempted=provider_effect_attempted,
        domain_id=domain_id,
        module_install_update_status=module_install_update_status,
        module_install_update_evidence=module_install_update_evidence or {},
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
                payload_keys=(
                    "host",
                    "port",
                    "certificateType",
                    "composeId",
                    "serviceName",
                    "domainType",
                ),
            ),
            OdooPreviewDokployOperation(
                name="compose_deploy",
                method="POST",
                path=spec.compose_redeploy_path if request.no_cache else spec.compose_deploy_path,
                target=compose_ref,
                payload_keys=("composeId",),
            ),
        )
    )
    operations.append(
        OdooPreviewDokployOperation(
            name="module_install_update",
            method="LOCAL",
            target=compose_ref,
        )
    )
    if request.smoke_check:
        operations.append(
            OdooPreviewDokployOperation(
                name="smoke_check",
                method="LOCAL",
                target=runtime_plan.preview_url,
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
        return _validated_immutable_image_reference(
            _required_text(
                normalized_image_reference,
                f"{label} requires image_reference or manifest.",
            ),
            label=label,
        )
    manifest_image_reference = _artifact_image_reference(manifest)
    if normalized_image_reference and normalized_image_reference != manifest_image_reference:
        raise ValueError(
            f"{label} image_reference does not match manifest image reference. "
            f"Request={normalized_image_reference} manifest={manifest_image_reference}."
        )
    return _validated_immutable_image_reference(manifest_image_reference, label=label)


def _validated_immutable_image_reference(value: str, *, label: str) -> str:
    normalized_value = value.strip()
    if not _IMMUTABLE_IMAGE_REFERENCE_PATTERN.fullmatch(normalized_value):
        raise ValueError(f"{label} requires a single-line immutable image reference.")
    return normalized_value


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
