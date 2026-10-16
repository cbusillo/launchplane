"""Value-free review and metadata correction for existing config-parameter pointers."""

from collections.abc import Callable
from typing import Annotated, Literal, cast

import click
from fastapi import Depends
from pydantic import BaseModel, ConfigDict, Field

from control_plane.http_routes.support import ApiRouteRegistrar, ReadRouteDependencies
from control_plane.odoo_post_deploy_http import OdooInstanceOverrideStore
from control_plane.odoo_product_driver_http import resolve_odoo_product_route
from control_plane.odoo_secret_references import (
    SecretReferencePlan,
    SecretReferenceStore,
    plan_secret_reference_reconciliation,
)
from control_plane.service_auth import (
    AuthorizationTarget,
    LaunchplaneIdentity,
    LocalOperatorIdentity,
    TerminalAgentIdentity,
)
from control_plane.storage.product_authority_bundle import (
    OdooInstanceOverrideConflictError,
    ProductAuthorityBundleStore,
    ProductContextOwnershipError,
    ProductProfileConflictError,
    RuntimeEnvironmentConflictError,
    SecretDeliveryConflictError,
    odoo_instance_override_records_match,
)

ROUTE = "/v1/products/{product}/environments/{environment}/odoo-secret-references/reconcile"


class SecretReferenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["dry-run", "apply"] = "dry-run"
    keys: tuple[str, ...] = Field(min_length=1, max_length=100)
    review_digest: str = ""
    confirmation: str = ""


class SecretReferenceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    result: SecretReferencePlan


def register_secret_reference_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: ReadRouteDependencies,
    mutation_identity: Callable[..., LaunchplaneIdentity],
) -> None:
    def reconcile(
        *,
        product: str,
        environment: str,
        identity: LaunchplaneIdentity,
        record_store: object,
        request: SecretReferenceRequest | None,
    ) -> SecretReferenceResponse:
        trace_id = dependencies.next_trace_id()
        if (
            request is not None
            and request.mode == "apply"
            and isinstance(identity, TerminalAgentIdentity)
        ):
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="terminal_agent_read_only",
                message="Terminal-agent credentials cannot correct secret reference metadata.",
            )
        try:
            profile = resolve_odoo_product_route(
                record_store=record_store, product=product, instance=environment
            )
            lane = next(lane for lane in profile.lanes if lane.instance == environment)
        except (ValueError, click.ClickException, StopIteration) as error:
            raise dependencies.http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="An unambiguous Odoo product lane is required.",
            ) from error
        action = (
            "product_environment.read"
            if request is None
            else ("product_config.apply" if request.mode == "apply" else "product_config.plan")
        )
        if not dependencies.authorization_allows(
            identity=identity,
            action=action,
            product=profile.product,
            context=lane.context,
            target=AuthorizationTarget(scope="instance", instances=(lane.instance,)),
        ):
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Identity cannot reconcile this lane's secret reference metadata.",
            )
        try:
            override_store = cast(OdooInstanceOverrideStore, record_store)
            record = override_store.read_odoo_instance_override_record(
                context_name=lane.context, instance_name=lane.instance
            )
            if (record.context, record.instance) != (lane.context, lane.instance):
                raise ValueError("Override record does not match its lane")
            keys = (
                request.keys
                if request is not None
                else tuple(
                    item.key
                    for item in record.config_parameters
                    if item.value.source == "secret_binding"
                )
            )
            plan, bundle = plan_secret_reference_reconciliation(
                store=cast(SecretReferenceStore, record_store),
                profile=profile,
                record=record,
                keys=keys,
            )
            if request is not None and request.mode == "apply":
                if request.confirmation != f"APPLY {profile.product}/{lane.instance}":
                    raise ValueError("Exact lane confirmation is required")
                if request.review_digest != plan.review_digest:
                    raise OdooInstanceOverrideConflictError("Review no longer matches authority")
                if bundle.requires_write():
                    if isinstance(identity, LocalOperatorIdentity):
                        bundle = bundle.model_copy(
                            update={"required_context_owner": (profile.product, lane.context)}
                        )
                    cast(ProductAuthorityBundleStore, record_store).write_product_authority_bundle(
                        bundle
                    )
                stored = override_store.read_odoo_instance_override_record(
                    context_name=lane.context, instance_name=lane.instance
                )
                expected = (
                    bundle.odoo_instance_override_writes[0].record
                    if bundle.requires_write()
                    else record
                )
                if not odoo_instance_override_records_match(stored, expected):
                    raise OdooInstanceOverrideConflictError("Correction readback changed")
                plan = plan.model_copy(
                    update={"applied": bundle.requires_write(), "read_back_matches": True}
                )
        except ProductContextOwnershipError as error:
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="local_operator_lane_scope_required",
                message="The context must belong to the named product only.",
            ) from error
        except FileNotFoundError as error:
            raise dependencies.http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message="The lane's override record was not found.",
            ) from error
        except (
            OdooInstanceOverrideConflictError,
            ProductProfileConflictError,
            RuntimeEnvironmentConflictError,
            SecretDeliveryConflictError,
        ) as error:
            raise dependencies.http_error(
                status_code=409,
                trace_id=trace_id,
                code="stale_review",
                message="Lane authority changed. Read back the record and review a fresh plan.",
            ) from error
        except (ValueError, click.ClickException) as error:
            raise dependencies.http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Existing unambiguous secret references and lane delivery metadata are required.",
            ) from error
        return SecretReferenceResponse(trace_id=trace_id, result=plan)

    def read_secret_reference_plan(
        product: str,
        environment: str,
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
    ) -> SecretReferenceResponse:
        return reconcile(
            product=product,
            environment=environment,
            identity=identity,
            record_store=record_store,
            request=None,
        )

    def reconcile_secret_references(
        product: str,
        environment: str,
        envelope: SecretReferenceRequest,
        identity: Annotated[LaunchplaneIdentity, Depends(mutation_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
    ) -> SecretReferenceResponse:
        return reconcile(
            product=product,
            environment=environment,
            identity=identity,
            record_store=record_store,
            request=envelope,
        )

    for endpoint, method, operation in (
        (read_secret_reference_plan, "GET", "read_odoo_secret_reference_plan"),
        (reconcile_secret_references, "POST", "reconcile_odoo_secret_references"),
    ):
        app.add_api_route(
            ROUTE,
            endpoint,
            methods=[method],
            response_model=SecretReferenceResponse,
            operation_id=operation,
            responses={
                code: {"model": dependencies.error_response_model}
                for code in (400, 401, 403, 404, 409)
            },
        )
