"""Read-only original preview reservation classification under existing preview authority."""

from typing import Annotated, Literal
from pathlib import Path as FilePath

import click
from fastapi import Depends, Path, Query, Response
from pydantic import BaseModel, ConfigDict

from control_plane import odoo_snapshot_version as snapshot_version
from control_plane.odoo_runtime_reads import OdooRuntimeReadError
from control_plane.http_routes.support import ApiRouteRegistrar, ReadRouteDependencies
from control_plane.preview_reservation_recovery import classify_preview_reservation
from control_plane.service_auth import AuthorizationTarget, LaunchplaneIdentity
from control_plane.storage.postgres import PostgresRecordStore
from control_plane import preview_evidence
from control_plane.dokploy.native_copy_stream import NativeSourceAuthorizationDenied
from control_plane.odoo_preview_qualification import PreviewQualificationMissing


class PreviewReservationClassificationsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    product: str
    reservations: tuple[dict[str, object], ...]
    truncated: bool


def register_preview_reservation_read_routes(
    app: ApiRouteRegistrar, *, dependencies: ReadRouteDependencies, root: FilePath | None = None
) -> None:
    def read_exact_preview_evidence(
        product: Annotated[str, Path(min_length=1, max_length=128, pattern=r"^\S+$")],
        query: Annotated[preview_evidence.PreviewEvidenceQuery, Query()],
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        response: Response,
    ) -> preview_evidence.PreviewEvidenceResponse:
        trace_id = dependencies.next_trace_id()
        response.headers["Cache-Control"] = "no-store"
        if not isinstance(record_store, PostgresRecordStore) or root is None:
            raise dependencies.http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message="Preview evidence requires service storage.",
            )
        try:
            profile = record_store.read_product_profile_record(product)
            if not dependencies.authorization_allows(
                identity=identity,
                action="preview.read",
                product=profile.product,
                context=profile.preview.context,
            ):
                raise dependencies.http_error(
                    status_code=403,
                    trace_id=trace_id,
                    code="authorization_denied",
                    message="Identity cannot read this product's previews.",
                )
            return preview_evidence.PreviewEvidenceResponse(
                trace_id=trace_id,
                result=preview_evidence.read_preview_evidence(
                    record_store, product=product, query=query, root=root
                ),
            )
        except NativeSourceAuthorizationDenied:
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Provider refused the preview evidence read.",
            ) from None
        except OdooRuntimeReadError as error:
            raise dependencies.http_error(
                status_code=error.status_code,
                trace_id=trace_id,
                code=error.code,
                message="Exact preview evidence could not be bound.",
            ) from None
        except FileNotFoundError:
            raise dependencies.http_error(
                status_code=404,
                trace_id=trace_id,
                code="preview_evidence_unavailable",
                message="Preview evidence is unavailable.",
            ) from None
        except (ValueError, PreviewQualificationMissing, click.ClickException):
            raise dependencies.http_error(
                status_code=409,
                trace_id=trace_id,
                code="preview_evidence_unavailable",
                message="Preview evidence could not be qualified.",
            ) from None

    def read_preview_reservations(
        product: Annotated[str, Path(min_length=1, max_length=128, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        limit: Annotated[int, Query(ge=1, le=100)] = 100,
    ) -> PreviewReservationClassificationsResponse:
        trace_id = dependencies.next_trace_id()
        if not isinstance(record_store, PostgresRecordStore):
            raise dependencies.http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message="Preview reservation classification requires database storage.",
            )
        try:
            profile = record_store.read_product_profile_record(product)
        except FileNotFoundError as error:
            raise dependencies.http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message="Product not found.",
            ) from error
        if not dependencies.authorization_allows(
            identity=identity,
            action="preview.read",
            product=profile.product,
            context=profile.preview.context,
        ):
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Identity cannot read this product's preview reservations.",
            )
        held = record_store.list_held_odoo_preview_reservations(product=product, limit=limit + 1)
        return PreviewReservationClassificationsResponse(
            trace_id=trace_id,
            product=product,
            reservations=tuple(
                classify_preview_reservation(record, profile=profile, store=record_store)
                for record in held[:limit]
            ),
            truncated=len(held) > limit,
        )

    def read_preview_snapshot_version(
        product: Annotated[str, Path(min_length=1, max_length=128, pattern=r"^\S+$")],
        query: Annotated[snapshot_version.SnapshotVersionQuery, Query()],
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        response: Response,
    ) -> snapshot_version.SnapshotVersionResponse:
        trace_id = dependencies.next_trace_id()
        response.headers["Cache-Control"] = "no-store"
        if not isinstance(record_store, PostgresRecordStore):
            raise dependencies.http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message="Snapshot provenance requires database storage.",
            )
        try:
            profile = record_store.read_product_profile_record(product)
        except FileNotFoundError as error:
            raise dependencies.http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message="Product not found.",
            ) from error
        if not dependencies.authorization_allows(
            identity=identity,
            action="preview.read",
            product=profile.product,
            context=profile.preview.context,
        ):
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Identity cannot read this product's retained preview snapshot metadata.",
            )
        if not dependencies.authorization_allows(
            identity=identity,
            action="operations.read",
            product=profile.product,
            context=profile.preview.context,
            target=AuthorizationTarget(scope="instance", instances=("testing",)),
        ):
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Identity cannot read this product's testing snapshot archive.",
            )
        try:
            return snapshot_version.SnapshotVersionResponse(
                trace_id=trace_id,
                result=snapshot_version.read_snapshot_version(
                    store=record_store, product=product, query=query
                ),
            )
        except OdooRuntimeReadError as error:
            raise dependencies.http_error(
                status_code=error.status_code,
                trace_id=trace_id,
                code=error.code,
                message="The retained snapshot version could not be qualified.",
            ) from error
        except FileNotFoundError as error:
            raise dependencies.http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message="The required snapshot utility record was not found.",
            ) from error
        except (ValueError, click.ClickException) as error:
            raise dependencies.http_error(
                status_code=503,
                trace_id=trace_id,
                code="snapshot_version_unavailable",
                message="The retained snapshot version could not be qualified.",
            ) from error

    app.add_api_route(
        "/v1/products/{product}/preview-evidence",
        read_exact_preview_evidence,
        methods=["GET"],
        response_model=preview_evidence.PreviewEvidenceResponse,
        operation_id="read_exact_preview_evidence",
        summary="Read exact original preview evidence without publication or settlement",
        responses={
            code: {"model": dependencies.error_response_model} for code in (401, 403, 404, 409, 503)
        },
    )

    app.add_api_route(
        snapshot_version.ROUTE,
        read_preview_snapshot_version,
        methods=["GET"],
        response_model=snapshot_version.SnapshotVersionResponse,
        operation_id="read_preview_snapshot_version",
        summary="Read recorded base version from one original-bound immutable preview source",
        responses={
            code: {"model": dependencies.error_response_model} for code in (401, 403, 404, 409, 503)
        },
    )

    app.add_api_route(
        "/v1/products/{product}/preview-reservations",
        read_preview_reservations,
        methods=["GET"],
        response_model=PreviewReservationClassificationsResponse,
        operation_id="read_preview_reservations",
        summary="Read original preview reservation classifications",
        responses={
            code: {"model": dependencies.error_response_model} for code in (401, 403, 404, 503)
        },
    )
