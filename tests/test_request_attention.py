import unittest
from contextlib import nullcontext
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from control_plane.contracts.repository_inventory import RepositoryInventoryRecord
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.request_attention import (
    AttentionSource,
    CollectedRequest,
    OpenRequestPage,
    REQUEST_PAGE_LIMIT,
    RequestAttentionPage,
    attention_sources,
    build_request_attention_page,
)
from control_plane.service_auth import LaunchplaneAuthzPolicy
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.request_attention_github import GitHubOpenRequestReader
from tests.http_app_test_support import (
    _RejectingVerifier,
    _asgi_request,
    _github_human_identity,
    _github_human_product_config_policy,
    _github_oauth_config,
    _local_operator_bearer_config,
)
from control_plane.service_human_auth import HumanSessionManager, InMemoryHumanSessionStore
from tests.support.stores import sqlite_database_url


def source_row(**changes: object) -> CollectedRequest:
    return CollectedRequest.model_validate(
        {
            "source_id": "source-request-7",
            "source_kind": "request",
            "title": "Inventory menu is missing",
            "source_url": "https://git.example.test/team/site/issues/7",
            "created_at": "2026-10-30T18:00:00Z",
            "open": True,
            "requester": "client-agent",
            "body": "## Current Status\nNext actor: agent:worker-one\nNext action: Restore the menu.",
            **changes,
        }
    )


class RequestAttentionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = AttentionSource(
            repository="team/site", products=("Example site",), available=True
        )
        self.read = Mock(return_value=OpenRequestPage(requests=(source_row(),), has_more=False))

    def page(self, *, max_pages: int = REQUEST_PAGE_LIMIT) -> RequestAttentionPage:
        return build_request_attention_page(
            sources=(self.source,),
            repository="team/site",
            page=1,
            read=self.read,
            trace_id="fixture-read",
            observed_at="2026-10-30T20:00:00Z",
            max_pages=max_pages,
        )

    def test_reply_does_not_remove_open_work_and_source_closure_does(self) -> None:
        first = self.page().requests[0]
        self.read.return_value = OpenRequestPage(requests=(source_row(),), has_more=False)
        replied = self.page().requests[0]
        self.assertEqual(replied, first)
        self.assertEqual(first.requester, "client-agent")
        self.assertEqual(first.owning_agent, "worker-one")
        self.assertEqual(first.age_seconds, int(timedelta(hours=2).total_seconds()))
        self.read.return_value = OpenRequestPage(requests=(source_row(open=False),), has_more=False)
        self.assertEqual(self.page().requests, ())

    def test_unowned_and_released_work_routes_to_supervisor_without_parsing_claims(self) -> None:
        self.read.return_value = OpenRequestPage(
            requests=(source_row(body="A fresh imported feedback item."),), has_more=False
        )
        self.assertIsNone(self.page().requests[0].owning_agent)
        released = '## Current Status\nNext actor: agent\nNext action: Review the next build.\n<!-- github-plan:claim {"worker":"old-owner"} -->'
        self.read.return_value = OpenRequestPage(
            requests=(source_row(body=released),), has_more=False
        )
        self.assertIsNone(self.page().requests[0].owning_agent)
        self.assertIn("Review the next build.", self.page().requests[0].next_action)

    def test_quoted_status_is_not_assignment_and_ambiguous_status_keeps_request(self) -> None:
        self.read.return_value = OpenRequestPage(
            requests=(source_row(body="```\n## Current Status\nNext actor: agent:quoted\n```\n"),),
            has_more=False,
        )
        self.assertIsNone(self.page().requests[0].owning_agent)
        self.read.return_value = OpenRequestPage(
            requests=(
                source_row(body="## Current Status\nNext actor: agent:a\nNext actor: agent:b"),
            ),
            has_more=False,
        )
        self.assertIsNone(self.page().requests[0].owning_agent)

    def test_pagination_and_bound_never_report_truncated_data_as_complete(self) -> None:
        self.read.return_value = OpenRequestPage(requests=(source_row(),), has_more=True)
        first = self.page(max_pages=2)
        self.assertEqual(first.next_page, 2)
        self.assertFalse(first.truncated)
        bounded = self.page(max_pages=1)
        self.assertTrue(bounded.truncated)
        self.assertIsNone(bounded.next_page)
        self.read.return_value = OpenRequestPage(
            requests=(source_row(), source_row()), has_more=False
        )
        self.assertEqual(len(self.page().requests), 1)

    def test_unknown_source_never_reads_provider_and_malformed_source_fails(self) -> None:
        with self.assertRaises(LookupError):
            build_request_attention_page(
                sources=(),
                repository="other/site",
                page=1,
                read=self.read,
                trace_id="test",
                observed_at="2026-10-30T20:00:00Z",
            )
        self.read.assert_not_called()
        with self.assertRaises(ValueError):
            source_row(created_at="2026-10-30T18:00:00")

    def test_source_adapter_keeps_acknowledged_work_open_and_only_sends_reads(self) -> None:
        requester = "client-agent"
        raw = {
            "node_id": "request-source",
            "html_url": "https://git.example.test/team/site/pull/9",
            "title": "Menu fix",
            "user": {"login": requester},
            "body": "Please restore the menu.",
            "created_at": "2026-10-30T18:00:00Z",
            "state": "open",
            "pull_request": {},
            "comments": 0,
        }
        transport = Mock()
        transport.request.return_value = [raw]
        with (
            patch(
                "control_plane.request_attention_github.launchplane_github_token",
                side_effect=lambda **_: nullcontext("synthetic-source-token"),
            ),
            patch(
                "control_plane.request_attention_github.UrllibMergeTrainGitHubTransport",
                return_value=transport,
            ),
        ):
            reader = GitHubOpenRequestReader(Path("/synthetic/control-plane"))
            first = reader("team/site", 1)
            transport.request.return_value = [{**raw, "comments": 2}]
            self.assertEqual(reader("team/site", 1), first)
            self.assertEqual(first.requests[0].requester, requester)
            transport.request.return_value = [{**raw, "state": "closed"}]
            self.assertFalse(reader("team/site", 1).requests[0].open)
            for call in transport.request.call_args_list:
                self.assertEqual(call.kwargs["method"], "GET")
            transport.request.return_value = [{**raw, "state": None}]
            with self.assertRaises(ValueError):
                reader("team/site", 1)
            transport.request.return_value = {"unexpected": "shape"}
            with self.assertRaises(ValueError):
                reader("team/site", 1)


class RequestAttentionHttpTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = PostgresRecordStore(
            database_url=sqlite_database_url(Path(directory.name) / "attention.db")
        )
        self.addCleanup(self.store.close)
        self.store.ensure_schema()
        self.store.write_repository_inventory_record(
            RepositoryInventoryRecord(
                repository_id="1001",
                repository_owner_id="100",
                repository="team/site",
                inventory_state="tracked",
                inventory_revision=1,
                recorded_at="2026-10-30T00:00:00Z",
                source="test",
                reason="Synthetic request source",
            )
        )
        self.read = Mock(return_value=OpenRequestPage(requests=(source_row(),), has_more=False))
        reader_patch = patch(
            "control_plane.http_app.GitHubOpenRequestReader", return_value=self.read
        )
        reader_patch.start()
        self.addCleanup(reader_patch.stop)
        self.app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=LaunchplaneAuthzPolicy(),
            bearer_identity_config=_local_operator_bearer_config(),
            record_store_factory=lambda: self.store,
        )
        self.headers = {"Authorization": "Bearer local-operator-token"}
        self.route = "/v1/attention/requests"

    async def test_inventory_then_source_reads_are_read_only_and_retirement_removes_source(
        self,
    ) -> None:
        inventory = await _asgi_request(self.app, "GET", self.route, headers=self.headers)
        self.assertEqual(inventory.status_code, 200, inventory.text)
        self.read.assert_not_called()
        response = await _asgi_request(
            self.app, "GET", self.route + "?repository=team/site", headers=self.headers
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["requests"][0]["source_id"], source_row().source_id)
        before = self.store.list_repository_inventory_records()[0]
        self.assertEqual(len(attention_sources(self.store)), 1)
        self.store.write_repository_inventory_record(
            RepositoryInventoryRecord(
                repository_id=before.repository_id,
                repository_owner_id=before.repository_owner_id,
                repository=before.repository,
                inventory_state="retired",
                inventory_revision=2,
                recorded_at="2026-10-30T01:00:00Z",
                source="test",
                reason="Retired fixture",
                supersedes_record_id=before.record_id,
            )
        )
        self.assertEqual(attention_sources(self.store), ())

    async def test_unknown_scope_and_provider_failures_are_bounded_without_private_error(
        self,
    ) -> None:
        unknown = await _asgi_request(
            self.app, "GET", self.route + "?repository=other/site", headers=self.headers
        )
        self.assertEqual(unknown.status_code, 404)
        self.read.assert_not_called()
        self.read.side_effect = RuntimeError("private-provider-detail")
        failed = await _asgi_request(
            self.app, "GET", self.route + "?repository=team/site", headers=self.headers
        )
        self.assertEqual(failed.status_code, 503)
        self.assertNotIn("private-provider-detail", failed.text)
        invalid = await _asgi_request(
            self.app, "GET", self.route + f"?page={REQUEST_PAGE_LIMIT + 1}", headers=self.headers
        )
        self.assertEqual(invalid.status_code, 400)

    async def test_signed_in_product_reader_cannot_read_cross_product_requests(self) -> None:
        sessions = HumanSessionManager(
            config=_github_oauth_config(), session_store=InMemoryHumanSessionStore()
        )
        session = sessions.issue(_github_human_identity(role="read_only"))
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=_github_human_product_config_policy(
                actions=("repository_inventory.read",),
                product="launchplane",
                context="launchplane",
                role="read_only",
            ),
            record_store_factory=lambda: self.store,
            human_session_manager=sessions,
        )
        response = await _asgi_request(
            app,
            "GET",
            self.route + "?repository=team/site",
            headers={"Cookie": sessions.session_cookie_header(session)},
        )
        self.assertEqual(response.status_code, 403, response.text)
        self.read.assert_not_called()
