"""Docker authority failures, distinct from programs failing inside containers."""


def docker_daemon_authorization_denied(stderr: bytes) -> bool:
    return any(
        marker in stderr.lower()
        for marker in (
            b"permission denied while trying to connect to the docker ",
            b"authorization denied by plugin",
            b"error response from daemon: unauthorized",
            b"error response from daemon: access denied",
        )
    )
