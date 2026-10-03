import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi import FastAPI
from httpx2 import Response

from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import LaunchplaneAuthzPolicy
from control_plane.storage.postgres import PostgresRecordStore
from tests.http_app_test_support import _asgi_request, _product_profile_write_policy
from tests.support.auth import StubVerifier, identity
from tests.support.profiles import product_profile_payload
from tests.support.stores import sqlite_database_url

_PRODUCT = "sellyouroutboard"
_ROUTE = f"/v1/product-profiles/{_PRODUCT}/image-repository"
_OLD = "ghcr.io/cbusillo/sellyouroutboard-app"
_NEW = "ghcr.io/cbusillo/sellyouroutboard"


def _profile() -> LaunchplaneProductProfileRecord:
    payload = product_profile_payload(_PRODUCT)
    payload["image"] = {"repository": _OLD}
    return LaunchplaneProductProfileRecord.model_validate(payload)


def _store(root: Path) -> PostgresRecordStore:
    store = PostgresRecordStore(database_url=sqlite_database_url(root / "launchplane.sqlite3"))
    store.ensure_schema()
    store.write_product_profile_record(_profile())
    return store


def _app(store: PostgresRecordStore, *, policy: LaunchplaneAuthzPolicy | None = None) -> FastAPI:
    return create_launchplane_fastapi_app(
        verifier=StubVerifier(identity()),
        authz_policy=policy or _product_profile_write_policy(product=_PRODUCT),
        record_store_factory=lambda: store,
    )


async def _post(app: FastAPI, payload: dict[str, object], idempotency_key: str = "") -> Response:
    headers = {"Authorization": "Bearer valid-token"}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    return await _asgi_request(app, "POST", _ROUTE, headers=headers, payload=payload)


def _apply(expected: str = _OLD) -> dict[str, object]:
    return {
        "mode": "apply",
        "image_repository": _NEW,
        "expected_image_repository": expected,
        "reason": "Publish to the package named after the repository.",
    }


class ProductImageRepositoryHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_dry_run_shows_the_change_and_writes_nothing(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = _store(Path(temporary_directory_name))
            response = await _post(
                _app(store),
                {"image_repository": _NEW, "reason": "Publish to the repository's package."},
            )
            stored = store.read_product_profile_record(_PRODUCT)
            store.close()

        self.assertEqual(response.status_code, 202)
        result = response.json()["result"]
        self.assertEqual(
            (result["image_repository_before"], result["image_repository_after"]), (_OLD, _NEW)
        )
        self.assertTrue(result["changed"])
        self.assertFalse(result["applied"])
        self.assertEqual([lane["instance"] for lane in result["lanes"]], ["testing"])
        self.assertEqual(stored, _profile())

    async def test_apply_changes_only_the_image_repository_and_replays(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = _store(Path(temporary_directory_name))
            app = _app(store)
            response = await _post(app, _apply(), idempotency_key="image-apply")
            replay = await _post(app, _apply(), idempotency_key="image-apply")
            stored = store.read_product_profile_record(_PRODUCT)
            store.close()

        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.json()["result"]["applied"])
        self.assertTrue(replay.json()["replayed"])
        self.assertEqual(stored.image.repository, _NEW)
        unchanged = {"image", "updated_at", "source"}
        self.assertEqual(
            stored.model_dump(exclude=unchanged), _profile().model_dump(exclude=unchanged)
        )

    async def test_only_the_package_named_after_the_repository_is_accepted(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = _store(Path(temporary_directory_name))
            response = await _post(
                _app(store),
                {"image_repository": "ghcr.io/cbusillo/other", "reason": "Wrong package."},
            )
            store.close()

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "image_repository_not_repository_named")

    async def test_apply_refuses_when_the_profile_changed_since_the_dry_run(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = _store(Path(temporary_directory_name))
            response = await _post(
                _app(store), _apply(expected="ghcr.io/cbusillo/elsewhere"), "image-apply"
            )
            stored = store.read_product_profile_record(_PRODUCT)
            store.close()

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "stale")
        self.assertEqual(stored.image.repository, _OLD)

    async def test_a_caller_without_profile_write_is_refused(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = _store(Path(temporary_directory_name))
            response = await _post(
                _app(store, policy=_product_profile_write_policy(product="other-product")),
                _apply(),
                "image-apply",
            )
            stored = store.read_product_profile_record(_PRODUCT)
            store.close()

        self.assertEqual(response.status_code, 403)
        self.assertEqual(stored.image.repository, _OLD)
