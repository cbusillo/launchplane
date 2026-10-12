"""Signed-in admin review/apply of shared-web profile declarations."""

from collections.abc import Callable
from typing import Annotated, NoReturn

from fastapi import Depends, Header, Path

from control_plane.http_routes.mutation_support import (
    AcceptedEvidenceResponse,
    accepted_evidence_response,
    idempotency_scope,
    replay_idempotent_response,
    request_fingerprint,
)
from control_plane.http_routes.support import (
    ApiRouteRegistrar,
    ReadRouteDependencies,
    LAUNCHPLANE_SERVICE_CONTEXT,
)
from control_plane.product_generic_web_transition import (
    ProductGenericWebTransitionRequest,
    build_generic_web_transition_plan,
)
from control_plane.service_auth import (
    GitHubHumanIdentity,
    LaunchplaneIdentity,
    LocalOperatorIdentity,
    TerminalAgentIdentity,
)
from control_plane.storage.postgres import DbOnlyMutationRequest, PostgresRecordStore
from control_plane.workflows.ship import utc_now_timestamp

ROUTE = "/v1/product-profiles/{product}/generic-web-transition"


def register_product_generic_web_transition_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: ReadRouteDependencies,
    mutation_identity: Callable[..., LaunchplaneIdentity],
) -> None:
    def transition(
        product: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        body: ProductGenericWebTransitionRequest,
        identity: Annotated[LaunchplaneIdentity, Depends(mutation_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        trace = dependencies.next_trace_id()

        def refusal(status: int, code: str, message: str) -> NoReturn:
            raise dependencies.http_error(
                status_code=status, trace_id=trace, code=code, message=message
            )

        admin_write = (
            isinstance(identity, GitHubHumanIdentity)
            and identity.role == "admin"
            and dependencies.authorization_allows(
                identity=identity,
                action="product_profile.write",
                product=product,
                context=LAUNCHPLANE_SERVICE_CONTEXT,
            )
        )
        agent_plan = (
            body.mode == "dry-run"
            and isinstance(identity, LocalOperatorIdentity | TerminalAgentIdentity)
            and product
            in dependencies.read_role_products(
                identity=identity, context=LAUNCHPLANE_SERVICE_CONTEXT
            )
            and dependencies.authorization_allows(
                identity=identity,
                action="product_profile.plan",
                product=product,
                context=LAUNCHPLANE_SERVICE_CONTEXT,
            )
        )
        if not admin_write and not agent_plan:
            refusal(
                403, "authorization_denied", "Caller cannot plan or apply this product transition."
            )
        if not isinstance(record_store, PostgresRecordStore):
            refusal(
                503, "database_storage_required", "Shared-web transitions require database storage."
            )
        assert isinstance(record_store, PostgresRecordStore)
        key = idempotency_key.strip()
        fingerprint = request_fingerprint(
            {"route": ROUTE, "product": product, "request": body.model_dump(mode="json")}
        )

        def prepare_apply() -> AcceptedEvidenceResponse | None:
            preflight = record_store.prepare_db_only_mutation(
                scope=idempotency_scope(identity),
                route_path=ROUTE,
                idempotency_key=key,
                request_fingerprint=fingerprint,
            )
            if preflight.status == "replayed":
                assert preflight.record is not None
                return replay_idempotent_response(
                    trace_id=trace, stored_record=preflight.record, route_path=ROUTE
                )
            if preflight.status == "conflict":
                refusal(409, "idempotency_key_reused", "This key belongs to another request.")
            if preflight.status == "in_progress":
                refusal(409, "mutation_in_progress", "Keep the original operation key and retry.")
            if preflight.status == "reconcile_required":
                refusal(409, "mutation_reconciliation_required", "Check the original operation.")
            if preflight.status not in {"missing", "released"}:
                raise RuntimeError("Unsupported transition mutation preflight status.")
            return None

        if body.mode == "apply":
            if not key:
                refusal(400, "idempotency_key_required", "Apply requires an Idempotency-Key.")
            replay = prepare_apply()
            if replay is not None:
                return replay
        try:
            profile = record_store.read_product_profile_record(product)
        except FileNotFoundError:
            refusal(404, "not_found", "Product profile was not found.")
        try:
            plan, replacement = build_generic_web_transition_plan(profile, body)
        except ValueError as error:
            refusal(400, "invalid_generic_web_transition", str(error))
        if body.mode == "dry-run":
            return accepted_evidence_response(
                trace_id=trace,
                records={"product_profile": product},
                result=plan.model_dump(mode="json"),
            )
        if body.reviewed_plan_sha256 != plan.plan_sha256:
            # A same-key original can commit between preflight and this read.
            replay = prepare_apply()
            if replay is not None:
                return replay
            refusal(409, "stale", "The profile or reviewed request changed. Preview again.")
        if plan.changed:
            replacement = replacement.model_copy(
                update={
                    "updated_at": utc_now_timestamp(),
                    "source": "service:product-generic-web-transition",
                }
            )
        response = accepted_evidence_response(
            trace_id=trace,
            records={"product_profile": product},
            result=plan.model_copy(update={"applied": True}).model_dump(mode="json"),
        )
        result = record_store.compare_and_write_product_profile_record(
            expected_record=profile,
            replacement_record=replacement,
            mutation=DbOnlyMutationRequest(
                scope=idempotency_scope(identity),
                route_path=ROUTE,
                idempotency_key=key,
                request_fingerprint=fingerprint,
                lease_owner=trace,
                response_status_code=202,
                response_trace_id=trace,
                response_payload=response.model_dump(mode="json", exclude_none=True),
                lease_seconds=300,
            ),
        )
        if result.status == "replayed":
            assert result.idempotency_record is not None
            return replay_idempotent_response(
                trace_id=trace, stored_record=result.idempotency_record, route_path=ROUTE
            )
        failures = {
            "idempotency_conflict": (
                409,
                "idempotency_key_reused",
                "This key belongs to another request.",
            ),
            "missing": (404, "not_found", "Product profile disappeared before apply."),
            "changed": (409, "stale", "Product profile changed during apply. Preview again."),
            "reservation_in_progress": (
                409,
                "mutation_in_progress",
                "Keep the original operation key and retry.",
            ),
            "reconciliation_required": (
                409,
                "mutation_reconciliation_required",
                "Check the original operation.",
            ),
        }
        if result.status in failures:
            refusal(*failures[result.status])
        if result.status != "written":
            raise RuntimeError("Unsupported transition compare-and-write status.")
        return response

    app.add_api_route(
        ROUTE,
        transition,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        operation_id="apply_product_generic_web_transition",
        summary="Preview or change a product shared-web driver declaration",
        responses={
            code: {"model": dependencies.error_response_model}
            for code in (400, 401, 403, 404, 409, 503)
        },
    )
