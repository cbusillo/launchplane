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
from control_plane.contracts.product_profile_record import ProductOwnerProfile
from control_plane.service_human_auth import HumanSessionManager, InMemoryHumanSessionStore
from tests.http_app_test_support import (
    _browser_mutation_headers,
    _github_human_identity,
    _github_human_product_config_policy,
    _github_oauth_config,
    _RejectingVerifier,
)

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
    async def test_raw_profile_write_cannot_enable_standing_or_replace_its_client(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            self.addCleanup(store.close)
            original = store.read_product_profile_record(_PRODUCT)
            standing = original.model_copy(
                update={
                    "owner": ProductOwnerProfile(github_login="example-client", github_id="1234"),
                    "release_on_acceptance": "director_standing",
                }
            )
            app = _app(store)
            response = await _asgi_request(
                app,
                "POST",
                "/v1/product-profiles",
                headers={"Authorization": "Bearer valid-token"},
                payload=standing.model_dump(mode="json"),
            )
            self.assertEqual(response.status_code, 409, response.text)
            self.assertEqual(store.read_product_profile_record(_PRODUCT), original)
            store.write_product_profile_record(standing)
            replacement = standing.model_copy(
                update={
                    "owner": ProductOwnerProfile(github_login="another-client", github_id="5678"),
                }
            )
            response = await _asgi_request(
                app,
                "POST",
                "/v1/product-profiles",
                headers={"Authorization": "Bearer valid-token"},
                payload=replacement.model_dump(mode="json"),
            )
            self.assertEqual(response.status_code, 409, response.text)
            self.assertEqual(store.read_product_profile_record(_PRODUCT), standing)

    async def test_standing_acceptance_requires_signed_in_admin_and_reviewed_profile(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            self.addCleanup(store.close)
            stored = store.read_product_profile_record(_PRODUCT)
            store.write_product_profile_record(
                stored.model_copy(
                    update={
                        "owner": ProductOwnerProfile(
                            github_login="example-client", github_id="1234"
                        ),
                    }
                )
            )
            payload: dict[str, object] = {
                "production_use": _OLD,
                "release_on_acceptance": "director_standing",
                "reason": "The recorded Client is the Director.",
            }
            dry = await _post(_app(store), payload)
            self.assertEqual(dry.status_code, 202, dry.text)
            apply = {
                **payload,
                "mode": "apply",
                "reviewed_plan_sha256": dry.json()["result"]["plan_sha256"],
            }
            denied = await _post(_app(store), apply, idempotency_key="machine-standing")
            self.assertEqual(denied.status_code, 403)
            self.assertEqual(
                store.read_product_profile_record(_PRODUCT).release_on_acceptance, "held"
            )
            sessions = HumanSessionManager(
                config=_github_oauth_config(), session_store=InMemoryHumanSessionStore()
            )
            session = sessions.issue(_github_human_identity())
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=_github_human_product_config_policy(
                    action="product_profile.write", product=_PRODUCT, context="launchplane"
                ),
                record_store_factory=lambda: store,
                human_session_manager=sessions,
            )
            headers = {
                **_browser_mutation_headers(sessions, session),
                "Idempotency-Key": "human-standing",
            }
            accepted = await _asgi_request(app, "POST", _ROUTE, headers=headers, payload=apply)
            self.assertEqual(accepted.status_code, 202, accepted.text)
            self.assertEqual(
                store.read_product_profile_record(_PRODUCT).release_on_acceptance,
                "director_standing",
            )

    async def test_standing_acceptance_without_client_and_generic_web_drill_are_refused(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            self.addCleanup(store.close)
            for mode in ("director_standing", "promote_with_rollback_drill"):
                response = await _post(
                    _app(store),
                    {
                        "production_use": _OLD,
                        "release_on_acceptance": mode,
                        "reason": "Configure releases.",
                    },
                )
                self.assertEqual(response.status_code, 400, response.text)
            self.assertEqual(
                store.read_product_profile_record(_PRODUCT).release_on_acceptance, "held"
            )

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

    async def test_admin_switches_release_on_acceptance_and_holds_it_again(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = _store(Path(temporary_directory_name))
            app = _app(store)
            for index, mode in enumerate(("promote", "held")):
                request: dict[str, object] = {
                    "production_use": _OLD,
                    "release_on_acceptance": mode,
                    "reason": "Start releases when the Client accepts.",
                }
                dry = await _post(app, request)
                self.assertEqual(dry.json()["result"]["release_on_acceptance_after"], mode)
                applied = await _post(
                    app,
                    {
                        **request,
                        "mode": "apply",
                        "reviewed_plan_sha256": dry.json()["result"]["plan_sha256"],
                    },
                    idempotency_key=f"release-on-acceptance-{index}",
                )
                self.assertEqual(applied.status_code, 202, applied.text)
                self.assertEqual(
                    store.read_product_profile_record(_PRODUCT).release_on_acceptance, mode
                )
            stored = store.read_product_profile_record(_PRODUCT)
            store.close()

        # Held again, the profile serializes as it did before the switch existed.
        unchanged = {"updated_at", "source"}
        self.assertEqual(
            stored.model_dump(mode="json", exclude=unchanged),
            _profile().model_dump(mode="json", exclude=unchanged),
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
