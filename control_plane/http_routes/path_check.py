"""``GET /v1/products/{product}/path-check``: what stands between the caller
and the end of a product's path, in one read."""

from datetime import UTC, datetime
from typing import Annotated, Literal, cast

from fastapi import Depends, Path, Query
from pydantic import BaseModel, ConfigDict

from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.http_routes.products import ProductReadRouteDependencies
from control_plane.http_routes.support import ApiRouteRegistrar
from control_plane.product_path_check import (
    PathName,
    ProductPathCheck,
    build_product_path_check,
    read_path_check_inputs,
)
from control_plane.release_review import current_release_review
from control_plane.service_auth import AuthorizationTarget, LaunchplaneIdentity

PRODUCT_PATH_CHECK_ROUTE = "/v1/products/{product}/path-check"


class ProductPathCheckResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    check: ProductPathCheck


def register_product_path_check_read_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: ProductReadRouteDependencies,
) -> None:
    common = dependencies.common

    def read_product_path_check(
        product: Annotated[str, Path(min_length=1, max_length=128, pattern=r"^\S+$")],
        path: Annotated[PathName, Query()],
        identity: Annotated[LaunchplaneIdentity, Depends(common.read_identity)],
        record_store: Annotated[object, Depends(common.get_record_store)],
    ) -> ProductPathCheckResponse:
        trace_id = common.next_trace_id()
        try:
            profile = cast(
                LaunchplaneProductProfileRecord,
                getattr(record_store, "read_product_profile_record")(product),
            )
        except (AttributeError, FileNotFoundError) as error:
            raise common.http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message="Product was not found.",
            ) from error
        contexts = sorted({lane.context for lane in profile.lanes}) or [profile.product]
        if not all(
            common.authorization_allows(
                identity=identity,
                action="product_environment.read",
                product=profile.product,
                context=context,
                target=AuthorizationTarget(scope="context"),
            )
            for context in contexts
        ):
            # The same answer as a missing product: the caller learns nothing.
            raise common.http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message="Product was not found.",
            )

        def action_allowed(action: str, context: str, instances: tuple[str, ...]) -> bool:
            return common.authorization_allows(
                identity=identity,
                action=action,
                product=profile.product,
                context=context,
                target=AuthorizationTarget(scope="instance", instances=instances),
            )

        inputs = read_path_check_inputs(
            path=path,
            profile=profile,
            record_store=record_store,
            action_allowed=action_allowed,
            read_release_review=lambda: current_release_review(
                control_plane_root=dependencies.control_plane_root,
                record_store=record_store,
                profile=profile,
                trace_id=trace_id,
            ),
            generated_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        return ProductPathCheckResponse(
            trace_id=trace_id,
            check=build_product_path_check(product=profile.product, path=path, inputs=inputs),
        )

    app.add_api_route(
        PRODUCT_PATH_CHECK_ROUTE,
        read_product_path_check,
        methods=["GET"],
        response_model=ProductPathCheckResponse,
        operation_id="read_product_path_check",
        summary="Report every step that stands between the caller and the end of a path",
        responses={code: {"model": common.error_response_model} for code in (401, 404, 422, 503)},
    )
