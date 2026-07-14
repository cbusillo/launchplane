from control_plane.http_routes.support import ReadRouteDependencies
from control_plane.service_auth import LaunchplaneIdentity


def ensure_every_code_read_allowed(
    *,
    dependencies: ReadRouteDependencies,
    identity: LaunchplaneIdentity | None,
    trace_id: str,
    action: str,
    message: str,
) -> None:
    if identity is None:
        return
    if not dependencies.authorization_allows(
        identity=identity,
        action=action,
        product="launchplane",
        context="launchplane",
    ):
        raise dependencies.http_error(
            status_code=403,
            trace_id=trace_id,
            code="authorization_denied",
            message=message,
        )


def every_code_pagination_value(
    raw_value: str,
    key: str,
    *,
    default: int,
    dependencies: ReadRouteDependencies,
    trace_id: str,
) -> int:
    try:
        value = int(raw_value.strip() or str(default))
    except ValueError as error:
        raise dependencies.http_error(
            status_code=400,
            trace_id=trace_id,
            code="invalid_payload",
            message=f"Every Code pagination {key} must be an integer",
        ) from error
    if value < 0:
        raise dependencies.http_error(
            status_code=400,
            trace_id=trace_id,
            code="invalid_payload",
            message=f"Every Code pagination {key} must be non-negative",
        )
    return value


def every_code_optional_int(
    raw_value: str,
    key: str,
    *,
    dependencies: ReadRouteDependencies,
    trace_id: str,
) -> int | None:
    normalized_value = raw_value.strip()
    if not normalized_value:
        return None
    try:
        return int(normalized_value)
    except ValueError as error:
        raise dependencies.http_error(
            status_code=400,
            trace_id=trace_id,
            code="invalid_payload",
            message=f"Query parameter {key} must be an integer",
        ) from error
