from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.message import Message
import unittest
from typing import Literal
from unittest.mock import MagicMock, Mock, patch
from urllib.error import HTTPError

from control_plane.contracts.ordinary_agent_effect import OrdinaryAgentProviderQuotaKey
from control_plane.ordinary_agent_custody import (
    OrdinaryAgentCustodyCleanupUnknown,
    ordinary_agent_provider_token_lease,
)
from control_plane.ordinary_agent_github_transport import (
    DeadlineMergeTrainGitHubTransport,
    OrdinaryAgentProviderDeferred,
    OrdinaryAgentProviderEvidenceError,
    require_complete_graphql_data,
    require_installation_provider_ready,
)
from control_plane.ordinary_agent_quota_transport import OrdinaryAgentQuotaTransport
from tests import test_ordinary_agent_custody as custody_support


NOW = datetime(2026, 9, 9, 12, tzinfo=timezone.utc)


class _Response:
    def __init__(self, payload: bytes, headers: Message | None = None) -> None:
        self._payload = payload
        self.headers = headers or Message()

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._payload


def _http_error(status: int, **headers: str) -> HTTPError:
    response_headers = Message()
    for name, value in headers.items():
        response_headers[name.replace("_", "-")] = value
    return HTTPError(
        url="https://api.github.com/rate-limited",
        code=status,
        msg="rate limited",
        hdrs=response_headers,
        fp=None,
    )


class OrdinaryAgentQuotaTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        custody_support.OrdinaryAgentCustodyTests.setUpClass()

    def setUp(self) -> None:
        self.fixture = custody_support.OrdinaryAgentCustodyTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def test_falsy_injected_transport_is_used_without_default_construction(self) -> None:
        injected = MagicMock()
        injected.__bool__.return_value = False
        injected.request.return_value = {"ok": True}
        transport = OrdinaryAgentQuotaTransport(
            token="unused",
            installation_id=77,
            writer=self.fixture.store.record_provider_wait,
            transport=injected,
            utc_now=lambda: NOW,
        )
        with patch(
            "control_plane.ordinary_agent_quota_transport.UrllibMergeTrainGitHubTransport"
        ) as default:
            self.assertEqual(
                transport.request(method="GET", path="/repos/example/project"), {"ok": True}
            )
        default.assert_not_called()
        injected.request.assert_called_once()

    def test_primary_and_secondary_headers_block_their_respective_resources(self) -> None:
        headers = Message()
        headers["X-RateLimit-Remaining"] = "0"
        headers["X-RateLimit-Reset"] = str(int(NOW.timestamp()) + 3600)
        headers["Retry-After"] = "300"
        transport = OrdinaryAgentQuotaTransport(
            token="unused",
            installation_id=77,
            writer=self.fixture.store.record_provider_wait,
            utc_now=lambda: NOW,
        )
        with patch(
            "control_plane.merge_train_github.urlopen", return_value=_Response(b"{}", headers)
        ):
            transport.request(method="GET", path="/repos/example/project")
        cases: list[tuple[tuple[Literal["core", "graphql", "secondary"], ...], int]] = [
            (("graphql", "secondary"), 300),
            (("core", "secondary"), 3600),
        ]
        for resources, deadline in cases:
            with (
                self.subTest(resources=resources),
                self.assertRaises(OrdinaryAgentProviderDeferred) as raised,
            ):
                require_installation_provider_ready(
                    app_id=42,
                    installation_id=77,
                    resource_classes=resources,
                    read_provider_wait=self.fixture.store.read_provider_wait,
                    utc_now=lambda: NOW,
                )
            self.assertEqual(raised.exception.retry_not_before, int(NOW.timestamp()) + deadline)

    def test_default_graphql_transport_records_header_and_body_waits_once(self) -> None:
        reset = int((NOW + timedelta(hours=2)).timestamp())
        headers = Message()
        headers["X-RateLimit-Remaining"] = "0"
        headers["X-RateLimit-Reset"] = str(reset)
        headers["X-Ignored-Response"] = "must-not-reach-observer"
        payload = b'{"data": null, "errors": [{"type": "RATE_LIMITED"}]}'
        transport = DeadlineMergeTrainGitHubTransport(
            transport=OrdinaryAgentQuotaTransport(
                token="installation-token",
                installation_id=77,
                writer=self.fixture.store.record_provider_wait,
                utc_now=lambda: NOW,
            ),
            work_deadline=100,
            token_deadline=100,
            monotonic=lambda: 0,
        )

        with patch(
            "control_plane.merge_train_github.urlopen",
            return_value=_Response(payload, headers),
        ):
            response = transport.request(method="POST", path="/graphql", body={"query": "q"})

        with self.assertRaisesRegex(OrdinaryAgentProviderEvidenceError, "provider_wait"):
            require_complete_graphql_data(response, transport=transport)
        primary_key = OrdinaryAgentProviderQuotaKey(
            authority_kind="installation", authority_id=77, resource_class="graphql"
        )
        secondary_key = primary_key.model_copy(update={"resource_class": "secondary"})
        primary = self.fixture.store.read_provider_wait(quota_key=primary_key)
        secondary = self.fixture.store.read_provider_wait(quota_key=secondary_key)
        self.assertIsNotNone(primary)
        self.assertIsNotNone(secondary)
        assert primary is not None and secondary is not None
        self.assertEqual(primary.retry_not_before, reset)
        self.assertEqual(secondary.retry_not_before, int(NOW.timestamp()) + 60)
        with self.assertRaises(OrdinaryAgentProviderDeferred) as raised:
            require_installation_provider_ready(
                app_id=42,
                installation_id=77,
                resource_classes=("graphql", "secondary"),
                read_provider_wait=self.fixture.store.read_provider_wait,
                utc_now=lambda: NOW,
            )
        self.assertEqual(raised.exception.retry_not_before, reset)
        self.assertEqual(
            (transport.rest_core_requests, transport.graphql_requests, transport.graphql_points),
            (0, 1, 0),
        )

    def test_default_transport_writer_failure_preserves_success_response(self) -> None:
        headers = Message()
        headers["Retry-After"] = "60"
        writer = Mock(side_effect=RuntimeError("wait store unavailable"))
        transport = OrdinaryAgentQuotaTransport(
            token="installation-token",
            installation_id=77,
            writer=writer,
            utc_now=lambda: NOW,
        )

        with patch(
            "control_plane.merge_train_github.urlopen",
            return_value=_Response(b'{"ok": true}', headers),
        ):
            response = transport.request(method="GET", path="/repos/example/repo")

        self.assertEqual(response, {"ok": True})
        writer.assert_called_once()

    def test_discovery_and_mint_quota_failures_use_app_authority(self) -> None:
        for stage in ("discovery", "mint"):
            with self.subTest(stage=stage):
                fixture = custody_support.OrdinaryAgentCustodyTests()
                fixture.setUp()
                self.addCleanup(fixture.tearDown)
                calls: list[str] = []

                def api_request(**kwargs: object) -> object:
                    path = str(kwargs["path"])
                    calls.append(path)
                    if path == "/repos/example/repo/installation" and stage == "mint":
                        return _installation_payload()
                    raise _http_error(429, retry_after="120")

                with self.assertRaises(HTTPError):
                    with ordinary_agent_provider_token_lease(
                        record_store=fixture.store,
                        secret_store=fixture.store,
                        candidate=custody_support._candidate(),
                        idempotency_key=f"quota-{stage}",
                        request_payload={"stage": stage},
                        api_request=api_request,
                        utc_now=lambda: NOW,
                        quota_writer=fixture.store.record_provider_wait,
                    ):
                        self.fail("quota failure must not yield a token")

                app_wait = fixture.store.read_provider_wait(
                    quota_key=OrdinaryAgentProviderQuotaKey(
                        authority_kind="app", authority_id=42, resource_class="secondary"
                    )
                )
                installation_wait = fixture.store.read_provider_wait(
                    quota_key=OrdinaryAgentProviderQuotaKey(
                        authority_kind="installation",
                        authority_id=77,
                        resource_class="secondary",
                    )
                )
                self.assertIsNotNone(app_wait)
                self.assertIsNone(installation_wait)
                self.assertEqual(
                    calls,
                    ["/repos/example/repo/installation"]
                    if stage == "discovery"
                    else [
                        "/repos/example/repo/installation",
                        "/app/installations/77/access_tokens",
                    ],
                )

    def test_revoke_quota_failure_uses_actual_installation_authority(self) -> None:
        calls: list[str] = []
        lease_now = datetime.now(timezone.utc)

        def api_request(**kwargs: object) -> object:
            path = str(kwargs["path"])
            calls.append(path)
            if path == "/repos/example/repo/installation":
                return _installation_payload()
            if path == "/app/installations/77/access_tokens":
                return _token_payload(now=lease_now)
            raise _http_error(429, retry_after="180")

        with self.assertRaises(OrdinaryAgentCustodyCleanupUnknown):
            with ordinary_agent_provider_token_lease(
                record_store=self.fixture.store,
                secret_store=self.fixture.store,
                candidate=custody_support._candidate().model_copy(
                    update={"expected_installation_id": 77}
                ),
                idempotency_key="quota-revoke",
                request_payload={"stage": "revoke"},
                api_request=api_request,
                utc_now=lambda: lease_now,
                quota_writer=self.fixture.store.record_provider_wait,
            ):
                pass

        app_wait = self.fixture.store.read_provider_wait(
            quota_key=OrdinaryAgentProviderQuotaKey(
                authority_kind="app", authority_id=42, resource_class="secondary"
            )
        )
        installation_wait = self.fixture.store.read_provider_wait(
            quota_key=OrdinaryAgentProviderQuotaKey(
                authority_kind="installation", authority_id=77, resource_class="secondary"
            )
        )
        self.assertIsNone(app_wait)
        self.assertIsNotNone(installation_wait)
        self.assertEqual(
            calls,
            [
                "/repos/example/repo/installation",
                "/app/installations/77/access_tokens",
                "/installation/token",
            ],
        )


def _installation_payload() -> dict[str, object]:
    return {
        "id": 77,
        "app_id": 42,
        "permissions": {
            "administration": "read",
            "checks": "read",
            "contents": "write",
            "metadata": "read",
            "pull_requests": "write",
            "statuses": "read",
        },
    }


def _token_payload(*, now: datetime) -> dict[str, object]:
    return {
        "token": "provider-token-secret",
        "expires_at": (now + timedelta(minutes=45)).isoformat(),
        "permissions": {
            "contents": "write",
            "metadata": "read",
            "pull_requests": "read",
        },
        "repositories": [{"id": 123, "full_name": "example/repo"}],
    }


if __name__ == "__main__":
    unittest.main()
