from pathlib import Path

from control_plane.service_bootstrap import (
    serve_launchplane_service as _serve_launchplane_service,
)


def serve_launchplane_service(
    *,
    state_dir: Path,
    policy_file: Path,
    host: str,
    port: int,
    audience: str,
    database_url: str | None = None,
) -> None:
    _serve_launchplane_service(
        state_dir=state_dir,
        policy_file=policy_file,
        host=host,
        port=port,
        audience=audience,
        database_url=database_url,
    )
