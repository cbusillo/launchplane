from urllib.parse import urlencode

from fastapi import FastAPI
from httpx2 import Response

from tests.support.http import get as http_get


async def _get_config_status(
    app: FastAPI,
    *,
    product: str = "example-site",
    environment: str = "prod",
    authorization: str = "Bearer valid-token",
) -> Response:
    headers = {"Authorization": authorization} if authorization else {}
    return await http_get(
        app,
        f"/v1/products/{product}/environments/{environment}/config-status",
        headers=headers,
    )


async def _get_repo_product_mapping(
    app: FastAPI,
    *,
    authorization: str = "Bearer valid-token",
) -> Response:
    headers = {"Authorization": authorization} if authorization else {}
    return await http_get(app, "/v1/repo-product-mapping", headers=headers)


async def _get_agent_context(
    app: FastAPI,
    *,
    repository: str = "",
    authorization: str = "Bearer valid-token",
) -> Response:
    headers = {"Authorization": authorization} if authorization else {}
    suffix = f"?{urlencode({'repository': repository})}" if repository else ""
    return await http_get(app, f"/v1/agent/context{suffix}", headers=headers)


async def _get_products(
    app: FastAPI,
    *,
    authorization: str = "Bearer valid-token",
) -> Response:
    headers = {"Authorization": authorization} if authorization else {}
    return await http_get(app, "/v1/products", headers=headers)


async def _get_product(
    app: FastAPI,
    product: str = "example-site",
    *,
    authorization: str = "Bearer valid-token",
) -> Response:
    headers = {"Authorization": authorization} if authorization else {}
    return await http_get(app, f"/v1/products/{product}", headers=headers)


async def _get_product_activity(
    app: FastAPI,
    product: str = "example-site",
    *,
    authorization: str = "Bearer valid-token",
) -> Response:
    headers = {"Authorization": authorization} if authorization else {}
    return await http_get(app, f"/v1/products/{product}/activity", headers=headers)


async def _get_product_environments(
    app: FastAPI,
    product: str = "example-site",
    *,
    authorization: str = "Bearer valid-token",
) -> Response:
    headers = {"Authorization": authorization} if authorization else {}
    return await http_get(app, f"/v1/products/{product}/environments", headers=headers)


async def _get_product_environment(
    app: FastAPI,
    product: str = "example-site",
    environment: str = "prod",
    *,
    authorization: str = "Bearer valid-token",
) -> Response:
    headers = {"Authorization": authorization} if authorization else {}
    return await http_get(
        app,
        f"/v1/products/{product}/environments/{environment}",
        headers=headers,
    )


async def _get_product_operational_readiness(
    app: FastAPI,
    *,
    product: str = "example-odoo",
    context: str = "example-odoo",
    instance: str = "testing",
    action: str = "odoo_target_replacement_plan.read",
    artifact_id: str = "artifact-example-odoo-0123456789abcdef",
    expected_current_artifact_id: str = "",
    authorization: str = "Bearer valid-token",
) -> Response:
    headers = {"Authorization": authorization} if authorization else {}
    query = {"action": action}
    if artifact_id:
        query["artifact_id"] = artifact_id
    if expected_current_artifact_id:
        query["expected_current_artifact_id"] = expected_current_artifact_id
    return await http_get(
        app,
        (
            f"/v1/products/{product}/contexts/{context}/instances/{instance}/"
            f"operational-readiness?{urlencode(query)}"
        ),
        headers=headers,
    )


async def _get_product_profiles(
    app: FastAPI,
    *,
    driver_id: str = "",
    authorization: str = "Bearer valid-token",
    headers: dict[str, str] | None = None,
) -> Response:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    suffix = f"?{urlencode({'driver_id': driver_id})}" if driver_id else ""
    return await http_get(app, f"/v1/product-profiles{suffix}", headers=request_headers)


async def _get_product_profile(
    app: FastAPI,
    product: str = "sellyouroutboard",
    *,
    authorization: str = "Bearer valid-token",
    headers: dict[str, str] | None = None,
) -> Response:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    return await http_get(
        app,
        f"/v1/product-profiles/{product}",
        headers=request_headers,
    )


async def _get_context_cutover_audit(
    app: FastAPI,
    *,
    product: str = "sellyouroutboard",
    source_context: str = "sellyouroutboard-testing",
    target_context: str = "sellyouroutboard",
    preview_context: str = "",
    authorization: str = "Bearer valid-token",
    headers: dict[str, str] | None = None,
) -> Response:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    params = {
        "source_context": source_context,
        "target_context": target_context,
    }
    if preview_context:
        params["preview_context"] = preview_context
    return await http_get(
        app,
        f"/v1/product-profiles/{product}/context-cutover-audit?{urlencode(params)}",
        headers=request_headers,
    )


async def _get_protected_artifacts(
    app: FastAPI,
    *,
    product: str,
    context: str = "",
    authorization: str = "Bearer local-operator-token",
    headers: dict[str, str] | None = None,
) -> Response:
    params: dict[str, str] = {}
    if product:
        params["product"] = product
    if context:
        params["context"] = context
    query_string = urlencode(params)
    suffix = f"?{query_string}" if query_string else ""
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    return await http_get(
        app,
        f"/v1/artifacts/protected{suffix}",
        headers=request_headers,
    )
