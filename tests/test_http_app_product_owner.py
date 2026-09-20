import unittest
from collections.abc import Callable
from email.message import Message
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.error import HTTPError

import click
from fastapi import FastAPI
from httpx2 import Response

from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import LaunchplaneAuthzPolicy
from control_plane.service_human_auth import HumanSessionManager, InMemoryHumanSessionStore
from control_plane.storage.postgres import PostgresRecordStore
from tests.http_app_test_support import (
    _asgi_request,
    _browser_mutation_headers,
    _github_human_identity,
    _github_human_product_config_policy,
    _github_oauth_config,
    _product_profile_write_policy,
    _RejectingVerifier,
)
from tests.support.auth import StubVerifier, identity
from tests.support.profiles import product_profile_payload
from tests.support.stores import sqlite_database_url

_PRODUCT = "sellyouroutboard"
_OWNER_ROUTE = f"/v1/product-profiles/{_PRODUCT}/owner"


def _profile(*, owner: dict[str, str] | None = None) -> LaunchplaneProductProfileRecord:
    payload = product_profile_payload(_PRODUCT)
    if owner is not None:
        payload["owner"] = owner
    return LaunchplaneProductProfileRecord.model_validate(payload)


def _store(root: Path, profile: LaunchplaneProductProfileRecord) -> PostgresRecordStore:
    store = PostgresRecordStore(database_url=sqlite_database_url(root / "launchplane.sqlite3"))
    store.ensure_schema()
    store.write_product_profile_record(profile)
    return store


def _github_user(
    *, login: str = "Site-Owner", user_id: int = 4242, account_type: str = "User"
) -> Callable[..., object]:
    def api_request(*, path: str, token: str) -> object:
        assert token == "managed-token"
        assert path.lower() == f"/users/{login}".lower()
        return {"login": login, "id": user_id, "type": account_type}

    return api_request


def _github_user_missing(*, path: str, token: str) -> object:
    try:
        raise HTTPError(f"https://api.github.com{path}", 404, "Not Found", Message(), None)
    except HTTPError as error:
        raise click.ClickException(f"GitHub API request failed for {path}: {error}") from error


async def _post_owner(
    app: FastAPI,
    payload: dict[str, object],
    *,
    github_api: Callable[..., object] = _github_user(),
    idempotency_key: str = "",
    headers: dict[str, str] | None = None,
) -> Response:
    request_headers = dict(headers) if headers else {"Authorization": "Bearer valid-token"}
    if idempotency_key:
        request_headers["Idempotency-Key"] = idempotency_key
    with (
        patch(
            "control_plane.http_app.resolve_launchplane_github_token",
            return_value="managed-token",
        ),
        patch("control_plane.http_app.github_api_request", side_effect=github_api),
    ):
        return await _asgi_request(
            app, "POST", _OWNER_ROUTE, headers=request_headers, payload=payload
        )


def _workflow_app(
    store: PostgresRecordStore, *, policy: LaunchplaneAuthzPolicy | None = None
) -> FastAPI:
    return create_launchplane_fastapi_app(
        verifier=StubVerifier(identity()),
        authz_policy=policy or _product_profile_write_policy(product=_PRODUCT),
        record_store_factory=lambda: store,
    )


class ProductOwnerSettingHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_dry_run_resolves_login_and_writes_nothing(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            original_profile = _profile()
            store = _store(Path(temporary_directory_name), original_profile)
            response = await _post_owner(
                _workflow_app(store),
                {"mode": "dry-run", "github_login": "@site-owner", "reason": "Name the Owner."},
            )
            stored_profile = store.read_product_profile_record(_PRODUCT)
            store.close()

        self.assertEqual(response.status_code, 202)
        result = response.json()["result"]
        self.assertEqual(result["operation"], "set")
        self.assertEqual(result["resolved_github_login"], "Site-Owner")
        self.assertEqual(result["resolved_github_id"], "4242")
        self.assertEqual(result["owner_before"], {"github_login": "", "github_id": ""})
        self.assertEqual(result["owner_after"], {"github_login": "Site-Owner", "github_id": "4242"})
        self.assertFalse(result["applied"])
        self.assertEqual(stored_profile, original_profile)

    async def test_apply_sets_only_the_owner_and_replays(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            original_profile = _profile()
            store = _store(Path(temporary_directory_name), original_profile)
            app = _workflow_app(store)
            payload: dict[str, object] = {
                "mode": "apply",
                "github_login": "site-owner",
                "reason": "Name the Owner.",
            }
            response = await _post_owner(app, payload, idempotency_key="owner-apply")
            replay_response = await _post_owner(app, payload, idempotency_key="owner-apply")
            stored_profile = store.read_product_profile_record(_PRODUCT)
            store.close()

        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.json()["result"]["applied"])
        self.assertTrue(replay_response.json()["replayed"])
        self.assertEqual(stored_profile.owner.github_login, "Site-Owner")
        self.assertEqual(stored_profile.owner.github_id, "4242")
        self.assertEqual(stored_profile.source, "service:product-owner")
        self.assertNotEqual(stored_profile.updated_at, original_profile.updated_at)
        untouched_fields = {"owner", "updated_at", "source"}
        self.assertEqual(
            stored_profile.model_dump_json(exclude=untouched_fields),
            original_profile.model_dump_json(exclude=untouched_fields),
        )

    async def test_apply_requires_idempotency_key(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = _store(Path(temporary_directory_name), _profile())
            response = await _post_owner(
                _workflow_app(store),
                {"mode": "apply", "github_login": "site-owner", "reason": "Name the Owner."},
            )
            stored_profile = store.read_product_profile_record(_PRODUCT)
            store.close()

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "idempotency_key_required")
        self.assertFalse(stored_profile.owner.is_set)

    async def test_unknown_login_is_rejected_and_nothing_is_written(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            original_profile = _profile()
            store = _store(Path(temporary_directory_name), original_profile)
            response = await _post_owner(
                _workflow_app(store),
                {"mode": "apply", "github_login": "nobody-here", "reason": "Name the Owner."},
                github_api=_github_user_missing,
                idempotency_key="owner-unknown",
            )
            stored_profile = store.read_product_profile_record(_PRODUCT)
            store.close()

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "owner_login_not_found")
        self.assertEqual(stored_profile, original_profile)

    async def test_organization_account_is_rejected_and_nothing_is_written(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            original_profile = _profile()
            store = _store(Path(temporary_directory_name), original_profile)
            response = await _post_owner(
                _workflow_app(store),
                {"mode": "apply", "github_login": "example-org", "reason": "Name the Owner."},
                github_api=_github_user(login="example-org", account_type="Organization"),
                idempotency_key="owner-organization",
            )
            stored_profile = store.read_product_profile_record(_PRODUCT)
            store.close()

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "owner_login_not_user")
        self.assertEqual(stored_profile, original_profile)

    async def test_missing_github_credential_fails_closed(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = _store(Path(temporary_directory_name), _profile())
            with patch("control_plane.http_app.resolve_launchplane_github_token", return_value=""):
                response = await _asgi_request(
                    _workflow_app(store),
                    "POST",
                    _OWNER_ROUTE,
                    headers={"Authorization": "Bearer valid-token"},
                    payload={"github_login": "site-owner", "reason": "Name the Owner."},
                )
            store.close()

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "github_credentials_unavailable")

    async def test_clear_removes_the_owner_without_a_github_lookup(self) -> None:
        def unexpected_lookup(*, path: str, token: str) -> object:
            raise AssertionError(f"clear must not look up {path}")

        with TemporaryDirectory() as temporary_directory_name:
            store = _store(
                Path(temporary_directory_name),
                _profile(
                    owner={
                        "github_login": "Site-Owner",
                        "github_id": "4242",
                        "review_label": "needs-owner",
                    }
                ),
            )
            response = await _post_owner(
                _workflow_app(store),
                {"mode": "apply", "clear": True, "reason": "Owner left the business."},
                github_api=unexpected_lookup,
                idempotency_key="owner-clear",
            )
            stored_profile = store.read_product_profile_record(_PRODUCT)
            store.close()

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["result"]["operation"], "clear")
        self.assertFalse(stored_profile.owner.is_set)
        self.assertEqual(stored_profile.owner.review_label, "needs-owner")

    async def test_caller_without_profile_write_is_denied(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            original_profile = _profile()
            store = _store(Path(temporary_directory_name), original_profile)
            response = await _post_owner(
                _workflow_app(store, policy=_product_profile_write_policy(product="other-product")),
                {"mode": "apply", "github_login": "site-owner", "reason": "Name the Owner."},
                idempotency_key="owner-denied",
            )
            stored_profile = store.read_product_profile_record(_PRODUCT)
            store.close()

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")
        self.assertEqual(stored_profile, original_profile)

    async def test_profile_changed_during_apply_is_rejected_not_overwritten(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = _store(Path(temporary_directory_name), _profile())
            concurrent_profile = _profile().model_copy(
                update={
                    "display_name": "Renamed Concurrently",
                    "updated_at": "2026-05-01T00:00:00Z",
                }
            )

            def lookup_during_concurrent_write(*, path: str, token: str) -> object:
                store.write_product_profile_record(concurrent_profile)
                return _github_user()(path=path, token=token)

            response = await _post_owner(
                _workflow_app(store),
                {"mode": "apply", "github_login": "site-owner", "reason": "Name the Owner."},
                github_api=lookup_during_concurrent_write,
                idempotency_key="owner-race",
            )
            stored_profile = store.read_product_profile_record(_PRODUCT)
            store.close()

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "stale")
        self.assertEqual(stored_profile, concurrent_profile)

    async def test_signed_in_operator_needs_browser_mutation_protection(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = _store(Path(temporary_directory_name), _profile())
            session_manager = HumanSessionManager(
                config=_github_oauth_config(),
                session_store=InMemoryHumanSessionStore(),
            )
            human_session = session_manager.issue(_github_human_identity())
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=_github_human_product_config_policy(
                    action="product_profile.write",
                    product=_PRODUCT,
                    context="launchplane",
                ),
                record_store_factory=lambda: store,
                human_session_manager=session_manager,
            )
            payload: dict[str, object] = {
                "mode": "apply",
                "github_login": "site-owner",
                "reason": "Name the Owner.",
            }
            unprotected_response = await _post_owner(
                app,
                payload,
                idempotency_key="owner-browser-unprotected",
                headers={"Cookie": session_manager.session_cookie_header(human_session)},
            )
            owner_after_unprotected = store.read_product_profile_record(_PRODUCT).owner
            protected_response = await _post_owner(
                app,
                payload,
                idempotency_key="owner-browser",
                headers=_browser_mutation_headers(session_manager, human_session),
            )
            stored_profile = store.read_product_profile_record(_PRODUCT)
            store.close()

        self.assertEqual(unprotected_response.status_code, 403)
        self.assertFalse(owner_after_unprotected.is_set)
        self.assertEqual(protected_response.status_code, 202)
        self.assertEqual(stored_profile.owner.github_id, "4242")


if __name__ == "__main__":
    unittest.main()
