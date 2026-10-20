import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import LaunchplaneAuthzPolicy
from control_plane.service_human_auth import HumanSessionManager, InMemoryHumanSessionStore
from control_plane.storage.postgres import PostgresRecordStore
from tests.http_app_test_support import (
    _RejectingVerifier,
    _asgi_request,
    _browser_mutation_headers,
    _github_human_identity,
    _github_human_product_config_policy,
    _github_oauth_config,
    _local_operator_bearer_config,
)
from tests.support.profiles import product_profile_payload
from tests.support.stores import sqlite_database_url


class DiscussionTopicHttpTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = PostgresRecordStore(
            database_url=sqlite_database_url(Path(directory.name) / "topics.db")
        )
        self.addCleanup(self.store.close)
        self.store.ensure_schema()
        for product in ("site-a", "site-b"):
            self.store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(product_profile_payload(product))
            )
        self.app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=LaunchplaneAuthzPolicy(),
            bearer_identity_config=_local_operator_bearer_config(),
            record_store_factory=lambda: self.store,
        )
        self.headers = {"Authorization": "Bearer local-operator-token"}
        self.route = "/v1/products/site-a/discussion-topics"
        self.payload: dict[str, object] = {
            "title": "Decide: Contact links",
            "issue_url": "https://git.example.test/issue/7",
        }

    async def test_supported_add_read_close_retries_do_not_change_product_configuration(
        self,
    ) -> None:
        before = self.store.read_product_profile_record("site-a")
        added = await _asgi_request(
            self.app, "PUT", self.route + "/contact", headers=self.headers, payload=self.payload
        )
        self.assertEqual(added.status_code, 200, added.text)
        duplicate = await _asgi_request(
            self.app, "PUT", self.route + "/contact", headers=self.headers, payload=self.payload
        )
        self.assertEqual(duplicate.json()["topic"], added.json()["topic"])
        conflict = await _asgi_request(
            self.app,
            "PUT",
            self.route + "/contact",
            headers=self.headers,
            payload={**self.payload, "title": "Decide: Another question"},
        )
        self.assertEqual(conflict.status_code, 409, conflict.text)
        read = await _asgi_request(self.app, "GET", self.route, headers=self.headers)
        self.assertEqual(read.json()["topics"], [added.json()["topic"]])
        closed = await _asgi_request(
            self.app, "POST", self.route + "/contact/close", headers=self.headers
        )
        self.assertEqual(closed.status_code, 200, closed.text)
        self.assertIsNotNone(closed.json()["topic"]["closed_at"])
        read = await _asgi_request(self.app, "GET", self.route, headers=self.headers)
        self.assertEqual(read.json()["topics"], [])
        self.assertEqual(self.store.read_product_profile_record("site-a"), before)

    async def test_existing_product_access_scopes_reads_and_cannot_write(self) -> None:
        await _asgi_request(
            self.app, "PUT", self.route + "/contact", headers=self.headers, payload=self.payload
        )
        sessions = HumanSessionManager(
            config=_github_oauth_config(), session_store=InMemoryHumanSessionStore()
        )
        session = sessions.issue(_github_human_identity(role="read_only"))
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=_github_human_product_config_policy(
                actions=("product_profile.read", "discussion_topic.write"),
                product="site-a",
                context="launchplane",
                role="read_only",
            ),
            human_session_manager=sessions,
            record_store_factory=lambda: self.store,
        )
        headers = {"Cookie": sessions.session_cookie_header(session)}
        own = await _asgi_request(app, "GET", self.route, headers=headers)
        self.assertEqual(own.status_code, 200, own.text)
        self.assertEqual(len(own.json()["topics"]), 1)
        other = await _asgi_request(
            app, "GET", "/v1/products/site-b/discussion-topics", headers=headers
        )
        self.assertEqual(other.status_code, 403, other.text)
        for method, suffix in (("PUT", "/new"), ("POST", "/contact/close")):
            with self.subTest(method=method):
                denied = await _asgi_request(
                    app,
                    method,
                    self.route + suffix,
                    headers=_browser_mutation_headers(sessions, session),
                    payload=self.payload if method == "PUT" else None,
                )
                self.assertEqual(denied.status_code, 403, denied.text)

    async def test_missing_product_and_topic_and_bad_input_fail_closed(self) -> None:
        missing = await _asgi_request(
            self.app,
            "PUT",
            "/v1/products/missing/discussion-topics/contact",
            headers=self.headers,
            payload=self.payload,
        )
        self.assertEqual(missing.status_code, 404, missing.text)
        missing = await _asgi_request(
            self.app, "POST", self.route + "/missing/close", headers=self.headers
        )
        self.assertEqual(missing.status_code, 404, missing.text)
        invalid = await _asgi_request(
            self.app,
            "PUT",
            self.route + "/contact",
            headers=self.headers,
            payload={**self.payload, "private_reason": "private"},
        )
        self.assertEqual(invalid.status_code, 400, invalid.text)
        self.assertEqual(self.store.list_discussion_topics("site-a"), ())

    async def test_signed_in_admin_can_add_and_close_only_with_browser_mutation_proof(self) -> None:
        sessions = HumanSessionManager(
            config=_github_oauth_config(), session_store=InMemoryHumanSessionStore()
        )
        session = sessions.issue(_github_human_identity())
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=_github_human_product_config_policy(
                actions=("discussion_topic.write",),
                product="site-a",
                context="launchplane",
            ),
            human_session_manager=sessions,
            record_store_factory=lambda: self.store,
        )
        denied = await _asgi_request(
            app,
            "PUT",
            self.route + "/contact",
            payload=self.payload,
            headers={"Cookie": sessions.session_cookie_header(session)},
        )
        self.assertEqual(denied.status_code, 403, denied.text)
        added = await _asgi_request(
            app,
            "PUT",
            self.route + "/contact",
            payload=self.payload,
            headers=_browser_mutation_headers(sessions, session),
        )
        self.assertEqual(added.status_code, 200, added.text)
        closed = await _asgi_request(
            app,
            "POST",
            self.route + "/contact/close",
            headers=_browser_mutation_headers(sessions, session),
        )
        self.assertEqual(closed.status_code, 200, closed.text)
