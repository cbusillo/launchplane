from __future__ import annotations

from dataclasses import dataclass
import hashlib
import logging
import re
import subprocess
from typing import Literal


_MAX_SAFE_DETAIL_LENGTH = 240
_MAX_SAFE_DIAGNOSTIC_LENGTH = 160
_MAX_SAFE_OPERATION_LENGTH = 96
_LOGGER = logging.getLogger(__name__)
_URL_PATTERN = re.compile(r"(?i)\b(?:https?|ssh)://[^\s<>\"'`]+")
_SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"""(?ix)
    \b
    (?P<name>
        [a-z0-9_.-]*
        (?:token|secret|password|passwd|api[_-]?key|credential|private[_-]?key|database[_-]?url)
        [a-z0-9_.-]*
    )
    \s*[:=]\s*
    (?P<value>\"[^\"\n]*\"|'[^'\n]*'|[^\s,;]+)
    """
)
_SECRET_OPTION_PATTERN = re.compile(
    r"""(?ix)
    (?P<option>--(?:token|secret|password|passwd|api[-_]key|credential|private[-_]key))
    \s+
    (?P<value>\"[^\"\n]*\"|'[^'\n]*'|\S+)
    """
)
_AUTHORIZATION_HEADER_PATTERN = re.compile(
    r"(?i)\b(?P<name>(?:proxy-)?authorization)\s*:\s*"
    r"(?:(?P<scheme>bearer|basic)\s+)?[^\r\n]*"
)
_COOKIE_HEADER_PATTERN = re.compile(r"(?i)\b(?P<name>set-cookie|cookie)\s*:[^\r\n]*")
_BEARER_PATTERN = re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]{4,}")
_GITHUB_TOKEN_PATTERN = re.compile(r"(?i)\b(?:gh[pousr]_[a-z0-9_]+|github_pat_[a-z0-9_]+)\b")
_LONG_SECRET_PATTERN = re.compile(r"(?<![a-zA-Z0-9])[a-zA-Z0-9.~+/-]{32,}(?![a-zA-Z0-9])")
_PATH_PATTERN = re.compile(r"(?<![\w.-])(?:~|/)(?:[^\s,;:=<>\"']+/)+[^\s,;:=<>\"']+")
_INTERNAL_HOST_PATTERN = re.compile(
    r"""(?ix)
    (?<![a-z0-9_.-])
    (?:
        localhost|
        host\.docker\.internal|
        0\.0\.0\.0|
        127(?:\.\d{1,3}){3}|
        10(?:\.\d{1,3}){3}|
        192\.168(?:\.\d{1,3}){2}|
        172\.(?:1[6-9]|2[0-9]|3[0-1])(?:\.\d{1,3}){2}|
        [a-z0-9][a-z0-9.-]*\.(?:internal|local|lan|home|corp)
    )
    (?::\d{1,5})?
    (?![a-z0-9_.-])
    """
)


ProcessTool = Literal["child_process", "github_cli"]


@dataclass(frozen=True)
class ChildProcessFailure:
    code: str
    detail: str
    correlation_id: str
    exit_code: int | None

    def operator_message(self) -> str:
        return f"{self.detail} [error_code={self.code} correlation_id={self.correlation_id}]"


def redact_untrusted_text(
    value: str,
    *,
    fallback: str,
    maximum_length: int = _MAX_SAFE_DETAIL_LENGTH,
) -> str:
    """Return bounded text safe for records, API responses, and operator output."""

    if maximum_length < 4:
        raise ValueError("maximum_length must be at least 4")
    text = _redact_text(value)
    if not text:
        text = _redact_text(fallback)
    return _bound_text(text, maximum_length)


def normalize_child_process_failure(
    *,
    operation: str,
    tool: ProcessTool,
    returncode: int | None = None,
    stdout: str = "",
    stderr: str = "",
    exception: OSError | subprocess.CalledProcessError | None = None,
) -> ChildProcessFailure:
    """Normalize subprocess failure output without retaining raw child-process text."""

    resolved_returncode = returncode
    resolved_stdout = stdout
    resolved_stderr = stderr
    if isinstance(exception, subprocess.CalledProcessError):
        if resolved_returncode is None:
            resolved_returncode = exception.returncode
        if not resolved_stdout:
            resolved_stdout = _string_value(exception.stdout)
        if not resolved_stderr:
            resolved_stderr = _string_value(exception.stderr)

    if isinstance(exception, FileNotFoundError):
        code = f"{tool}_unavailable"
        outcome = "is unavailable"
    elif isinstance(exception, OSError):
        code = f"{tool}_start_failed"
        outcome = "could not start"
    else:
        code = f"{tool}_failed"
        outcome = "failed"
        if resolved_returncode is not None:
            outcome = f"{outcome} with exit status {resolved_returncode}"

    safe_operation = redact_untrusted_text(
        operation,
        fallback="Child-process operation",
        maximum_length=_MAX_SAFE_OPERATION_LENGTH,
    )
    raw_diagnostic = resolved_stderr or resolved_stdout
    if not raw_diagnostic and exception is not None:
        raw_diagnostic = str(exception)
    safe_diagnostic = redact_untrusted_text(
        raw_diagnostic,
        fallback="",
        maximum_length=_MAX_SAFE_DIAGNOSTIC_LENGTH,
    )
    detail = f"{safe_operation} {outcome}"
    if safe_diagnostic:
        detail = f"{detail}: {safe_diagnostic}"
    else:
        detail = f"{detail}."
    detail = redact_untrusted_text(
        detail,
        fallback="Child-process operation failed.",
        maximum_length=_MAX_SAFE_DETAIL_LENGTH,
    )
    correlation_source = "\x1f".join((code, detail, str(resolved_returncode)))
    correlation_id = f"cpf-{hashlib.sha256(correlation_source.encode()).hexdigest()[:16]}"
    failure = ChildProcessFailure(
        code=code,
        detail=detail,
        correlation_id=correlation_id,
        exit_code=resolved_returncode,
    )
    _LOGGER.info(
        "Normalized child-process failure: error_code=%s correlation_id=%s exit_code=%s",
        failure.code,
        failure.correlation_id,
        failure.exit_code,
    )
    return failure


def _redact_text(value: str) -> str:
    text = _AUTHORIZATION_HEADER_PATTERN.sub(
        lambda match: (
            f"{match.group('name')}: {match.group('scheme').title()} [redacted]"
            if match.group("scheme")
            else f"{match.group('name')}: [redacted]"
        ),
        value.strip(),
    )
    text = _COOKIE_HEADER_PATTERN.sub(
        lambda match: f"{match.group('name')}: [redacted]",
        text,
    )
    text = " ".join(text.split())
    if not text:
        return ""
    text = _URL_PATTERN.sub("[redacted-url]", text)
    text = _SECRET_ASSIGNMENT_PATTERN.sub(
        lambda match: f"{match.group('name')}=[redacted]",
        text,
    )
    text = _SECRET_OPTION_PATTERN.sub(lambda match: f"{match.group('option')} [redacted]", text)
    text = _BEARER_PATTERN.sub("Bearer [redacted]", text)
    text = _GITHUB_TOKEN_PATTERN.sub("[redacted-token]", text)
    text = _PATH_PATTERN.sub("[redacted-path]", text)
    text = _INTERNAL_HOST_PATTERN.sub("[redacted-host]", text)
    return _LONG_SECRET_PATTERN.sub("[redacted-secret]", text)


def _bound_text(value: str, maximum_length: int) -> str:
    if len(value) <= maximum_length:
        return value
    return value[: maximum_length - 3].rstrip() + "..."


def _string_value(value: object | None) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)
