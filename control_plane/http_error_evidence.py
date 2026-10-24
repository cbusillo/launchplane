import json
import logging
from pathlib import Path
from traceback import walk_tb
from typing import get_args, get_origin

from pydantic import BaseModel, ValidationError
from pydantic_core import ErrorDetails
from pydantic_core.core_schema import ErrorType


_logger = logging.getLogger(__name__)
_SOURCE_ROOT = Path(__file__).parent
_MAX_SOURCE_SITES = 8
_MAX_VALIDATION_ERRORS = 4
_MAX_VALIDATION_LOCATION = 6
_VALIDATION_ERROR_TYPES = frozenset(get_args(ErrorType))


def _source_site(error: BaseException) -> dict[str, str | int] | None:
    site: dict[str, str | int] | None = None
    for frame, line in walk_tb(error.__traceback__):
        try:
            source = Path(frame.f_code.co_filename).relative_to(_SOURCE_ROOT)
        except ValueError:
            continue
        parts = source.with_suffix("").parts
        if source.suffix == ".py" and all(part.isidentifier() for part in parts):
            site = {"module": ".".join((_SOURCE_ROOT.name, *parts)), "line": line}
    return site


def _field_location(model: type[BaseModel], location: tuple[str | int, ...]) -> list[str | int]:
    current: object = model
    fields: list[str | int] = []
    for part in location[:_MAX_VALIDATION_LOCATION]:
        if isinstance(current, type) and issubclass(current, BaseModel):
            if not isinstance(part, str) or part not in current.model_fields:
                fields.append("dynamic_field")
                break
            fields.append(part)
            current = current.model_fields[part].annotation
        elif get_origin(current) in (tuple, list) and isinstance(part, int):
            fields.append(part)
            current = get_args(current)[0]
        else:
            break
    return fields


def _validation_entry(item: ErrorDetails, model: type[BaseModel]) -> dict[str, object]:
    root = item.get("ctx", {}).get("error")
    site = _source_site(root) if isinstance(root, BaseException) else None
    return {
        "model": model.__name__,
        "type": item["type"] if item["type"] in _VALIDATION_ERROR_TYPES else "custom_error",
        "loc": _field_location(model, item["loc"]),
        "sites": [site] if site is not None else [],
    }


class ProviderResponseValidationError(ValueError):
    def __init__(self, error: ValidationError, model: type[BaseModel]) -> None:
        super().__init__("Provider response failed model validation.")
        self.validation = tuple(
            _validation_entry(item, model)
            for item in error.errors(include_input=False, include_context=True, include_url=False)[
                :_MAX_VALIDATION_ERRORS
            ]
        )
        self.error_count = error.error_count()


def log_handled_error_cause(error: BaseException, trace_id: str, *, operation_id: str = "") -> None:
    from control_plane.dokploy.native_copy_stream import NativeProgramFailed

    sites: list[dict[str, str | int]] = []
    native_programs: list[dict[str, object]] = []
    validation: list[dict[str, object]] = []
    validation_error_count = 0
    cause: BaseException | None = error
    seen: set[int] = set()
    while cause is not None and id(cause) not in seen and len(seen) < _MAX_SOURCE_SITES:
        seen.add(id(cause))
        site = _source_site(cause)
        if site is not None:
            sites.append(site)
        if isinstance(cause, ProviderResponseValidationError):
            validation.extend(cause.validation[: _MAX_VALIDATION_ERRORS - len(validation)])
            validation_error_count += cause.error_count
        if isinstance(cause, NativeProgramFailed) and cause.program_sites:
            native_programs.append(
                {"sha": cause.program_sha256, "lines": list(cause.program_sites)}
            )
        cause = cause.__cause__ or (None if cause.__suppress_context__ else cause.__context__)
    evidence: dict[str, object] = {
        "event": "handled_error_cause",
        "trace_id": trace_id,
        "sites": sites,
    }
    if operation_id:
        evidence["operation_id"] = operation_id
    if validation:
        evidence["validation"] = validation
        evidence["validation_error_count"] = validation_error_count
    if native_programs:
        evidence["native_programs"] = native_programs
    _logger.warning("%s", json.dumps(evidence))
