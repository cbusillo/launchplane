"""Shared backup credential references and a capture-free provider qualification."""

from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, Header, Path, Query

from control_plane.contracts.production_backup_gate import ProductionBackupGateRequest
from control_plane.contracts.product_profile_record import (
    is_exclusive_product_context,
    product_context_owner_map,
)
from control_plane.http_routes.mutation_support import (
    AcceptedEvidenceResponse,
    accepted_evidence_response,
    idempotency_scope,
    replay_idempotent_response,
    request_fingerprint,
)
from control_plane.http_routes.support import ApiRouteRegistrar, ReadRouteDependencies
from control_plane.production_backup_credentials import (
    BackupCredentialBindingRequest,
    credential_metadata,
    credential_options,
    plan_credential_bindings,
)
from control_plane.service_auth import (
    AuthorizationTarget,
    LaunchplaneIdentity,
    TerminalAgentIdentity,
)
from control_plane.storage.postgres import DbOnlyMutationRequest, PostgresRecordStore
from control_plane.storage.product_authority_bundle import (
    SecretCopySourceConflictError,
    ProductProfileConflictError,
    ProductContextOwnershipError,
)
from control_plane import secrets
from control_plane.workflows.production_backup_gate import (
    resolve_production_backup_binding,
    SSH_PRIVATE_KEY,
    SSH_KNOWN_HOSTS,
)
from control_plane.workflows.production_backup_provider import execute_production_backup_provider

CREDENTIAL_ROUTE = "/v1/products/{product}/environments/prod/backup-worker-bindings"
CREDENTIAL_APPLY_ROUTE = CREDENTIAL_ROUTE + "/apply"
PREFLIGHT_ROUTE = "/v1/production-backup-authority/preflight"


def register_production_backup_credential_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: ReadRouteDependencies,
    mutation_identity: Callable[..., LaunchplaneIdentity],
) -> None:
    def require_access(
        identity: LaunchplaneIdentity,
        action: str,
        product: str,
        context: str,
        instance: str,
        trace: str,
    ) -> None:
        if not dependencies.authorization_allows(
            identity=identity,
            action=action,
            product=product,
            context=context,
            target=AuthorizationTarget(scope="instance", instances=(instance,)),
        ):
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace,
                code="authorization_denied",
                message="Caller lacks scoped backup configuration access.",
            )

    def lane(store: object, product: str, trace: str) -> tuple[PostgresRecordStore, str, str]:
        if not isinstance(store, PostgresRecordStore):
            raise dependencies.http_error(
                status_code=503,
                trace_id=trace,
                code="database_storage_required",
                message="Worker rebinding requires database storage.",
            )
        try:
            profile = store.read_product_profile_record(product)
            lanes = [lane for lane in profile.lanes if lane.instance == "prod"]
            if len(lanes) != 1:
                raise ValueError("Product needs one recorded production lane.")
            context, instance = lanes[0].context, lanes[0].instance
            if not is_exclusive_product_context(
                context=context,
                product=product,
                owners=product_context_owner_map(store.list_product_profile_records()),
            ):
                raise ValueError("Production context must belong exclusively to this product.")
            return store, context, instance
        except (ValueError, FileNotFoundError) as error:
            raise dependencies.http_error(
                status_code=409,
                trace_id=trace,
                code="backup_credential_scope_invalid",
                message="The product production lane is unavailable or ambiguous.",
            ) from error

    def read_bindings(
        product: Annotated[str, Path(min_length=1)],
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
    ) -> AcceptedEvidenceResponse:
        trace = dependencies.next_trace_id()
        store, context, instance = lane(record_store, product, trace)
        require_access(identity, "product_environment.read", product, context, instance, trace)
        records, bindings = credential_metadata(store, context=context, instance=instance)
        return accepted_evidence_response(
            trace_id=trace,
            records={},
            result={
                "product": product,
                "context": context,
                "instance": instance,
                "credentials": credential_options(records, bindings),
            },
        )

    def rebind(
        product: Annotated[str, Path(min_length=1)],
        body: BackupCredentialBindingRequest,
        identity: Annotated[LaunchplaneIdentity, Depends(mutation_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        trace = dependencies.next_trace_id()
        store, context, instance = lane(record_store, product, trace)
        if isinstance(identity, TerminalAgentIdentity):
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace,
                code="authorization_denied",
                message="Terminal agent credentials cannot rebind worker credentials.",
            )
        require_access(
            identity,
            "product_config.apply" if body.mode == "apply" else "product_config.plan",
            product,
            context,
            instance,
            trace,
        )
        # Config access already permits creating these exact-lane worker bindings.
        fingerprint = request_fingerprint(
            {"product": product, "request": body.model_dump(mode="json")}
        )
        scope = idempotency_scope(identity)
        if body.mode == "apply":
            if not idempotency_key.strip():
                raise dependencies.http_error(
                    status_code=400,
                    trace_id=trace,
                    code="idempotency_key_required",
                    message="Apply requires the original operation key.",
                )
            replay = store.prepare_db_only_mutation(
                scope=scope,
                route_path=CREDENTIAL_APPLY_ROUTE,
                idempotency_key=idempotency_key,
                request_fingerprint=fingerprint,
            )
            if replay.status == "replayed":
                assert replay.record is not None
                return replay_idempotent_response(
                    trace_id=trace, stored_record=replay.record, route_path=CREDENTIAL_APPLY_ROUTE
                )
            if replay.status not in {"missing", "released"}:
                raise dependencies.http_error(
                    status_code=409,
                    trace_id=trace,
                    code="idempotency_key_reused",
                    message="Keep the original request and operation key.",
                )
        profile = store.read_product_profile_record(product)
        records, bindings = credential_metadata(store, context=context, instance=instance)
        try:
            plan, replacements = plan_credential_bindings(profile, records, bindings, body)
        except ValueError as error:
            raise dependencies.http_error(
                status_code=409,
                trace_id=trace,
                code="backup_credential_conflict",
                message=str(error),
            ) from error
        response = accepted_evidence_response(
            trace_id=trace, records={}, result={**plan, "applied": body.mode == "apply"}
        )
        if body.mode == "dry-run":
            return response
        if body.reviewed_plan_sha256 != plan["plan_sha256"]:
            # Recheck same-key completion before classifying stale review.
            replay = store.prepare_db_only_mutation(
                scope=scope,
                route_path=CREDENTIAL_APPLY_ROUTE,
                idempotency_key=idempotency_key,
                request_fingerprint=fingerprint,
            )
            if replay.status == "replayed":
                assert replay.record is not None
                return replay_idempotent_response(
                    trace_id=trace, stored_record=replay.record, route_path=CREDENTIAL_APPLY_ROUTE
                )
            raise dependencies.http_error(
                status_code=409,
                trace_id=trace,
                code="backup_credential_conflict",
                message="Worker records changed; review a fresh dry run.",
            )
        try:
            status, receipt = store.compare_and_rebind_worker_credentials(
                expected_profile=profile,
                context=context,
                instance=instance,
                expected_records=records,
                expected_bindings=bindings,
                replacements=replacements,
                reason=body.reason,
                mutation=DbOnlyMutationRequest(
                    scope=scope,
                    route_path=CREDENTIAL_APPLY_ROUTE,
                    idempotency_key=idempotency_key,
                    request_fingerprint=fingerprint,
                    lease_owner=trace,
                    response_status_code=202,
                    response_trace_id=trace,
                    response_payload=response.model_dump(mode="json"),
                ),
            )
        except (
            SecretCopySourceConflictError,
            ProductProfileConflictError,
            ProductContextOwnershipError,
        ) as error:
            raise dependencies.http_error(
                status_code=409,
                trace_id=trace,
                code="backup_credential_conflict",
                message="Worker records changed before commit; review again.",
            ) from error
        if status == "replayed":
            return replay_idempotent_response(
                trace_id=trace, stored_record=receipt, route_path=CREDENTIAL_APPLY_ROUTE
            )
        if status != "written":
            raise dependencies.http_error(
                status_code=409,
                trace_id=trace,
                code="mutation_in_progress",
                message="Keep the original request and operation key.",
            )
        return response

    def preflight(
        product: Annotated[str, Query(min_length=1)],
        context: Annotated[str, Query(min_length=1)],
        instance: Annotated[str, Query(min_length=1)],
        promotion_action: Annotated[str, Query(min_length=1)],
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
    ) -> AcceptedEvidenceResponse:
        trace = dependencies.next_trace_id()
        require_access(
            identity, "production_backup_authority.read", product, context, instance, trace
        )
        store, expected_context, expected_instance = lane(record_store, product, trace)
        if (context, instance) != (expected_context, expected_instance):
            raise dependencies.http_error(
                status_code=409,
                trace_id=trace,
                code="backup_credential_scope_invalid",
                message="Use the product's recorded production lane.",
            )
        try:
            binding = resolve_production_backup_binding(
                store,
                ProductionBackupGateRequest(
                    product=product,
                    context=context,
                    instance=instance,
                    promotion_action=promotion_action,
                    backup_record_id="read-only-preflight",
                    timeout_seconds=30,
                ),
            )
            records, bindings = credential_metadata(store, context=context, instance=instance)
            for key in (SSH_PRIVATE_KEY, SSH_KNOWN_HOSTS):
                if (
                    len(
                        [
                            o
                            for o in credential_options(records, bindings)
                            if o["binding_key"] == key
                        ]
                    )
                    != 1
                ):
                    raise ValueError("Shared backup credentials are missing or ambiguous.")
            values = secrets.resolve_secret_values_for_integration_from_store(
                record_store=store,
                integration=secrets.LAUNCHPLANE_WORKER_SECRET_INTEGRATION,
                context_name=context,
                instance_name=instance,
                scopes=frozenset({"context_instance"}),
            )
            result = execute_production_backup_provider(
                binding,
                ssh_private_key=values.get(SSH_PRIVATE_KEY, ""),
                ssh_known_hosts=values.get(SSH_KNOWN_HOSTS, ""),
                preflight_only=True,
            )
            current = resolve_production_backup_binding(store, binding.request)
            current_records, current_bindings = credential_metadata(
                store, context=context, instance=instance
            )
            unchanged = (
                current == binding and records == current_records and bindings == current_bindings
            )
            return accepted_evidence_response(
                trace_id=trace,
                records={},
                result={
                    "status": result.status if unchanged else "fail",
                    "error_code": result.error_code if unchanged else "backup_authority_changed",
                    "policy_record_id": binding.policy.record_id,
                    "policy_revision": binding.policy.policy_revision,
                    "boundary_status": result.evidence.get("boundary_status", "unverified")
                    if unchanged
                    else "unverified",
                    "storage_status": result.evidence.get("storage_status", "unverified")
                    if unchanged
                    else "unverified",
                    "finished_at": result.finished_at,
                    "capture_started": False,
                },
            )
        except (ValueError, FileNotFoundError) as error:
            raise dependencies.http_error(
                status_code=409,
                trace_id=trace,
                code="backup_preflight_unavailable",
                message="Backup authority or exact-lane worker credentials are unavailable.",
            ) from error

    app.add_api_route(
        CREDENTIAL_ROUTE,
        read_bindings,
        methods=["GET"],
        response_model=AcceptedEvidenceResponse,
        operation_id="read_backup_worker_bindings",
    )
    app.add_api_route(
        CREDENTIAL_APPLY_ROUTE,
        rebind,
        methods=["POST"],
        response_model=AcceptedEvidenceResponse,
        operation_id="rebind_backup_worker_credentials",
        status_code=202,
    )
    app.add_api_route(
        PREFLIGHT_ROUTE,
        preflight,
        methods=["GET"],
        response_model=AcceptedEvidenceResponse,
        operation_id="qualify_production_backup_preflight",
    )
