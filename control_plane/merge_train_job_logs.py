"""Bounded Actions log reads without forwarding the API credential to storage."""

from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

MAX_JOB_LOG_BYTES = 2 * 1024 * 1024


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, req: Request, fp: object, code: int, msg: str, headers: object, newurl: str
    ) -> None:
        return None


def read_job_log(request: Request) -> str:
    opener = build_opener(_NoRedirect())
    try:
        opener.open(request, timeout=15).close()
    except HTTPError as error:
        if error.code != 302:
            raise
        location = error.headers.get("Location", "")
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
    with opener.open(Request(location, headers={"Accept": "text/plain"}), timeout=15) as response:
        payload: bytes = response.read(MAX_JOB_LOG_BYTES + 1)
    if len(payload) > MAX_JOB_LOG_BYTES:
        raise ValueError("GitHub job log exceeds the recovery evidence bound.")
    return payload.decode("utf-8")
