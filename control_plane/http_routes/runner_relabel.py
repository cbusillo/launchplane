"""Native reviewed exact-runner operations under the caller's own identity."""

from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from fastapi import Depends, Header, Path

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.runner_relabel import (
    RunnerRelabelApplyRequest,
    RunnerRelabelPolicy,
    RunnerRelabelPolicyWrite,
    RunnerRelabelReviewRequest,
    RunnerRelabelOriginalIntent,
)
from control_plane.http_routes.mutation_support import (
    AcceptedEvidenceResponse,
    accepted_evidence_response,
)
from control_plane.http_routes.support import (
    ApiRouteRegistrar,
    ReadRouteDependencies,
    run_in_worker_thread,
)
from control_plane.provider_operations import (
    DurableProviderOperationResult,
    run_durable_provider_operation,
)
from control_plane.runner_relabel import (
    ROUTE,
    REFUSED_BEFORE_DISPATCH, REFUSED_BEFORE_DISPATCH_MESSAGE,
    RunnerRelabelAdapter,
    RunnerRelabelInactive,
    actor_identity,
    actor_scope,
    create_review,
    load_policy,
)
from control_plane.service_auth import (
    GitHubActionsIdentity,
    GitHubHumanIdentity,
    LaunchplaneIdentity,
    LocalOperatorIdentity,
    TerminalAgentIdentity,
)
from control_plane.storage.postgres import PostgresRecordStore

PolicyId = Annotated[str, Path(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")]


def register_runner_relabel_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: ReadRouteDependencies,
    mutation_identity: Callable[..., LaunchplaneIdentity],
    run_provider_operation_http: Callable[..., Awaitable[AcceptedEvidenceResponse]],
) -> None:
    def database(value: object) -> PostgresRecordStore:
        if not isinstance(value, PostgresRecordStore):
            raise ValueError("Native runner custody requires database storage.")
        return value

    def strict_admin(store: PostgresRecordStore, identity: LaunchplaneIdentity) -> bool:
        if not isinstance(identity, GitHubHumanIdentity):
            return False
        policies = store.list_authz_policy_records(status="active", limit=2)
        return len(policies) == 1 and policies[0].policy.names_administrator(identity)

    def authorize(
        store: PostgresRecordStore,
        identity: LaunchplaneIdentity,
        policy: RunnerRelabelPolicy,
        action: str,
    ) -> None:
        mutation = action in ("runner_relabel.apply", "runner_relabel.recover")
        if mutation and not (
            strict_admin(store, identity)
            or isinstance(identity, LocalOperatorIdentity | TerminalAgentIdentity)
        ):
            raise PermissionError("Runner mutation requires a named admin or scoped agent.")
        if not strict_admin(store, identity) and not dependencies.authorization_allows(
            identity=identity,
            action=action,
            product=policy.product,
            context=policy.context,
            inherit_agent_read_role=not mutation,
        ):
            raise PermissionError("Runner action is outside the caller's recorded scope.")

    def refuse(error: Exception, trace: str) -> Exception:
        return dependencies.http_error(
            status_code=403
            if isinstance(error, PermissionError)
            else 503
            if isinstance(error, RunnerRelabelInactive)
            else 409,
            trace_id=trace,
            code="authorization_denied"
            if isinstance(error, PermissionError)
            else str(error)
            if isinstance(error, RunnerRelabelInactive)
            else "runner_relabel_unqualified",
            message="Runner action requires its own recorded scope, exact review and active runner-purpose capability.",
        )

    def may_read_unscoped(store: PostgresRecordStore, identity: LaunchplaneIdentity) -> bool:
        return strict_admin(store, identity) or dependencies.authorization_allows(
            identity=identity, action="runner_relabel.read", product="", context="",
        )

    def policy_for_caller(
        store: PostgresRecordStore, identity: LaunchplaneIdentity, policy_id: str, action: str,
    ) -> dict[str, Any]:
        saved = store.read_runner_relabel_policy(policy_id)
        if saved is None:
            if may_read_unscoped(store, identity):
                raise RunnerRelabelInactive("runner_policy_missing")
            raise PermissionError("Runner record is unavailable in the caller's scope.")
        authorize(store, identity, RunnerRelabelPolicy.model_validate(saved["policy"]), action)
        return saved

    def review_for_caller(
        store: PostgresRecordStore, identity: LaunchplaneIdentity, policy_id: str,
        review_id: str, action: str,
    ) -> dict[str, Any]:
        saved = store.read_runner_relabel_document("review", review_id)
        if saved is None or saved["policy_id"] != policy_id:
            if may_read_unscoped(store, identity):
                raise ValueError("Runner review is unavailable.")
            raise PermissionError("Runner record is unavailable in the caller's scope.")
        authorize(store, identity, RunnerRelabelPolicy.model_validate(saved["policy"]), action)
        return saved

    async def read_policy(
        policy_id: PolicyId,
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_identity)],
        store: Annotated[object, Depends(dependencies.get_record_store)],
    ) -> AcceptedEvidenceResponse:
        trace = dependencies.next_trace_id()
        try:
            record_store = database(store)
            saved = policy_for_caller(record_store, identity, policy_id, "runner_relabel.read")
            return accepted_evidence_response(trace_id=trace, records={}, result=saved)
        except (ValueError, PermissionError) as error:
            raise refuse(error, trace) from error

    async def policy_write(
        policy_id: PolicyId,
        body: RunnerRelabelPolicyWrite,
        identity: Annotated[LaunchplaneIdentity, Depends(mutation_identity)],
        store: Annotated[object, Depends(dependencies.get_record_store)],
    ) -> AcceptedEvidenceResponse:
        trace = dependencies.next_trace_id()
        try:
            record_store = database(store)
            if body.mode == "apply" and not strict_admin(record_store, identity):
                raise PermissionError(
                    "Only an immutable-ID administrator manages runner authority."
                )
            if body.mode == "dry-run":
                authorize(record_store, identity, body.policy, "runner_relabel.plan")
            if policy_id != body.policy.policy_id:
                raise ValueError("Policy path differs.")
            plan: dict[str, Any] = {
                "policy": body.policy.model_dump(mode="json"),
                "reason": body.reason,
                "actor": actor_identity(identity),
                "expected_policy_sha256": body.expected_policy_sha256,
            }
            digest = canonical_json_sha256(plan)
            if body.mode == "dry-run":
                record_store.write_runner_relabel_document("policy-review", digest, plan)
                return accepted_evidence_response(
                    trace_id=trace,
                    records={},
                    result={**plan, "plan_sha256": digest, "status": "dry_run"},
                )
            if record_store.database_dialect_name != "postgresql":
                raise RunnerRelabelInactive("postgresql_runner_fences_required")
            if (
                body.expected_plan_sha256 != digest
                or record_store.read_runner_relabel_document("policy-review", digest) != plan
            ):
                raise ValueError("Saved policy review differs.")
            saved = record_store.compare_and_write_runner_relabel_policy(
                policy_id, plan, expected_sha256=body.expected_policy_sha256
            )
            return accepted_evidence_response(trace_id=trace, records={}, result=saved)
        except (ValueError, PermissionError) as error:
            raise refuse(error, trace) from error

    async def review(
        policy_id: PolicyId,
        body: RunnerRelabelReviewRequest,
        identity: Annotated[LaunchplaneIdentity, Depends(mutation_identity)],
        store: Annotated[object, Depends(dependencies.get_record_store)],
    ) -> AcceptedEvidenceResponse:
        trace = dependencies.next_trace_id()
        try:
            record_store = database(store)
            policy_for_caller(record_store, identity, policy_id, "runner_relabel.plan")
            load_policy(record_store, policy_id)
            result = await run_in_worker_thread(
                lambda: create_review(record_store, policy_id, body, actor_identity(identity))
            )
            return accepted_evidence_response(
                trace_id=trace,
                records={"runner_relabel_review_id": result["review_id"]},
                result=result,
            )
        except (ValueError, PermissionError) as error:
            raise refuse(error, trace) from error

    async def operate(
        policy_id: PolicyId,
        body: RunnerRelabelApplyRequest,
        identity: Annotated[LaunchplaneIdentity, Depends(mutation_identity)],
        store: Annotated[object, Depends(dependencies.get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        trace = dependencies.next_trace_id()
        try:
            if body.mode == "apply" and (isinstance(identity, GitHubActionsIdentity) or (
                isinstance(identity, GitHubHumanIdentity) and identity.role != "admin"
            )):
                raise PermissionError(
                    "Workflow and read-only identities cannot mutate runner state."
                )
            record_store = database(store)
            saved = review_for_caller(record_store, identity, policy_id, body.review_id,
                "runner_relabel.apply" if body.mode == "apply" else "runner_relabel.read")
            caller = actor_identity(identity)
            actor = saved["actor"]
            if (
                saved is None
                or (body.mode == "apply" and actor != caller)
                or saved["policy_id"] != policy_id
                or saved["review_sha256"] != body.expected_review_sha256
                or saved["review_sha256"]
                != canonical_json_sha256({k: v for k, v in saved.items() if k != "review_sha256"})
            ):
                raise ValueError("Original actor/review binding differs.")
            policy = RunnerRelabelPolicy.model_validate(saved["policy"])
            if record_store.database_dialect_name != "postgresql":
                raise RunnerRelabelInactive("postgresql_runner_fences_required")
            if not idempotency_key.strip() or len(idempotency_key) > 200:
                raise ValueError("An original operation key is required.")
            key = idempotency_key.strip()
            claim = {"actor": actor, "key": key, "review_sha256": saved["review_sha256"]}
            if body.mode == "apply":
                if load_policy(record_store, policy_id)["policy_sha256"] != saved["policy_sha256"]:
                    raise ValueError("Runner policy changed since review.")
                record_store.write_runner_relabel_document(
                    "review-operation", body.review_id, claim
                )
            elif (
                record_store.read_runner_relabel_document("review-operation", body.review_id)
                != claim
            ):
                raise ValueError("Original operation custody differs.")
            adapter = RunnerRelabelAdapter(
                record_store,
                saved,
                trace,
                key,
                body.mode == "recover-evidence",
                lambda: authorize(record_store, identity, policy, "runner_relabel.apply"),
                caller if body.mode != "apply" else None,
            )
            route = ROUTE.format(policy_id=policy_id)
            intent: dict[str, object] = RunnerRelabelOriginalIntent.model_validate(
                {"review": saved, "actor": actor, "operation_key": key}
            ).model_dump(mode="json")
            if body.mode != "apply":
                original = record_store.read_idempotency_record(
                    scope=actor_scope(actor),
                    route_path=route,
                    idempotency_key=key,
                )
                if (
                    original is None
                    or record_store.read_original_mutation_intent(original) != intent
                ):
                    raise ValueError("Original runner operation is unavailable.")

            def operation(active_store: PostgresRecordStore) -> DurableProviderOperationResult:
                authorize(
                    active_store,
                    identity,
                    policy,
                    "runner_relabel.apply" if body.mode == "apply" else "runner_relabel.read",
                )
                return run_durable_provider_operation(
                    store=active_store,
                    scope=actor_scope(actor),
                    route_path=route,
                    idempotency_key=key,
                    request_fingerprint=canonical_json_sha256(intent),
                    lease_owner=trace,
                    response_trace_id=trace,
                    adapter=adapter,
                    original_intent=intent,
                    allow_mutation=body.mode == "apply",
                )

            return await run_provider_operation_http(
                record_store=record_store,
                route_path=route,
                trace_id=trace,
                operation=operation,
                in_progress_message="Runner operation is held by its original actor and key.",
                reconcile_message="Runner outcome is unknown. Reconcile the original operation; do not repeat the label write.",
                public_failure_messages={REFUSED_BEFORE_DISPATCH: REFUSED_BEFORE_DISPATCH_MESSAGE},
            )
        except (ValueError, PermissionError) as error:
            raise refuse(error, trace) from error

    async def read_review(
        policy_id: PolicyId,
        review_id: Annotated[str, Path(pattern=r"^[a-f0-9]{32}$")],
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_identity)],
        store: Annotated[object, Depends(dependencies.get_record_store)],
    ) -> AcceptedEvidenceResponse:
        trace = dependencies.next_trace_id()
        try:
            active_store = database(store)
            saved = review_for_caller(active_store, identity, policy_id, review_id, "runner_relabel.read")
            claim = active_store.read_runner_relabel_document("review-operation", review_id)
            operation = (
                None
                if claim is None
                else active_store.read_idempotency_record(
                    scope=actor_scope(saved["actor"]),
                    route_path=ROUTE.format(policy_id=policy_id),
                    idempotency_key=claim["key"],
                )
            )
            return accepted_evidence_response(
                trace_id=trace,
                records={},
                result={
                    "review": saved,
                    "operation": None
                    if operation is None
                    else {
                        "state": operation.state,
                        "operation_key": operation.idempotency_key,
                        "provider_effect_phase": operation.provider_effect_phase,
                        "response_status_code": operation.response_status_code,
                        "response_payload": operation.response_payload,
                    },
                },
            )
        except (ValueError, PermissionError) as error:
            raise refuse(error, trace) from error

    for path, endpoint, method, operation_id in (
        ("/v1/runner-relabel/{policy_id}/policy", read_policy, "GET", "read_runner_relabel_policy"),
        (
            "/v1/runner-relabel/{policy_id}/policy",
            policy_write,
            "POST",
            "review_runner_relabel_policy",
        ),
        ("/v1/runner-relabel/{policy_id}/reviews", review, "POST", "review_runner_relabel"),
        (
            "/v1/runner-relabel/{policy_id}/reviews/{review_id}",
            read_review,
            "GET",
            "read_runner_relabel_review",
        ),
        (ROUTE, operate, "POST", "apply_runner_relabel"),
    ):
        app.add_api_route(
            path,
            endpoint,
            methods=[method],
            status_code=202,
            operation_id=operation_id,
            response_model=AcceptedEvidenceResponse,
            responses={
                code: {"model": dependencies.error_response_model}
                for code in (400, 401, 403, 409, 503)
            },
        )
