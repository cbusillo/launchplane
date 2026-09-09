from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.message import Message
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

import click

from control_plane.contracts.ordinary_agent_effect import OrdinaryAgentProviderQuotaKey
from control_plane.merge_train_github import MergeTrainGitHubError
from control_plane.merge_train_github import UrllibMergeTrainGitHubTransport
from control_plane.ordinary_agent_provider_wait import call_with_provider_wait_observation
from control_plane.ordinary_agent_provider_wait import observe_graphql_provider_wait
from control_plane.ordinary_agent_provider_wait import observe_provider_wait_error
from control_plane.ordinary_agent_provider_wait import provider_wait_observation_from_exception
from control_plane.ordinary_agent_provider_wait import provider_wait_observation_from_graphql
from control_plane.workflows.launchplane import github_api_request


NOW = datetime(2026, 9, 9, 12, tzinfo=timezone.utc)
QUOTA_KEY = OrdinaryAgentProviderQuotaKey(
    authority_kind="installation", authority_id=123, resource_class="core"
)


def _http_error(status: int, **headers: str) -> HTTPError:
    response_headers = Message()
    for name, value in headers.items():
        response_headers[name.replace("_", "-")] = value
    return HTTPError(
        url="https://api.github.com/rate-limited",
        code=status,
        msg="secret response body must not be persisted",
        hdrs=response_headers,
        fp=None,
    )


class OrdinaryAgentProviderWaitTests(unittest.TestCase):
    def test_graphql_observer_failure_does_not_disrupt_response_handling(self) -> None:
        writer = Mock()
        for clock in (lambda: datetime(2026, 1, 1), Mock(side_effect=RuntimeError("clock"))):
            self.assertIsNone(
                observe_graphql_provider_wait(
                    {"errors": [{"type": "RATE_LIMITED"}]},
                    quota_key=QUOTA_KEY,
                    record_provider_wait=writer,
                    utc_now=clock,
                )
            )
        writer.assert_not_called()

    def test_secondary_http_date_deadline_is_honored_and_past_date_is_ignored(self) -> None:
        observation = provider_wait_observation_from_exception(
            _http_error(403, retry_after="Wed, 09 Sep 2026 12:05:00 GMT"), utc_now=lambda: NOW
        )
        assert observation is not None
        self.assertEqual(observation.retry_not_before, int(NOW.timestamp()) + 300)
        self.assertIsNone(
            provider_wait_observation_from_exception(
                _http_error(403, retry_after="Wed, 09 Sep 2026 11:05:00 GMT"), utc_now=lambda: NOW
            )
        )

    def test_plain_403_is_not_quota_evidence_through_real_transport_chain(self) -> None:
        provider_error = _http_error(403)
        with patch("control_plane.merge_train_github.urlopen", side_effect=provider_error):
            with self.assertRaises(MergeTrainGitHubError) as raised:
                UrllibMergeTrainGitHubTransport(token="secret-token").request(
                    method="GET", path="/repos/example/project"
                )
        self.assertIs(raised.exception.__cause__, provider_error)
        self.assertIsNone(
            provider_wait_observation_from_exception(raised.exception, utc_now=lambda: NOW)
        )

    def test_429_uses_fallback_through_actual_api_request_chain(self) -> None:
        provider_error = _http_error(429)
        with patch("control_plane.workflows.launchplane.urlopen", side_effect=provider_error):
            with self.assertRaises(click.ClickException) as raised:
                github_api_request(path="/installation/token", token="secret-token")
        self.assertIs(raised.exception.__cause__, provider_error)
        observation = provider_wait_observation_from_exception(
            raised.exception, utc_now=lambda: NOW
        )
        self.assertIsNotNone(observation)
        assert observation is not None
        self.assertEqual(
            observation.model_dump(),
            {
                "retry_not_before": int(NOW.timestamp()) + 60,
                "classification": "secondary_rate_limit",
            },
        )

    def test_primary_reset_longer_than_one_hour_is_retained(self) -> None:
        reset = int((NOW + timedelta(hours=7)).timestamp())
        error = _http_error(
            403,
            x_ratelimit_remaining="0",
            x_ratelimit_reset=str(reset),
            retry_after="60",
        )
        observation = provider_wait_observation_from_exception(error, utc_now=lambda: NOW)
        self.assertIsNotNone(observation)
        assert observation is not None
        self.assertEqual(observation.retry_not_before, reset)
        self.assertEqual(observation.classification, "primary_rate_limit")

    def test_retry_after_longer_than_one_hour_is_retained(self) -> None:
        observation = provider_wait_observation_from_exception(
            _http_error(429, retry_after=str(7 * 60 * 60)),
            utc_now=lambda: NOW,
        )
        self.assertIsNotNone(observation)
        assert observation is not None
        self.assertEqual(observation.retry_not_before, int(NOW.timestamp()) + 7 * 60 * 60)
        self.assertEqual(observation.classification, "secondary_rate_limit")

    def test_malformed_headers_do_not_poison_wait(self) -> None:
        for error, expected in (
            (_http_error(403, x_ratelimit_remaining="0", x_ratelimit_reset="huge"), None),
            (
                _http_error(
                    403,
                    x_ratelimit_remaining="0",
                    x_ratelimit_reset=str(2**62),
                ),
                None,
            ),
            (
                _http_error(
                    429,
                    x_ratelimit_remaining="0",
                    x_ratelimit_reset=str(2**63),
                    retry_after="invalid",
                ),
                int(NOW.timestamp()) + 60,
            ),
            (
                _http_error(
                    429,
                    x_ratelimit_remaining="0",
                    x_ratelimit_reset="9" * 5000,
                    retry_after="9" * 5000,
                ),
                int(NOW.timestamp()) + 60,
            ),
        ):
            with self.subTest(status=error.code, headers=dict(error.headers or {})):
                observation = provider_wait_observation_from_exception(error, utc_now=lambda: NOW)
                if expected is None:
                    self.assertIsNone(observation)
                else:
                    self.assertIsNotNone(observation)
                    assert observation is not None
                    self.assertEqual(observation.retry_not_before, expected)

    def test_graphql_rate_limited_with_null_data_uses_fallback(self) -> None:
        payload = {"data": None, "errors": [{"type": "RATE_LIMITED"}]}
        observation = provider_wait_observation_from_graphql(payload, utc_now=lambda: NOW)
        self.assertIsNotNone(observation)
        assert observation is not None
        self.assertEqual(
            observation.model_dump(),
            {
                "retry_not_before": int(NOW.timestamp()) + 60,
                "classification": "secondary_rate_limit",
            },
        )

    def test_graphql_primary_reset_is_retained(self) -> None:
        reset = NOW + timedelta(hours=5)
        payload = {
            "data": {
                "repository": {},
                "rateLimit": {
                    "cost": 7,
                    "remaining": 0,
                    "resetAt": reset.isoformat().replace("+00:00", "Z"),
                },
            },
            "errors": [{"type": "RATE_LIMITED"}],
        }
        observation = provider_wait_observation_from_graphql(payload, utc_now=lambda: NOW)
        self.assertIsNotNone(observation)
        assert observation is not None
        self.assertEqual(observation.retry_not_before, int(reset.timestamp()))

    def test_observers_persist_only_typed_key_and_observation(self) -> None:
        writer = Mock()
        error = RuntimeError("secret-token and response body")
        error.__cause__ = _http_error(429, retry_after="120")
        observation = observe_provider_wait_error(
            error,
            quota_key=QUOTA_KEY,
            record_provider_wait=writer,
            utc_now=lambda: NOW,
        )
        self.assertIsNotNone(observation)
        writer.assert_called_once()
        persisted = writer.call_args.kwargs
        self.assertEqual(set(persisted), {"quota_key", "observation"})
        self.assertEqual(
            persisted["quota_key"], QUOTA_KEY.model_copy(update={"resource_class": "secondary"})
        )
        self.assertNotIn("secret", str(persisted))

    def test_primary_observer_preserves_exact_resource_class(self) -> None:
        writer = Mock()
        reset = int((NOW + timedelta(hours=2)).timestamp())
        observe_provider_wait_error(
            _http_error(
                403,
                x_ratelimit_remaining="0",
                x_ratelimit_reset=str(reset),
            ),
            quota_key=QUOTA_KEY,
            record_provider_wait=writer,
            utc_now=lambda: NOW,
        )
        self.assertEqual(writer.call_args.kwargs["quota_key"], QUOTA_KEY)

    def test_writer_failure_never_replaces_original_request_exception(self) -> None:
        original = _http_error(429)
        writer = Mock(side_effect=RuntimeError("database unavailable"))
        with self.assertRaises(HTTPError) as raised:
            call_with_provider_wait_observation(
                lambda: (_ for _ in ()).throw(original),
                quota_key=QUOTA_KEY,
                record_provider_wait=writer,
                utc_now=lambda: NOW,
            )
        self.assertIs(raised.exception, original)
        writer.assert_called_once()

    def test_observer_clock_failure_preserves_provider_error(self) -> None:
        original = _http_error(429)
        with self.assertRaises(HTTPError) as raised:
            call_with_provider_wait_observation(
                lambda: (_ for _ in ()).throw(original),
                quota_key=QUOTA_KEY,
                record_provider_wait=Mock(),
                utc_now=lambda: NOW.replace(tzinfo=None),
            )
        self.assertIs(raised.exception, original)

    def test_relative_wait_never_rounds_down_provider_delay(self) -> None:
        now = NOW.replace(microsecond=900000)
        observation = provider_wait_observation_from_exception(
            _http_error(429, retry_after="60"), utc_now=lambda: now
        )
        assert observation is not None
        self.assertGreaterEqual(observation.retry_not_before - now.timestamp(), 60)

    def test_graphql_observer_returns_signal_even_when_writer_fails(self) -> None:
        writer = Mock(side_effect=RuntimeError("database unavailable"))
        observation = observe_graphql_provider_wait(
            {"data": None, "errors": [{"type": "RATE_LIMITED"}]},
            quota_key=QUOTA_KEY.model_copy(update={"resource_class": "graphql"}),
            record_provider_wait=writer,
            utc_now=lambda: NOW,
        )
        self.assertIsNotNone(observation)
        writer.assert_called_once()
        self.assertEqual(
            writer.call_args.kwargs["quota_key"],
            QUOTA_KEY.model_copy(update={"resource_class": "secondary"}),
        )


if __name__ == "__main__":
    unittest.main()
