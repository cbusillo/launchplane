from __future__ import annotations

import re
from typing import Literal, Protocol

import click
from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.artifact_identity import ArtifactIdentityManifest
from control_plane.contracts.release_tuple_record import ReleaseTupleRecord


class GenericPromotionInputsStore(Protocol):
    def read_artifact_manifest(self, artifact_id: str) -> ArtifactIdentityManifest: ...

    def read_release_tuple_record(
        self, *, context_name: str, channel_name: str
    ) -> ReleaseTupleRecord: ...


class GenericPromotionInputsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    context: str
    from_instance: str = "testing"
    to_instance: str = "prod"
    request_id: str = ""

    @model_validator(mode="after")
    def _validate_request(self) -> "GenericPromotionInputsRequest":
        self.context = self.context.strip().lower()
        self.from_instance = self.from_instance.strip().lower()
        self.to_instance = self.to_instance.strip().lower()
        self.request_id = self.request_id.strip()
        if not self.context:
            raise ValueError("Generic promotion inputs require context.")
        if self.from_instance == self.to_instance:
            raise ValueError(
                "Generic promotion inputs require different source and destination instances."
            )
        return self


class GenericPromotionInputsResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context: str
    from_instance: str
    to_instance: str
    request_id: str = ""
    input_status: Literal["ready", "blocked"]
    artifact_id: str = ""
    source_git_ref: str = ""
    release_tuple_id: str = ""
    image_repository: str = ""
    image_digest: str = ""
    error_message: str = ""

    @model_validator(mode="after")
    def _validate_result(self) -> "GenericPromotionInputsResult":
        self.context = self.context.strip().lower()
        self.from_instance = self.from_instance.strip().lower()
        self.to_instance = self.to_instance.strip().lower()
        self.request_id = self.request_id.strip()
        self.artifact_id = self.artifact_id.strip()
        self.source_git_ref = self.source_git_ref.strip()
        self.release_tuple_id = self.release_tuple_id.strip()
        self.image_repository = self.image_repository.strip()
        self.image_digest = self.image_digest.strip()
        self.error_message = self.error_message.strip()
        if not self.context:
            raise ValueError("Generic promotion inputs result requires context.")
        if not self.from_instance:
            raise ValueError("Generic promotion inputs result requires from_instance.")
        if not self.to_instance:
            raise ValueError("Generic promotion inputs result requires to_instance.")
        return self


def resolve_generic_promotion_inputs(
    *,
    record_store: GenericPromotionInputsStore,
    request: GenericPromotionInputsRequest,
    product_label: str = "Generic",
) -> GenericPromotionInputsResult:
    try:
        source_tuple = record_store.read_release_tuple_record(
            context_name=request.context,
            channel_name=request.from_instance,
        )
    except FileNotFoundError:
        return _blocked_result(
            request=request,
            error_message=(
                f"{product_label} promotion inputs require a current "
                f"{request.from_instance} release tuple. Ship and verify "
                f"{request.context}/{request.from_instance} before promoting "
                f"{request.to_instance}."
            ),
        )

    if source_tuple.context != request.context or source_tuple.channel != request.from_instance:
        return _blocked_result(
            request=request,
            error_message=(
                f"{product_label} promotion inputs found a source tuple for the wrong lane. "
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
                f"{product_label} promotion inputs require the source tuple artifact manifest. "
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
                f"{product_label} promotion inputs require artifact source_commit before promotion."
            ),
            release_tuple_id=source_tuple.tuple_id,
            artifact_id=source_tuple.artifact_id,
        )

    return GenericPromotionInputsResult(
        context=request.context,
        from_instance=request.from_instance,
        to_instance=request.to_instance,
        request_id=request.request_id,
        input_status="ready",
        artifact_id=source_tuple.artifact_id,
        source_git_ref=source_git_ref,
        release_tuple_id=source_tuple.tuple_id,
        image_repository=source_tuple.image_repository or artifact_manifest.image.repository,
        image_digest=source_tuple.image_digest or artifact_manifest.image.digest,
    )


def build_generic_promotion_backup_record_id(
    *, context: str, instance: str, request_id: str
) -> str:
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
    request: GenericPromotionInputsRequest,
    error_message: str,
    release_tuple_id: str = "",
    artifact_id: str = "",
) -> GenericPromotionInputsResult:
    return GenericPromotionInputsResult(
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
