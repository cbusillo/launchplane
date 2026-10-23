from functools import partial
from time import monotonic
from typing import Any
from urllib.parse import parse_qs, urlsplit
import json
import unittest
from unittest.mock import patch

from control_plane import discord_external_resources as reader
from control_plane.outbound_http import PublicHttpResponse, request_public_http
from tests.test_outbound_http import _FakeConnection, _FakeResponse, _resolver_for


class DiscordExternalResourcesTests(unittest.TestCase):
    def test_rate_limit_evidence_survives_without_exposing_provider_secrets(self) -> None:
        response = PublicHttpResponse(
            429,
            "",
            0,
            (
                ("X-RateLimit-Scope", "global"),
                ("X-RateLimit-Global", "true"),
                ("Retry-After", "120"),
                ("Authorization", "synthetic-bot-token"),
            ),
            json.dumps(
                {"retry_after": 125.5, "global": True, "message": "synthetic-bot-token"}
            ).encode(),
        )
        with patch.object(reader, "sleep", create=True) as sleep:
            result = self.observe(lambda *_, **__: response)
        evidence = result.rate_limits[0]
        self.assertEqual(evidence.retry_after, 125.5)
        self.assertIs(evidence.global_limit, True)
        self.assertEqual(evidence.scope, "global")
        self.assertIs(evidence.header_global, True)
        self.assertEqual(evidence.header_retry_after, 120)
        self.assertEqual(evidence.phase, "identity")
        self.assertFalse(evidence.non_json_body)
        self.assertFalse(evidence.cloudflare_error_page)
        self.assertNotIn("synthetic-bot-token", result.model_dump_json())
        self.assertFalse(result.guild_enumeration_complete)
        sleep.assert_not_called()

    def test_provider_backoff_retries_same_guild_read_once_and_retains_evidence(self) -> None:
        for repeat in (False, True):
            with self.subTest(repeat=repeat):
                calls = []

                def respond(url: str, **kwargs: Any) -> PublicHttpResponse:
                    if "/guilds?" in url:
                        calls.append(url)
                        if len(calls) == 1 or repeat:
                            return PublicHttpResponse(
                                429,
                                "",
                                0,
                                (("Retry-After", "0.5"),),
                                b'{"retry_after":0.25,"global":false}',
                            )
                    return self.base(url, **kwargs)

                with patch.object(reader, "sleep", create=True) as sleep:
                    result = self.observe(respond)
                sleep.assert_called_once_with(0.5)
                self.assertEqual(calls[0], calls[1])
                self.assertEqual(len(calls), 2)
                self.assertEqual(result.guild_enumeration_complete, not repeat)
                self.assertEqual(result.rate_limits[0].phase, "guilds")
                self.assertEqual(result.rate_limits[0].retry_after, 0.25)
                if repeat:
                    self.assertEqual(result.http_status, 429)

    def test_non_json_and_cloudflare_rate_limits_are_classified_without_body_disclosure(
        self,
    ) -> None:
        for body, cloudflare in (
            (b"", False),
            (b"not json", False),
            (b"<html>Cloudflare error synthetic-bot-token</html>", True),
        ):
            with self.subTest(body=body):
                with patch.object(reader, "sleep", create=True) as sleep:
                    result = self.observe(lambda *_, **__: PublicHttpResponse(429, "", 0, (), body))
                evidence = result.rate_limits[0]
                self.assertTrue(evidence.non_json_body)
                self.assertEqual(evidence.cloudflare_error_page, cloudflare)
                self.assertNotIn("synthetic-bot-token", result.model_dump_json())
                sleep.assert_not_called()

    def test_invalid_backoff_and_untrusted_header_values_never_escape_or_retry(self) -> None:
        for delay in (True, -1, "synthetic-bot-token", float("nan"), float("inf")):
            with self.subTest(delay=delay):
                response = PublicHttpResponse(
                    429,
                    "",
                    0,
                    (
                        ("Retry-After", "synthetic-bot-token"),
                        ("X-RateLimit-Scope", "synthetic-bot-token"),
                        ("X-RateLimit-Global", "synthetic-bot-token"),
                    ),
                    json.dumps({"retry_after": delay, "global": "synthetic-bot-token"}).encode(),
                )
                with patch.object(reader, "sleep", create=True) as sleep:
                    result = self.observe(lambda *_, **__: response)
                self.assertIsNone(result.rate_limits[0].retry_after)
                self.assertNotIn("synthetic-bot-token", result.model_dump_json())
                sleep.assert_not_called()

    def test_retry_rechecks_inputs_after_backoff_before_provider_call(self) -> None:
        from unittest.mock import Mock

        current = Mock(side_effect=[None, reader.DiscordResourceReadError("inputs_changed")])
        with (
            patch.object(reader, "sleep", create=True) as sleep,
            patch.object(
                reader,
                "request_public_http",
                return_value=PublicHttpResponse(
                    429,
                    "",
                    0,
                    (),
                    b'{"retry_after":0.25,"global":false}',
                ),
            ) as request,
        ):
            result = reader.observe_discord_bot(
                "synthetic-bot-token", deadline=monotonic() + 60, check_current=current
            )
        self.assertEqual(result.failure_code, "inputs_changed")
        request.assert_called_once()
        sleep.assert_called_once_with(0.25)
        with (
            patch.object(reader, "sleep"),
            patch.object(reader, "monotonic", side_effect=[0, 0, 2]),
            patch.object(
                reader,
                "request_public_http",
                return_value=PublicHttpResponse(
                    429,
                    "",
                    0,
                    (),
                    b'{"retry_after":0.25,"global":false}',
                ),
            ) as request,
        ):
            result = reader.observe_discord_bot(
                "synthetic-bot-token", deadline=1, check_current=lambda: None
            )
        self.assertEqual(result.failure_code, "collection_budget_exhausted")
        self.assertEqual(result.rate_limits[0].retry_after, 0.25)
        request.assert_called_once()

    def observe(self, respond: Any) -> Any:
        with patch.object(reader, "request_public_http", side_effect=respond):
            return reader.observe_discord_bot(
                "synthetic-bot-token", deadline=monotonic() + 60, check_current=lambda: None
            )

    def response(self, url: str, payload: Any, status: int = 200) -> PublicHttpResponse:
        return PublicHttpResponse(status, url, 0, (), json.dumps(payload).encode())

    def base(self, url: str, **kwargs: Any) -> PublicHttpResponse:
        path = urlsplit(url).path
        if path.endswith("/users/@me"):
            payload: Any = {"id": "123", "bot": True, "username": "not-returned"}
        elif path.endswith("/applications/@me"):
            payload = {"id": "456", "bot": {"id": "123"}, "secret": "not-returned"}
        elif path.endswith("/gateway/bot"):
            payload = {"session_start_limit": {"max_concurrency": 1}}
        else:
            payload = []
        return self.response(url, payload)

    def test_all_pages_and_partitions_are_collected_to_empty_terminal_pages(self) -> None:
        calls = []

        def respond(url: str, **kwargs: Any) -> PublicHttpResponse:
            calls.append((url, kwargs))
            if url.endswith("/gateway/bot"):
                return self.response(url, {"session_start_limit": {"max_concurrency": 2}})
            query = parse_qs(urlsplit(url).query)
            if "/guilds?" in url:
                shard = query["shard"][0]
                after = query.get("after", [""])[0]
                payload = (
                    [{"id": "10"}, {"id": "20"}]
                    if shard == "0" and not after
                    else [{"id": "30"}]
                    if shard == "0" and after == "20"
                    else [{"id": "40"}]
                    if shard == "1" and not after
                    else []
                )
                return self.response(url, payload)
            return self.base(url, **kwargs)

        result = self.observe(respond)
        self.assertEqual(result.identity_status, "observed")
        self.assertEqual((result.bot_id, result.application_id), ("123", "456"))
        self.assertEqual(result.guild_ids, ("10", "20", "30", "40"))
        self.assertTrue(result.guild_enumeration_complete)
        self.assertNotIn("not-returned", result.model_dump_json())
        self.assertTrue(
            all(
                options["headers"]["Authorization"] == "Bot synthetic-bot-token"
                for _, options in calls
            )
        )

    def test_repeated_partition_does_not_become_a_complete_deduplicated_inventory(self) -> None:
        def respond(url: str, **kwargs: Any) -> PublicHttpResponse:
            if url.endswith("/gateway/bot"):
                return self.response(url, {"session_start_limit": {"max_concurrency": 2}})
            if "/guilds?" in url:
                payload = [] if "after=" in url else [{"id": "10"}]
                return self.response(url, payload)
            return self.base(url, **kwargs)

        result = self.observe(respond)
        self.assertEqual(result.identity_status, "observed")
        self.assertEqual(result.guild_status, "unavailable")
        self.assertFalse(result.guild_enumeration_complete)
        self.assertFalse(result.guild_ids)

    def test_truncated_valid_json_prefix_is_not_accepted_as_complete(self) -> None:
        def respond(url: str, **kwargs: Any) -> PublicHttpResponse:
            if "/guilds?" in url:
                body = b"[]" + b" " * reader.DISCORD_RESPONSE_LIMIT + b"malformed remainder"
                connection = _FakeConnection(_FakeResponse(status=200, body=body))
                return request_public_http(
                    url,
                    **kwargs,
                    resolver=_resolver_for("8.8.8.8"),
                    connection_factory=lambda *_: connection,
                )
            return self.base(url, **kwargs)

        result = self.observe(respond)
        self.assertEqual(result.failure_code, "response_too_large")
        self.assertFalse(result.guild_enumeration_complete)

    def test_public_transport_rejects_private_dns_before_sending_and_never_follows_redirect(
        self,
    ) -> None:
        with patch.object(
            reader,
            "request_public_http",
            new=partial(
                request_public_http,
                resolver=_resolver_for("127.0.0.1"),
                connection_factory=lambda *_: self.fail("Private DNS must not connect."),
            ),
        ):
            result = reader.observe_discord_bot(
                "synthetic", deadline=monotonic() + 60, check_current=lambda: None
            )
        self.assertEqual(result.identity_status, "unavailable")
        self.assertEqual(result.failure_code, "provider_unavailable")
        response = _FakeResponse(
            status=302, headers=(("Location", "https://other.example.invalid"),)
        )
        connection = _FakeConnection(response)
        destinations = []

        def connect(destination: Any, timeout: float) -> Any:
            destinations.append(destination)
            return connection

        with patch.object(
            reader,
            "request_public_http",
            new=partial(
                request_public_http, resolver=_resolver_for("8.8.8.8"), connection_factory=connect
            ),
        ):
            result = reader.observe_discord_bot(
                "synthetic", deadline=monotonic() + 60, check_current=lambda: None
            )
        self.assertEqual(result.failure_code, "provider_unavailable")
        self.assertEqual(len(destinations), 1)
        self.assertEqual(destinations[0].hostname, urlsplit(reader.DISCORD_API_URL).hostname)
        self.assertEqual(connection.requests[0][3]["Authorization"], "Bot synthetic")
        self.assertTrue(connection.closed)
        self.assertTrue(response.closed)

    def test_collection_budget_is_not_reported_as_complete(self) -> None:
        def respond(url: str, **kwargs: Any) -> PublicHttpResponse:
            if "/guilds?" in url:
                after = parse_qs(urlsplit(url).query).get("after", ["0"])[0]
                return self.response(url, [{"id": str(int(after) + 1)}])
            return self.base(url, **kwargs)

        with patch.object(reader, "DISCORD_PAGE_BUDGET", 2):
            result = self.observe(respond)
        self.assertEqual(result.failure_code, "collection_budget_exhausted")
        self.assertFalse(result.guild_enumeration_complete)
        self.assertFalse(result.guild_ids)

    def test_partition_count_change_discards_collected_guilds(self) -> None:
        count = 0

        def respond(url: str, **kwargs: Any) -> PublicHttpResponse:
            nonlocal count
            if url.endswith("/gateway/bot"):
                count += 1
                return self.response(url, {"session_start_limit": {"max_concurrency": count}})
            return self.base(url, **kwargs)

        result = self.observe(respond)
        self.assertEqual(result.failure_code, "guild_partition_changed")
        self.assertFalse(result.guild_ids)

    def test_auth_failure_redirect_and_malformed_body_are_bounded_and_sanitized(self) -> None:
        for outcome in (
            PublicHttpResponse(401, "", 0, (), b"private error synthetic-bot-token"),
            RuntimeError("private redirect synthetic-bot-token"),
            PublicHttpResponse(200, "", 0, (), b"malformed synthetic-bot-token"),
        ):
            with self.subTest(outcome=type(outcome)):

                def respond(*args: Any, **kwargs: Any) -> PublicHttpResponse:
                    if isinstance(outcome, Exception):
                        raise outcome
                    return outcome

                result = self.observe(respond)
                self.assertEqual(result.identity_status, "unavailable")
                self.assertFalse(result.guild_enumeration_complete)
                self.assertNotIn("synthetic-bot-token", result.model_dump_json())

    def test_deadline_and_stalled_pages_remain_incomplete(self) -> None:
        with patch.object(reader, "request_public_http") as request:
            result = reader.observe_discord_bot(
                "synthetic", deadline=monotonic() - 1, check_current=lambda: None
            )
            request.assert_not_called()
        self.assertEqual(result.failure_code, "collection_budget_exhausted")

        def respond(url: str, **kwargs: Any) -> PublicHttpResponse:
            if "/guilds?" in url:
                return self.response(url, [{"id": "10"}])
            return self.base(url, **kwargs)

        result = self.observe(respond)
        self.assertEqual(result.failure_code, "response_invalid")
        self.assertFalse(result.guild_ids)
