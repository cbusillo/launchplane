"""Start a disposable replacement API against a rehearsal database."""

import asyncio
import sys

from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import LaunchplaneAuthzPolicy
from control_plane.storage.postgres import PostgresRecordStore
from tests.http_app_test_support import _RejectingVerifier, _asgi_get


async def main(database_url: str) -> None:
    store = PostgresRecordStore(database_url=database_url)
    try:
        # Production bootstrap uses this factory path, not the app-owned store path.
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=LaunchplaneAuthzPolicy(),
            record_store_factory=lambda: store,
        )
        async with app.router.lifespan_context(app):
            response = await _asgi_get(app, "/v1/health")
            if response.status_code != 200 or response.json()["status"] != "ok":
                raise AssertionError(response.text)
    finally:
        store.close()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
