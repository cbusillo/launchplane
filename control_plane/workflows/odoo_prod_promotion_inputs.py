from __future__ import annotations

import re
from typing import Literal, Protocol

import click
from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.artifact_identity import ArtifactIdentityManifest
from control_plane.contracts.release_tuple_record import ReleaseTupleRecord


class OdooProdPromotionInputsStore(Protocol):
    def read_artifact_manifest(self, artifact_id: str) -> ArtifactIdentityManifest: ...

    def read_release_tuple_record(
        self, *, context_name: str, channel_name: str
    ) -> ReleaseTupleRecord: ...


class OdooProdPromotionInputsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    context: str
    from_instance: str = "testing"
    to_instance: str = "prod"
    request_id: str

    @model_validator(mode="after")
    def _validate_request(self) -> "OdooProdPromotionInputsRequest":
        self.context = self.context.strip().lower()
        self.from_instance = self.from_instance.strip().lower()
        self.to_instance = self.to_instance.strip().lower()
        self.request_id = self.request_id.strip()
        if not self.context:
            raise ValueError("Odoo prod promotion inputs require context.")
        if self.from_instance != "testing" or self.to_instance != "prod":
            raise ValueError("Odoo prod promotion inputs require testing -> prod.")
        if not self.request_id:
            raise ValueError("Odoo prod promotion inputs require request_id.")
        return self


class OdooProdPromotionInputsResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context: str
    from_instance: str
    to_instance: str
    request_id: str
    input_status: Literal["ready", "blocked"]
    artifact_id: str = ""
    source_git_ref: str = ""
    backup_record_id: str = ""
    release_tuple_id: str = ""
    image_repository: str = ""
    image_digest: str = ""
    error_message: str = ""


def resolve_odoo_prod_promotion_inputs(
    *,
    record_store: OdooProdPromotionInputsStore,
    request: OdooProdPromotionInputsRequest,
) -> OdooProdPromotionInputsResult:
    try:
        source_tuple = record_store.read_release_tuple_record(
            context_name=request.context,
            channel_name=request.from_instance,
        )
    except FileNotFoundError:
        return _blocked_result(
            request=request,
            error_message=(
                "Odoo prod promotion inputs require a current testing release tuple. "
                f"Ship and verify {request.context}/testing before promoting prod."
            ),
        )

    if source_tuple.context != request.context or source_tuple.channel != request.from_instance:
        return _blocked_result(
            request=request,
            error_message=(
                "Odoo prod promotion inputs found a source tuple for the wrong lane. "
                f"Tuple={source_tuple.context}/{source_tuple.channel} "
                f"request={request.context}/{request.from_instance}."
            ),
            release_tuple_id=source_tuple.tuple_id,
            artifact_id=source_tuple.artifact_id,
        )

    try:
        artifact_manifest = record_store.read_artifact_manifest(source_tuple.artifact_id)
    except FileNotFoundError:
        return _blocked_result(
            request=request,
            error_message=(
                "Odoo prod promotion inputs require the source tuple artifact manifest. "
                f"Missing artifact manifest {source_tuple.artifact_id!r}."
            ),
            release_tuple_id=source_tuple.tuple_id,
            artifact_id=source_tuple.artifact_id,
        )
    except click.ClickException as error:
        return _blocked_result(
            request=request,
            error_message=str(error),
            release_tuple_id=source_tuple.tuple_id,
            artifact_id=source_tuple.artifact_id,
        )

    source_git_ref = artifact_manifest.source_commit.strip()
    if not source_git_ref:
        return _blocked_result(
            request=request,
            error_message=(
                "Odoo prod promotion inputs require artifact source_commit before promotion."
            ),
            release_tuple_id=source_tuple.tuple_id,
            artifact_id=source_tuple.artifact_id,
        )

    return OdooProdPromotionInputsResult(
        context=request.context,
        from_instance=request.from_instance,
        to_instance=request.to_instance,
        request_id=request.request_id,
        input_status="ready",
        artifact_id=source_tuple.artifact_id,
        source_git_ref=source_git_ref,
        backup_record_id=build_odoo_prod_backup_record_id(
            context=request.context,
            instance=request.to_instance,
            request_id=request.request_id,
        ),
        release_tuple_id=source_tuple.tuple_id,
        image_repository=source_tuple.image_repository or artifact_manifest.image.repository,
        image_digest=source_tuple.image_digest or artifact_manifest.image.digest,
    )


def build_odoo_prod_backup_record_id(*, context: str, instance: str, request_id: str) -> str:
    return "-".join(
        (
            "backup-gate",
            _record_slug(context),
            _record_slug(instance),
            _record_slug(request_id),
        )
    )


def _blocked_result(
    *,
    request: OdooProdPromotionInputsRequest,
    error_message: str,
    release_tuple_id: str = "",
    artifact_id: str = "",
) -> OdooProdPromotionInputsResult:
    return OdooProdPromotionInputsResult(
        context=request.context,
        from_instance=request.from_instance,
        to_instance=request.to_instance,
        request_id=request.request_id,
        input_status="blocked",
        artifact_id=artifact_id,
        release_tuple_id=release_tuple_id,
        error_message=error_message,
    )


def _record_slug(value: str) -> str:
    compact = re.sub(r"[^A-Za-z0-9]+", "-", value.strip()).strip("-").lower()
    return compact or "request"
