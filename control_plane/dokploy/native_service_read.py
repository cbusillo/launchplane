from typing import Any

from control_plane.docker_errors import docker_daemon_authorization_denied


def read_stored_docker_service(
    service_id: str, api_version: str, *, max_bytes: int, timeout_seconds: float = 60
) -> dict[str, Any]:
    import http.client
    import json
    import re
    import subprocess
    import threading
    from typing import IO, cast

    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", service_id) or not re.fullmatch(
        r"1\.[0-9]{1,3}", api_version
    ):
        raise ValueError("Stored service coordinates are invalid")

    class ResponsePipe:
        def __init__(self, stream: IO[bytes]) -> None:
            self.stream = stream

        def makefile(self, _mode: str) -> Any:
            return self

        def read(self, size: int = -1) -> bytes:
            return self.stream.read(size)

        def readline(self, size: int = -1) -> bytes:
            return self.stream.readline(size)

        def close(self) -> None:
            return None

        def flush(self) -> None:
            self.stream.flush()

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate service field")
            result[key] = value
        return result

    def reject_constant(_value: str) -> Any:
        raise ValueError("Non-JSON service value")

    request = (
        f"GET /v{api_version}/services/{service_id}?insertDefaults=false HTTP/1.1\r\n"
        "Host: docker\r\nConnection: close\r\n\r\n"
    ).encode("ascii")
    try:
        with subprocess.Popen(
            ["docker", "system", "dial-stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        ) as process:
            timer = threading.Timer(timeout_seconds, process.kill)
            timer.daemon = True
            timer.start()
            try:
                assert process.stdin is not None and process.stdout is not None
                process.stdin.write(request)
                process.stdin.flush()
                with http.client.HTTPResponse(cast(Any, ResponsePipe(process.stdout))) as response:
                    response.begin()
                    status = response.status
                    lengths = response.headers.get_all("Content-Length", [])
                    transfers = response.headers.get_all("Transfer-Encoding", [])
                    if (
                        response.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
                        != "application/json"
                        or response.getheader("Content-Encoding") is not None
                        or bool(lengths) == bool(transfers)
                        or len(lengths) > 1
                        or len(transfers) > 1
                        or (transfers and transfers[0].strip().lower() != "chunked")
                        or (
                            lengths
                            and (
                                not re.fullmatch(r"[0-9]+", lengths[0])
                                or int(lengths[0]) > max_bytes
                            )
                        )
                    ):
                        raise ValueError("Stored service response framing is unavailable")
                    body = response.read(max_bytes + 1)
                    if (
                        len(body) > max_bytes
                        or response.length not in (None, 0)
                        or not response.isclosed()
                    ):
                        raise ValueError("Stored service response is incomplete")
                    if process.stdout.read(1):
                        raise ValueError("Stored service response has trailing data")
                if process.wait() != 0:
                    raise RuntimeError("Stored service transport did not complete")
                if status in (401, 403) and docker_daemon_authorization_denied(body):
                    raise PermissionError("The managed testing service read was refused")
                if status != 200:
                    raise RuntimeError("Stored service read did not complete")
                result = json.loads(
                    body, object_pairs_hook=unique_object, parse_constant=reject_constant
                )
                if not isinstance(result, dict):
                    raise ValueError("Stored service object is unavailable")
                return result
            finally:
                timer.cancel()
                if process.poll() is None:
                    process.kill()
                process.wait()
    except PermissionError:
        raise
    except (http.client.HTTPException, OSError, ValueError):
        raise RuntimeError("Stored service read is unavailable") from None
