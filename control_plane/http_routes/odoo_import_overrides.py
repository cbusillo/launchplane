"""Testing-only import reconciliation, with inert review and a separate record apply."""

from typing import Annotated, Literal, cast
from collections.abc import Callable

import click
from fastapi import Depends
from pydantic import BaseModel, ConfigDict, Field

from control_plane.contracts.odoo_import_parameters import ODOO_IMPORT_PARAMETER_KEYS
from control_plane.http_routes.support import ApiRouteRegistrar, ReadRouteDependencies
from control_plane.odoo_import_overrides import (
    ImportOverridePlan,
    ImportRuntimeStore,
    plan_import_override_reconciliation,
)
from control_plane.odoo_post_deploy_http import OdooInstanceOverrideStore
from control_plane.odoo_product_driver_http import resolve_odoo_product_route
from control_plane.runtime_key_safety import runtime_key_safety_environment_class
from control_plane.service_auth import (
    AuthorizationTarget,
    LaunchplaneIdentity,
    TerminalAgentIdentity,
)
from control_plane.storage.product_authority_bundle import (
    OdooInstanceOverrideConflictError,
    ProductAuthorityBundleStore,
    ProductProfileConflictError,
    RuntimeEnvironmentConflictError,
)

ROUTE = "/v1/products/{product}/environments/{environment}/odoo-import-overrides/reconcile"


class ImportOverrideReconcileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["dry-run", "apply"] = "dry-run"
    keys: tuple[str, ...] = Field(min_length=1, max_length=len(ODOO_IMPORT_PARAMETER_KEYS))
    review_digest: str = ""
    confirmation: str = ""


class ImportOverrideReconcileResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    result: ImportOverridePlan


def register_import_override_reconcile_routes(
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
        request: ImportOverrideReconcileRequest | None,
    ) -> ImportOverrideReconcileResponse:
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
                message="Terminal-agent credentials cannot apply import override changes.",
            )
        try:
            profile = resolve_odoo_product_route(
                record_store=record_store, product=product, instance=environment
            )
            lane = next(lane for lane in profile.lanes if lane.instance == environment)
            if runtime_key_safety_environment_class(lane.instance) != "testing":
                raise ValueError("Import reconciliation supports testing lanes only")
        except (ValueError, click.ClickException, StopIteration) as error:
            raise dependencies.http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="An unambiguous Odoo testing lane is required.",
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
                message="Identity cannot reconcile this lane's import overrides.",
            )
        try:
            record = cast(
                OdooInstanceOverrideStore, record_store
            ).read_odoo_instance_override_record(
                context_name=lane.context, instance_name=lane.instance
            )
            if (record.context, record.instance) != (lane.context, lane.instance):
                raise ValueError("Override record does not match its lane")
            keys = (
                request.keys
                if request is not None
                else tuple(
                    sorted(
                        override.key
                        for override in record.config_parameters
                        if override.key in ODOO_IMPORT_PARAMETER_KEYS
                    )
                )
            )
            plan, bundle = plan_import_override_reconciliation(
                record_store=cast(ImportRuntimeStore, record_store),
                profile=profile,
                record=record,
                keys=keys,
            )
            if request is not None and request.mode == "apply":
                if request.confirmation != f"APPLY {profile.product}/{lane.instance}":
                    raise ValueError("Exact testing-lane apply confirmation is required")
                if request.review_digest != plan.review_digest:
                    raise OdooInstanceOverrideConflictError("Review no longer matches authority")
                cast(ProductAuthorityBundleStore, record_store).write_product_authority_bundle(
                    bundle
                )
                plan = plan.model_copy(update={"applied": True, "live_sync_required": True})
        except FileNotFoundError as error:
            raise dependencies.http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message="The lane's override record was not found.",
            ) from error
        except (
            OdooInstanceOverrideConflictError,
            RuntimeEnvironmentConflictError,
            ProductProfileConflictError,
        ) as error:
            raise dependencies.http_error(
                status_code=409,
                trace_id=trace_id,
                code="stale_review",
                message="Lane authority changed. Review a fresh dry run.",
            ) from error
        except (ValueError, click.ClickException) as error:
            raise dependencies.http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Supported non-secret keys and current lane authority are required.",
            ) from error
        return ImportOverrideReconcileResponse(trace_id=trace_id, result=plan)

    def read_import_override_reconciliation(
        product: str,
        environment: str,
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
    ) -> ImportOverrideReconcileResponse:
        return reconcile(
            product=product,
            environment=environment,
            identity=identity,
            record_store=record_store,
            request=None,
        )

    def reconcile_import_overrides(
        product: str,
        environment: str,
        envelope: ImportOverrideReconcileRequest,
        identity: Annotated[LaunchplaneIdentity, Depends(mutation_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
    ) -> ImportOverrideReconcileResponse:
        return reconcile(
            product=product,
            environment=environment,
            identity=identity,
            record_store=record_store,
            request=envelope,
        )

    for endpoint, method, operation in (
        (read_import_override_reconciliation, "GET", "read_odoo_import_override_reconciliation"),
        (reconcile_import_overrides, "POST", "reconcile_odoo_import_overrides"),
    ):
        app.add_api_route(
            ROUTE,
            endpoint,
            methods=[method],
            response_model=ImportOverrideReconcileResponse,
            operation_id=operation,
            responses={
                code: {"model": dependencies.error_response_model}
                for code in (400, 401, 403, 404, 409)
            },
        )
