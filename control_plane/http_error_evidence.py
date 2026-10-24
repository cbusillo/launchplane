from collections import deque
import json
import logging
from pathlib import Path
from traceback import walk_tb


_logger = logging.getLogger(__name__)
_SOURCE_ROOT = Path(__file__).parent
_MAX_SOURCE_SITES = 8


def log_handled_error_cause(error: BaseException, trace_id: str) -> None:
    sites: deque[dict[str, str | int]] = deque(maxlen=_MAX_SOURCE_SITES)
    for frame, line in walk_tb(error.__traceback__):
        try:
            source = Path(frame.f_code.co_filename).relative_to(_SOURCE_ROOT)
        except ValueError:
            continue
        parts = source.with_suffix("").parts
        if source.suffix == ".py" and all(part.isidentifier() for part in parts):
            sites.append({"module": ".".join((_SOURCE_ROOT.name, *parts)), "line": line})
    _logger.warning(
        "%s",
        json.dumps({"event": "handled_error_cause", "trace_id": trace_id, "sites": list(sites)}),
    )
