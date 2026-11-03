from control_plane.contracts.durable_operation_authorization import DurableOperationAuthorization
from control_plane.contracts.testing_rebuild import REBUILD_ROUTE, REBUILD_SCOPE
from control_plane.contracts.odoo_stable_target_replacement import (
    OdooStableTargetReplacementApplyRequest,
)
from control_plane.contracts.odoo_stable_target_replacement_operation import (
    OdooStableTargetReplacementOperationRecord,
)
from control_plane.durable_operation_authorization import (
    DurableOperationAuthorizationCaptureError,
    durable_operation_caller_identity,
)
from control_plane.launchplane_reconcile_authorization import TESTING_TARGET_REPLACEMENT_ACTION
from control_plane.contracts.product_profile_record import (
    is_exclusive_product_context,
    product_context_owner_map,
)
from control_plane.testing_rebuild_driver import (
    resolve_testing_rebuild_driver,
    TestingRebuildRefused,
)
from control_plane.service_auth import (
    BearerIdentityConfig,
    LaunchplaneIdentity,
    LocalOperatorIdentity,
    configured_local_operator_identity,
    director_agent_identity,
)
from control_plane.storage.postgres import PostgresRecordStore


def director_testing_rebuild_allowed(
    *,
    identity: LaunchplaneIdentity,
    request: OdooStableTargetReplacementApplyRequest,
    context: str,
    store: object,
) -> bool:
    if (
        not isinstance(identity, LocalOperatorIdentity)
        or not director_agent_identity(identity)
        or request.instance != "testing"
        or request.strategy != "replace-and-cutover"
        or not isinstance(store, PostgresRecordStore)
    ):
        return False
    try:
        profile = store.read_product_profile_record(request.product)
        if profile.driver_id == "odoo":
            return (
                profile.is_active
                and any(
                    lane.context == context and lane.instance == "testing" for lane in profile.lanes
                )
                and is_exclusive_product_context(
                    context=context,
                    product=request.product,
                    owners=product_context_owner_map(store.list_product_profile_records()),
                )
            )
        driver = resolve_testing_rebuild_driver(store, request.product)
    except (FileNotFoundError, TestingRebuildRefused):
        return False
    return driver.execution_available and driver.lane.context == context


def capture_director_testing_rebuild_authorization(
    *,
    identity: LaunchplaneIdentity,
    request: OdooStableTargetReplacementApplyRequest,
    context: str,
    store: object,
    authorized_at: str,
    recovery_operation: OdooStableTargetReplacementOperationRecord | None = None,
) -> DurableOperationAuthorization:
    if not director_testing_rebuild_allowed(
        identity=identity, request=request, context=context, store=store
    ) and not (
        recovery_operation is not None
        and recovery_operation.request == request
        and recovery_operation.context == context
        and director_testing_rebuild_recovery_allowed(
            identity=identity, operation=recovery_operation, store=store
        )
    ):
        raise DurableOperationAuthorizationCaptureError(
            "Standing native testing rebuild authority is unavailable."
        )
    return DurableOperationAuthorization(
        grant="director_testing_rebuild",
        action=TESTING_TARGET_REPLACEMENT_ACTION,
        product=request.product,
        context=context,
        instances=("testing",),
        caller=durable_operation_caller_identity(identity),
        authorized_at=authorized_at,
    )


def director_testing_rebuild_authorization_allows(
    *,
    authorization: DurableOperationAuthorization,
    operation: OdooStableTargetReplacementOperationRecord,
    store: object,
    config: BearerIdentityConfig,
) -> bool:
    identity = configured_local_operator_identity(config)
    return (
        identity is not None
        and authorization.grant == "director_testing_rebuild"
        and authorization.action == TESTING_TARGET_REPLACEMENT_ACTION
        and authorization.product == operation.product
        and authorization.context == operation.context
        and authorization.instances == (operation.instance,) == ("testing",)
        and authorization.caller == durable_operation_caller_identity(identity)
        and (
            director_testing_rebuild_allowed(
                identity=identity, request=operation.request, context=operation.context, store=store
            )
            or director_testing_rebuild_recovery_allowed(
                identity=identity, operation=operation, store=store
            )
        )
    )


def director_testing_rebuild_owner_allowed(
    *,
    identity: LaunchplaneIdentity,
    operation: OdooStableTargetReplacementOperationRecord,
    store: object,
) -> bool:
    authorization = operation.authorization
    return (
        authorization is not None
        and authorization.grant == "director_testing_rebuild"
        and authorization.caller == durable_operation_caller_identity(identity)
        and (
            director_testing_rebuild_allowed(
                identity=identity, request=operation.request, context=operation.context, store=store
            )
            or director_testing_rebuild_recovery_allowed(
                identity=identity, operation=operation, store=store
            )
        )
    )


def director_testing_rebuild_recovery_allowed(
    *,
    identity: LaunchplaneIdentity,
    operation: OdooStableTargetReplacementOperationRecord,
    store: object,
) -> bool:
    from control_plane.testing_rebuild_service import GenericWebTestingRebuildIntent

    if (
        not isinstance(identity, LocalOperatorIdentity)
        or not director_agent_identity(identity)
        or not isinstance(store, PostgresRecordStore)
        or not operation.is_native_testing_rebuild
        or operation.authorization is None
        or operation.authorization.caller != durable_operation_caller_identity(identity)
    ):
        return False
    found = store.lookup_existing_mutation_reservation(
        route_path=REBUILD_ROUTE,
        idempotency_key=operation.operation_id,
        request_fingerprint=operation.request_fingerprint,
    )
    row = found.record
    if row is None or found.status != "found" or row.scope != REBUILD_SCOPE:
        return False
    try:
        original = GenericWebTestingRebuildIntent.model_validate(
            store.read_original_mutation_intent(row)
        )
    except (ValueError, FileNotFoundError):
        return False
    return (
        original.operation_id == operation.operation_id
        and original.profile.product == operation.product
        and original.profile.driver_id == "generic-web"
        and original.before.target.context == operation.context
        and original.before.target.target_type == "application"
        and original.state.kind == "none"
        and row.provider_target_key == f"testing-rebuild:{operation.product}:{operation.context}"
        and is_exclusive_product_context(
            context=operation.context,
            product=operation.product,
            owners=product_context_owner_map(store.list_product_profile_records()),
        )
    )
