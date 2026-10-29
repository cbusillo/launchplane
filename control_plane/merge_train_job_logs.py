"""Bounded Actions log reads without forwarding the API credential to storage."""

from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from control_plane.github_request_timing import record_github_response
from control_plane.github_response_headers import (
    GitHubResponseHeadersObserver,
    notify_github_quota_response_headers,
)

MAX_JOB_LOG_BYTES = 2 * 1024 * 1024


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, req: Request, fp: object, code: int, msg: str, headers: object, newurl: str
    ) -> None:
        return None


def read_job_log(request: Request, *, observer: GitHubResponseHeadersObserver | None = None) -> str:
    opener = build_opener(_NoRedirect())
    try:
        opener.open(request, timeout=15).close()
    except HTTPError as error:
        if error.code != 302:
            raise
        record_github_response(error.code)
        notify_github_quota_response_headers(observer, error.headers)
        location = error.headers.get("Location", "")
        error.close()
        target = urlsplit(location)
        if (
            target.scheme != "https"
            or not target.hostname
            or not target.hostname.endswith(".blob.core.windows.net")
            or target.username
            or target.password
            or target.port not in {None, 443}
        ):
            raise ValueError("GitHub job log redirect is not trusted HTTPS storage.") from None
    else:
        raise ValueError("GitHub job log response did not supply a storage redirect.")
    # A second redirect is refused. Neither the API Authorization header nor
    # conditional cache headers ever accompany the storage request.
    try:
        with opener.open(
            Request(location, headers={"Accept": "text/plain"}), timeout=15
        ) as response:
            payload: bytes = response.read(MAX_JOB_LOG_BYTES + 1)
    except (HTTPError, URLError, OSError) as error:
        if isinstance(error, HTTPError):
            error.close()
        raise ValueError("GitHub job log storage evidence unavailable.") from None
    if len(payload) > MAX_JOB_LOG_BYTES:
        raise ValueError("GitHub job log exceeds the recovery evidence bound.")
    return payload.decode("utf-8")
