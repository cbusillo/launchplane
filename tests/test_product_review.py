import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi import FastAPI
from httpx2 import Response
from pydantic import ValidationError

from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.product_review import ProductReviewDecisionRecord
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import GitHubHumanIdentity, LaunchplaneAuthzPolicy, TokenVerifier
from control_plane.service_human_auth import HumanSessionManager, InMemoryHumanSessionStore
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore
from tests.http_app_test_support import (
    _asgi_get,
    _asgi_request,
    _browser_mutation_headers,
    _github_oauth_config,
    _preview_generation_read_record,
    _preview_read_record,
    _RejectingVerifier,
)
from tests.support.auth import StubVerifier, identity
from tests.support.profiles import product_profile_payload
from tests.support.stores import sqlite_database_url

_REPOSITORY = "every/example-site"
_PULL_REQUEST = 42
_OWNER_GITHUB_ID = 9001
_REVIEW_PATH = f"/v1/product-review?repository={_REPOSITORY}&pull_request={_PULL_REQUEST}"


def _human(*, login: str, github_id: int) -> GitHubHumanIdentity:
    return GitHubHumanIdentity(
        login=login,
        github_id=github_id,
        name=login,
        email=f"{login}@example.com",
        organizations=frozenset(),
        teams=frozenset(),
        role="read_only",
    )


def _profile(*, owner_set: bool = True) -> LaunchplaneProductProfileRecord:
    payload = product_profile_payload("example-site")
    payload["repository"] = _REPOSITORY
    payload["preview"] = {"enabled": True, "context": "example-site"}
    if owner_set:
        payload["owner"] = {"github_login": "site-owner", "github_id": str(_OWNER_GITHUB_ID)}
    return LaunchplaneProductProfileRecord.model_validate(payload)


def _operator_read_policy() -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_humans": [
                {
                    "logins": ["example-operator"],
                    "roles": ["read_only"],
                    "products": ["example-site"],
                    "contexts": ["launchplane"],
                    "actions": ["product_profile.read"],
                }
            ],
        }
    )


def _decision(
    *, record_id: str, decided_at: str, pull_request_number: int = _PULL_REQUEST
) -> ProductReviewDecisionRecord:
    return ProductReviewDecisionRecord(
        record_id=record_id,
        product="example-site",
        repository=_REPOSITORY,
        pull_request_number=pull_request_number,
        head_sha="abcdef1234567890abcdef1234567890abcdef12",
        preview_url="https://pr-42.example.invalid",
        decision="accepted",
        owner_github_id=str(_OWNER_GITHUB_ID),
        owner_github_login="site-owner",
        decided_at=decided_at,
    )


class ProductReviewDecisionContractTests(unittest.TestCase):
    def test_requesting_changes_without_a_reason_is_rejected(self) -> None:
        payload = _decision(record_id="decision-1", decided_at="2026-09-20T10:00:00Z").model_dump()
        payload.update(decision="changes_requested", reason="   ")

        with self.assertRaises(ValidationError):
            ProductReviewDecisionRecord.model_validate(payload)


class ProductReviewDecisionStorageTests(unittest.TestCase):
    def _assert_newest_first_for_one_pull_request(self, store: object) -> None:
        assert isinstance(store, FilesystemRecordStore | PostgresRecordStore)
        store.write_product_review_decision_record(
            _decision(record_id="decision-old", decided_at="2026-09-20T10:00:00Z")
        )
        store.write_product_review_decision_record(
            _decision(record_id="decision-new", decided_at="2026-09-20T11:00:00Z")
        )
        store.write_product_review_decision_record(
            _decision(
                record_id="decision-other-pull-request",
                decided_at="2026-09-20T12:00:00Z",
                pull_request_number=43,
            )
        )

        records = store.list_product_review_decision_records(
            repository=_REPOSITORY, pull_request_number=_PULL_REQUEST
        )
        latest = store.list_product_review_decision_records(
            repository=_REPOSITORY, pull_request_number=_PULL_REQUEST, limit=1
        )

        self.assertEqual([record.record_id for record in records], ["decision-new", "decision-old"])
        self.assertEqual(
            records[0], _decision(record_id="decision-new", decided_at="2026-09-20T11:00:00Z")
        )
        self.assertEqual([record.record_id for record in latest], ["decision-new"])

    def test_filesystem_store_lists_decisions_newest_first(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            self._assert_newest_first_for_one_pull_request(
                FilesystemRecordStore(state_dir=Path(temporary_directory_name))
            )

    def test_database_store_lists_decisions_newest_first(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            self.addCleanup(store.close)
            store.ensure_schema()
            self._assert_newest_first_for_one_pull_request(store)


class ProductReviewHttpTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        temporary_directory = TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.store = FilesystemRecordStore(state_dir=Path(temporary_directory.name))
        self.session_manager = HumanSessionManager(
            config=_github_oauth_config(),
            session_store=InMemoryHumanSessionStore(),
        )

    def _write_product(self, *, owner_set: bool = True, with_preview: bool = True) -> None:
        self.store.write_product_profile_record(_profile(owner_set=owner_set))
        if with_preview:
            self.store.write_preview_record(_preview_read_record(anchor_pr_number=_PULL_REQUEST))
            self.store.write_preview_generation_record(
                _preview_generation_read_record(anchor_pr_number=_PULL_REQUEST)
            )

    def _app(self, *, verifier: TokenVerifier | None = None) -> FastAPI:
        return create_launchplane_fastapi_app(
            verifier=verifier or _RejectingVerifier(),
            authz_policy=_operator_read_policy(),
            record_store_factory=lambda: self.store,
            human_session_manager=self.session_manager,
        )

    async def _get(self, app: FastAPI, human: GitHubHumanIdentity) -> Response:
        session = self.session_manager.issue(human)
        return await _asgi_get(
            app,
            _REVIEW_PATH,
            headers={"Cookie": self.session_manager.session_cookie_header(session)},
        )

    async def _post(
        self,
        app: FastAPI,
        human: GitHubHumanIdentity,
        *,
        decision: str,
        reason: str = "",
    ) -> Response:
        session = self.session_manager.issue(human)
        return await _asgi_request(
            app,
            "POST",
            "/v1/product-review/decisions",
            headers=_browser_mutation_headers(self.session_manager, session),
            payload={
                "repository": _REPOSITORY,
                "pull_request": _PULL_REQUEST,
                "decision": decision,
                "reason": reason,
            },
        )

    async def test_owner_reviews_preview_and_decision_becomes_latest(self) -> None:
        self._write_product()
        app = self._app()
        owner = _human(login="site-owner", github_id=_OWNER_GITHUB_ID)

        before = await self._get(app, owner)
        written = await self._post(
            app, owner, decision="changes_requested", reason="The price is wrong."
        )
        after = await self._get(app, owner)

        self.assertEqual(before.status_code, 200)
        self.assertTrue(before.json()["can_decide"])
        self.assertEqual(before.json()["preview_url"], "https://pr-42.example.invalid")
        self.assertEqual(
            before.json()["pull_request_url"], "https://github.com/every/example-site/pull/42"
        )
        self.assertIsNone(before.json()["latest_decision"])
        self.assertEqual(written.status_code, 200)
        latest = after.json()["latest_decision"]
        self.assertEqual(latest["decision"], "changes_requested")
        self.assertEqual(latest["reason"], "The price is wrong.")
        self.assertEqual(latest["owner_github_login"], "site-owner")
        self.assertEqual(latest["head_sha"], "abcdef1234567890abcdef1234567890abcdef12")

    async def test_owner_is_recognized_by_github_id_after_a_login_rename(self) -> None:
        self._write_product()

        response = await self._post(
            self._app(),
            _human(login="renamed-owner", github_id=_OWNER_GITHUB_ID),
            decision="accepted",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["latest_decision"]["decision"], "accepted")

    async def test_other_signed_in_human_gets_the_same_closed_answer_for_any_repository(
        self,
    ) -> None:
        self._write_product()
        app = self._app()
        stranger = _human(login="site-owner", github_id=5)

        known_read = await self._get(app, stranger)
        known_write = await self._post(app, stranger, decision="accepted")
        session = self.session_manager.issue(stranger)
        unknown_read = await _asgi_get(
            app,
            "/v1/product-review?repository=every/missing&pull_request=1",
            headers={"Cookie": self.session_manager.session_cookie_header(session)},
        )

        self.assertEqual(known_read.status_code, 403)
        self.assertEqual(known_write.status_code, 403)
        self.assertEqual(known_read.json()["error"], unknown_read.json()["error"])
        self.assertEqual(known_read.json()["error"]["code"], known_write.json()["error"]["code"])
        self.assertEqual(
            self.store.list_product_review_decision_records(
                repository=_REPOSITORY, pull_request_number=_PULL_REQUEST
            ),
            (),
        )

    async def test_operator_with_profile_read_can_see_but_not_decide(self) -> None:
        self._write_product()
        app = self._app()
        operator = _human(login="example-operator", github_id=123)

        read = await self._get(app, operator)
        write = await self._post(app, operator, decision="accepted")

        self.assertEqual(read.status_code, 200)
        self.assertFalse(read.json()["viewer_is_owner"])
        self.assertFalse(read.json()["can_decide"])
        self.assertEqual(write.status_code, 403)

    async def test_bearer_identity_cannot_record_a_decision(self) -> None:
        self._write_product()
        app = self._app(verifier=StubVerifier(identity()))

        response = await _asgi_request(
            app,
            "POST",
            "/v1/product-review/decisions",
            headers={"Authorization": "Bearer valid-token"},
            payload={
                "repository": _REPOSITORY,
                "pull_request": _PULL_REQUEST,
                "decision": "accepted",
                "reason": "",
            },
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            self.store.list_product_review_decision_records(
                repository=_REPOSITORY, pull_request_number=_PULL_REQUEST
            ),
            (),
        )

    async def test_owner_decision_without_browser_mutation_headers_is_rejected(self) -> None:
        self._write_product()
        session = self.session_manager.issue(_human(login="site-owner", github_id=_OWNER_GITHUB_ID))

        response = await _asgi_request(
            self._app(),
            "POST",
            "/v1/product-review/decisions",
            headers={"Cookie": self.session_manager.session_cookie_header(session)},
            payload={
                "repository": _REPOSITORY,
                "pull_request": _PULL_REQUEST,
                "decision": "accepted",
                "reason": "",
            },
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "browser_mutation_denied")

    async def test_requesting_changes_without_a_reason_is_rejected(self) -> None:
        self._write_product()

        response = await self._post(
            self._app(),
            _human(login="site-owner", github_id=_OWNER_GITHUB_ID),
            decision="changes_requested",
            reason="  ",
        )

        self.assertGreaterEqual(response.status_code, 400)
        self.assertLess(response.status_code, 500)
        self.assertEqual(
            self.store.list_product_review_decision_records(
                repository=_REPOSITORY, pull_request_number=_PULL_REQUEST
            ),
            (),
        )

    async def test_product_without_owner_says_so_and_accepts_no_decision(self) -> None:
        self._write_product(owner_set=False)
        app = self._app()
        operator = _human(login="example-operator", github_id=123)

        read = await self._get(app, operator)
        write = await self._post(app, operator, decision="accepted")

        self.assertEqual(read.status_code, 200)
        self.assertFalse(read.json()["owner_set"])
        self.assertFalse(read.json()["can_decide"])
        self.assertEqual(read.json()["cannot_decide_reason"], "No Owner set for this product")
        self.assertEqual(write.status_code, 409)
        self.assertEqual(
            self.store.list_product_review_decision_records(
                repository=_REPOSITORY, pull_request_number=_PULL_REQUEST
            ),
            (),
        )

    async def test_owner_cannot_decide_before_a_preview_is_serving(self) -> None:
        self._write_product(with_preview=False)
        app = self._app()
        owner = _human(login="site-owner", github_id=_OWNER_GITHUB_ID)

        read = await self._get(app, owner)
        write = await self._post(app, owner, decision="accepted")

        self.assertEqual(read.status_code, 200)
        self.assertEqual(read.json()["preview_url"], "")
        self.assertFalse(read.json()["can_decide"])
        self.assertEqual(write.status_code, 409)


if __name__ == "__main__":
    unittest.main()
