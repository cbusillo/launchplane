"""Read existing failure evidence without changing release execution."""

from control_plane.contracts.record_failures import RecordedFailureView, release_failure_reason


def client_release_failure(store: object, operation: object, kind: str) -> RecordedFailureView:
    result = getattr(operation, "result", None)
    record_id = str(getattr(result, "deployment_record_id", "") or "")
    reason = release_failure_reason(
        code=str(getattr(operation, "error_code", "") or f"{kind}_failed"),
        detail=str(getattr(operation, "error_message", "") or getattr(result, "error_message", "")),
    )
    if record_id:
        try:
            deployment = getattr(store, "read_deployment_record")(record_id)
        except FileNotFoundError:
            pass
        else:
            if deployment.failure is not None:
                reason = deployment.failure
    return RecordedFailureView(
        **reason.model_dump(),
        record_id=record_id or str(getattr(operation, "operation_id", "")),
        trace_id=str(getattr(operation, "runner_trace_id", "") or ""),
    )
