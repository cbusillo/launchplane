from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path as FilePath
from typing import Annotated, Iterator, Literal, cast

import click
from fastapi import Depends, Path, Query, Response
from pydantic import BaseModel, ConfigDict

from control_plane import odoo_runtime_reads as reads
from control_plane.contracts.odoo_instance_override_record import (
    OdooOverrideApplyPhase,
    OdooWebsiteBootstrapPayload,
)
from control_plane.dokploy import api
from control_plane.http_routes.support import ApiRouteRegistrar, ReadRouteDependencies
from control_plane.odoo_post_deploy_http import OdooInstanceOverrideStore
from control_plane.odoo_product_driver_http import (
    OdooProductMismatchError,
    OdooRouteDependencyError,
    resolve_odoo_product_route,
)
from control_plane.preview_serving_evidence import PreviewServingEvidenceError
from control_plane.service_auth import AuthorizationTarget, LaunchplaneIdentity


@dataclass(frozen=True, slots=True)
class OdooRuntimeReadDependencies:
    common: ReadRouteDependencies
    control_plane_root: FilePath
    database_url: str | None


class OdooRuntimeLogsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    runtime: reads.OdooRuntimeEvidence
    lines: tuple[str, ...]
    redacted: bool = True
    line_limit: int
    since: str
    search: str


class OdooOutgoingEmailResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    runtime: reads.OdooRuntimeEvidence
    query: reads.OutgoingEmailQuery
    email: reads.OutgoingEmailResult


class OdooWebsiteBootstrapResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    product: str
    context: str
    instance: str
    website_bootstrap: OdooWebsiteBootstrapPayload | None
    apply_on: tuple[OdooOverrideApplyPhase, ...]
    updated_at: str
    source_label: str


def register_odoo_runtime_read_routes(
    app: ApiRouteRegistrar, *, dependencies: OdooRuntimeReadDependencies
) -> None:
    common = dependencies.common

    @contextmanager
    def errors(trace_id: str) -> Iterator[None]:
        try:
            yield
        except reads.OdooRuntimeReadError as error:
            raise common.http_error(
                status_code=error.status_code,
                trace_id=trace_id,
                code=error.code,
                message=str(error),
            ) from error
        except PreviewServingEvidenceError as error:
            raise common.http_error(
                status_code=409,
                trace_id=trace_id,
                code=error.code,
                message=str(error),
            ) from error
        except (FileNotFoundError, OdooRouteDependencyError) as error:
            raise common.http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message="The required Odoo runtime record was not found.",
            ) from error
        except (ValueError, click.ClickException) as error:
            # Provider messages may contain credentials or Odoo payloads.
            raise common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="odoo_runtime_read_unavailable",
                message="The selected Odoo runtime could not provide verified read evidence.",
            ) from error

    def authorize(
        identity: LaunchplaneIdentity, action: str, context: str, trace_id: str, instance: str = ""
    ) -> None:
        if not common.authorization_allows(
            identity=identity,
            action=action,
            product="launchplane",
            context=context,
            target=AuthorizationTarget(scope="instance", instances=(instance,))
            if instance
            else None,
        ):
            raise common.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=f"The caller cannot perform {action} for the requested runtime.",
            )

    def email_query(
        subject: str, recipient: str, created_after: datetime, limit: int, trace_id: str
    ) -> reads.OutgoingEmailQuery:
        try:
            return reads.OutgoingEmailQuery(
                subject=subject, recipient=recipient, created_after=created_after, limit=limit
            )
        except ValueError as error:
            raise common.http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_query",
                message="Email reads require a subject, recipient, and created_after timestamp with a timezone.",
            ) from error

    def preview_selection(
        store: object, preview_id: str, identity: LaunchplaneIdentity, action: str, trace_id: str
    ) -> reads.OdooRuntimeSelection:
        typed = cast(reads.OdooRuntimeReadStore, store)
        preview = typed.read_preview_record(preview_id)
        authorize(identity, "preview.read", preview.context, trace_id)
        selection = reads.select_preview_runtime(typed, preview)
        authorize(
            identity, action, selection.identity.context, trace_id, selection.identity.instance
        )
        return selection

    def connect(
        store: object, selection: reads.OdooRuntimeSelection
    ) -> reads.OdooRuntimeConnection:
        return reads.connect_runtime(
            store=cast(reads.OdooRuntimeReadStore, store),
            selection=selection,
            control_plane_root=dependencies.control_plane_root,
            database_url=dependencies.database_url,
        )

    def email_response(
        store: object,
        selection: reads.OdooRuntimeSelection,
        query: reads.OutgoingEmailQuery,
        trace_id: str,
    ) -> OdooOutgoingEmailResponse:
        connection = connect(store, selection)
        result = reads.read_outgoing_email(connection, query)
        reads.assert_selection_current(cast(reads.OdooRuntimeReadStore, store), selection)
        return OdooOutgoingEmailResponse(
            trace_id=trace_id, runtime=connection.evidence, query=query, email=result
        )

    def read_preview_logs(
        preview_id: Annotated[str, Path(min_length=1)],
        identity: Annotated[LaunchplaneIdentity, Depends(common.read_identity)],
        record_store: Annotated[object, Depends(common.get_record_store)],
        response: Response,
        lines: Annotated[int, Query(ge=1, le=api.MAX_DOKPLOY_LOG_LINE_COUNT)] = 200,
        since: Annotated[str, Query(pattern=r"^(all|\d+[smhd])$")] = "all",
        search: Annotated[str, Query(max_length=500, pattern=r"^[a-zA-Z0-9 ._-]*$")] = "",
    ) -> OdooRuntimeLogsResponse:
        trace_id = common.next_trace_id()
        response.headers["Cache-Control"] = "no-store"
        with errors(trace_id):
            selection = preview_selection(
                record_store, preview_id, identity, "target_logs.read", trace_id
            )
            connection = connect(record_store, selection)
            log_lines = reads.read_runtime_logs(connection, lines=lines, since=since, search=search)
            reads.assert_selection_current(
                cast(reads.OdooRuntimeReadStore, record_store), selection
            )
            return OdooRuntimeLogsResponse(
                trace_id=trace_id,
                runtime=connection.evidence,
                lines=log_lines,
                line_limit=lines,
                since=since,
                search=search,
            )

    def read_preview_outgoing_email(
        preview_id: Annotated[str, Path(min_length=1)],
        identity: Annotated[LaunchplaneIdentity, Depends(common.read_identity)],
        record_store: Annotated[object, Depends(common.get_record_store)],
        response: Response,
        subject: Annotated[str, Query(min_length=1, max_length=200)],
        recipient: Annotated[str, Query(min_length=3, max_length=320)],
        created_after: Annotated[datetime, Query()],
        limit: Annotated[int, Query(ge=1, le=20)] = 20,
    ) -> OdooOutgoingEmailResponse:
        trace_id = common.next_trace_id()
        response.headers["Cache-Control"] = "no-store"
        query = email_query(subject, recipient, created_after, limit, trace_id)
        with errors(trace_id):
            selection = preview_selection(
                record_store, preview_id, identity, "operations.read", trace_id
            )
            return email_response(record_store, selection, query, trace_id)

    def read_environment_outgoing_email(
        product: Annotated[str, Path(min_length=1)],
        environment: Annotated[str, Path(min_length=1)],
        identity: Annotated[LaunchplaneIdentity, Depends(common.read_identity)],
        record_store: Annotated[object, Depends(common.get_record_store)],
        response: Response,
        subject: Annotated[str, Query(min_length=1, max_length=200)],
        recipient: Annotated[str, Query(min_length=3, max_length=320)],
        created_after: Annotated[datetime, Query()],
        limit: Annotated[int, Query(ge=1, le=20)] = 20,
    ) -> OdooOutgoingEmailResponse:
        trace_id = common.next_trace_id()
        response.headers["Cache-Control"] = "no-store"
        query = email_query(subject, recipient, created_after, limit, trace_id)
        with errors(trace_id):
            typed = cast(reads.OdooRuntimeReadStore, record_store)
            profile = typed.read_product_profile_record(product)
            lane = next((lane for lane in profile.lanes if lane.instance == environment), None)
            if lane is None:
                raise reads.OdooRuntimeReadError(
                    "invalid_odoo_environment", "The product does not own this environment.", 400
                )
            authorize(identity, "operations.read", lane.context, trace_id, lane.instance)
            selection = reads.select_stable_runtime(typed, profile, environment)
            return email_response(record_store, selection, query, trace_id)

    def read_environment_website_bootstrap(
        product: Annotated[str, Path(min_length=1)],
        environment: Annotated[str, Path(min_length=1)],
        identity: Annotated[LaunchplaneIdentity, Depends(common.read_identity)],
        record_store: Annotated[object, Depends(common.get_record_store)],
        response: Response,
    ) -> OdooWebsiteBootstrapResponse:
        trace_id = common.next_trace_id()
        response.headers["Cache-Control"] = "no-store"
        with errors(trace_id):
            try:
                profile = resolve_odoo_product_route(
                    record_store=record_store, product=product, instance=environment
                )
            except OdooProductMismatchError as error:
                raise reads.OdooRuntimeReadError(
                    "invalid_odoo_environment",
                    "The product does not own this Odoo environment.",
                    400,
                ) from error
            lane = next(
                lane for lane in profile.lanes if lane.instance.strip() == environment.strip()
            )
            authorize(identity, "operations.read", lane.context, trace_id, lane.instance)
            record = cast(
                OdooInstanceOverrideStore, record_store
            ).read_odoo_instance_override_record(
                context_name=lane.context, instance_name=lane.instance
            )
            return OdooWebsiteBootstrapResponse(
                trace_id=trace_id,
                product=profile.product,
                context=lane.context,
                instance=lane.instance,
                website_bootstrap=record.website_bootstrap,
                apply_on=record.apply_on,
                updated_at=record.updated_at,
                source_label=record.source_label,
            )

    for path, endpoint, model, operation_id, summary in (
        (
            "/v1/previews/{preview_id}/logs",
            read_preview_logs,
            OdooRuntimeLogsResponse,
            "read_preview_runtime_logs",
            "Read redacted serving Odoo preview logs",
        ),
        (
            "/v1/previews/{preview_id}/outgoing-email",
            read_preview_outgoing_email,
            OdooOutgoingEmailResponse,
            "read_preview_outgoing_email",
            "Read serving Odoo preview outgoing-email status",
        ),
        (
            "/v1/products/{product}/environments/{environment}/outgoing-email",
            read_environment_outgoing_email,
            OdooOutgoingEmailResponse,
            "read_environment_outgoing_email",
            "Read Odoo environment outgoing-email status",
        ),
        (
            "/v1/products/{product}/environments/{environment}/website-bootstrap",
            read_environment_website_bootstrap,
            OdooWebsiteBootstrapResponse,
            "read_environment_website_bootstrap",
            "Read persisted Odoo website bootstrap settings",
        ),
    ):
        app.add_api_route(
            path,
            endpoint,
            methods=["GET"],
            response_model=model,
            operation_id=operation_id,
            summary=summary,
            responses={
                code: {"model": common.error_response_model}
                for code in (400, 401, 403, 404, 409, 410, 503)
            },
        )
