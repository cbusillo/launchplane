from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.client import HTTPException as HTTPTransportError
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Literal, Protocol
from urllib.parse import urlsplit
from urllib.request import (
    HTTPCookieProcessor,
    HTTPRedirectHandler,
    OpenerDirector,
    Request,
    build_opener,
)

from pydantic import BaseModel, ConfigDict, Field, field_validator

from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.preview_generation_record import PreviewGenerationRecord
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.runtime_identity import (
    RUNTIME_IDENTITY_ENV_KEY,
    RuntimeIdentity,
    parse_runtime_identity_payload,
    runtime_identity_from_health_payload,
)
from control_plane.dokploy import api, source
from control_plane.preview_serving_evidence import verify_serving_preview
from control_plane.workflows.odoo_preview_runtime import (
    discover_odoo_preview_target,
    odoo_compose_has_domain,
)
from control_plane.workflows.ship import utc_now_timestamp


class OdooRuntimeReadError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 409) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(message)


class OdooRuntimeReadStore(Protocol):
    def read_preview_record(self, preview_id: str) -> PreviewRecord: ...

    def read_preview_generation_record(self, generation_id: str) -> PreviewGenerationRecord: ...

    def read_product_profile_record(self, product: str) -> LaunchplaneProductProfileRecord: ...

    def read_environment_inventory(
        self, *, context_name: str, instance_name: str
    ) -> EnvironmentInventory: ...

    def read_dokploy_target_record(
        self, *, context_name: str, instance_name: str
    ) -> DokployTargetRecord: ...

    def read_dokploy_target_id_record(
        self, *, context_name: str, instance_name: str
    ) -> DokployTargetIdRecord: ...


@dataclass(frozen=True)
class OdooRuntimeSelection:
    identity: RuntimeIdentity
    base_url: str
    preview: PreviewRecord | None = None


def select_preview_runtime(
    store: OdooRuntimeReadStore, preview: PreviewRecord
) -> OdooRuntimeSelection:
    if preview.state == "destroyed":
        raise OdooRuntimeReadError("preview_destroyed", "The preview has been destroyed.", 410)
    if preview.state != "active" or not preview.serving_generation_id:
        raise OdooRuntimeReadError(
            "preview_unavailable", "The preview has no active serving generation."
        )
    generation = store.read_preview_generation_record(preview.serving_generation_id)
    identity = generation.runtime_identity
    if identity is None:
        raise OdooRuntimeReadError(
            "runtime_identity_missing", "The serving generation has no runtime identity."
        )
    verify_serving_preview(
        product=identity.product,
        preview=preview,
        generation=generation,
        require_runtime_generation_id=True,
    )
    profile = store.read_product_profile_record(identity.product)
    if (
        profile.driver_id != "odoo"
        or profile.lifecycle_state != "active"
        or not profile.preview.enabled
        or profile.preview.context != preview.context
        or profile.repository.rsplit("/", 1)[-1].lower() != preview.anchor_repo.lower()
        or preview.anchor_pr_url
        != f"https://github.com/{profile.repository}/pull/{preview.anchor_pr_number}"
        or any(
            lane.context == identity.context and lane.instance == identity.instance
            for lane in profile.lanes
        )
    ):
        raise OdooRuntimeReadError(
            "preview_profile_mismatch",
            "The serving preview does not match its Odoo product profile.",
        )
    return OdooRuntimeSelection(identity, preview.canonical_url, preview)


def select_stable_runtime(
    store: OdooRuntimeReadStore, profile: LaunchplaneProductProfileRecord, environment: str
) -> OdooRuntimeSelection:
    lanes = [lane for lane in profile.lanes if lane.instance == environment]
    if profile.driver_id != "odoo" or len(lanes) != 1:
        raise OdooRuntimeReadError(
            "invalid_odoo_environment",
            "The requested environment is not owned by this Odoo product.",
            400,
        )
    lane = lanes[0]
    inventory = store.read_environment_inventory(
        context_name=lane.context, instance_name=lane.instance
    )
    identity = inventory.runtime_identity
    if (
        identity is None
        or identity.product != profile.product
        or identity.context != lane.context
        or identity.instance != lane.instance
        or identity.environment_kind != "stable"
        or identity.preview_id
        or identity.preview_generation_id
        or identity.deployment_record_id != inventory.deployment_record_id
    ):
        raise OdooRuntimeReadError(
            "runtime_identity_mismatch", "The environment has no matching stable runtime identity."
        )
    return OdooRuntimeSelection(identity, lane.base_url)


def assert_selection_current(store: OdooRuntimeReadStore, selection: OdooRuntimeSelection) -> None:
    if selection.preview is not None:
        current = select_preview_runtime(
            store, store.read_preview_record(selection.preview.preview_id)
        )
    else:
        profile = store.read_product_profile_record(selection.identity.product)
        current = select_stable_runtime(store, profile, selection.identity.instance)
    if current.identity != selection.identity or current.base_url != selection.base_url:
        raise OdooRuntimeReadError(
            "runtime_changed",
            "The runtime changed during the read; retry against its current generation.",
        )


class OdooRuntimeEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identity: RuntimeIdentity
    target_id: str
    target_name: str
    container_id: str
    observed_at: str


@dataclass(frozen=True)
class OdooRuntimeConnection:
    selection: OdooRuntimeSelection
    evidence: OdooRuntimeEvidence
    host: str
    token: str = field(repr=False)
    server_id: str
    app_name: str
    credentials: dict[str, str] = field(repr=False)


def connect_runtime(
    *,
    store: OdooRuntimeReadStore,
    selection: OdooRuntimeSelection,
    control_plane_root: Path,
    database_url: str | None,
) -> OdooRuntimeConnection:
    identity = selection.identity
    host, token = source.read_dokploy_config(
        control_plane_root=control_plane_root, database_url=database_url
    )
    if selection.preview is not None:
        # The runtime instance is the compose name issued by the preview deployment,
        # not a mutable product-profile prefix reconstructed for an old generation.
        target = discover_odoo_preview_target(
            control_plane_root=control_plane_root,
            context_name=identity.context,
            preview_slug=identity.instance,
            preview_url=selection.base_url,
            compose_name=identity.instance,
            database_url=database_url,
        )
        if target is None:
            raise OdooRuntimeReadError(
                "preview_target_missing",
                "The serving preview's provider target is unavailable.",
                503,
            )
        target_id, target_name = target.target_id, target.target_name
    else:
        target_record = store.read_dokploy_target_record(
            context_name=identity.context, instance_name=identity.instance
        )
        target_id_record = store.read_dokploy_target_id_record(
            context_name=identity.context, instance_name=identity.instance
        )
        if target_record.target_type != "compose":
            raise OdooRuntimeReadError(
                "unsupported_odoo_target", "Odoo runtime reads require a compose target.", 400
            )
        target_id, target_name = target_id_record.target_id, target_record.target_name
    payload = api.fetch_dokploy_target_payload(
        host=host, token=token, target_type="compose", target_id=target_id
    )
    app_name = str(payload.get("appName") or "")
    server_id = str(payload.get("serverId") or "")
    if not app_name or str(payload.get("name") or "") != target_name:
        raise OdooRuntimeReadError(
            "provider_target_mismatch",
            "The provider target identity does not match the selected runtime.",
        )
    query: dict[str, str | int] = {"appName": app_name, "appType": "docker-compose"}
    if server_id:
        query["serverId"] = server_id
    containers = api.dokploy_request(
        host=host, token=token, path="/api/docker.getContainersByAppNameMatch", query=query
    )
    if not isinstance(containers, list):
        raise OdooRuntimeReadError(
            "container_unavailable", "Provider container inventory is unavailable.", 503
        )
    container = api.select_dokploy_compose_container(
        api.collect_dokploy_object_items(containers), app_name=app_name, service_name="web"
    )
    container_id = str(container.get("containerId") or "")
    if not container_id:
        raise OdooRuntimeReadError(
            "container_unavailable", "The Odoo web container is unavailable.", 503
        )
    environment = inspect_runtime_container(
        host, token, server_id, container_id, app_name, identity
    )
    return OdooRuntimeConnection(
        selection=selection,
        evidence=OdooRuntimeEvidence(
            identity=identity,
            target_id=target_id,
            target_name=target_name,
            container_id=container_id,
            observed_at=utc_now_timestamp(),
        ),
        host=host,
        token=token,
        server_id=server_id,
        app_name=app_name,
        credentials={
            key: environment.get(key, "")
            for key in ("ODOO_DB_NAME", "ODOO_ADMIN_LOGIN", "ODOO_ADMIN_PASSWORD")
        },
    )


def inspect_runtime_container(
    host: str,
    token: str,
    server_id: str,
    container_id: str,
    app_name: str,
    identity: RuntimeIdentity,
) -> dict[str, str]:
    query: dict[str, str | int] = {"containerId": container_id}
    if server_id:
        query["serverId"] = server_id
    raw = api.dokploy_request(host=host, token=token, path="/api/docker.getConfig", query=query)
    if isinstance(raw, str):
        raw = json.loads(raw)
    if isinstance(raw, list) and len(raw) == 1:
        raw = raw[0]
    config = raw.get("Config") if isinstance(raw, dict) else None
    state = raw.get("State") if isinstance(raw, dict) else None
    if (
        not isinstance(config, dict)
        or not isinstance(state, dict)
        or state.get("Running") is not True
    ):
        raise OdooRuntimeReadError(
            "container_unavailable", "The selected Odoo container is not running.", 503
        )
    labels = config.get("Labels")
    if (
        not isinstance(labels, dict)
        or labels.get("com.docker.compose.project") != app_name
        or labels.get("com.docker.compose.service") != "web"
    ):
        raise OdooRuntimeReadError(
            "container_mismatch", "The container does not belong to the selected compose service."
        )
    env_list = config.get("Env")
    environment = (
        dict(item.split("=", 1) for item in env_list if isinstance(item, str) and "=" in item)
        if isinstance(env_list, list)
        else {}
    )
    observed = parse_runtime_identity_payload(environment.get(RUNTIME_IDENTITY_ENV_KEY))
    require_runtime_identity(identity, observed)
    if (
        "@sha256:" not in identity.image_reference
        or config.get("Image") != identity.image_reference
    ):
        raise OdooRuntimeReadError(
            "runtime_image_mismatch", "The container image does not match the selected runtime."
        )
    return environment


def require_runtime_identity(expected: RuntimeIdentity, observed: RuntimeIdentity | None) -> None:
    if observed is None or expected.model_dump(exclude={"deployed_at"}) != observed.model_dump(
        exclude={"deployed_at"}
    ):
        raise OdooRuntimeReadError(
            "runtime_identity_mismatch",
            "The observed runtime does not match the selected generation or environment.",
        )


def read_runtime_logs(
    connection: OdooRuntimeConnection, *, lines: int, since: str, search: str
) -> tuple[str, ...]:
    payload = api.dokploy_request(
        host=connection.host,
        token=connection.token,
        path="/api/compose.readLogs",
        query={
            "composeId": connection.evidence.target_id,
            "containerId": connection.evidence.container_id,
            "tail": lines,
            "since": since,
        },
    )
    bounded = api.normalize_dokploy_log_payload(payload)[-lines:]
    return api.filter_dokploy_log_lines(bounded, search)


class OutgoingEmailQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject: str = Field(min_length=1, max_length=200)
    recipient: str = Field(min_length=3, max_length=320)
    created_after: datetime
    limit: int = Field(default=20, ge=1, le=20)

    @field_validator("subject", "recipient")
    @classmethod
    def validate_filter(cls, value: str) -> str:
        value = value.strip()
        if not value or any(ord(character) < 32 for character in value):
            raise ValueError("Email filters must be non-empty and contain no control characters.")
        return value

    @field_validator("created_after")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("created_after requires a timezone.")
        return value


class OutgoingEmailStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mail_id: int
    state: Literal["queued", "sent", "failed", "cancelled", "unknown"]
    failure_type: str
    failure_reason: str
    message_id: str
    sender: str
    created_at: str
    updated_at: str
    auto_delete: bool


class OutgoingEmailResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    match: Literal["not_found", "unique", "ambiguous"]
    truncated: bool
    left_odoo: bool | None
    messages: tuple[OutgoingEmailStatus, ...]
    delivery_scope: Literal["odoo_smtp_handoff_only"] = "odoo_smtp_handoff_only"


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(
        self, req: Request, fp: object, code: int, msg: str, headers: object, newurl: str
    ) -> None:
        raise OdooRuntimeReadError(
            "odoo_redirect", "Odoo runtime reads do not follow redirects.", 503
        )


def _http_json(opener: OpenerDirector, request: Request) -> object:
    try:
        with opener.open(request, timeout=20) as response:
            raw = response.read(1_000_001)
        if len(raw) > 1_000_000:
            raise OdooRuntimeReadError(
                "odoo_response_too_large", "Odoo returned an oversized response.", 503
            )
        return json.loads(raw)
    except (OSError, HTTPTransportError, UnicodeError, json.JSONDecodeError) as error:
        raise OdooRuntimeReadError(
            "odoo_read_unavailable",
            "Odoo did not return a valid response to the bounded read.",
            503,
        ) from error


def _rpc(
    opener: OpenerDirector,
    base_url: str,
    path: str,
    params: dict[str, object],
    *,
    expect_result: bool = True,
) -> object:
    result = _http_json(
        opener,
        Request(
            base_url + path,
            data=json.dumps(
                {"jsonrpc": "2.0", "method": "call", "params": params, "id": 1}
            ).encode(),
            headers={"Content-Type": "application/json"},
        ),
    )
    if (
        not isinstance(result, dict)
        or result.get("jsonrpc") != "2.0"
        or result.get("id") != 1
        or "error" in result
        or (expect_result and "result" not in result)
    ):
        raise OdooRuntimeReadError(
            "odoo_read_rejected", "Odoo rejected the authenticated mail-status read.", 503
        )
    return result.get("result")


def read_outgoing_email(
    connection: OdooRuntimeConnection, query: OutgoingEmailQuery
) -> OutgoingEmailResult:
    base_url = connection.selection.base_url.rstrip("/")
    url = urlsplit(base_url)
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
        or url.path
    ):
        raise OdooRuntimeReadError(
            "invalid_odoo_url", "Mail-status reads require the recorded HTTPS runtime origin.", 503
        )
    credentials = connection.credentials
    if not credentials["ODOO_DB_NAME"] or not credentials["ODOO_ADMIN_PASSWORD"]:
        raise OdooRuntimeReadError(
            "odoo_credentials_missing",
            "The running Odoo container has no configured diagnostic credentials.",
            503,
        )
    if not odoo_compose_has_domain(
        host=connection.host,
        token=connection.token,
        compose_id=connection.evidence.target_id,
        domain_host=url.hostname,
    ):
        raise OdooRuntimeReadError(
            "runtime_domain_mismatch",
            "The recorded origin is not bound to the selected Odoo deployment.",
        )
    opener = build_opener(_NoRedirects(), HTTPCookieProcessor(CookieJar()))
    health = _http_json(opener, Request(base_url + "/launchplane/health"))
    require_runtime_identity(
        connection.selection.identity, runtime_identity_from_health_payload(health)
    )
    authenticated = _rpc(
        opener,
        base_url,
        "/web/session/authenticate",
        {
            "db": credentials["ODOO_DB_NAME"],
            # This is the Odoo image's startup contract, not a tenant identity default.
            "login": credentials["ODOO_ADMIN_LOGIN"] or "admin",
            "password": credentials["ODOO_ADMIN_PASSWORD"],
        },
    )
    if (
        not isinstance(authenticated, dict)
        or type(authenticated.get("uid")) is not int
        or authenticated["uid"] <= 0
    ):
        raise OdooRuntimeReadError(
            "odoo_authentication_failed",
            "Odoo rejected its configured diagnostic credentials.",
            503,
        )
    fields = [
        "id",
        "state",
        "failure_type",
        "failure_reason",
        "message_id",
        "email_from",
        "create_date",
        "write_date",
        "auto_delete",
    ]
    try:
        result = _search_mail(opener, base_url, fields, query)
    finally:
        _rpc(opener, base_url, "/web/session/destroy", {}, expect_result=False)
    health = _http_json(opener, Request(base_url + "/launchplane/health"))
    require_runtime_identity(
        connection.selection.identity, runtime_identity_from_health_payload(health)
    )
    return _mail_result(query, result, credentials)


def _search_mail(
    opener: OpenerDirector, base_url: str, fields: list[str], query: OutgoingEmailQuery
) -> object:
    return _rpc(
        opener,
        base_url,
        "/web/dataset/call_kw",
        {
            "model": "mail.mail",
            "method": "search_read",
            "args": [
                [
                    ["subject", "=", query.subject],
                    ["email_to", "=ilike", _literal_like(query.recipient)],
                    [
                        "create_date",
                        ">=",
                        query.created_after.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                    ],
                ]
            ],
            "kwargs": {
                "fields": fields,
                "limit": query.limit + 1,
                "order": "create_date desc, id desc",
            },
        },
    )


def _mail_result(
    query: OutgoingEmailQuery,
    result: object,
    credentials: dict[str, str],
) -> OutgoingEmailResult:
    if not isinstance(result, list) or len(result) > query.limit + 1:
        raise OdooRuntimeReadError(
            "invalid_mail_status", "Odoo returned an invalid bounded mail-status result.", 503
        )
    messages: list[OutgoingEmailStatus] = []
    for row in result[: query.limit]:
        if (
            not isinstance(row, dict)
            or type(row.get("id")) is not int
            or not isinstance(row.get("state"), str)
        ):
            raise OdooRuntimeReadError(
                "invalid_mail_status", "Odoo returned a malformed mail-status record.", 503
            )
        states: dict[str, Literal["queued", "sent", "failed", "cancelled", "unknown"]] = {
            "outgoing": "queued",
            "sent": "sent",
            "exception": "failed",
            "cancel": "cancelled",
        }
        state = states.get(row["state"], "unknown")
        messages.append(
            OutgoingEmailStatus(
                mail_id=row["id"],
                state=state,
                failure_type=_redact(row.get("failure_type"), credentials),
                failure_reason=_redact(row.get("failure_reason"), credentials),
                message_id=_redact(row.get("message_id"), credentials),
                sender=_redact(row.get("email_from"), credentials),
                created_at=_redact(row.get("create_date"), credentials),
                updated_at=_redact(row.get("write_date"), credentials),
                auto_delete=row.get("auto_delete") is True,
            )
        )
    unique = len(result) == 1
    left_odoo = (messages[0].state == "sent") if unique and messages[0].state != "unknown" else None
    return OutgoingEmailResult(
        match="unique" if unique else "ambiguous" if result else "not_found",
        truncated=len(result) > query.limit,
        left_odoo=left_odoo,
        messages=tuple(messages),
    )


def _literal_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _redact(value: object, credentials: dict[str, str]) -> str:
    text = value if isinstance(value, str) else ""
    password = credentials.get("ODOO_ADMIN_PASSWORD", "")
    if password:
        text = text.replace(password, "[redacted]")
    return api.redact_dokploy_log_line(text)[:1000]
