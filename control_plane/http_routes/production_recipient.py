"""Production preparation keeps the existing setup authority and serving binding."""

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated

import click
from fastapi import Depends, Header, Path as PathParameter

from control_plane.contracts.production_recipient import (
    ProductionRecipientPlan,
    ProductionRecipientRequest,
    RecipientRecoveryRequest,
    RecipientRecoveryPlan,
)
from control_plane.http_routes.mutation_support import (
    AcceptedEvidenceResponse,
    accepted_evidence_response,
    idempotency_scope,
    request_fingerprint,
)
from control_plane.http_routes.support import (
    ApiRouteRegistrar,
    LAUNCHPLANE_SERVICE_CONTEXT,
    ReadRouteDependencies,
    run_in_worker_thread,
)
from control_plane.production_recipient import ROUTE, ProductionRecipientAdapter, custody, plan
from control_plane.production_recipient_recovery import RECOVERY_ROUTE, RecipientRecoveryAdapter
from control_plane.service_auth import (
    AuthorizationTarget,
    LaunchplaneIdentity,
    TerminalAgentIdentity,
)
from control_plane.storage.postgres import PostgresRecordStore


def register_production_recipient_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: ReadRouteDependencies,
    mutation_identity: Callable[..., LaunchplaneIdentity],
    actor: Callable[[LaunchplaneIdentity], str],
    root: Path,
    run_provider_mutation: Callable[..., Awaitable[AcceptedEvidenceResponse]],
) -> None:
    async def prepare(
        product: Annotated[str, PathParameter(min_length=1, pattern=r"^[A-Za-z0-9._-]+$")],
        body: ProductionRecipientRequest,
        identity: Annotated[LaunchplaneIdentity, Depends(mutation_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        trace = dependencies.next_trace_id()
        apply = body.mode == "apply"
        allowed = dependencies.authorization_allows(
            identity=identity,
            action="dokploy_target.setup" if apply else "product_profile.read",
            product=product,
            context=LAUNCHPLANE_SERVICE_CONTEXT if apply else body.context,
            target=None if apply else AuthorizationTarget(scope="instance", instances=("prod",)),
        )
        # Match production target setup: terminal identities do not inherit
        # service-scoped creation authority from the disposable testing path.
        if not allowed or (apply and isinstance(identity, TerminalAgentIdentity)):
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace,
                code="authorization_denied",
                message="Existing production target setup authority is required for preparation.",
            )
        if not isinstance(record_store, PostgresRecordStore):
            raise dependencies.http_error(
                status_code=503,
                trace_id=trace,
                code="database_storage_required",
                message="Service-owned production custody is required.",
            )
        scope = idempotency_scope(identity)
        adapter = ProductionRecipientAdapter(
            root,
            record_store,
            product,
            body,
            actor(identity),
            scope,
            trace,
            idempotency_key.strip(),
        )
        try:
            if body.mode == "receipt":
                if not idempotency_key.strip():
                    raise ValueError("The original preparation key is required.")
                row = record_store.read_idempotency_record(
                    scope=scope,
                    route_path=ROUTE.format(product=product),
                    idempotency_key=adapter.execution_key(),
                )
                if row is None or row.state != "completed":
                    raise ValueError("No completed receipt exists for this original key.")
                result = ProductionRecipientPlan.model_validate(row.response_payload.get("result"))
                if (result.product, result.context) != (product, body.context):
                    raise ValueError("The receipt belongs to another lane.")
                return accepted_evidence_response(
                    trace_id=trace, records={}, result=result.model_dump(mode="json")
                )
            if apply:
                if record_store.database_dialect_name != "postgresql":
                    raise dependencies.http_error(
                        status_code=503,
                        trace_id=trace,
                        code="postgresql_required",
                        message="Production preparation requires cross-process execution and authority guards.",
                    )
                if not idempotency_key.strip() or not body.reviewed_plan_sha256:
                    raise dependencies.http_error(
                        status_code=400,
                        trace_id=trace,
                        code="review_required",
                        message="Apply requires the saved plan and original stable key.",
                    )
                return await run_provider_mutation(
                    record_store=record_store,
                    identity=identity,
                    route_path=ROUTE.format(product=product),
                    idempotency_key=adapter.execution_key(),
                    request_fingerprint=request_fingerprint(
                        {"product": product, **body.model_dump(mode="json")}
                    ),
                    trace_id=trace,
                    adapter=adapter,
                    in_progress_message="Production preparation remains under its original key.",
                    reconcile_message="Retain both recipients and the original key; do not activate or delete anything.",
                )

            def read() -> dict[str, object]:
                if body.mode == "custody":
                    retained = custody(record_store, product, body.context)
                    if retained is None:
                        raise ValueError("No retained preparation exists for this product lane.")
                    return plan(retained)
                record = adapter.current()
                adapter.require_unchanged(record)
                if body.mode == "readback":
                    if (
                        custody(record_store, product, body.context) is None
                        or not record.qualified_at
                    ):
                        raise ValueError("A completed qualified recipient is required.")
                    adapter.verify(record)
                return plan(record)

            result_payload = await run_in_worker_thread(read)
            return accepted_evidence_response(trace_id=trace, records={}, result=result_payload)
        except (
            ValueError,
            FileNotFoundError,
            RuntimeError,
            OSError,
            click.ClickException,
        ) as error:
            raise dependencies.http_error(
                status_code=409,
                trace_id=trace,
                code="production_recipient_unverified",
                message="Preparation or readback refused; retain both recipients, the old binding and the original key.",
            ) from error

    app.add_api_route(
        ROUTE,
        prepare,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        operation_id="prepare_independent_production_recipient",
        summary="Prepare a fresh private production recipient without changing the serving binding",
        responses={
            code: {"model": dependencies.error_response_model} for code in (400, 401, 403, 409, 503)
        },
    )

    async def recover(
        product: Annotated[str, PathParameter(min_length=1, pattern=r"^[A-Za-z0-9._-]+$")],
        body: RecipientRecoveryRequest,
        identity: Annotated[LaunchplaneIdentity, Depends(mutation_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        trace = dependencies.next_trace_id()
        apply = body.mode == "apply"
        allowed = dependencies.authorization_allows(
            identity=identity,
            action="dokploy_target.setup" if apply else "product_profile.read",
            product=product,
            context=LAUNCHPLANE_SERVICE_CONTEXT if apply else body.context,
            target=None if apply else AuthorizationTarget(scope="instance", instances=("prod",)),
        )
        if not allowed or (apply and isinstance(identity, TerminalAgentIdentity)):
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace,
                code="authorization_denied",
                message="Existing production setup authority is required.",
            )
        if not isinstance(record_store, PostgresRecordStore):
            raise dependencies.http_error(
                status_code=503,
                trace_id=trace,
                code="database_storage_required",
                message="Service-owned custody is required.",
            )
        adapter = RecipientRecoveryAdapter(
            root,
            record_store,
            product,
            body,
            actor(identity),
            idempotency_scope(identity),
            trace,
            idempotency_key.strip(),
        )
        try:
            if body.mode == "receipt":
                row = record_store.read_idempotency_record(
                    scope=adapter.scope,
                    route_path=RECOVERY_ROUTE.format(product=product),
                    idempotency_key=idempotency_key.strip(),
                )
                if not idempotency_key.strip() or row is None or row.state != "completed":
                    raise ValueError("The completed original recovery receipt is required.")
                result = RecipientRecoveryPlan.model_validate(row.response_payload.get("result"))
                if (result.product, result.context, result.preparation_plan_sha256) != (
                    product,
                    body.context,
                    body.preparation_plan_sha256,
                ):
                    raise ValueError("The receipt belongs to another preparation.")
                return accepted_evidence_response(
                    trace_id=trace, records={}, result=result.model_dump(mode="json")
                )
            if apply:
                if record_store.database_dialect_name != "postgresql":
                    raise dependencies.http_error(
                        status_code=503,
                        trace_id=trace,
                        code="postgresql_required",
                        message="Recovery requires cross-process guards.",
                    )
                if not idempotency_key.strip() or not body.reviewed_plan_sha256:
                    raise ValueError("Recovery requires saved review and its original key.")
                return await run_provider_mutation(
                    record_store=record_store,
                    identity=identity,
                    route_path=RECOVERY_ROUTE.format(product=product),
                    idempotency_key=idempotency_key.strip(),
                    request_fingerprint=request_fingerprint(
                        {"product": product, **body.model_dump(mode="json")}
                    ),
                    trace_id=trace,
                    adapter=adapter,
                    in_progress_message="Recovery remains under its original key.",
                    reconcile_message="Retain both recipients, capture and original key.",
                )

            def read_recovery() -> dict[str, object]:
                _, result = adapter.current()
                if body.mode == "readback" and not result.recovered:
                    raise ValueError("No isolated recovery has completed.")
                return result.model_dump(mode="json")

            return accepted_evidence_response(
                trace_id=trace, records={}, result=await run_in_worker_thread(read_recovery)
            )
        except (
            ValueError,
            FileNotFoundError,
            RuntimeError,
            OSError,
            click.ClickException,
        ) as error:
            raise dependencies.http_error(
                status_code=409,
                trace_id=trace,
                code="recipient_recovery_unverified",
                message="Recovery unverified; retain original custody and key.",
            ) from error

    app.add_api_route(
        RECOVERY_ROUTE,
        recover,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        operation_id="qualify_production_recipient_recovery",
        summary="Capture and prove isolated cold recipient recovery without live outbound connections",
        responses={
            code: {"model": dependencies.error_response_model} for code in (400, 401, 403, 409, 503)
        },
    )

    from control_plane.production_recipient_continuation import (
        CONTINUATION_ROUTE,
        RecipientContinuationRequest,
        continuation_plan,
        supersede_recipient,
    )
    from control_plane.production_recipient_cutover import (
        CUTOVER_ROUTE,
        RecipientCutoverRequest,
        cutover_intent,
        save_cutover_intent,
    )

    def require_custody_access(
        identity: LaunchplaneIdentity,
        product: str,
        context: str,
        apply: bool,
        trace: str,
        store: object,
    ) -> PostgresRecordStore:
        allowed = dependencies.authorization_allows(
            identity=identity,
            action="dokploy_target.setup" if apply else "product_profile.read",
            product=product,
            context=LAUNCHPLANE_SERVICE_CONTEXT if apply else context,
            target=None if apply else AuthorizationTarget(scope="instance", instances=("prod",)),
        )
        if not allowed or (apply and isinstance(identity, TerminalAgentIdentity)):
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace,
                code="authorization_denied",
                message="Existing production setup authority is required.",
            )
        if not isinstance(store, PostgresRecordStore) or (
            apply and store.database_dialect_name != "postgresql"
        ):
            raise dependencies.http_error(
                status_code=503,
                trace_id=trace,
                code="postgresql_required",
                message="Service-owned PostgreSQL custody is required.",
            )
        return store

    async def continue_custody(
        product: Annotated[str, PathParameter(min_length=1, pattern=r"^[A-Za-z0-9._-]+$")],
        body: RecipientContinuationRequest,
        identity: Annotated[LaunchplaneIdentity, Depends(mutation_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        trace = dependencies.next_trace_id()
        apply = body.mode == "apply"
        store = require_custody_access(identity, product, body.context, apply, trace, record_store)
        if idempotency_key.strip() != body.original_key:
            raise dependencies.http_error(
                status_code=400,
                trace_id=trace,
                code="original_key_required",
                message="Continuation retains the original preparation key.",
            )

        def reauthorize(_phase: str) -> None:
            require_custody_access(identity, product, body.context, True, trace, store)

        def execute() -> dict[str, object]:
            if apply:
                return supersede_recipient(
                    root,
                    store,
                    product,
                    body,
                    actor(identity),
                    idempotency_scope(identity),
                    reauthorize,
                )
            return continuation_plan(
                root, store, product, body, actor(identity), idempotency_scope(identity)
            )[2]

        try:
            return accepted_evidence_response(
                trace_id=trace, records={}, result=await run_in_worker_thread(execute)
            )
        except (
            ValueError,
            FileNotFoundError,
            RuntimeError,
            OSError,
            click.ClickException,
        ) as error:
            raise dependencies.http_error(
                status_code=409,
                trace_id=trace,
                code="recipient_continuation_unverified",
                message="Retain original custody and every observed or uncertain provider reference.",
            ) from error

    async def arm_cutover(
        product: Annotated[str, PathParameter(min_length=1, pattern=r"^[A-Za-z0-9._-]+$")],
        body: RecipientCutoverRequest,
        identity: Annotated[LaunchplaneIdentity, Depends(mutation_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
    ) -> AcceptedEvidenceResponse:
        trace = dependencies.next_trace_id()
        apply = body.mode == "apply"
        store = require_custody_access(identity, product, body.context, apply, trace, record_store)

        def execute() -> dict[str, object]:
            record = custody(store, product, body.context)
            if record is None or (record.actor, record.scope) != (
                actor(identity),
                idempotency_scope(identity),
            ):
                raise ValueError("Retain the original preparation caller.")
            intent = cutover_intent(root, store, record, body)
            if apply:
                if body.reviewed_plan_sha256 != intent["plan_sha256"]:
                    raise ValueError("Review the exact current cutover intent.")
                save_cutover_intent(store, record, intent)
            elif body.mode == "readback" and record.activation != intent:
                raise ValueError("No matching armed intent is recorded.")
            return {
                "product": product,
                "context": body.context,
                "plan_sha256": intent["plan_sha256"],
                "armed": apply or record.activation == intent,
                "bootstrap_eligible": False,
                "release_required": True,
            }

        try:
            return accepted_evidence_response(
                trace_id=trace, records={}, result=await run_in_worker_thread(execute)
            )
        except (
            ValueError,
            FileNotFoundError,
            RuntimeError,
            OSError,
            click.ClickException,
        ) as error:
            raise dependencies.http_error(
                status_code=409,
                trace_id=trace,
                code="recipient_cutover_unverified",
                message="Cutover intent is unverified; only the accepted-release worker can bootstrap or publish.",
            ) from error

    for path, handler, operation_id, summary in (
        (
            CONTINUATION_ROUTE,
            continue_custody,
            "continue_production_recipient_custody",
            "Review original-key fenced supersession while retaining every old resource",
        ),
        (
            CUTOVER_ROUTE,
            arm_cutover,
            "arm_production_recipient_cutover",
            "Arm exact cutover intent without starting a production release",
        ),
    ):
        app.add_api_route(
            path,
            handler,
            methods=["POST"],
            status_code=202,
            response_model=AcceptedEvidenceResponse,
            response_model_exclude_none=True,
            operation_id=operation_id,
            summary=summary,
            responses={
                code: {"model": dependencies.error_response_model}
                for code in (400, 401, 403, 409, 503)
            },
        )

    from control_plane.production_recipient_writer_control import (
        WRITER_ROUTE,
        RecipientWriterRequest,
        RecipientWriterPlan,
        RecipientWriterAdapter,
    )

    async def testing_writer(
        product: Annotated[str, PathParameter(min_length=1, pattern=r"^[A-Za-z0-9._-]+$")],
        body: RecipientWriterRequest,
        identity: Annotated[LaunchplaneIdentity, Depends(mutation_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        trace = dependencies.next_trace_id()
        apply = body.mode == "apply"

        def authorize() -> None:
            context = body.context
            if apply and isinstance(record_store, PostgresRecordStore):
                binding = next(
                    (
                        row
                        for row in record_store.list_secret_bindings()
                        if row.binding_id == body.binding_id
                    ),
                    None,
                )
                if binding is not None:
                    context = binding.context
            if not dependencies.authorization_allows(
                identity=identity,
                action="generic_web_deploy.execute" if apply else "product_profile.read",
                product=product,
                context=context,
                target=AuthorizationTarget(scope="instance", instances=("testing",)),
            ):
                raise dependencies.http_error(
                    status_code=403,
                    trace_id=trace,
                    code="authorization_denied",
                    message="Existing testing deploy authority is required for writer control.",
                )

        authorize()
        if not isinstance(record_store, PostgresRecordStore) or (
            apply and record_store.database_dialect_name != "postgresql"
        ):
            raise dependencies.http_error(
                status_code=503,
                trace_id=trace,
                code="postgresql_required",
                message="Service-owned custody is required.",
            )
        adapter = RecipientWriterAdapter(root, record_store, product, body, trace, authorize)
        try:
            if body.mode == "receipt":
                row = record_store.read_idempotency_record(
                    scope=idempotency_scope(identity),
                    route_path=WRITER_ROUTE.format(product=product),
                    idempotency_key=idempotency_key.strip(),
                )
                if not idempotency_key.strip() or row is None or row.state != "completed":
                    raise ValueError("The completed original writer-control receipt is required.")
                result = RecipientWriterPlan.model_validate(row.response_payload.get("result"))
                if (
                    result.product,
                    result.context,
                    result.action,
                    result.preparation_plan_sha256,
                    result.binding_id,
                ) != (
                    product,
                    body.context,
                    body.action,
                    body.preparation_plan_sha256,
                    body.binding_id,
                ):
                    raise ValueError("The writer receipt belongs to another action.")
                return accepted_evidence_response(
                    trace_id=trace, records={}, result=result.model_dump(mode="json")
                )
            if apply:
                if not idempotency_key.strip() or not body.reviewed_plan_sha256:
                    raise ValueError("Writer control requires exact review and its original key.")
                return await run_provider_mutation(
                    record_store=record_store,
                    identity=identity,
                    route_path=WRITER_ROUTE.format(product=product),
                    idempotency_key=idempotency_key.strip(),
                    request_fingerprint=request_fingerprint(
                        {"product": product, **body.model_dump(mode="json")}
                    ),
                    trace_id=trace,
                    adapter=adapter,
                    in_progress_message="Testing writer coordination remains under its original key.",
                    reconcile_message="Retain the original key, peer container and writer witness.",
                )

            def read_writer() -> dict[str, object]:
                if body.mode == "readback":
                    from control_plane.production_recipient_writer_control import (
                        retained_writer_receipt,
                    )

                    row = record_store.read_idempotency_record(
                        scope=idempotency_scope(identity),
                        route_path=WRITER_ROUTE.format(product=product),
                        idempotency_key=idempotency_key.strip(),
                    )
                    retained = custody(record_store, product, body.context)
                    if row is None or retained is None:
                        raise ValueError("Readback requires the original writer-control key.")
                    entries = [
                        entry
                        for entry in retained.writer_custody
                        if entry.get("record_id") == row.record_id
                    ]
                    if len(entries) != 1 or any(
                        entries[0]["request"].get(key) != getattr(body, key)
                        for key in ("action", "binding_id", "preparation_plan_sha256")
                    ):
                        raise ValueError("The writer-control key belongs to another request.")
                    return retained_writer_receipt(
                        root, retained, entries[0], lambda _phase: None
                    ).model_dump(mode="json")
                return adapter.current()[2].model_dump(mode="json")

            return accepted_evidence_response(
                trace_id=trace, records={}, result=await run_in_worker_thread(read_writer)
            )
        except (
            ValueError,
            FileNotFoundError,
            RuntimeError,
            OSError,
            click.ClickException,
        ) as error:
            raise dependencies.http_error(
                status_code=409,
                trace_id=trace,
                code="recipient_writer_unverified",
                message="Keep the exact testing writer and its original operation key.",
            ) from error

    app.add_api_route(
        WRITER_ROUTE,
        testing_writer,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        operation_id="coordinate_production_recipient_testing_writer",
        summary="Pause or restore only the recorded unused testing API writer",
        responses={
            code: {"model": dependencies.error_response_model} for code in (400, 401, 403, 409, 503)
        },
    )

    from control_plane.production_recipient_release_recovery import (
        RELEASE_RECOVERY_ROUTE,
        RecipientReleaseRecoveryRequest,
        recover_release_step,
    )

    async def recover_release(
        product: Annotated[str, PathParameter(min_length=1, pattern=r"^[A-Za-z0-9._-]+$")],
        body: RecipientReleaseRecoveryRequest,
        identity: Annotated[LaunchplaneIdentity, Depends(mutation_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        trace = dependencies.next_trace_id()
        apply = body.mode == "apply"
        if not dependencies.authorization_allows(
            identity=identity,
            action=(
                "generic_web_prod_promotion.execute"
                if body.kind == "promote"
                else "generic_web_rollback.execute"
            )
            if apply
            else "product_profile.read",
            product=product,
            context=body.context,
            target=AuthorizationTarget(scope="instance", instances=("prod",)),
        ) or (apply and isinstance(identity, TerminalAgentIdentity)):
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace,
                code="authorization_denied",
                message="Existing production release authority is required for reconciliation.",
            )
        if not isinstance(record_store, PostgresRecordStore) or (
            apply and record_store.database_dialect_name != "postgresql"
        ):
            raise dependencies.http_error(
                status_code=503,
                trace_id=trace,
                code="postgresql_required",
                message="Service-owned release custody is required.",
            )
        try:
            result = await run_in_worker_thread(
                lambda: recover_release_step(
                    root, record_store, product, body, idempotency_key.strip()
                )
            )
            return accepted_evidence_response(
                trace_id=trace, records={}, result=result.model_dump(mode="json")
            )
        except (
            ValueError,
            FileNotFoundError,
            RuntimeError,
            OSError,
            click.ClickException,
        ) as error:
            raise dependencies.http_error(
                status_code=409,
                trace_id=trace,
                code="recipient_release_recovery_unverified",
                message="Retain both recipients and reconcile only the original release step key.",
            ) from error

    app.add_api_route(
        RELEASE_RECOVERY_ROUTE,
        recover_release,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        operation_id="recover_production_recipient_release",
        summary="Reconcile an existing Client release step without changing its operation key",
        responses={
            code: {"model": dependencies.error_response_model} for code in (400, 401, 403, 409, 503)
        },
    )

    from control_plane.production_recipient_inverse import (
        INVERSE_ROUTE,
        RecipientInverseRequest,
        RecipientInversePlan,
        RecipientInverseAdapter,
        inverse_plan,
    )

    async def invert_publication(
        product: Annotated[str, PathParameter(min_length=1, pattern=r"^[A-Za-z0-9._-]+$")],
        body: RecipientInverseRequest,
        identity: Annotated[LaunchplaneIdentity, Depends(mutation_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        trace = dependencies.next_trace_id()
        apply = body.mode == "apply"

        def authorize() -> None:
            if (
                not dependencies.authorization_allows(
                    identity=identity,
                    action="generic_web_rollback.execute" if apply else "product_profile.read",
                    product=product,
                    context=body.context,
                    target=AuthorizationTarget(scope="instance", instances=("prod",)),
                )
                or apply
                and isinstance(identity, TerminalAgentIdentity)
            ):
                raise dependencies.http_error(
                    status_code=403,
                    trace_id=trace,
                    code="authorization_denied",
                    message="Existing production rollback authority is required for the exact inverse.",
                )

        authorize()
        if (
            not isinstance(record_store, PostgresRecordStore)
            or apply
            and record_store.database_dialect_name != "postgresql"
        ):
            raise dependencies.http_error(
                status_code=503,
                trace_id=trace,
                code="postgresql_required",
                message="Service-owned inverse custody is required.",
            )
        try:
            if body.mode == "receipt":
                row = record_store.read_idempotency_record(
                    scope=idempotency_scope(identity),
                    route_path=INVERSE_ROUTE.format(product=product),
                    idempotency_key=idempotency_key.strip(),
                )
                if not idempotency_key.strip() or row is None or row.state != "completed":
                    raise ValueError("Read the completed inverse under its original key.")
                result = RecipientInversePlan.model_validate(row.response_payload.get("result"))
                if (
                    result.product,
                    result.context,
                    result.preparation_plan_sha256,
                    result.publication_sha256,
                ) != (product, body.context, body.preparation_plan_sha256, body.publication_sha256):
                    raise ValueError("The inverse receipt belongs to another publication.")
                return accepted_evidence_response(
                    trace_id=trace, records={}, result=result.model_dump(mode="json")
                )
            record = await run_in_worker_thread(
                lambda: custody(record_store, product, body.context)
            )
            if record is None:
                raise ValueError("The retained publication is required.")
            if apply:
                if not idempotency_key.strip() or not body.reviewed_plan_sha256:
                    raise ValueError("Inverse apply requires exact review and its original key.")
                adapter = RecipientInverseAdapter(
                    root, record_store, record, trace, authorize, request=body
                )
                return await run_provider_mutation(
                    record_store=record_store,
                    identity=identity,
                    route_path=INVERSE_ROUTE.format(product=product),
                    idempotency_key=idempotency_key.strip(),
                    request_fingerprint=request_fingerprint(
                        {"product": product, **body.model_dump(mode="json")}
                    ),
                    trace_id=trace,
                    adapter=adapter,
                    in_progress_message="The exact inverse remains under its original key.",
                    reconcile_message="Retain both recipients, the original inverse key and every writer witness.",
                )
            result = await run_in_worker_thread(lambda: inverse_plan(record_store, record, body))
            if body.mode == "readback" and not result.completed:
                raise ValueError("The exact inverse has not completed.")
            return accepted_evidence_response(
                trace_id=trace, records={}, result=result.model_dump(mode="json")
            )
        except (
            ValueError,
            FileNotFoundError,
            RuntimeError,
            OSError,
            click.ClickException,
        ) as error:
            raise dependencies.http_error(
                status_code=409,
                trace_id=trace,
                code="recipient_inverse_unverified",
                message="Preserve changed authority, both recipients and the original inverse key.",
            ) from error

    app.add_api_route(
        INVERSE_ROUTE,
        invert_publication,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        operation_id="invert_production_recipient_publication",
        summary="Review and apply only the exact retained recipient inverse",
        responses={
            code: {"model": dependencies.error_response_model} for code in (400, 401, 403, 409, 503)
        },
    )
