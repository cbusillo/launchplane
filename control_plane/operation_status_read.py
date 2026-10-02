"""Structured operation status for callers that hold only a read action.

An operation's status read used to need the grant that starts the operation.
A caller holding only ``operations.read`` for the operation's context now gets
this view: ids, statuses, phases, times, the error code and the env-key names
the failure is about. Free text never appears here: no error message, request,
plan, checkpoint evidence or provider output, because redacting free text has
leaked target ids, hosts and IPs before (#2688).
"""

import re

from pydantic import BaseModel

from control_plane.contracts.odoo_stable_target_replacement_operation import (
    safe_error_detail_keys,
)

OPERATION_STATUS_READ_ACTION = "operations.read"
OPERATION_STATUS_READ_PRODUCT = "launchplane"

_SAFE_CODE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,95}$")
_SAFE_RECORD_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,191}$")
_RESULT_STEP_STATUSES = frozenset({"pending", "pass", "fail", "skipped"})
_UNRECOGNIZED_CODE = "unrecognized_code"

_OPERATION_TEXT_FIELDS = (
    "operation_id",
    "operation_kind",
    "product",
    "context",
    "instance",
    "status",
    "phase",
    "created_at",
    "updated_at",
    "started_at",
    "finished_at",
)
_RECORD_ID_FIELDS = ("deployment_record_id", "release_tuple_id", "artifact_id")


def operation_status_read_view(operation: BaseModel, *, poll_url: str) -> dict[str, object]:
    """The structured status of ``operation``, without any free text."""
    record = operation.model_dump(mode="json")
    view: dict[str, object] = {
        field: record[field] for field in _OPERATION_TEXT_FIELDS if field in record
    }
    view["attempt"] = int(record.get("attempt") or 0)
    view.update(_safe_record_ids(record))
    view["error_code"] = safe_operation_error_code(str(record.get("error_code") or ""))
    view["error_detail_keys"] = list(safe_error_detail_keys(record.get("error_detail_keys") or ()))
    checkpoints = record.get("checkpoints")
    if isinstance(checkpoints, list):
        view["checkpoints"] = [
            {"phase": checkpoint.get("phase", ""), "recorded_at": checkpoint.get("recorded_at", "")}
            for checkpoint in checkpoints
            if isinstance(checkpoint, dict)
        ]
    view["poll_url"] = poll_url
    view["free_text_omitted"] = True
    return view


def operation_result_read_view(result: BaseModel | None) -> dict[str, object] | None:
    """Each step status and record id of ``result``; nothing else."""
    if result is None:
        return None
    record = result.model_dump(mode="json")
    view: dict[str, object] = {
        field: value
        for field, value in record.items()
        if field.endswith("_status") and value in _RESULT_STEP_STATUSES
    }
    view.update(_safe_record_ids(record))
    return view


def _safe_record_ids(record: dict[str, object]) -> dict[str, str]:
    return {
        field: value
        for field in _RECORD_ID_FIELDS
        if isinstance(value := record.get(field), str) and _SAFE_RECORD_ID_PATTERN.match(value)
    }


def safe_operation_error_code(code: str) -> str:
    """``code`` when it looks like an error code; a fixed marker otherwise."""
    if not code:
        return ""
    return code if _SAFE_CODE_PATTERN.match(code) else _UNRECOGNIZED_CODE
