"""Names-only readback for every testing lane in an authorized product."""

from typing import Annotated, cast

from fastapi import Depends, Response

from control_plane.http_routes.support import ApiRouteRegistrar, ReadRouteDependencies
from control_plane.service_auth import AuthorizationTarget, LaunchplaneIdentity
from control_plane.testing_local_credentials import (
    TestingCredentialStore,
    TestingCredentialAuditResponse,
    audit_testing_credentials,
    TestingCredentialInventoryResponse,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord


def register_testing_credential_routes(
    app: ApiRouteRegistrar, *, dependencies: ReadRouteDependencies
) -> None:
    def authorize(
        product: str,
        response: Response,
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
    ) -> LaunchplaneProductProfileRecord:
        trace = dependencies.next_trace_id()
        response.headers["Cache-Control"] = "no-store"
        store = cast(TestingCredentialStore, record_store)
        if not dependencies.authorization_allows(
            identity=identity,
            action="product_profile.read",
            product=product,
            context="launchplane",
            target=AuthorizationTarget(scope="context"),
        ):
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace,
                code="authorization_denied",
                message="The caller cannot read this product.",
            )
        try:
            profiles = store.list_product_profile_records()
            profile = next(item for item in profiles if item.product == product)
        except (AttributeError, StopIteration):
            raise dependencies.http_error(
                status_code=404,
                trace_id=trace,
                code="not_found",
                message="Product credential audit is unavailable.",
            ) from None
        # Authorize every participating lane before decrypting anything. Partial access
        # never produces an apparently complete product audit.
        for lane in profile.lanes:
            if lane.instance not in {"prod", "testing"}:
                continue
            if not dependencies.authorization_allows(
                identity=identity,
                action="product_environment.read",
                product=product,
                context=lane.context,
                target=AuthorizationTarget(scope="instance", instances=(lane.instance,)),
            ):
                raise dependencies.http_error(
                    status_code=403,
                    trace_id=trace,
                    code="authorization_denied",
                    message="The caller cannot audit every product lane.",
                )
        return profile

    def read(
        product: str,
        response: Response,
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
    ) -> TestingCredentialAuditResponse:
        profile = authorize(product, response, identity, record_store)
        return TestingCredentialAuditResponse(
            trace_id=dependencies.next_trace_id(),
            product=product,
            lanes=audit_testing_credentials(
                cast(TestingCredentialStore, record_store), product, profile_snapshot=profile
            ),
        )

    app.add_api_route(
        "/v1/products/{product}/testing-credentials",
        read,
        methods=["GET"],
        response_model=TestingCredentialAuditResponse,
        operation_id="read_testing_credentials",
        summary="Audit testing credential separation without values or fingerprints",
        responses={
            code: {"model": dependencies.error_response_model} for code in (401, 403, 404, 503)
        },
    )

    def inventory(
        response: Response,
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
    ) -> TestingCredentialInventoryResponse:
        store = cast(TestingCredentialStore, record_store)
        profiles = store.list_product_profile_records()
        for profile in profiles:
            authorize(profile.product, response, identity, record_store)
        lanes = [
            lane
            for profile in profiles
            for lane in audit_testing_credentials(
                store, profile.product, profile_snapshot=profile, production_profiles=profiles
            )
        ]
        if store.list_product_profile_records() != profiles:
            raise dependencies.http_error(
                status_code=409,
                trace_id=dependencies.next_trace_id(),
                code="credential_audit_changed",
                message="Product inventory changed during the credential audit.",
            )
        # Reading every stored product is complete or refuses; no silently filtered inventory.
        return TestingCredentialInventoryResponse(
            trace_id=dependencies.next_trace_id(),
            products=sorted(profile.product for profile in profiles),
            lanes=lanes,
        )

    app.add_api_route(
        "/v1/testing-credentials",
        inventory,
        methods=["GET"],
        response_model=TestingCredentialInventoryResponse,
        operation_id="audit_testing_credentials",
        summary="Audit all products' testing lanes without credential values",
        responses={code: {"model": dependencies.error_response_model} for code in (401, 403, 503)},
    )
