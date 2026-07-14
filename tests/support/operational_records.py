from fastapi import FastAPI
from httpx2 import Response

from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.contracts.promotion_record import PromotionRecord
from tests.support.http import get as http_get


async def _get_deployment_record(
    app: FastAPI,
    record_id: str,
    *,
    authorization: str = "Bearer valid-token",
    headers: dict[str, str] | None = None,
) -> Response:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    return await http_get(app, f"/v1/deployments/{record_id}", headers=request_headers)


async def _get_promotion_record(
    app: FastAPI,
    record_id: str,
    *,
    authorization: str = "Bearer valid-token",
    headers: dict[str, str] | None = None,
) -> Response:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    return await http_get(app, f"/v1/promotions/{record_id}", headers=request_headers)


async def _get_environment_inventory(
    app: FastAPI,
    context: str,
    instance: str,
    *,
    authorization: str = "Bearer valid-token",
    headers: dict[str, str] | None = None,
) -> Response:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    return await http_get(
        app,
        f"/v1/inventory/{context}/{instance}",
        headers=request_headers,
    )


async def _get_recent_operations(
    app: FastAPI,
    context: str,
    *,
    authorization: str = "Bearer valid-token",
    headers: dict[str, str] | None = None,
) -> Response:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    return await http_get(
        app,
        f"/v1/contexts/{context}/operations/recent",
        headers=request_headers,
    )


async def _get_context_secret_statuses(
    app: FastAPI,
    context: str,
    *,
    authorization: str = "Bearer valid-token",
    headers: dict[str, str] | None = None,
) -> Response:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    return await http_get(
        app,
        f"/v1/contexts/{context}/secrets",
        headers=request_headers,
    )


async def _get_instance_secret_statuses(
    app: FastAPI,
    context: str,
    instance: str,
    *,
    authorization: str = "Bearer valid-token",
    headers: dict[str, str] | None = None,
) -> Response:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    return await http_get(
        app,
        f"/v1/contexts/{context}/instances/{instance}/secrets",
        headers=request_headers,
    )


async def _get_secret_status(
    app: FastAPI,
    secret_id: str,
    *,
    authorization: str = "Bearer valid-token",
    headers: dict[str, str] | None = None,
) -> Response:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    return await http_get(app, f"/v1/secrets/{secret_id}", headers=request_headers)


class _SecretStatusProbeStore:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def read_secret_record(self, secret_id: str) -> object:
        self.calls.append(f"read_secret_record:{secret_id}")
        raise FileNotFoundError(secret_id)

    def list_secret_records(
        self,
        *,
        integration: str = "",
        context_name: str = "",
        instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[object, ...]:
        del integration, context_name, instance_name, limit
        self.calls.append("list_secret_records")
        return ()

    def read_secret_version(self, version_id: str) -> object:
        self.calls.append(f"read_secret_version:{version_id}")
        raise FileNotFoundError(version_id)

    def list_secret_versions(self, *, secret_id: str) -> tuple[object, ...]:
        self.calls.append(f"list_secret_versions:{secret_id}")
        return ()

    def list_secret_bindings(
        self,
        *,
        integration: str = "",
        context_name: str = "",
        instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[object, ...]:
        del integration, context_name, instance_name, limit
        self.calls.append("list_secret_bindings")
        return ()

    def list_secret_audit_events(self, *, secret_id: str) -> tuple[object, ...]:
        self.calls.append(f"list_secret_audit_events:{secret_id}")
        return ()


class _RecentOperationsProbeStore:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def list_environment_inventory(self) -> tuple[EnvironmentInventory, ...]:
        self.calls.append("list_environment_inventory")
        return ()

    def list_deployment_records(
        self,
        *,
        context_name: str = "",
        instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[DeploymentRecord, ...]:
        del context_name, instance_name, limit
        self.calls.append("list_deployment_records")
        return ()

    def list_promotion_records(
        self,
        *,
        context_name: str = "",
        from_instance_name: str = "",
        to_instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[PromotionRecord, ...]:
        del context_name, from_instance_name, to_instance_name, limit
        self.calls.append("list_promotion_records")
        return ()

    def list_preview_records(
        self,
        *,
        context_name: str = "",
        anchor_repo: str = "",
        anchor_pr_number: int | None = None,
        limit: int | None = None,
    ) -> tuple[PreviewRecord, ...]:
        del context_name, anchor_repo, anchor_pr_number, limit
        self.calls.append("list_preview_records")
        return ()
