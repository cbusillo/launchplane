from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated

import click
from fastapi import Depends, Query, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from control_plane import secrets
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneProfile,
)
from control_plane.http_routes.support import (
    ApiRouteRegistrar,
    ReadRouteDependencies,
    LAUNCHPLANE_SERVICE_CONTEXT,
)
from control_plane.owner_secret_inputs import (
    owner_secret_request_revision,
    owner_secret_environments,
    owner_submission_record,
    owner_submission_receipt,
    requested_owner_secrets,
    store_owner_secret_submission,
)
from control_plane.product_review import viewer_is_product_owner
from control_plane.service_auth import GitHubHumanIdentity, LaunchplaneIdentity
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.storage.product_authority_bundle import ProductProfileConflictError

OWNER_SECRET_INPUT_ROUTE = "/v1/owner-secret-inputs"
OWNER_SECRET_SUBMIT_ROUTE = "/v1/owner-secret-inputs/submit"


class OwnerSecretInputField(BaseModel):
    model_config = ConfigDict(extra="forbid")

    integration: str
    binding_key: str
    label: str
    instructions: str
    environments: tuple[str, ...]
    request_revision: str
    submitted_at: str = ""
    submission_version_id: str = ""


class OwnerSecretInputResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str = "ok"
    trace_id: str
    product: str
    display_name: str
    environment: str
    can_submit: bool
    fields: tuple[OwnerSecretInputField, ...]


class OwnerSecretInputEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product: str = Field(min_length=1, max_length=256)
    environment: str = Field(min_length=1, max_length=256)
    request_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    value: SecretStr = Field(min_length=1, max_length=65536)


@dataclass(frozen=True, slots=True)
class OwnerSecretInputDependencies:
    common: ReadRouteDependencies
    read_human_mutation_identity: Callable[..., GitHubHumanIdentity]


def register_owner_secret_input_routes(
    app: ApiRouteRegistrar, *, dependencies: OwnerSecretInputDependencies
) -> None:
    common = dependencies.common

    def target(
        product: str,
        environment: str,
        identity: LaunchplaneIdentity,
        record_store: object,
        trace_id: str,
        *,
        writing: bool = False,
    ) -> tuple[PostgresRecordStore, LaunchplaneProductProfileRecord, ProductLaneProfile]:
        if not isinstance(record_store, PostgresRecordStore):
            raise common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message="Owner credential input requires database storage.",
            )
        lane: ProductLaneProfile | None = None
        try:
            profile = record_store.read_product_profile_record(product)
            lane = next(lane for lane in profile.lanes if lane.instance == environment)
        except (FileNotFoundError, StopIteration):
            profile = None
        allowed = profile is not None and (
            viewer_is_product_owner(profile=profile, identity=identity)
            or (
                not writing
                and common.authorization_allows(
                    identity=identity,
                    action="product_profile.read",
                    product=profile.product,
                    context=LAUNCHPLANE_SERVICE_CONTEXT,
                )
            )
        )
        if not allowed or profile is None or lane is None:
            raise common.http_error(
                status_code=403,
                trace_id=trace_id,
                code="owner_secret_input_unavailable",
                message="This credential request is unavailable to you.",
            )
        return record_store, profile, lane

    def projection(
        store: PostgresRecordStore,
        profile: LaunchplaneProductProfileRecord,
        lane: ProductLaneProfile,
        identity: LaunchplaneIdentity,
        trace_id: str,
    ) -> OwnerSecretInputResponse:
        fields = []
        for requirement in requested_owner_secrets(profile, lane):
            assert requirement.owner_input is not None
            record = owner_submission_record(
                store, profile=profile, lane=lane, requirement=requirement
            )
            receipt = (
                owner_submission_receipt(store, record, owner_github_id=profile.owner.github_id)
                if record
                else None
            )
            fields.append(
                OwnerSecretInputField(
                    integration=requirement.integration,
                    binding_key=requirement.binding_key,
                    label=requirement.owner_input.label,
                    instructions=requirement.owner_input.instructions,
                    environments=owner_secret_environments(profile, requirement),
                    request_revision=owner_secret_request_revision(profile, lane, requirement),
                    submitted_at=receipt.recorded_at if receipt else "",
                    submission_version_id=record.current_version_id if record and receipt else "",
                )
            )
        return OwnerSecretInputResponse(
            trace_id=trace_id,
            product=profile.product,
            display_name=profile.display_name,
            environment=lane.instance,
            can_submit=viewer_is_product_owner(profile=profile, identity=identity),
            fields=tuple(fields),
        )

    def read_owner_secret_inputs(
        product: Annotated[str, Query(min_length=1, max_length=256)],
        environment: Annotated[str, Query(min_length=1, max_length=256)],
        response: Response,
        identity: Annotated[LaunchplaneIdentity, Depends(common.read_identity)],
        record_store: Annotated[object, Depends(common.get_record_store)],
    ) -> OwnerSecretInputResponse:
        response.headers["Cache-Control"] = "no-store"
        trace_id = common.next_trace_id()
        store, profile, lane = target(product, environment, identity, record_store, trace_id)
        return projection(store, profile, lane, identity, trace_id)

    def submit_owner_secret_input(
        envelope: OwnerSecretInputEnvelope,
        response: Response,
        identity: Annotated[
            GitHubHumanIdentity, Depends(dependencies.read_human_mutation_identity)
        ],
        record_store: Annotated[object, Depends(common.get_record_store)],
    ) -> OwnerSecretInputResponse:
        response.headers["Cache-Control"] = "no-store"
        trace_id = common.next_trace_id()
        store, profile, lane = target(
            envelope.product, envelope.environment, identity, record_store, trace_id, writing=True
        )
        requirement = next(
            (
                item
                for item in requested_owner_secrets(profile, lane)
                if owner_secret_request_revision(profile, lane, item) == envelope.request_revision
            ),
            None,
        )
        if requirement is None or not envelope.value.get_secret_value().strip():
            raise common.http_error(
                status_code=409,
                trace_id=trace_id,
                code="owner_secret_request_changed",
                message="This credential request changed. Refresh before submitting.",
            )
        try:
            secrets.validate_secret_key_configuration()
            store_owner_secret_submission(
                store,
                profile=profile,
                lane=lane,
                requirement=requirement,
                value=envelope.value.get_secret_value(),
            )
        except ProductProfileConflictError as error:
            raise common.http_error(
                status_code=409,
                trace_id=trace_id,
                code="owner_secret_request_changed",
                message="This credential request changed. Refresh before submitting.",
            ) from error
        except click.ClickException as error:
            raise common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="secret_storage_unavailable",
                message="Encrypted credential storage is unavailable.",
            ) from error
        return projection(store, profile, lane, identity, trace_id)

    app.add_api_route(
        OWNER_SECRET_INPUT_ROUTE,
        read_owner_secret_inputs,
        methods=["GET"],
        response_model=OwnerSecretInputResponse,
        operation_id="read_owner_secret_inputs",
    )
    app.add_api_route(
        OWNER_SECRET_SUBMIT_ROUTE,
        submit_owner_secret_input,
        methods=["POST"],
        response_model=OwnerSecretInputResponse,
        operation_id="submit_owner_secret_input",
    )
