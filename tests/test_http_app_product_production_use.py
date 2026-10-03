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
_ROUTE = f"/v1/product-profiles/{_PRODUCT}/production-use"
_OLD = "unknown"
_NEW = "live"


def _profile() -> LaunchplaneProductProfileRecord:
    payload = product_profile_payload(_PRODUCT)
    payload["production_use"] = _OLD
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


def _apply(expected: str) -> dict[str, object]:
    return {
        "mode": "apply",
        "production_use": _NEW,
        "reviewed_plan_sha256": expected,
        "reason": "Classify current production use.",
    }


class ProductProductionUseHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_dry_run_shows_the_change_and_writes_nothing(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = _store(Path(temporary_directory_name))
            response = await _post(
                _app(store),
                {"production_use": _NEW, "reason": "Classify current production use."},
            )
            stored = store.read_product_profile_record(_PRODUCT)
            store.close()

        self.assertEqual(response.status_code, 202)
        result = response.json()["result"]
        self.assertEqual(
            (result["production_use_before"], result["production_use_after"]), (_OLD, _NEW)
        )
        self.assertTrue(result["changed"])
        self.assertFalse(result["applied"])
        self.assertTrue(result["plan_sha256"])
        self.assertEqual(stored, _profile())

    async def test_apply_changes_only_production_use_and_replays(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = _store(Path(temporary_directory_name))
            app = _app(store)
            dry = await _post(
                app, {"production_use": _NEW, "reason": "Classify current production use."}
            )
            digest = dry.json()["result"]["plan_sha256"]
            response = await _post(app, _apply(digest), idempotency_key="production-use-apply")
            replay = await _post(app, _apply(digest), idempotency_key="production-use-apply")
            stored = store.read_product_profile_record(_PRODUCT)
            store.close()

        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.json()["result"]["applied"])
        self.assertTrue(replay.json()["replayed"])
        self.assertEqual(stored.production_use, _NEW)
        unchanged = {"production_use", "updated_at", "source"}
        self.assertEqual(
            stored.model_dump(exclude=unchanged), _profile().model_dump(exclude=unchanged)
        )

    async def test_unknown_classification_is_refused(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = _store(Path(temporary_directory_name))
            response = await _post(
                _app(store),
                {"production_use": "not-a-classification", "reason": "Wrong package."},
            )
            store.close()

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_request")

    async def test_apply_refuses_when_the_profile_changed_since_the_dry_run(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = _store(Path(temporary_directory_name))
            response = await _post(_app(store), _apply(expected="0" * 64), "production-use-apply")
            stored = store.read_product_profile_record(_PRODUCT)
            store.close()

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "stale")
        self.assertEqual(stored.production_use, _OLD)

    async def test_a_caller_without_profile_write_is_refused(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = _store(Path(temporary_directory_name))
            response = await _post(
                _app(store, policy=_product_profile_write_policy(product="other-product")),
                _apply("0" * 64),
                "production-use-apply",
            )
            stored = store.read_product_profile_record(_PRODUCT)
            store.close()

        self.assertEqual(response.status_code, 403)
        self.assertEqual(stored.production_use, _OLD)

    async def test_reviewed_plan_binds_profile_value_and_reason(self) -> None:
        for change in ("profile", "value", "reason"):
            with self.subTest(change=change), TemporaryDirectory() as directory:
                store = _store(Path(directory))
                app = _app(store)
                dry = await _post(
                    app, {"production_use": _NEW, "reason": "Classify current production use."}
                )
                payload = _apply(dry.json()["result"]["plan_sha256"])
                if change == "profile":
                    original = store.read_product_profile_record(_PRODUCT)
                    store.write_product_profile_record(
                        original.model_copy(update={"updated_at": "2026-10-03T13:00:00Z"})
                    )
                elif change == "value":
                    payload["production_use"] = "prelaunch"
                else:
                    payload["reason"] = "A different reason"
                response = await _post(app, payload, "bound-plan")
                stored = store.read_product_profile_record(_PRODUCT)
                store.close()
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.json()["error"]["code"], "stale")
                self.assertEqual(stored.production_use, _OLD)

    async def test_prelaunch_and_unknown_can_be_applied_with_a_fresh_plan(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            app = _app(store)
            for value in ("prelaunch", "live", "unknown"):
                dry = await _post(
                    app, {"production_use": value, "reason": "Reviewed classification."}
                )
                apply = await _post(
                    app,
                    {
                        "mode": "apply",
                        "production_use": value,
                        "reason": "Reviewed classification.",
                        "reviewed_plan_sha256": dry.json()["result"]["plan_sha256"],
                    },
                    value,
                )
                self.assertEqual(apply.status_code, 202)
                self.assertEqual(store.read_product_profile_record(_PRODUCT).production_use, value)
            store.close()
