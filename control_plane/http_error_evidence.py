import json
import logging
from pathlib import Path
from traceback import walk_tb


_logger = logging.getLogger(__name__)
_SOURCE_ROOT = Path(__file__).parent
_MAX_SOURCE_SITES = 8


def log_handled_error_cause(error: BaseException, trace_id: str, *, operation_id: str = "") -> None:
    sites: list[dict[str, str | int]] = []
    cause: BaseException | None = error
    seen: set[int] = set()
    while cause is not None and id(cause) not in seen and len(seen) < _MAX_SOURCE_SITES:
        seen.add(id(cause))
        site: dict[str, str | int] | None = None
        for frame, line in walk_tb(cause.__traceback__):
            try:
                source = Path(frame.f_code.co_filename).relative_to(_SOURCE_ROOT)
            except ValueError:
                continue
            parts = source.with_suffix("").parts
            if source.suffix == ".py" and all(part.isidentifier() for part in parts):
                site = {"module": ".".join((_SOURCE_ROOT.name, *parts)), "line": line}
        if site is not None:
            sites.append(site)
        cause = cause.__cause__ or (None if cause.__suppress_context__ else cause.__context__)
    evidence: dict[str, object] = {
        "event": "handled_error_cause",
        "trace_id": trace_id,
        "sites": sites,
    }
    if operation_id:
        evidence["operation_id"] = operation_id
    _logger.warning("%s", json.dumps(evidence))
