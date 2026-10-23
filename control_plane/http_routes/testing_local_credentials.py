"""Names-only readback for every testing lane in an authorized product."""

from typing import Annotated, cast

from fastapi import Depends, Request, Response

from control_plane import discord_identity

from control_plane.http_routes.support import ApiRouteRegistrar, ReadRouteDependencies
from control_plane.service_auth import AuthorizationTarget, LaunchplaneIdentity
from control_plane.testing_local_credentials import (
    TestingCredentialStore,
    TestingCredentialAuditResponse,
    audit_testing_credentials,
    TestingCredentialInventoryResponse,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.testing_external_resources import (
    ExternalResourceInputSnapshot,
    audit_external_resources,
    capture_external_resource_inputs,
)


def authorize_credential_profile(
    dependencies: ReadRouteDependencies,
    store: TestingCredentialStore,
    product: str,
    identity: LaunchplaneIdentity,
) -> LaunchplaneProductProfileRecord:
    trace = dependencies.next_trace_id()
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


def authorize_discord_profiles(
    dependencies: ReadRouteDependencies,
    store: TestingCredentialStore,
    identity: LaunchplaneIdentity,
) -> tuple[LaunchplaneProductProfileRecord, ...]:
    profiles = store.list_product_profile_records()
    for profile in profiles:
        authorize_credential_profile(dependencies, store, profile.product, identity)
    return profiles


def register_testing_credential_routes(
    app: ApiRouteRegistrar, *, dependencies: ReadRouteDependencies
) -> None:
    def capture(
        store: TestingCredentialStore,
        profiles: tuple[LaunchplaneProductProfileRecord, ...],
        *,
        inventory: bool = False,
    ) -> ExternalResourceInputSnapshot | None:
        try:
            return capture_external_resource_inputs(store, profiles, inventory=inventory)
        except Exception:
            return None

    def authorize(
        product: str,
        response: Response,
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
    ) -> LaunchplaneProductProfileRecord:
        response.headers["Cache-Control"] = "no-store"
        return authorize_credential_profile(
            dependencies, cast(TestingCredentialStore, record_store), product, identity
        )

    def read(
        product: str,
        response: Response,
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        include_external_resources: bool = False,
    ) -> TestingCredentialAuditResponse:
        profile = authorize(product, response, identity, record_store)
        snapshot = (
            capture(cast(TestingCredentialStore, record_store), (profile,))
            if include_external_resources
            else None
        )
        trace = dependencies.next_trace_id()
        lanes = audit_testing_credentials(
            cast(TestingCredentialStore, record_store), product, profile_snapshot=profile
        )
        external = (
            audit_external_resources(
                cast(TestingCredentialStore, record_store),
                (profile,),
                captured=snapshot,
                trace_id=trace,
                actor=type(identity).__name__,
            )
            if include_external_resources
            else None
        )
        if external is not None and external.failure_code == "inputs_changed":
            for lane in lanes:
                lane.status = "unavailable"
        return TestingCredentialAuditResponse(
            trace_id=trace,
            product=product,
            lanes=lanes,
            external_resources=external,
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
        include_external_resources: bool = False,
    ) -> TestingCredentialInventoryResponse:
        response.headers["Cache-Control"] = "no-store"
        store = cast(TestingCredentialStore, record_store)
        profiles = authorize_discord_profiles(dependencies, store, identity)
        snapshot = (
            capture(store, tuple(profiles), inventory=True) if include_external_resources else None
        )
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
        trace = dependencies.next_trace_id()
        external = (
            audit_external_resources(
                store,
                tuple(profiles),
                captured=snapshot,
                trace_id=trace,
                actor=type(identity).__name__,
                inventory=True,
            )
            if include_external_resources
            else None
        )
        if external is not None and external.failure_code == "inputs_changed":
            for lane in lanes:
                lane.status = "unavailable"
        return TestingCredentialInventoryResponse(
            trace_id=trace,
            products=sorted(profile.product for profile in profiles),
            lanes=lanes,
            external_resources=external,
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

    def identify_discord(
        product: str,
        environment: str,
        request: Request,
        response: Response,
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
    ) -> discord_identity.DiscordIdentityResponse:
        trace = dependencies.next_trace_id()
        response.headers["Cache-Control"] = "no-store"
        if environment != "testing" or request.query_params:
            raise dependencies.http_error(
                status_code=400,
                trace_id=trace,
                code="invalid_request",
                message="Discord identity reads require testing and accept no overrides.",
            )
        authorize(product, response, identity, record_store)
        store = cast(TestingCredentialStore, record_store)
        profiles = authorize_discord_profiles(dependencies, store, identity)
        return discord_identity.DiscordIdentityResponse(
            trace_id=trace,
            result=discord_identity.read_discord_identity(
                store,
                product,
                profiles=profiles,
                trace_id=trace,
                actor=getattr(identity, "subject", getattr(identity, "login", "")),
            ),
        )

    app.add_api_route(
        discord_identity.ROUTE,
        identify_discord,
        methods=["GET"],
        response_model=discord_identity.DiscordIdentityResponse,
        operation_id="read_testing_discord_identity",
        summary="Identify the stored testing Discord bot without credential output or qualification",
        responses={
            code: {"model": dependencies.error_response_model} for code in (400, 401, 403, 404, 503)
        },
    )
