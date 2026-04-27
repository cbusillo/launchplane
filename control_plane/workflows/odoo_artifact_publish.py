from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Literal

import click
from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane import runtime_environments as control_plane_runtime_environments
from control_plane.contracts.artifact_identity import ArtifactIdentityManifest
from control_plane.storage.filesystem import FilesystemRecordStore

SUPPORTED_ODOO_CONTEXTS = {"cm", "opw"}
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
        self.context = self.context.strip().lower()
        self.instance = self.instance.strip().lower()
        self.image_repository = self.image_repository.strip()
        self.image_tag = self.image_tag.strip()
        if self.context not in SUPPORTED_ODOO_CONTEXTS:
            supported = ", ".join(sorted(SUPPORTED_ODOO_CONTEXTS))
            raise ValueError(
                f"Odoo artifact publish supports contexts {supported}; got {self.context!r}."
            )
        if self.instance not in {"testing", "prod"}:
            raise ValueError("Odoo artifact publish requires instance 'testing' or 'prod'.")
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
        self.context = self.context.strip().lower()
        self.instance = self.instance.strip().lower()
        if self.context not in SUPPORTED_ODOO_CONTEXTS:
            supported = ", ".join(sorted(SUPPORTED_ODOO_CONTEXTS))
            raise ValueError(
                f"Odoo artifact publish supports contexts {supported}; got {self.context!r}."
            )
        if self.instance not in {"testing", "prod"}:
            raise ValueError("Odoo artifact publish requires instance 'testing' or 'prod'.")
        expected_prefix = f"artifact-{self.context}-"
        if not self.manifest.artifact_id.startswith(expected_prefix):
            raise ValueError(
                "Odoo artifact publish evidence has an artifact for the wrong context. "
                f"Expected prefix {expected_prefix!r}; got {self.manifest.artifact_id!r}."
            )
        return self


class OdooArtifactPublishInputsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    context: str
    instance: str = "testing"

    @model_validator(mode="after")
    def _validate_request(self) -> "OdooArtifactPublishInputsRequest":
        self.context = self.context.strip().lower()
        self.instance = self.instance.strip().lower()
        if self.context not in SUPPORTED_ODOO_CONTEXTS:
            supported = ", ".join(sorted(SUPPORTED_ODOO_CONTEXTS))
            raise ValueError(
                f"Odoo artifact publish supports contexts {supported}; got {self.context!r}."
            )
        if self.instance not in {"testing", "prod"}:
            raise ValueError("Odoo artifact publish requires instance 'testing' or 'prod'.")
        return self


def _validate_manifest_context(*, manifest: ArtifactIdentityManifest, context: str) -> None:
    expected_prefix = f"artifact-{context}-"
    if not manifest.artifact_id.startswith(expected_prefix):
        raise click.ClickException(
            "Odoo artifact publish produced an artifact for the wrong context. "
            f"Expected prefix {expected_prefix!r}; got {manifest.artifact_id!r}."
        )


def _runtime_environment_payload(
    *, request: OdooArtifactPublishRequest, control_plane_root: Path
) -> str:
    environment_values = control_plane_runtime_environments.resolve_runtime_environment_values(
        control_plane_root=control_plane_root,
        context_name=request.context,
        instance_name=request.instance,
    )
    return json.dumps(
        {
            "context": request.context,
            "instance": request.instance,
            "environment": environment_values,
        },
        sort_keys=True,
    )


def build_odoo_artifact_publish_inputs(
    *,
    control_plane_root: Path,
    request: OdooArtifactPublishInputsRequest,
) -> dict[str, object]:
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
    return {
        "context": request.context,
        "instance": request.instance,
        "environment": publish_environment,
    }


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
    record_store: FilesystemRecordStore,
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
    record_store: FilesystemRecordStore,
    request: OdooArtifactPublishRequest,
) -> OdooArtifactPublishResult:
    runtime_payload = _runtime_environment_payload(
        request=request,
        control_plane_root=control_plane_root,
    )
    with tempfile.TemporaryDirectory(prefix="launchplane-odoo-artifact-") as temporary_directory:
        output_file = request.output_file or Path(temporary_directory) / "artifact.json"
        try:
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
