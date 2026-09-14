import os
import unittest
from unittest.mock import patch

from fastapi import FastAPI

from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import BearerIdentityConfig
from control_plane.storage.postgres import PostgresRecordStore
from tests.http_app_test_support import _post_merge_train_controller_run_once
from tests.support.auth import _StubVerifier, _identity
from tests.test_merge_train_historical_completion import _ReadOnlyTransport, _provider_responses
from tests.test_merge_train_historical_disposition_postgres import (
    IDENTITY,
    _authz_policy_record,
    _prepared_store,
)


def _app(store: PostgresRecordStore) -> FastAPI:
    return create_launchplane_fastapi_app(
        verifier=_StubVerifier(_identity()),
        authz_policy=_authz_policy_record().policy,
        record_store_factory=lambda: store,
        bearer_identity_config=BearerIdentityConfig(
            local_operator_token="historical-test-operator-token",
            local_operator_subject=IDENTITY.subject,
            local_operator_token_label=IDENTITY.token_label,
        ),
    )


AUTHORIZATION = "Bearer historical-test-operator-token"
TRANSPORT = "control_plane.merge_train_historical_disposition_http.UrllibMergeTrainGitHubTransport"


class HistoricalDispositionHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_dry_run_then_apply_and_lost_response_replay(self) -> None:
        with _prepared_store() as (store, fixture):
            app = _app(store)
            transport = _ReadOnlyTransport(responses=_provider_responses() + _provider_responses())
            with (
                patch.dict(os.environ, {"GH_TOKEN": "test-token"}),
                patch(TRANSPORT, return_value=transport),
            ):
                dry = await _post_merge_train_controller_run_once(
                    app, fixture.request_payload(), authorization=AUTHORIZATION
                )
                self.assertEqual(dry.status_code, 202, dry.json())
                preflight = dry.json()["result"]["historical_completion_preflight"]
                self.assertTrue(preflight["evidence_eligible"])
                self.assertTrue(preflight["disposition_supported"])
                self.assertFalse(preflight["fence_released"])
                self.assertEqual(
                    store.list_merge_train_controller_state_records()[0], fixture.controller
                )
                applied = await _post_merge_train_controller_run_once(
                    app,
                    fixture.request_payload(mutate=True),
                    authorization=AUTHORIZATION,
                    idempotency_key="http-historical-1",
                )
                self.assertEqual(applied.status_code, 202, applied.json())
                result = applied.json()["result"]["historical_completion_disposition"]
                self.assertTrue(result["fence_released"])
                self.assertFalse(result["admission_created"])
                self.assertFalse(result["provider_effect_attempted"])
            with patch(TRANSPORT, side_effect=AssertionError("replay contacted provider")):
                replay = await _post_merge_train_controller_run_once(
                    app,
                    fixture.request_payload(mutate=True),
                    authorization=AUTHORIZATION,
                    idempotency_key="http-historical-1",
                )
                self.assertEqual(replay.status_code, 202, replay.json())
                self.assertTrue(replay.json()["replayed"])
                self.assertEqual(replay.json()["original_trace_id"], applied.json()["trace_id"])
                self.assertEqual(replay.json()["records"], applied.json()["records"])
                duplicate = await _post_merge_train_controller_run_once(
                    app,
                    fixture.request_payload(mutate=True),
                    authorization=AUTHORIZATION,
                    idempotency_key="http-historical-other-key",
                )
                self.assertEqual(duplicate.status_code, 409, duplicate.json())
                self.assertEqual(duplicate.json()["details"]["reason_code"], "already_recorded")

    async def test_missing_key_and_untrusted_provider_endpoint_do_not_observe_or_write(
        self,
    ) -> None:
        with _prepared_store() as (store, fixture):
            app = _app(store)
            with patch(
                TRANSPORT, side_effect=AssertionError("rejected request contacted provider")
            ):
                missing_key = await _post_merge_train_controller_run_once(
                    app, fixture.request_payload(mutate=True), authorization=AUTHORIZATION
                )
                self.assertEqual(missing_key.status_code, 400)
                alternate = await _post_merge_train_controller_run_once(
                    app,
                    {**fixture.request_payload(), "github_api_base_url": "https://example.invalid"},
                    authorization=AUTHORIZATION,
                )
                self.assertEqual(alternate.status_code, 409)
                self.assertEqual(
                    alternate.json()["details"]["reason_code"], "provider_endpoint_unsupported"
                )
            self.assertEqual(
                store.list_merge_train_controller_state_records()[0], fixture.controller
            )


if __name__ == "__main__":
    unittest.main()
