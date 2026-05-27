from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Literal, Protocol

import click
from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane import runtime_environments as control_plane_runtime_environments
from control_plane.contracts.artifact_publish_inputs import (
    GenericArtifactPublishInputsRequest,
    GenericArtifactPublishInputsResult,
)
from control_plane.contracts.artifact_identity import ArtifactIdentityManifest
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.runtime_key_safety_policy import RuntimeKeySafetyTarget
from control_plane.runtime_key_safety import (
    RuntimeKeySafetyPolicyReadStore,
    evaluate_runtime_key_safety_from_store,
    latest_active_runtime_key_safety_policy,
    runtime_key_safety_environment_class,
)

DEVKIT_RUNTIME_ENVIRONMENT_PAYLOAD_KEY = "ODOO_DEVKIT_RUNTIME_ENVIRONMENT_JSON"
PUBLISH_RUNTIME_ENVIRONMENT_KEYS = (
    "ODOO_VERSION",
    "ODOO_BASE_RUNTIME_IMAGE",
    "ODOO_BASE_DEVTOOLS_IMAGE",
    "ODOO_ADDON_REPOSITORIES",
    "OPENUPGRADE_ADDON_REPOSITORY",
    "OPENUPGRADELIB_INSTALL_SPEC",
    "ODOO_PYTHON_SYNC_SKIP_ADDONS",
)


class OdooArtifactPublishEvidenceStore(Protocol):
    def write_artifact_manifest(self, manifest: ArtifactIdentityManifest) -> Path | None: ...


class OdooArtifactPublishStore(
    RuntimeKeySafetyPolicyReadStore, OdooArtifactPublishEvidenceStore, Protocol
):
    pass


def _normalize_publish_scope(context: str, instance: str) -> tuple[str, str]:
    normalized_context = context.strip().lower()
    normalized_instance = instance.strip().lower()
    if not normalized_context:
        raise ValueError("Odoo artifact publish requires context.")
    if normalized_instance not in {"testing", "prod"}:
        raise ValueError("Odoo artifact publish requires instance 'testing' or 'prod'.")
    return normalized_context, normalized_instance


class OdooArtifactPublishRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    context: str
    instance: str = "testing"
    manifest_path: Path
    devkit_root: Path
    image_repository: str
    image_tag: str
    platforms: tuple[str, ...] = ()
    output_file: Path | None = None
    no_cache: bool = False

    @model_validator(mode="after")
    def _validate_request(self) -> "OdooArtifactPublishRequest":
        self.context, self.instance = _normalize_publish_scope(self.context, self.instance)
        self.image_repository = self.image_repository.strip()
        self.image_tag = self.image_tag.strip()
        if not self.image_repository:
            raise ValueError("Odoo artifact publish requires image_repository.")
        if not self.image_tag:
            raise ValueError("Odoo artifact publish requires image_tag.")
        self.platforms = tuple(platform.strip() for platform in self.platforms if platform.strip())
        return self


class OdooArtifactPublishResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["pass", "fail"]
    context: str
    instance: str
    artifact_id: str = ""
    image_repository: str = ""
    image_digest: str = ""
    source_commit: str = ""
    output_file: str = ""
    error_message: str = ""


class OdooArtifactPublishEvidenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    context: str
    instance: str = "testing"
    manifest: ArtifactIdentityManifest

    @model_validator(mode="after")
    def _validate_request(self) -> "OdooArtifactPublishEvidenceRequest":
        self.context, self.instance = _normalize_publish_scope(self.context, self.instance)
        expected_prefix = f"artifact-{self.context}-"
        if not self.manifest.artifact_id.startswith(expected_prefix):
            raise ValueError(
                "Odoo artifact publish evidence has an artifact for the wrong context. "
                f"Expected prefix {expected_prefix!r}; got {self.manifest.artifact_id!r}."
            )
        return self


class OdooArtifactPublishInputsRequest(GenericArtifactPublishInputsRequest):
    @model_validator(mode="after")
    def _validate_odoo_request(self) -> "OdooArtifactPublishInputsRequest":
        self.context, self.instance = _normalize_publish_scope(self.context, self.instance)
        return self


def _validate_manifest_context(*, manifest: ArtifactIdentityManifest, context: str) -> None:
    expected_prefix = f"artifact-{context}-"
    if not manifest.artifact_id.startswith(expected_prefix):
        raise click.ClickException(
            "Odoo artifact publish produced an artifact for the wrong context. "
            f"Expected prefix {expected_prefix!r}; got {manifest.artifact_id!r}."
        )


def _runtime_environment_payload(
    *,
    record_store: RuntimeKeySafetyPolicyReadStore,
    request: OdooArtifactPublishRequest,
    control_plane_root: Path,
) -> str:
    environment_values = control_plane_runtime_environments.resolve_runtime_environment_values(
        control_plane_root=control_plane_root,
        context_name=request.context,
        instance_name=request.instance,
    )
    _enforce_runtime_key_safety_for_publish_payload(
        record_store=record_store,
        context_name=request.context,
        instance_name=request.instance,
        environment_values=environment_values,
    )
    return json.dumps(
        {
            "context": request.context,
            "instance": request.instance,
            "environment": environment_values,
        },
        sort_keys=True,
    )


def _enforce_runtime_key_safety_for_publish_payload(
    *,
    record_store: RuntimeKeySafetyPolicyReadStore,
    context_name: str,
    instance_name: str,
    environment_values: dict[str, str],
) -> None:
    bindings = record_store.list_secret_bindings(
        integration="runtime_environment",
        context_name=context_name,
        instance_name=instance_name,
    )
    binding_keys = tuple(
        binding.binding_key for binding in bindings if binding.binding_key in environment_values
    )
    if not binding_keys:
        return
    try:
        policy_record = latest_active_runtime_key_safety_policy(record_store)
        evaluation = evaluate_runtime_key_safety_from_store(
            record_store=record_store,
            policy_record=policy_record,
            target=RuntimeKeySafetyTarget(
                context=context_name,
                instance=instance_name,
                environment_class=runtime_key_safety_environment_class(instance_name),
            ),
            required_binding_keys=binding_keys,
        )
    except ValueError as exc:
        raise click.ClickException(
            "Runtime key-safety policy is unavailable for Odoo artifact publish runtime payload."
        ) from exc
    if evaluation.status == "pass":
        return
    finding_codes = sorted({finding.code for finding in evaluation.findings})
    suffix = f": {', '.join(finding_codes)}" if finding_codes else ""
    raise click.ClickException(
        f"Runtime key-safety gate failed for Odoo artifact publish runtime payload{suffix}."
    )


def build_odoo_artifact_publish_inputs(
    *,
    control_plane_root: Path,
    request: OdooArtifactPublishInputsRequest,
    product_profile: LaunchplaneProductProfileRecord | None = None,
) -> dict[str, object]:
    if _publish_metadata_requested(request=request) and product_profile is None:
        raise click.ClickException(
            "Odoo artifact publish metadata requires a product profile. "
            "Use a product-specific Odoo driver id instead of the generic 'odoo' driver."
        )
    environment_values = control_plane_runtime_environments.resolve_runtime_environment_values(
        control_plane_root=control_plane_root,
        context_name=request.context,
        instance_name=request.instance,
    )
    publish_environment = {
        env_key: environment_values[env_key]
        for env_key in PUBLISH_RUNTIME_ENVIRONMENT_KEYS
        if environment_values.get(env_key, "").strip()
    }
    payload: dict[str, object] = {
        "context": request.context,
        "instance": request.instance,
        "environment": publish_environment,
    }
    if product_profile is not None:
        payload["image_repository"] = product_profile.image.repository.strip().rstrip("/")
        payload["repository"] = product_profile.repository.strip()
        payload["product"] = product_profile.product.strip()
    preview_slug = _publish_preview_slug(product_profile=product_profile, request=request)
    if preview_slug:
        payload["preview_slug"] = preview_slug
    if request.source_git_ref:
        payload["source_git_ref"] = request.source_git_ref
    image_repository = (
        product_profile.image.repository.strip() if product_profile is not None else ""
    )
    if image_repository and request.source_git_ref:
        payload["image_tag"] = _publish_image_tag(
            context=request.context,
            preview_slug=preview_slug,
            source_git_ref=request.source_git_ref,
            isolated=request.isolated,
        )
    result = GenericArtifactPublishInputsResult.model_validate(payload)
    return result.model_dump(exclude_defaults=True)


def _publish_metadata_requested(request: OdooArtifactPublishInputsRequest) -> bool:
    return bool(request.pr_number or request.preview_slug or request.source_git_ref)


def _publish_preview_slug(
    *,
    product_profile: LaunchplaneProductProfileRecord | None,
    request: OdooArtifactPublishInputsRequest,
) -> str:
    if request.preview_slug:
        return request.preview_slug
    if request.pr_number is None:
        return ""
    if product_profile is None:
        return f"pr-{request.pr_number}"
    return product_profile.preview.slug_template.strip().replace("{number}", str(request.pr_number))


def _publish_image_tag(
    *, context: str, preview_slug: str, source_git_ref: str, isolated: bool
) -> str:
    normalized_source = source_git_ref.strip()
    source_short = normalized_source[:8]
    platform_suffix = "isolated-amd64" if isolated else "amd64"
    slug = preview_slug.strip()
    if slug:
        return f"{context}-{slug}-{source_short}-{platform_suffix}"
    return f"{context}-{source_short}-{platform_suffix}"


def _publish_command(*, request: OdooArtifactPublishRequest, output_file: Path) -> list[str]:
    command = [
        "uv",
        "--directory",
        str(request.devkit_root),
        "run",
        "platform",
        "runtime",
        "publish",
        "--manifest",
        str(request.manifest_path),
        "--instance",
        request.instance,
        "--image-repository",
        request.image_repository,
        "--image-tag",
        request.image_tag,
        "--output-file",
        str(output_file),
    ]
    for platform in request.platforms:
        command.extend(["--platform", platform])
    if request.no_cache:
        command.append("--no-cache")
    return command


def _run_devkit_publish(
    *, request: OdooArtifactPublishRequest, runtime_payload: str, output_file: Path
) -> None:
    execution_environment = dict(os.environ)
    execution_environment[DEVKIT_RUNTIME_ENVIRONMENT_PAYLOAD_KEY] = runtime_payload
    result = subprocess.run(
        _publish_command(request=request, output_file=output_file),
        capture_output=True,
        text=True,
        env=execution_environment,
    )
    if result.returncode == 0:
        return
    details = (result.stderr or result.stdout or "").strip()
    raise click.ClickException(
        "Odoo artifact publish failed in odoo-devkit"
        + (f": {details}" if details else f" with exit code {result.returncode}.")
    )


def _read_manifest(
    *, output_file: Path, request: OdooArtifactPublishRequest
) -> ArtifactIdentityManifest:
    try:
        payload = json.loads(output_file.read_text(encoding="utf-8"))
    except OSError as error:
        raise click.ClickException(
            f"Odoo artifact publish did not create output file {output_file}."
        ) from error
    except json.JSONDecodeError as error:
        raise click.ClickException(
            "Odoo artifact publish produced invalid artifact manifest JSON."
        ) from error
    manifest = ArtifactIdentityManifest.model_validate(payload)
    _validate_manifest_context(manifest=manifest, context=request.context)
    return manifest


def ingest_odoo_artifact_publish_evidence(
    *,
    record_store: OdooArtifactPublishEvidenceStore,
    request: OdooArtifactPublishEvidenceRequest,
) -> OdooArtifactPublishResult:
    try:
        record_store.write_artifact_manifest(request.manifest)
    except click.ClickException as error:
        return OdooArtifactPublishResult(
            status="fail",
            context=request.context,
            instance=request.instance,
            artifact_id=request.manifest.artifact_id,
            image_repository=request.manifest.image.repository,
            image_digest=request.manifest.image.digest,
            source_commit=request.manifest.source_commit,
            error_message=str(error),
        )
    return OdooArtifactPublishResult(
        status="pass",
        context=request.context,
        instance=request.instance,
        artifact_id=request.manifest.artifact_id,
        image_repository=request.manifest.image.repository,
        image_digest=request.manifest.image.digest,
        source_commit=request.manifest.source_commit,
    )


def execute_odoo_artifact_publish(
    *,
    control_plane_root: Path,
    record_store: OdooArtifactPublishStore,
    request: OdooArtifactPublishRequest,
) -> OdooArtifactPublishResult:
    with tempfile.TemporaryDirectory(prefix="launchplane-odoo-artifact-") as temporary_directory:
        output_file = request.output_file or Path(temporary_directory) / "artifact.json"
        try:
            runtime_payload = _runtime_environment_payload(
                record_store=record_store,
                request=request,
                control_plane_root=control_plane_root,
            )
            _run_devkit_publish(
                request=request,
                runtime_payload=runtime_payload,
                output_file=output_file,
            )
            manifest = _read_manifest(output_file=output_file, request=request)
            record_store.write_artifact_manifest(manifest)
        except click.ClickException as error:
            return OdooArtifactPublishResult(
                status="fail",
                context=request.context,
                instance=request.instance,
                output_file=str(output_file),
                error_message=str(error),
            )
    return OdooArtifactPublishResult(
        status="pass",
        context=request.context,
        instance=request.instance,
        artifact_id=manifest.artifact_id,
        image_repository=manifest.image.repository,
        image_digest=manifest.image.digest,
        source_commit=manifest.source_commit,
        output_file=str(request.output_file or ""),
    )
