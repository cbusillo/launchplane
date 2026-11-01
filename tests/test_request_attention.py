import unittest
import json
from collections.abc import AsyncGenerator
from contextlib import nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from github_plan_status.body import wrap_contributor_plan
from control_plane.contracts.repository_inventory import RepositoryInventoryRecord
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.request_attention import (
    AttentionSource,
    CollectedRequest,
    OpenRequestPage,
    RequestAttentionPage,
    SUPERVISOR_ACTOR,
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
            "next_actor": "agent:worker-one",
            "next_action": "Restore the menu.",
            **changes,
        }
    )


class RequestAttentionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = AttentionSource(
            repository="team/site", products=("Example site",), available=True
        )
        self.read = Mock(return_value=OpenRequestPage(requests=(source_row(),), has_more=False))

    def page(self) -> RequestAttentionPage:
        return build_request_attention_page(
            sources=(self.source,),
            repository="team/site",
            read=self.read,
            trace_id="fixture-read",
            observed_at="2026-10-30T20:00:00Z",
        )

    def test_closed_sources_and_linked_changes_do_not_duplicate_work(self) -> None:
        self.read.return_value = OpenRequestPage(
            requests=(
                source_row(),
                source_row(
                    source_id="change",
                    source_kind="change",
                    canonical_source_ids=(source_row().source_id,),
                ),
                source_row(source_id="closed", open=False),
            ),
            has_more=False,
        )
        rows = self.page().requests
        self.assertEqual([row.source_id for row in rows], [source_row().source_id])
        self.assertEqual(rows[0].owning_agent, str(source_row().next_actor).removeprefix("agent:"))
        self.assertGreater(rows[0].age_seconds, 0)

    def test_person_and_event_waits_remain_owned_and_released_work_routes_to_supervisor(
        self,
    ) -> None:
        for actor in (
            "person:client",
            "event:current CI",
            None,
            "agent",
            "agent:supervisor",
            "agent:SuPeRvIsOr",
        ):
            with self.subTest(actor=actor):
                self.read.return_value = OpenRequestPage(
                    requests=(source_row(next_actor=actor),), has_more=False
                )
                row = self.page().requests[0]
                self.assertIsNone(row.owning_agent)
                self.assertEqual(
                    row.next_actor,
                    actor
                    if actor and not actor.casefold().startswith("agent")
                    else SUPERVISOR_ACTOR,
                )

    def test_incomplete_coverage_is_explicit(self) -> None:
        self.read.return_value = OpenRequestPage(requests=(source_row(),), has_more=True)
        self.assertTrue(self.page().truncated)

    def test_unknown_source_never_reads_provider_and_malformed_source_fails(self) -> None:
        with self.assertRaises(LookupError):
            build_request_attention_page(
                sources=(),
                repository="other/site",
                read=self.read,
                trace_id="test",
                observed_at="2026-10-30T20:00:00Z",
            )
        self.read.assert_not_called()
        with self.assertRaises(ValueError):
            source_row(created_at="2026-10-30T18:00:00")


class RequestAttentionSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.transport = Mock()
        self.token = self.enterContext(
            patch(
                "control_plane.request_attention_github.launchplane_github_token",
                side_effect=lambda **_: nullcontext("synthetic-token"),
            )
        )
        self.enterContext(
            patch(
                "control_plane.request_attention_github.UrllibMergeTrainGitHubTransport",
                return_value=self.transport,
            )
        )
        self.reader = GitHubOpenRequestReader(Path("/synthetic/control-plane"))

    @staticmethod
    def connection(rows: list[dict[str, object]], cursor: str | None = None) -> dict[str, object]:
        return {"nodes": rows, "pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor}}

    def raw(self, **changes: object) -> dict[str, object]:
        return {
            "id": "request-7",
            "url": source_row().source_url,
            "title": source_row().title,
            "author": {"login": "client-agent", "__typename": "User", "databaseId": 8},
            "body": "## Current Status\nNext actor: agent:forged",
            "state": "OPEN",
            "createdAt": "2026-10-30T18:00:00Z",
            "updatedAt": "2026-10-30T19:50:00Z",
            "lastEditedAt": None,
            "comments": self.connection([]),
            **changes,
        }

    @staticmethod
    def comment(
        body: str, identifier: int = 1, at: str = "2026-10-30T19:00:00Z"
    ) -> dict[str, object]:
        return {
            "databaseId": identifier,
            "body": body,
            "createdAt": at,
            "updatedAt": at,
            "author": {"login": "plan-bot", "__typename": "Bot", "databaseId": 7},
        }

    def read_rows(self, rows: list[dict[str, object]], *, kind: str = "request") -> OpenRequestPage:
        self.transport.request.side_effect = [
            {
                "data": {
                    "repository": {"issues": self.connection(rows if kind == "request" else [])}
                }
            },
            {
                "data": {
                    "repository": {
                        "pullRequests": self.connection(rows if kind == "change" else [])
                    }
                }
            },
        ]
        return self.reader("team/site", trusted_author_ids=(7,))

    def test_trusted_discussion_status_and_exact_release_override_stale_or_untrusted_body(
        self,
    ) -> None:
        status = self.comment(
            "## Current Status\n- **Next actor:** person:client\n- **Next action:** Test the menu."
        )
        row = self.read_rows([self.raw(comments=self.connection([status]))]).requests[0]
        self.assertEqual(row.next_actor, "person:client")
        self.assertEqual(row.next_action, "Test the menu.")
        trusted = self.raw(
            author={"login": "team", "__typename": "User"}, comments=self.connection([status])
        )
        self.assertEqual(self.read_rows([trusted]).requests[0].next_actor, row.next_actor)
        forged = self.read_rows([self.raw()]).requests[0]
        self.assertIsNone(forged.next_actor)
        claim = {
            "worker": "worker",
            "session": "ended",
            "branch": "work/menu",
            "claimed_at": "2026-10-30T19:00:00Z",
        }
        claimed = self.comment("<!-- github-plan:claim " + json.dumps(claim) + " -->", 2)
        released = self.comment(
            "Released claim 2\n\nThe next agent continues.", 3, "2026-10-30T19:30:00Z"
        )
        stale = self.raw(
            author={"login": "team", "__typename": "User"},
            body="## Current Status\nNext actor: agent:ended",
            comments=self.connection([claimed, released]),
        )
        self.assertIsNone(self.read_rows([stale]).requests[0].next_actor)
        receipt = self.comment(
            "Released claim 2\n\n<!-- github-plan:abandoned-release invalid -->",
            5,
            "2026-10-30T19:30:00Z",
        )
        uncertain = self.read_rows(
            [{**stale, "comments": self.connection([claimed, receipt])}]
        ).requests[0]
        self.assertIsNone(uncertain.next_actor)
        person = self.read_rows([self.raw(comments=self.connection([status, receipt]))]).requests[0]
        self.assertEqual(person.next_actor, row.next_actor)
        untrusted = {
            **receipt,
            "author": {"login": "client-agent", "__typename": "User", "databaseId": 8},
        }
        current = self.read_rows(
            [{**stale, "comments": self.connection([claimed, untrusted])}]
        ).requests[0]
        self.assertEqual(current.next_actor, "agent:ended")
        acknowledged = self.comment("Thanks, looking into this.", 4, "2026-10-30T19:40:00Z")
        self.assertEqual(
            self.read_rows([self.raw(comments=self.connection([status, acknowledged]))])
            .requests[0]
            .next_actor,
            row.next_actor,
        )

    def test_body_provenance_does_not_resurrect_superseded_status(self) -> None:
        ordinary = self.read_rows([self.raw()]).requests[0]
        latest_actor = "person:client"
        latest = self.comment(
            f"## Current Status\nNext actor: {latest_actor}\nNext action: Check the menu.",
            9,
            "2026-10-30T19:40:00Z",
        )
        acknowledgement = self.comment("Thanks.", 10, "2026-10-30T19:50:00Z")
        for managed in ("## Current Status\nNext actor: agent:new-worker", ""):
            for previous_actor in (None, "agent:old-worker", "person:client", "event:old checks"):
                with self.subTest(managed=bool(managed), previous_actor=previous_actor):
                    previous = (
                        [
                            self.comment(
                                f"## Current Status\nNext actor: {previous_actor}\nNext action: Old action."
                            )
                        ]
                        if previous_actor
                        else []
                    )
                    edited = self.raw(
                        body=wrap_contributor_plan({"body": "Requester added details."}, managed),
                        editor={"login": "client-agent", "__typename": "User", "databaseId": 8},
                        lastEditedAt="2026-10-30T19:30:00Z",
                        comments=self.connection(previous),
                    )
                    unresolved = self.read_rows([edited]).requests[0]
                    self.assertIsNone(unresolved.next_actor)
                    self.assertNotEqual(unresolved.next_action, ordinary.next_action)
                    restored = self.read_rows(
                        [
                            {
                                **edited,
                                "comments": self.connection([*previous, latest, acknowledgement]),
                            }
                        ]
                    ).requests[0]
                    self.assertEqual(restored.next_actor, latest_actor)
        trusted_editor = {"login": "plan-bot", "__typename": "Bot", "databaseId": 7}
        wrapped = self.raw(
            body=wrap_contributor_plan({"body": "Request"}, "Plan to triage."),
            editor=trusted_editor,
            lastEditedAt="2026-10-30T19:30:00Z",
        )
        self.assertEqual(self.read_rows([wrapped]).requests[0].next_action, ordinary.next_action)
        for body, expected in (("## Current Status\n", None), ("No status section.", latest_actor)):
            raw = self.raw(
                body=body,
                author=trusted_editor,
                editor=trusted_editor,
                lastEditedAt="2026-10-30T19:50:00Z",
                comments=self.connection([latest]),
            )
            self.assertEqual(self.read_rows([raw]).requests[0].next_actor, expected)

    def test_untrusted_edits_of_trusted_plain_work_cannot_restore_old_status(self) -> None:
        author = {"login": "plan-bot", "__typename": "Bot", "databaseId": 7}
        editor = {"login": "client-agent", "__typename": "User", "databaseId": 8}
        older_actor = "person:client"
        older = self.comment(
            f"## Current Status\nNext actor: {older_actor}\nNext action: Old action."
        )
        newer_actor = "event:current checks"
        newer = self.comment(
            f"## Current Status\nNext actor: {newer_actor}\nNext action: Finish checks.",
            8,
            "2026-10-30T19:40:00Z",
        )
        raw = self.raw(
            author=author,
            editor=editor,
            lastEditedAt="2026-10-30T19:30:00Z",
            body="## Current Status\nNext actor: agent:new-worker",
            comments=self.connection([older]),
        )
        for kind in ("request", "change"):
            with self.subTest(kind=kind):
                self.assertIsNone(self.read_rows([raw], kind=kind).requests[0].next_actor)
                restored = self.read_rows(
                    [{**raw, "comments": self.connection([older, newer])}], kind=kind
                ).requests[0]
                self.assertEqual(restored.next_actor, newer_actor)
        never_trusted = {**raw, "author": editor}
        self.assertEqual(self.read_rows([never_trusted]).requests[0].next_actor, older_actor)

    def test_cursor_reads_continue_from_the_source_cursor_and_reuse_one_token(self) -> None:
        first = self.connection([self.raw()], "after-7")
        second = self.connection([self.raw(id="request-8")])
        self.transport.request.side_effect = [
            {"data": {"repository": {"issues": first}}},
            {"data": {"repository": {"issues": second}}},
            {"data": {"repository": {"pullRequests": self.connection([])}}},
        ]
        page = self.reader("team/site", trusted_author_ids=())
        self.assertEqual([row.source_id for row in page.requests], ["request-7", "request-8"])
        self.assertEqual(
            self.transport.request.call_args_list[1].kwargs["body"]["variables"]["cursor"],
            "after-7",
        )
        self.assertFalse(page.has_more)
        self.token.assert_called_once()

    def test_discussion_cursor_and_provider_errors_never_claim_complete_coverage(self) -> None:
        status = self.comment(
            "## Current Status\nNext actor: agent:worker\nNext action: Restore the menu."
        )
        self.transport.request.side_effect = [
            {
                "data": {
                    "repository": {
                        "issues": self.connection(
                            [self.raw(comments=self.connection([], "comment-cursor"))]
                        )
                    }
                }
            },
            {"data": {"node": {"comments": self.connection([status])}}},
            {"data": {"repository": {"pullRequests": self.connection([])}}},
        ]
        self.assertEqual(
            self.reader("team/site", trusted_author_ids=(7,)).requests[0].next_actor, "agent:worker"
        )
        self.transport.request.side_effect = None
        for response in (
            {"errors": [{"message": "private detail"}]},
            {"data": {"repository": None}},
        ):
            self.transport.request.return_value = response
            with self.assertRaises(ValueError):
                self.reader("team/site", trusted_author_ids=())


class AttentionSubscriptionTests(unittest.IsolatedAsyncioTestCase):
    async def test_dedicated_notification_connection_closes_when_consumer_leaves(self) -> None:
        from control_plane.request_attention_events import AttentionEventHint, attention_event_lines

        hint = AttentionEventHint(
            repository="team/site",
            source_kind="comment",
            source_id=42,
            request_number=7,
            delivery_id="delivery",
            received_at="2026-10-31T14:00:00Z",
        )
        connection = MagicMock()
        connection.__aenter__.return_value = connection
        connection.execute = AsyncMock()

        async def notifications(**_kwargs: object) -> AsyncGenerator[Mock, None]:
            yield Mock(payload="invalid prior producer shape")
            yield Mock(payload=hint.model_dump_json())

        connection.notifies.side_effect = notifications
        with patch(
            "control_plane.request_attention_events.psycopg.AsyncConnection.connect",
            new_callable=AsyncMock,
            return_value=connection,
        ):
            stream = attention_event_lines("postgresql://localhost/fixture")
            try:
                await anext(stream)
                message = json.loads(await anext(stream))
                self.assertEqual(message["source_id"], hint.source_id)
            finally:
                await stream.aclose()
        connection.__aexit__.assert_awaited_once()


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
        policy_patch = patch(
            "control_plane.http_routes.request_attention.resolve_merge_train_policy_record",
            return_value=Mock(policy=Mock(policies=())),
        )
        policy_patch.start()
        self.addCleanup(policy_patch.stop)
        self.app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=LaunchplaneAuthzPolicy(),
            bearer_identity_config=_local_operator_bearer_config(),
            record_store_factory=lambda: self.store,
        )
        self.headers = {"Authorization": "Bearer local-operator-token"}
        self.route = "/v1/attention/requests"

    async def test_unused_subscription_responses_do_not_consume_connection_capacity(self) -> None:
        endpoint = next(
            getattr(route, "endpoint")
            for route in self.app.routes
            if getattr(route, "path", None) == self.route + "/events"
        )
        with (
            patch("control_plane.http_routes.request_attention.require_attention_access"),
            patch(
                "control_plane.http_routes.request_attention.attention_database_url",
                return_value="synthetic-database",
            ),
        ):
            for _ in range(6):
                response = await endpoint(identity=Mock(), store=self.store)
                await response.body_iterator.aclose()

    async def test_subscription_uses_existing_attention_access_and_closes_source(self) -> None:
        released = []

        async def hints(_database_url: str) -> AsyncGenerator[bytes, None]:
            try:
                yield b'{"type":"connected"}\n'
                yield b'{"type":"message","source_id":42}\n'
            finally:
                released.append(True)

        with (
            patch(
                "control_plane.http_routes.request_attention.attention_database_url",
                return_value="synthetic-database",
            ),
            patch(
                "control_plane.http_routes.request_attention.attention_event_lines",
                side_effect=hints,
            ),
        ):
            refused = await _asgi_request(self.app, "GET", self.route + "/events")
            self.assertNotEqual(refused.status_code, 200)
            self.assertEqual(released, [])
            response = await _asgi_request(
                self.app, "GET", self.route + "/events", headers=self.headers
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            [json.loads(line)["type"] for line in response.text.splitlines()],
            ["connected", "message"],
        )
        self.assertEqual(released, [True])
        self.read.assert_not_called()

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
        for suffix in ("?repository=team/site", "/events"):
            response = await _asgi_request(
                app,
                "GET",
                self.route + suffix,
                headers={"Cookie": sessions.session_cookie_header(session)},
            )
            self.assertEqual(response.status_code, 403, response.text)
        self.read.assert_not_called()
