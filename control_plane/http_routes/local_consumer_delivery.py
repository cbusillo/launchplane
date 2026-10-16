from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated

import click
from fastapi import Depends, Request, Response
from control_plane.live_target_runtime import LiveTargetRuntimeError

from control_plane.http_routes.support import ApiRouteRegistrar, ReadRouteDependencies
from control_plane.local_consumer_contract import (
    DELIVERY_ROUTE,
    VERIFY_ROUTE,
    ConsumerDeliveryEnvelope,
    ConsumerDeliveryResponse,
    ConsumerPackage,
    ConsumerVerificationResponse,
    digest,
)
from control_plane.local_consumer_delivery import (
    ConsumerCredentialUnavailable,
    issue_delivery,
    prepare_delivery,
    verify_delivery,
)
from control_plane.service_auth import AuthorizationTarget, GitHubHumanIdentity
from control_plane.storage.postgres import PostgresRecordStore


@dataclass(frozen=True)
class LocalConsumerDependencies:
    common: ReadRouteDependencies
    read_human_mutation_identity: Callable[..., GitHubHumanIdentity]


def register_local_consumer_delivery_routes(
    app: ApiRouteRegistrar, *, dependencies: LocalConsumerDependencies
) -> None:
    common = dependencies.common

    def deliver(
        envelope: ConsumerDeliveryEnvelope,
        request: Request,
        response: Response,
        identity: Annotated[
            GitHubHumanIdentity, Depends(dependencies.read_human_mutation_identity)
        ],
        record_store: Annotated[object, Depends(common.get_record_store)],
    ) -> ConsumerDeliveryResponse:
        response.headers["Cache-Control"] = "no-store"
        trace_id = common.next_trace_id()
        target = envelope.request
        if (
            identity.role != "admin"
            or request.headers.get("origin") != target.service_origin
            or not common.authorization_allows(
                identity=identity,
                action="product_config.plan"
                if envelope.mode == "dry-run"
                else "product_config.apply",
                product=target.product,
                context=target.context,
                target=AuthorizationTarget(scope="instance", instances=(target.environment,)),
                inherit_agent_read_role=False,
            )
        ):
            raise common.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Local delivery requires the existing signed-in testing administrator.",
            )
        if not isinstance(record_store, PostgresRecordStore):
            raise common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message="Local delivery requires database custody and receipts.",
            )
        try:
            actor = f"github-human:{identity.github_id}"
            package = None
            if envelope.mode == "dry-run":
                plan = prepare_delivery(record_store, target, actor=actor, trace_id=trace_id)
            else:
                package = issue_delivery(
                    record_store,
                    target,
                    actor=actor,
                    trace_id=trace_id,
                    reviewed_plan_digest=envelope.reviewed_plan_digest,
                )
                plan = package.plan
        except ConsumerCredentialUnavailable:
            if envelope.mode == "dry-run":
                return ConsumerDeliveryResponse(
                    trace_id=trace_id,
                    request_fingerprint=digest(target),
                    ready_to_deliver=False,
                )
            raise common.http_error(
                status_code=409,
                trace_id=trace_id,
                code="local_credential_not_ready",
                message="The destination is verified; the testing credential is not ready.",
            ) from None
        except (ValueError, FileNotFoundError, click.ClickException, LiveTargetRuntimeError):
            raise common.http_error(
                status_code=409,
                trace_id=trace_id,
                code="local_delivery_not_ready",
                message="Verify the isolated testing binding, endpoint and original review; "
                "expired or changed requests need a new testing destination.",
            ) from None
        return ConsumerDeliveryResponse(
            trace_id=trace_id,
            request_fingerprint=digest(target),
            ready_to_deliver=True,
            plan_digest=digest(plan),
            version_id=plan.version_id,
            package=package,
        )

    def verify(
        package: ConsumerPackage,
        response: Response,
        record_store: Annotated[object, Depends(common.get_record_store)],
    ) -> ConsumerVerificationResponse:
        # An unguessable receipt plus its exact ciphertext is a comparison input,
        # not Launchplane authentication. Return no records or credential values.
        response.headers["Cache-Control"] = "no-store"
        try:
            if not isinstance(record_store, PostgresRecordStore):
                raise ValueError("Database custody required.")
            package_digest = verify_delivery(record_store, package)
        except (ValueError, FileNotFoundError, click.ClickException, LiveTargetRuntimeError):
            raise common.http_error(
                status_code=409,
                trace_id=common.next_trace_id(),
                code="local_delivery_unavailable",
                message="The original delivery cannot be verified; nothing may be installed.",
            ) from None
        return ConsumerVerificationResponse(package_digest=package_digest)

    app.add_api_route(
        DELIVERY_ROUTE,
        deliver,
        methods=["POST"],
        response_model=ConsumerDeliveryResponse,
        operation_id="deliver_testing_local_consumer",
    )
    app.add_api_route(
        VERIFY_ROUTE,
        verify,
        methods=["POST"],
        response_model=ConsumerVerificationResponse,
        operation_id="verify_testing_local_consumer_delivery",
    )
