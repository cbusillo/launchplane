from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from pydantic import BaseModel, ConfigDict, SecretStr

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.preview_fixture import PreviewFixtureRequest
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.runtime_identity import RUNTIME_IDENTITY_ENV_KEY
from control_plane.dokploy.odoo_client_signin import client_signin_command
from control_plane.disposable_lanes import require_lane_target_ownership
from control_plane.testing_serving_copy import published_testing_copy
from control_plane.native_testing_route_refresh import native_testing_origin_matches
from control_plane.workflows.odoo_preview_runtime import odoo_compose_has_domain
from control_plane.odoo_runtime_reads import (
    OdooRuntimeConnection,
    require_exclusive_odoo_signin_origin,
    assert_selection_current,
    connect_runtime,
    select_stable_runtime,
)
from control_plane.preview_fixture import PreviewFixtureAdapter
from control_plane.product_review import resolve_serving_preview, viewer_is_product_owner
from control_plane.service_auth import LaunchplaneIdentity
from control_plane.storage.postgres import PostgresRecordStore


class ClientSigninUser(BaseModel):
    model_config = ConfigDict(extra="forbid")

    xmlid: str
    name: str
    login: str


class ClientSigninInvitation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    xmlid: str
    name: str
    token: SecretStr


@dataclass(frozen=True)
class ClientSigninLane:
    repository: str
    pull_request: int | None
    generation_id: str
    instance: str


def _connection(
    store: PostgresRecordStore,
    root: Path,
    profile: LaunchplaneProductProfileRecord,
    identity: LaunchplaneIdentity,
    lane: ClientSigninLane,
    *,
    mutation: bool = False,
) -> OdooRuntimeConnection:
    if not viewer_is_product_owner(profile=profile, identity=identity):
        raise PermissionError("Client sign-in is unavailable.")
    if profile.repository.casefold() != lane.repository.casefold() or (
        lane.pull_request is not None
    ) != (lane.instance == "preview"):
        raise ValueError("The requested Client lane does not match this product.")
    if not profile.is_active or profile.driver_id != "odoo":
        raise ValueError("This product does not support native Odoo sign-in.")
    if lane.pull_request is not None:
        preview = resolve_serving_preview(
            store=store, profile=profile, pull_request_number=lane.pull_request
        )
        if not lane.generation_id or preview.generation_id != lane.generation_id:
            raise ValueError("The reviewed preview changed.")
        generation = store.read_preview_generation_record(lane.generation_id)
        current = store.read_preview_record(generation.preview_id)
        connection = PreviewFixtureAdapter(
            store=store,
            root=root,
            product=profile.product,
            trace_id="client-signin",
            request=PreviewFixtureRequest(
                action="preview-fixture",
                intent="preview-fixture",
                reason="Read the current Client sign-in lane identity.",
                context=current.context,
                instance=generation.runtime_identity.instance
                if generation.runtime_identity
                else "",
                preview_id=current.preview_id,
                generation_id=lane.generation_id,
                fixture="read-only-test-identity",
            ),
        ).connect(mutation=mutation)
    elif lane.instance == "testing":
        selection = select_stable_runtime(store, profile, "testing")
        if selection.identity.artifact_id != lane.generation_id:
            raise ValueError("The reviewed testing version changed.")
        connection = connect_runtime(
            store=store,
            selection=selection,
            control_plane_root=root,
            database_url=store.database_url,
        )
        publication = published_testing_copy(
            store, product=profile.product, context=selection.identity.context
        )
        if publication is not None:
            if publication.plan.candidate.compose_id != connection.evidence.target_id:
                raise ValueError("The published testing recipient changed.")
            if not native_testing_origin_matches(
                store,
                root=root,
                product=profile.product,
                context=selection.identity.context,
                target_id=connection.evidence.target_id,
                origin=selection.base_url,
            ):
                raise ValueError("The published testing origin is unverified.")
        else:
            require_lane_target_ownership(
                (
                    *store.list_dokploy_target_id_records(),
                    *store.list_physical_provider_target_records(),
                ),
                context=selection.identity.context,
                instance="testing",
                target_id=connection.evidence.target_id,
            )
        hostname = urlsplit(selection.base_url).hostname
        if publication is None and not (
            hostname == selection.route_host
            if selection.route_host
            else odoo_compose_has_domain(
                host=connection.host,
                token=connection.token,
                compose_id=connection.evidence.target_id,
                domain_host=hostname or "",
            )
        ):
            raise ValueError("The origin does not belong to this testing target.")
    else:
        raise ValueError("Unsupported Client sign-in lane.")
    require_exclusive_odoo_signin_origin(
        store, connection.selection.identity, connection.selection.base_url
    )
    return connection


def _binding(connection: OdooRuntimeConnection) -> str:
    return canonical_json_sha256(
        {
            "identity": connection.selection.identity.model_dump(
                mode="json", exclude={"deployed_at"}
            ),
            "origin": connection.selection.base_url,
            "target_id": connection.evidence.target_id,
            "database": connection.credentials["ODOO_DB_NAME"],
        }
    )


def _native(
    connection: OdooRuntimeConnection, *, operation: str, xmlid: str = ""
) -> dict[str, object]:
    identity = connection.selection.identity
    return client_signin_command(
        host=connection.host,
        token=connection.token,
        server_id=connection.server_id,
        payload={
            "identity": identity.model_dump(mode="json"),
            "identity_key": RUNTIME_IDENTITY_ENV_KEY,
            "image": identity.image_reference,
            "project": connection.app_name,
            "database": connection.credentials["ODOO_DB_NAME"],
            "operation": operation,
            "xmlid": xmlid,
        },
    )


def _require_current(
    store: PostgresRecordStore,
    connection: OdooRuntimeConnection,
    profile: LaunchplaneProductProfileRecord,
    identity: LaunchplaneIdentity,
) -> None:
    assert_selection_current(store, connection.selection)
    current = store.read_product_profile_record(profile.product)
    if not current.is_active or not viewer_is_product_owner(profile=current, identity=identity):
        raise PermissionError("Client sign-in is unavailable.")


def list_client_signin_users(
    *,
    store: PostgresRecordStore,
    root: Path,
    profile: LaunchplaneProductProfileRecord,
    identity: LaunchplaneIdentity,
    lane: ClientSigninLane,
) -> dict[str, object]:
    connection = _connection(store, root, profile, identity, lane)
    result = _native(connection, operation="list")
    raw = result.get("users")
    if not isinstance(raw, list):
        raise ValueError("Native Client users are unverified.")
    users = [ClientSigninUser.model_validate(user).model_dump() for user in raw]
    _require_current(store, connection, profile, identity)
    return {"users": users, "runtime_binding": _binding(connection)}


def mint_client_signin(
    *,
    store: PostgresRecordStore,
    root: Path,
    profile: LaunchplaneProductProfileRecord,
    identity: LaunchplaneIdentity,
    lane: ClientSigninLane,
    reviewed_runtime_binding: str,
    xmlid: str,
) -> dict[str, str]:
    connection = _connection(store, root, profile, identity, lane, mutation=True)
    if not reviewed_runtime_binding or _binding(connection) != reviewed_runtime_binding:
        raise ValueError("The reviewed sign-in lane changed.")
    _require_current(store, connection, profile, identity)
    invitation = ClientSigninInvitation.model_validate(
        _native(connection, operation="mint", xmlid=xmlid)
    )
    if invitation.xmlid != xmlid:
        raise ValueError("The native Client user changed.")
    _require_current(store, connection, profile, identity)
    return {
        "name": invitation.name,
        "sign_in_url": connection.selection.base_url.rstrip("/")
        + "/web/signup?"
        + urlencode(
            {
                "db": connection.credentials["ODOO_DB_NAME"],
                "token": invitation.token.get_secret_value(),
                "redirect": "/odoo",
            }
        ),
    }
