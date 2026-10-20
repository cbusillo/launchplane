"""Real browser sessions and store-backed reviewed runtime synchronization."""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta
import json
import os
import threading
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
import unittest
from unittest.mock import patch

import click
from cryptography.fernet import Fernet

from control_plane import browser_runtime_sync as sync
from control_plane import live_target_runtime as runtime
from control_plane import secrets
from control_plane.dokploy import api as dokploy_api
from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.runtime_key_safety_policy import (
    RuntimeKeySafetyPolicyRecord,
    RuntimeSecretSafetyRule,
)
from control_plane.http_app import create_launchplane_fastapi_app, idempotency_scope
from control_plane.provider_operations import ProviderMutationUnknownError
from control_plane.service_auth import LaunchplaneAuthzPolicy
from control_plane.service_human_auth import HumanSessionManager, InMemoryHumanSessionStore
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.public_ingress_monitor import run_public_ingress_monitor_once
from tests.http_app_test_support import (
    _browser_mutation_headers,
    _github_human_identity,
    _github_oauth_config,
    _RejectingVerifier,
    _local_operator_bearer_config,
)
from tests.support.http import request
from tests.support.stores import _seed_tracked_target_records, _sqlite_database_url
from tests.test_service import _live_target_runtime_profile_payload


class BrowserRuntimeSyncTests(unittest.IsolatedAsyncioTestCase):
    database_url: str | None = None

    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = PostgresRecordStore(
            database_url=self.database_url or _sqlite_database_url(self.root / "db")
        )
        self.addCleanup(self.store.close)
        self.store.ensure_schema()
        self.product, self.context, self.instance = "example-site", "example-context", "testing"
        self.profile = LaunchplaneProductProfileRecord.model_validate(
            _live_target_runtime_profile_payload(
                product=self.product,
                context=self.context,
                instance=self.instance,
            )
        )
        self.store.write_product_profile_record(self.profile)
        self.setting = RuntimeEnvironmentRecord(
            scope="instance",
            context=self.context,
            instance=self.instance,
            env={"GOOGLE_ANALYTICS_MEASUREMENT_ID": "private-desired-value"},
            updated_at=self.profile.updated_at,
            source_label="fixture",
        )
        self.store.write_runtime_environment_record(self.setting)
        _seed_tracked_target_records(
            database_url=self.store.database_url,
            context=self.context,
            instance=self.instance,
            target_id="application-fixture",
            target_type="application",
            target_name="fixture-target",
        )
        self.target = self.store.read_dokploy_target_record(
            context_name=self.context, instance_name=self.instance
        )
        self.target_id = self.store.read_dokploy_target_id_record(
            context_name=self.context, instance_name=self.instance
        )
        self.provider = ProviderTargetRecord.from_dokploy_records(
            target_record=self.target, target_id_record=self.target_id
        )
        self.store.write_provider_target_record(self.provider)
        self.manager = HumanSessionManager(
            config=_github_oauth_config(), session_store=InMemoryHumanSessionStore()
        )
        self.session = self.manager.issue(_github_human_identity())
        self.policy = LaunchplaneAuthzPolicy.model_validate(
            {
                "schema_version": 2,
                "github_humans": [
                    {
                        "github_ids": [self.session.identity.github_id],
                        "roles": ["admin"],
                        "products": ["launchplane"],
                        "contexts": ["launchplane"],
                        "actions": ["authz_policy_grant.write"],
                    }
                ],
            }
        )
        self.app = self.make_app()
        self.payload: dict[str, Any] = {
            "mode": "dry-run",
            "product": self.product,
            "context": self.context,
            "instance": self.instance,
            "browser_review": {},
        }
        self.env = "GOOGLE_ANALYTICS_MEASUREMENT_ID=old-private-value\nPROVIDER_ONLY=keep-private\n"
        self.updates: list[dict[str, Any]] = []
        self.fetch_count = 0
        self.fetch_hook: Any = None
        self.timeout_after_update = False
        self.provider_failure: dokploy_api.DokployRequestFailed | None = None
        self.mismatched_update = False
        self.host = "https://provider.invalid"
        for replacement in (
            patch.dict(
                "os.environ", {secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "fixture-master-key"}
            ),
            patch(
                "control_plane.dokploy.source.read_dokploy_config",
                side_effect=self.read_provider_config,
            ),
            patch("control_plane.dokploy.api.fetch_dokploy_target_payload", side_effect=self.fetch),
            patch("control_plane.dokploy.api._dokploy_request", side_effect=self.provider_request),
            patch("control_plane.live_target_runtime.trigger_and_wait_for_dokploy_target_deploy"),
        ):
            replacement.start()
            self.addCleanup(replacement.stop)

    def make_app(self, policy: LaunchplaneAuthzPolicy | None = None) -> Any:
        return create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=policy or self.policy,
            human_session_manager=self.manager,
            record_store_factory=lambda: self.store,
            control_plane_root_path=self.root,
            bearer_identity_config=_local_operator_bearer_config(token_label="local-owner-write"),
        )

    def read_provider_config(self, **kwargs: Any) -> tuple[str, str]:
        if kwargs["control_plane_root"] != self.root:
            raise click.ClickException(
                "Fixture provider configuration is unavailable at this root."
            )
        return self.host, "fixture-token"

    def fetch(self, **kwargs: Any) -> dict[str, str]:
        self.fetch_count += 1
        if self.fetch_hook:
            self.fetch_hook(self.fetch_count)
        return {
            "applicationId": self.target_id.target_id,
            "name": self.target.target_name,
            "env": self.env,
        }

    def update(self, **kwargs: Any) -> None:
        self.updates.append(kwargs)
        self.env = kwargs["env_text"]
        if self.timeout_after_update:
            raise click.ClickException("provider body contains private-desired-value fixture-token")

    def provider_request(self, **kwargs: Any) -> dict[str, object]:
        self.assertEqual(kwargs["method"], "POST")
        self.assertEqual(
            kwargs["path"], dokploy_api.dokploy_target_env_update_path(self.target.target_type)
        )
        id_key = "applicationId" if self.target.target_type == "application" else "composeId"
        self.assertEqual(kwargs["payload"][id_key], self.target_id.target_id)
        if self.provider_failure is not None:
            raise self.provider_failure
        self.update(env_text=kwargs["payload"]["env"])
        if self.mismatched_update:
            self.env = "GOOGLE_ANALYTICS_MEASUREMENT_ID=unexpected-value\n"
        return {}

    async def interrupt_acknowledged(self, payload: dict[str, Any], key: str) -> Any:
        with patch.object(
            self.store,
            "complete_mutation_reservation",
            side_effect=ValueError("fixture receipt lost"),
        ):
            response = await self.post(payload, key)
        self.assertEqual(response.status_code, 503, response.text)
        record = self.store.read_idempotency_record(
            scope=idempotency_scope(self.session.identity),
            route_path="/v1/live-target-runtime/apply",
            idempotency_key=key,
        )
        assert record is not None
        transition = self.store.mark_mutation_reconcile_required(
            reservation=record, reconciliation_key=record.reconciliation_key
        )
        self.assertEqual(transition.status, "updated")
        return response

    async def post(
        self,
        payload: dict[str, Any],
        key: str = "",
        *,
        app: Any = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        selected = (
            headers
            if headers is not None
            else _browser_mutation_headers(self.manager, self.session)
        )
        if key:
            selected = {**selected, "Idempotency-Key": key}
        return await request(
            app or self.app,
            "POST",
            "/v1/live-target-runtime/apply",
            payload=payload,
            headers=selected,
        )

    async def plan(self) -> dict[str, Any]:
        response = await self.post(self.payload)
        self.assertEqual(response.status_code, 202, response.text)
        return dict(response.json()["review"])

    def apply_payload(self, review: dict[str, Any]) -> dict[str, Any]:
        return {
            **self.payload,
            "mode": "apply",
            "browser_review": {"reviewed_plan_sha256": review["plan_sha256"]},
        }

    def change_setting(self, value: str = "changed-private-value") -> None:
        self.store.write_runtime_environment_record(
            self.setting.model_copy(update={"env": {"GOOGLE_ANALYTICS_MEASUREMENT_ID": value}})
        )

    def assert_redacted(self, response: Any) -> None:
        for value in (
            "private-desired-value",
            "old-private-value",
            "keep-private",
            "fixture-token",
            "secret-private-value",
        ):
            self.assertNotIn(value, response.text)

    async def test_real_review_apply_and_completed_replay_precede_input_drift(self) -> None:
        review = await self.plan()
        self.assertEqual(review, await self.plan())
        self.assertFalse(self.updates)
        applied = self.apply_payload(review)
        response = await self.post(applied, "original")
        self.assertEqual(response.status_code, 202, response.text)
        self.assertTrue(response.json()["review"]["applied"])
        self.assertEqual(len(self.updates), 1)
        self.assertIn("private-desired-value", self.env)
        self.assertIn("keep-private", self.env)
        self.assertFalse(response.json()["result"]["deploy"]["requested"])
        self.assert_redacted(response)
        self.change_setting()
        replay = await self.post(applied, "original")
        self.assertEqual(replay.status_code, 202, replay.text)
        self.assertTrue(replay.json()["replayed"])
        self.assertEqual(replay.json()["review"], response.json()["review"])
        self.assertEqual(len(self.updates), 1)
        readback = await self.plan()
        self.assertNotEqual(readback["plan_sha256"], review["plan_sha256"])
        self.store.write_product_profile_record(
            self.profile.model_copy(update={"driver_id": "odoo"})
        )
        self.store.write_dokploy_target_id_record(
            self.target_id.model_copy(update={"target_id": "replacement-target"})
        )
        replay = await self.post(applied, "original")
        self.assertEqual(replay.status_code, 202, replay.text)
        self.assertTrue(replay.json()["replayed"])
        self.assertEqual(replay.json()["review"], response.json()["review"])
        self.assertEqual(len(self.updates), 1)

    async def test_same_keys_changed_value_refuses_before_effect(self) -> None:
        review = await self.plan()
        self.change_setting()
        response = await self.post(self.apply_payload(review), "stale")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["error"]["code"], "stale")
        self.assertFalse(self.updates)
        receipt = self.store.read_idempotency_record(
            scope=idempotency_scope(self.session.identity),
            route_path="/v1/live-target-runtime/apply",
            idempotency_key="stale",
        )
        assert receipt is not None
        self.assertEqual(receipt.response_status_code, 409)
        self.store.write_runtime_environment_record(self.setting)
        delayed = await self.post(self.apply_payload(review), "stale")
        self.assertEqual(delayed.status_code, 409, delayed.text)
        self.assertEqual(delayed.json()["error"]["code"], "stale")
        self.assertFalse(self.updates)

    async def test_target_private_evidence_provider_destination_and_baseline_drift(self) -> None:
        for drift in ("target", "private-evidence", "destination", "baseline"):
            with self.subTest(drift=drift):
                self.store.write_provider_target_record(self.provider)
                self.store.write_dokploy_target_id_record(self.target_id)
                self.env = "GOOGLE_ANALYTICS_MEASUREMENT_ID=old-private-value\n"
                review = await self.plan()
                if drift == "target":
                    self.store.write_dokploy_target_id_record(
                        self.target_id.model_copy(update={"target_id": "other-target"})
                    )
                elif drift == "private-evidence":
                    self.store.write_provider_target_record(
                        self.provider.model_copy(
                            update={"provider_evidence": {"private": "changed"}}
                        )
                    )
                elif drift == "baseline":
                    self.env += "NEW_PROVIDER_ONLY=private-new\n"
                with patch(
                    "control_plane.dokploy.source.read_dokploy_config",
                    return_value=(
                        (
                            "https://other.invalid"
                            if drift == "destination"
                            else "https://provider.invalid"
                        ),
                        "fixture-token",
                    ),
                ):
                    response = await self.post(self.apply_payload(review), drift)
                self.assertNotEqual(response.status_code, 202, response.text)
                self.assertFalse(self.updates)
                self.assert_redacted(response)

    async def test_late_input_and_provider_drift_never_write_captured_old_values(self) -> None:
        for drift in ("setting", "provider"):
            with self.subTest(drift=drift):
                self.store.write_runtime_environment_record(self.setting)
                self.env = "GOOGLE_ANALYTICS_MEASUREMENT_ID=old-private-value\n"
                review = await self.plan()
                self.fetch_count = 0

                def late(count: int) -> None:
                    if count == 3:
                        if drift == "setting":
                            self.change_setting()
                        else:
                            self.env += "LATE_PROVIDER_KEY=private-late\n"

                self.fetch_hook = late
                response = await self.post(self.apply_payload(review), f"late-{drift}")
                self.fetch_hook = None
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(response.json()["error"]["code"], "stale")
                self.assertFalse(self.updates)

    async def test_unknown_write_holds_shared_fence_and_original_key_adopts_without_redispatch(
        self,
    ) -> None:
        review = await self.plan()
        payload = self.apply_payload(review)
        first = await self.interrupt_acknowledged(payload, "unknown")
        self.assert_redacted(first)
        other = await self.post(payload, "new-key")
        self.assertEqual(other.status_code, 409, other.text)
        self.assertEqual(other.json()["error"]["code"], "runtime_sync_target_busy")
        self.assertIsNone(
            self.store.read_idempotency_record(
                scope=idempotency_scope(self.session.identity),
                route_path="/v1/live-target-runtime/apply",
                idempotency_key="new-key",
            )
        )
        self.assertEqual(len(self.updates), 1)
        self.store.write_product_profile_record(
            self.profile.model_copy(update={"display_name": "Updated display name"})
        )
        self.store.write_provider_target_record(
            self.provider.model_copy(update={"display_name": "Updated provider label"})
        )
        adopted = await self.post(payload, "unknown")
        self.assertEqual(adopted.status_code, 202, adopted.text)
        self.assertTrue(adopted.json()["review"]["applied"])
        self.assertTrue(adopted.json()["result"]["apply"]["recovered_by_readback"])
        self.assertIsNone(adopted.json()["result"]["apply"]["env_updated"])
        self.assertTrue(adopted.json()["result"]["apply"]["update_acknowledged"])
        self.assertEqual(len(self.updates), 1)
        self.assert_redacted(adopted)

    async def test_unacknowledged_write_cannot_settle_from_matching_values(self) -> None:
        payload = self.apply_payload(await self.plan())
        lane = (self.product, self.context, self.instance)
        before_revision = self.store.health_monitor_authority_revisions()[lane]
        self.timeout_after_update = True
        first = await self.post(payload, "unacknowledged")
        self.assertEqual(first.status_code, 409, first.text)
        resumed = await self.post(payload, "unacknowledged")
        self.assertEqual(resumed.status_code, 409, resumed.text)
        self.assertEqual(resumed.json()["error"]["code"], "mutation_reconciliation_required")
        other = await self.post(payload, "must-still-be-fenced")
        self.assertEqual(other.json()["error"]["code"], "runtime_sync_target_busy")
        self.assertEqual(len(self.updates), 1)
        self.assertNotEqual(self.store.health_monitor_authority_revisions()[lane], before_revision)
        record = self.store.read_idempotency_record(
            scope=idempotency_scope(self.session.identity),
            route_path="/v1/live-target-runtime/apply",
            idempotency_key="unacknowledged",
        )
        assert record is not None
        checked_at = (
            datetime.fromisoformat(record.updated_at.replace("Z", "+00:00")) + timedelta(hours=1)
        ).isoformat()
        monitored = run_public_ingress_monitor_once(record_store=self.store, checked_at=checked_at)
        self.assertEqual(monitored.open_incident_count, 1)
        self.assertIn(record.record_id, monitored.records[0].summary)
        self.assertNotIn(record.idempotency_key, monitored.records[0].summary)

    async def test_delayed_old_post_stays_fenced_after_machine_writes_matching_values(self) -> None:
        payload = self.apply_payload(await self.plan())
        accepted, release = threading.Event(), threading.Event()
        with ThreadPoolExecutor(max_workers=1) as pool:

            def delayed(env_text: str) -> None:
                accepted.set()
                if not release.wait(10):
                    raise AssertionError("Delayed provider fixture was not released")
                self.env = env_text

            def dispatch(**kwargs: Any) -> dict[str, object]:
                self.updates.append(kwargs)
                future = pool.submit(delayed, kwargs["payload"]["env"])
                delayed_futures.append(future)
                self.assertTrue(accepted.wait(10))
                raise dokploy_api.DokployRequestFailed(
                    method="POST", path=kwargs["path"], detail="fixture timeout"
                )

            delayed_futures: list[Any] = []
            try:
                with patch("control_plane.dokploy.api._dokploy_request", side_effect=dispatch):
                    first = await self.post(payload, "delayed-old-post")
                self.assertEqual(first.status_code, 409, first.text)
                runtime.apply_live_target_runtime_environment(
                    control_plane_root=self.root,
                    database_url=self.store.database_url,
                    product_name=self.product,
                    context_name=self.context,
                    instance_name=self.instance,
                    apply_changes=True,
                    deploy=False,
                    no_cache=False,
                    deploy_timeout_seconds=None,
                    deploy_trigger=runtime.trigger_and_wait_for_dokploy_target_deploy,
                )
                resumed = await self.post(payload, "delayed-old-post")
                self.assertEqual(
                    resumed.json()["error"]["code"], "mutation_reconciliation_required"
                )
                other = await self.post(payload, "blocked-new-operation")
                self.assertEqual(other.json()["error"]["code"], "runtime_sync_target_busy")
                self.assertEqual(len(self.updates), 2)
            finally:
                release.set()
                for future in delayed_futures:
                    future.result(timeout=10)

    async def test_provider_refusal_and_local_preflight_are_terminal_and_replay_exactly(
        self,
    ) -> None:
        for local in (False, True):
            with self.subTest(local=local):
                payload = self.apply_payload(await self.plan())
                key = f"definite-refusal-{local}"
                if local:
                    with patch(
                        "control_plane.dokploy.api.update_dokploy_target_env",
                        side_effect=click.ClickException("fixture local preflight"),
                    ):
                        first = await self.post(payload, key)
                    code = "runtime_sync_not_applied"
                else:
                    self.provider_failure = dokploy_api.DokployRequestFailed(
                        method="POST",
                        path=dokploy_api.dokploy_target_env_update_path(self.target.target_type),
                        status_code=403,
                        detail="fixture denied",
                    )
                    first = await self.post(payload, key)
                    self.provider_failure = None
                    code = "runtime_sync_provider_refused"
                self.assertEqual(first.status_code, 409, first.text)
                self.assertEqual(first.json()["error"]["code"], code)
                self.change_setting()
                replay = await self.post(payload, key)
                self.assertEqual(replay.json(), first.json())
                self.store.write_runtime_environment_record(self.setting)
        self.assertFalse(self.updates)
        terminal = self.store.read_idempotency_record(
            scope=idempotency_scope(self.session.identity),
            route_path="/v1/live-target-runtime/apply",
            idempotency_key=key,
        )
        assert terminal is not None
        for status, error in (
            (409, {}),
            (409, {"code": "unexpected_terminal"}),
            (503, {"code": "runtime_sync_unverified"}),
            (202, {"code": "runtime_sync_unverified"}),
        ):
            with self.subTest(status=status, error=error):
                self.store.write_idempotency_record(
                    terminal.model_copy(
                        update={
                            "response_status_code": status,
                            "response_payload": {"error": error},
                        }
                    )
                )
                uncertain = await self.post(payload, key)
                self.assertEqual(uncertain.status_code, 503, uncertain.text)
                self.assertEqual(uncertain.json()["error"]["code"], "runtime_sync_unavailable")
        self.store.write_idempotency_record(terminal)
        self.assertEqual(
            (await self.post(self.apply_payload(await self.plan()), "after-refusals")).status_code,
            202,
        )

    async def test_retryable_provider_failure_remains_unknown(self) -> None:
        await self.check_unknown_provider_failure(503, False)

    async def test_partial_remote_command_failure_remains_unknown(self) -> None:
        await self.check_unknown_provider_failure(400, True)

    async def check_unknown_provider_failure(self, status: int, remote: bool) -> None:
        payload = self.apply_payload(await self.plan())
        self.provider_failure = dokploy_api.DokployRequestFailed(
            method="POST",
            path=dokploy_api.dokploy_target_env_update_path(self.target.target_type),
            status_code=status,
            detail="fixture unknown",
            remote_command_failed=remote,
        )
        first = await self.post(payload, "unknown-provider")
        self.assertEqual(first.json()["error"]["code"], "mutation_reconciliation_required")
        self.provider_failure = None
        self.assertEqual(
            (await self.post(payload, "unknown-provider")).json()["error"]["code"],
            "mutation_reconciliation_required",
        )
        self.assertEqual(
            (await self.post(payload, "other-provider")).json()["error"]["code"],
            "runtime_sync_target_busy",
        )
        self.assertFalse(self.updates)

    async def test_acknowledged_but_unverified_is_terminal_and_replays(self) -> None:
        for mismatch in (False, True):
            with self.subTest(mismatch=mismatch):
                self.env = "GOOGLE_ANALYTICS_MEASUREMENT_ID=old-private-value\n"
                payload = self.apply_payload(await self.plan())
                before = len(self.updates)

                def fail_readback(count: int) -> None:
                    if len(self.updates) > before:
                        raise click.ClickException("fixture verification unavailable")

                self.mismatched_update = mismatch
                self.fetch_hook = None if mismatch else fail_readback
                first = await self.post(payload, f"unverified-{mismatch}")
                self.fetch_hook = None
                self.mismatched_update = False
                self.assertEqual(first.status_code, 409, first.text)
                self.assertEqual(first.json()["error"]["code"], "runtime_sync_unverified")
                self.change_setting()
                replay = await self.post(payload, f"unverified-{mismatch}")
                self.assertEqual(replay.json(), first.json())
                fresh = await self.post(
                    self.apply_payload(await self.plan()), f"new-after-unverified-{mismatch}"
                )
                self.assertEqual(fresh.status_code, 202, fresh.text)
                self.store.write_runtime_environment_record(self.setting)

    async def test_acknowledgement_commit_loss_preserves_exact_lease_and_dispatch_count(
        self,
    ) -> None:
        for committed in (False, True):
            with self.subTest(committed=committed):
                self.env = "GOOGLE_ANALYTICS_MEASUREMENT_ID=old-private-value\n"
                payload = self.apply_payload(await self.plan())
                key = f"ack-commit-{committed}"
                original = self.store.checkpoint_mutation_provider_effect
                stale: list[Any] = []

                def checkpoint(**kwargs: Any) -> Any:
                    if kwargs["effect_phase"] == sync._ACKNOWLEDGED_PHASE:
                        stale.append(kwargs["reservation"])
                        if committed:
                            original(**kwargs)
                        raise ValueError("fixture ACK commit acknowledgement lost")
                    return original(**kwargs)

                with patch.object(
                    self.store, "checkpoint_mutation_provider_effect", side_effect=checkpoint
                ):
                    first = await self.post(payload, key)
                self.assertEqual(first.json()["error"]["code"], "mutation_reconciliation_required")
                record = self.store.read_idempotency_record(
                    scope=idempotency_scope(self.session.identity),
                    route_path="/v1/live-target-runtime/apply",
                    idempotency_key=key,
                )
                assert record is not None
                if committed:
                    self.assertEqual(
                        original(
                            reservation=stale[0], effect_phase=sync._ACKNOWLEDGED_PHASE
                        ).status,
                        "reservation_mismatch",
                    )
                self.store.mark_mutation_reconcile_required(
                    reservation=record, reconciliation_key=record.reconciliation_key
                )
                resumed = await self.post(payload, key)
                self.assertEqual(resumed.status_code, 202 if committed else 409, resumed.text)
                if not committed:
                    # Keep this unacknowledged target held; the next independent
                    # case uses another qualified recipient, not a forced release.
                    self.target_id = self.target_id.model_copy(
                        update={"target_id": "second-ack-target"}
                    )
                    self.store.write_dokploy_target_id_record(self.target_id)
                    self.provider = ProviderTargetRecord.from_dokploy_records(
                        target_record=self.target, target_id_record=self.target_id
                    )
                    self.store.write_provider_target_record(self.provider)
                self.assertEqual(len(self.updates), 1 + int(committed))

    async def test_historical_observation_ignores_new_write_qualification(self) -> None:
        payload = self.apply_payload(await self.plan())
        await self.interrupt_acknowledged(payload, "historical")
        self.store.write_product_profile_record(
            self.profile.model_copy(update={"is_active": False, "lanes": ()})
        )
        self.store.write_provider_target_record(
            self.provider.model_copy(
                update={
                    "context": "alias-context",
                    "instance": "prod",
                    "target_category": "changed-category",
                }
            )
        )
        self.store.write_dokploy_target_id_record(
            self.target_id.model_copy(update={"context": "unrelated-incomplete"})
        )
        with patch(
            "control_plane.dokploy.api.fetch_dokploy_target_payload", side_effect=self.fetch
        ) as fetch:
            recovered = await self.post(payload, "historical")
        self.assertEqual(recovered.status_code, 202, recovered.text)
        self.assertEqual(fetch.call_args.kwargs["target_id"], self.target_id.target_id)
        self.assertTrue(recovered.json()["result"]["apply"]["recovered_by_readback"])
        self.assertEqual(len(self.updates), 1)
        self.assertNotEqual((await self.post(self.payload)).status_code, 202)

    async def test_both_env_write_adapters_record_successful_acknowledgement(self) -> None:
        for target_type in ("application", "compose"):
            with self.subTest(target_type=target_type):
                self.target = self.target.model_copy(update={"target_type": target_type})
                self.store.write_dokploy_target_record(self.target)
                self.provider = ProviderTargetRecord.from_dokploy_records(
                    target_record=self.target, target_id_record=self.target_id
                )
                self.store.write_provider_target_record(self.provider)
                self.env = "GOOGLE_ANALYTICS_MEASUREMENT_ID=old-private-value\n"
                payload = self.apply_payload(await self.plan())
                await self.interrupt_acknowledged(payload, target_type)
                recovered = await self.post(payload, target_type)
                self.assertEqual(recovered.status_code, 202, recovered.text)
                self.assertTrue(recovered.json()["result"]["apply"]["update_acknowledged"])
        self.assertEqual(len(self.updates), 2)

    async def test_predispatch_checkpoint_commit_ack_loss_never_dispatches_or_settles_by_values(
        self,
    ) -> None:
        payload = self.apply_payload(await self.plan())
        original = self.store.checkpoint_mutation_provider_effect
        stale: list[Any] = []

        def checkpoint(**kwargs: Any) -> Any:
            stale.append(kwargs["reservation"])
            original(**kwargs)
            raise ValueError("fixture predispatch checkpoint commit acknowledgement lost")

        with patch.object(
            self.store, "checkpoint_mutation_provider_effect", side_effect=checkpoint
        ):
            response = await self.post(payload, "predispatch-ack-lost")
        self.assertEqual(response.json()["error"]["code"], "mutation_reconciliation_required")
        record = self.store.read_idempotency_record(
            scope=idempotency_scope(self.session.identity),
            route_path="/v1/live-target-runtime/apply",
            idempotency_key="predispatch-ack-lost",
        )
        assert record is not None
        self.assertEqual(
            original(reservation=stale[0], effect_phase=record.provider_effect_phase).status,
            "reservation_mismatch",
        )
        self.store.mark_mutation_reconcile_required(
            reservation=record, reconciliation_key=record.reconciliation_key
        )
        self.env = "GOOGLE_ANALYTICS_MEASUREMENT_ID=private-desired-value\n"
        resumed = await self.post(payload, "predispatch-ack-lost")
        self.assertEqual(resumed.json()["error"]["code"], "mutation_reconciliation_required")
        self.assertFalse(self.updates)

    async def test_interrupted_pre_effect_drift_settles_original_key_and_releases_target(
        self,
    ) -> None:
        class InterruptedBeforeEffect(BaseException):
            pass

        payload = self.apply_payload(await self.plan())
        with patch.object(sync.ReviewedRuntimeSync, "apply", side_effect=InterruptedBeforeEffect):
            with self.assertRaises((InterruptedBeforeEffect, BaseExceptionGroup)) as raised:
                await self.post(payload, "interrupted")
            if isinstance(raised.exception, BaseExceptionGroup):
                self.assertIsNotNone(raised.exception.subgroup(InterruptedBeforeEffect))
        with patch(
            "control_plane.dokploy.api.fetch_dokploy_target_payload",
            side_effect=click.ClickException("fixture transient provider read failure"),
        ):
            unavailable = await self.post(payload, "interrupted")
        self.assertEqual(unavailable.status_code, 409, unavailable.text)
        self.assertEqual(unavailable.json()["error"]["code"], "mutation_reconciliation_required")
        self.change_setting()
        stale = await self.post(payload, "interrupted")
        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertEqual(stale.json()["error"]["code"], "stale")
        self.assertFalse(self.updates)
        self.store.write_runtime_environment_record(self.setting)
        replay = await self.post(payload, "interrupted")
        self.assertEqual(replay.json(), stale.json())
        fresh = await self.post(self.apply_payload(await self.plan()), "after-interruption")
        self.assertEqual(fresh.status_code, 202, fresh.text)
        self.assertEqual(len(self.updates), 1)

    async def test_acknowledged_receipt_requires_retained_key_for_verified_success(self) -> None:
        old_key, new_key = Fernet.generate_key().decode(), Fernet.generate_key().decode()
        for change in ("retained", "missing", "replaced"):
            with self.subTest(change=change):
                config = {"active_key_id": "old", "keys": {"old": old_key, "new": new_key}}
                with patch.dict(
                    os.environ,
                    {secrets.LAUNCHPLANE_SECRET_KEYS_JSON_ENV_VAR: json.dumps(config)},
                    clear=True,
                ):
                    self.env = "GOOGLE_ANALYTICS_MEASUREMENT_ID=old-private-value\n"
                    payload = self.apply_payload(await self.plan())
                    await self.interrupt_acknowledged(payload, change)
                    config["active_key_id"] = "new"
                    if change == "missing":
                        config["keys"] = {"new": new_key}
                    elif change == "replaced":
                        config["keys"] = {"old": new_key, "new": new_key}
                    os.environ[secrets.LAUNCHPLANE_SECRET_KEYS_JSON_ENV_VAR] = json.dumps(config)
                    outcome = await self.post(payload, change)
                    self.assertEqual(
                        outcome.status_code, 202 if change == "retained" else 409, outcome.text
                    )
                    if change != "retained":
                        self.assertEqual(outcome.json()["error"]["code"], "runtime_sync_unverified")
                        config["keys"] = {"old": old_key, "new": new_key}
                        os.environ[secrets.LAUNCHPLANE_SECRET_KEYS_JSON_ENV_VAR] = json.dumps(
                            config
                        )
                        self.assertEqual((await self.post(payload, change)).json(), outcome.json())
        self.assertEqual(len(self.updates), 3)

    async def test_unknown_effect_adopts_after_policy_metadata_change(self) -> None:
        secrets.write_secret_value(
            record_store=self.store,
            scope="context_instance",
            integration="runtime_environment",
            name="runtime-secret",
            plaintext_value="secret-private-value",
            binding_key="APP_SECRET",
            context_name=self.context,
            instance_name=self.instance,
            actor="fixture",
            source_label="fixture",
        )
        policy = RuntimeKeySafetyPolicyRecord(
            record_id="supplier-policy",
            source="fixture",
            updated_at=self.profile.updated_at,
            rules=(
                RuntimeSecretSafetyRule(
                    binding_key="APP_SECRET",
                    secret_class="testing",
                    allowed_contexts=(self.context,),
                    allowed_instances=(self.instance,),
                ),
            ),
        )
        self.store.write_runtime_key_safety_policy_record(policy)
        payload = self.apply_payload(await self.plan())
        await self.interrupt_acknowledged(payload, "policy-observation")
        self.store.write_runtime_key_safety_policy_record(
            policy.model_copy(
                update={
                    "rules": (
                        RuntimeSecretSafetyRule(binding_key="APP_SECRET", secret_class="prod_only"),
                    )
                }
            )
        )
        adopted = await self.post(payload, "policy-observation")
        self.assertEqual(adopted.status_code, 202, adopted.text)
        self.assertEqual(len(self.updates), 1)
        revoked = self.store.list_runtime_key_safety_policy_records(status="active", limit=1)[0]
        self.store.write_runtime_key_safety_policy_record(policy)
        before_effect = self.apply_payload(await self.plan())
        with patch.object(
            sync.ReviewedRuntimeSync,
            "apply",
            side_effect=ProviderMutationUnknownError("fixture interruption before checkpoint"),
        ):
            interrupted = await self.post(before_effect, "policy-before-effect")
        self.assertEqual(interrupted.status_code, 409, interrupted.text)
        self.store.write_runtime_key_safety_policy_record(revoked)
        stale = await self.post(before_effect, "policy-before-effect")
        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertEqual(stale.json()["error"]["code"], "stale")
        self.assertEqual(len(self.updates), 1)

    async def test_acknowledged_drift_settles_unverified_without_false_success(self) -> None:
        for change in ("values", "host", "retirement", "live", "missing-record"):
            with self.subTest(change=change):
                self.env = "GOOGLE_ANALYTICS_MEASUREMENT_ID=old-private-value\n"
                payload = self.apply_payload(await self.plan())
                await self.interrupt_acknowledged(payload, change)
                before = len(self.updates)
                if change == "values":
                    self.change_setting()
                elif change == "host":
                    self.host = "https://changed-provider.invalid"
                elif change == "retirement":
                    self.store.write_runtime_environment_record(
                        RuntimeEnvironmentRecord.model_validate(
                            {
                                **self.setting.model_dump(mode="json"),
                                "schema_version": 2,
                                "retired_provider_keys": ["PROVIDER_ONLY"],
                            }
                        )
                    )
                elif change == "missing-record":
                    self.assertEqual(
                        self.store.delete_dokploy_target_record(expected_record=self.target),
                        "deleted",
                    )
                else:
                    self.env = "GOOGLE_ANALYTICS_MEASUREMENT_ID=unexpected-live-value\n"
                reads = self.fetch_count
                outcome = await self.post(payload, change)
                self.assertEqual(outcome.status_code, 409, outcome.text)
                self.assertEqual(outcome.json()["error"]["code"], "runtime_sync_unverified")
                if change in {"values", "host", "retirement", "missing-record"}:
                    self.assertEqual(self.fetch_count, reads)
                self.assertEqual(len(self.updates), before)
                self.host = "https://provider.invalid"
                self.store.write_runtime_environment_record(self.setting)
                self.store.write_dokploy_target_record(self.target)
                self.assertEqual((await self.post(payload, change)).json(), outcome.json())
                fresh = await self.post(self.apply_payload(await self.plan()), f"fresh-{change}")
                self.assertEqual(fresh.status_code, 202, fresh.text)

    async def test_unchanged_apply_has_no_provider_effect_and_still_checks_baseline(self) -> None:
        self.env = "GOOGLE_ANALYTICS_MEASUREMENT_ID=private-desired-value\n"
        review = await self.plan()
        self.assertEqual(review["changed_keys"], [])
        response = await self.post(self.apply_payload(review), "unchanged")
        self.assertEqual(response.status_code, 202, response.text)
        self.assertFalse(self.updates)
        self.assertFalse(response.json()["result"]["apply"]["env_updated"])

    async def test_browser_requires_csrf_named_administrator_and_stable_exclusive_lane(
        self,
    ) -> None:
        headers = _browser_mutation_headers(self.manager, self.session)
        headers.pop("Origin")
        denied = await self.post(self.payload, headers=headers)
        self.assertEqual(denied.status_code, 403, denied.text)
        for role in ("read_only", "admin"):
            restricted = LaunchplaneAuthzPolicy.model_validate(
                {
                    "schema_version": 2,
                    "github_humans": [
                        {
                            "github_ids": [self.session.identity.github_id],
                            "roles": [role],
                            "products": [self.product],
                            "contexts": [self.context],
                            "actions": ["live_target_runtime.plan"],
                        }
                    ],
                }
            )
            denied = await self.post(self.payload, app=self.make_app(restricted))
            self.assertEqual(denied.status_code, 403, denied.text)
        for payload in (
            {**self.payload, "context": "other-context"},
            {**self.payload, "instance": "pr-7"},
        ):
            self.assertNotEqual((await self.post(payload)).status_code, 202)
        self.store.write_product_profile_record(
            self.profile.model_copy(update={"product": "other-product"})
        )
        self.assertEqual((await self.post(self.payload)).status_code, 409)
        self.assertFalse(self.updates)

    async def test_odoo_is_explicitly_unsupported_and_machine_browser_review_is_refused(
        self,
    ) -> None:
        self.store.write_product_profile_record(
            self.profile.model_copy(update={"driver_id": "odoo"})
        )
        for mode in ("dry-run", "apply"):
            payload = {
                **self.payload,
                "mode": mode,
                "browser_review": {} if mode == "dry-run" else {"reviewed_plan_sha256": "0" * 64},
            }
            self.assertEqual((await self.post(payload, "odoo")).status_code, 409)
        self.assertEqual(self.fetch_count, 0)
        self.assertFalse(self.updates)
        response = await self.post(
            self.payload, headers={"Authorization": "Bearer local-operator-token"}
        )
        self.assertEqual(response.status_code, 403, response.text)

    async def test_browser_flags_and_missing_review_or_key_cannot_mutate(self) -> None:
        for changes in (
            {"deploy": True},
            {"no_cache": True},
            {"deploy_timeout_seconds": 10},
            {"separation_only": True},
        ):
            response = await self.post({**self.payload, **changes})
            self.assertEqual(response.status_code, 400, response.text)
        no_review = {key: value for key, value in self.payload.items() if key != "browser_review"}
        self.assertEqual((await self.post(no_review)).status_code, 403)
        review = await self.plan()
        self.assertEqual((await self.post(self.apply_payload(review))).status_code, 400)
        self.assertFalse(self.updates)

    async def test_secret_rotation_and_key_policy_revoke_old_review(self) -> None:
        secrets.write_secret_value(
            record_store=self.store,
            scope="context_instance",
            integration="runtime_environment",
            name="runtime-secret",
            plaintext_value="secret-private-value",
            binding_key="APP_SECRET",
            context_name=self.context,
            instance_name=self.instance,
            actor="fixture",
            source_label="fixture",
        )
        policy = RuntimeKeySafetyPolicyRecord(
            record_id="fixture-policy",
            source="fixture",
            updated_at=self.profile.updated_at,
            rules=(
                RuntimeSecretSafetyRule(
                    binding_key="APP_SECRET",
                    secret_class="testing",
                    allowed_contexts=(self.context,),
                    allowed_instances=(self.instance,),
                ),
            ),
        )
        self.store.write_runtime_key_safety_policy_record(policy)
        review = await self.plan()
        secrets.write_secret_value(
            record_store=self.store,
            scope="context_instance",
            integration="runtime_environment",
            name="runtime-secret",
            plaintext_value="rotated-private-value",
            binding_key="APP_SECRET",
            context_name=self.context,
            instance_name=self.instance,
            actor="fixture",
            source_label="fixture",
        )
        response = await self.post(self.apply_payload(review), "rotated")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertFalse(self.updates)
        review = await self.plan()
        binding = self.store.list_secret_bindings(integration="runtime_environment")[0]
        self.fetch_count = 0

        def revoke_binding(count: int) -> None:
            if count == 3:
                self.store.write_secret_binding(
                    binding.model_copy(update={"declared_secret_class": "prod_only"})
                )

        self.fetch_hook = revoke_binding
        late = await self.post(self.apply_payload(review), "late-binding-revocation")
        self.fetch_hook = None
        self.assertEqual(late.status_code, 409, late.text)
        self.assertEqual(late.json()["error"]["code"], "stale")
        self.assertFalse(self.updates)
        self.store.write_secret_binding(binding)
        review = await self.plan()
        self.store.write_runtime_key_safety_policy_record(
            policy.model_copy(
                update={
                    "record_id": "revoked-policy",
                    "updated_at": (
                        datetime.fromisoformat(policy.updated_at.replace("Z", "+00:00"))
                        + timedelta(seconds=1)
                    ).isoformat(),
                    "rules": (
                        RuntimeSecretSafetyRule(
                            binding_key="APP_SECRET",
                            secret_class="prod_only",
                            allowed_contexts=(self.context,),
                            allowed_instances=("prod",),
                        ),
                    ),
                }
            )
        )
        response = await self.post(self.apply_payload(review), "policy-drift")
        self.assertNotEqual(response.status_code, 202, response.text)
        self.assertFalse(self.updates)

    async def test_availability_is_admin_and_driver_qualified_separately_from_config_edits(
        self,
    ) -> None:
        status = sync.runtime_sync_availability(
            self.store,
            control_plane_root=self.root,
            administrator=True,
            product=self.product,
            context=self.context,
            instance=self.instance,
        )
        self.assertTrue(status.plan.available)
        self.assertTrue(status.apply.available)
        readback = await request(
            self.app,
            "GET",
            f"/v1/products/{self.product}/environments/{self.instance}/config-status",
            headers={"Cookie": self.manager.session_cookie_header(self.session)},
        )
        self.assertEqual(readback.status_code, 200, readback.text)
        self.assertTrue(readback.json()["config_status"]["runtime_sync"]["plan"]["available"])
        self.assertFalse(
            sync.runtime_sync_availability(
                self.store,
                control_plane_root=self.root,
                administrator=False,
                product=self.product,
                context=self.context,
                instance=self.instance,
            ).plan.available
        )
        self.store.write_product_profile_record(
            self.profile.model_copy(update={"driver_id": "odoo"})
        )
        status = sync.runtime_sync_availability(
            self.store,
            control_plane_root=self.root,
            administrator=True,
            product=self.product,
            context=self.context,
            instance=self.instance,
        )
        self.assertFalse(status.plan.available)
        self.assertTrue(status.plan.reason)

    async def test_late_provider_destination_change_refuses_changed_and_unchanged_writes(
        self,
    ) -> None:
        for unchanged in (False, True):
            with self.subTest(unchanged=unchanged):
                self.host = "https://provider.invalid"
                self.env = (
                    "GOOGLE_ANALYTICS_MEASUREMENT_ID="
                    + ("private-desired-value" if unchanged else "old-private-value")
                    + "\n"
                )
                review = await self.plan()
                self.fetch_count = 0

                def late(count: int) -> None:
                    if count == 3:
                        self.host = "https://changed-provider.invalid"

                self.fetch_hook = late
                response = await self.post(self.apply_payload(review), f"late-host-{unchanged}")
                self.fetch_hook = None
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(response.json()["error"]["code"], "stale")
                self.assertFalse(self.updates)

    async def test_provider_read_failure_is_redacted_and_cannot_write(self) -> None:
        with patch(
            "control_plane.dokploy.api.fetch_dokploy_target_payload",
            side_effect=click.ClickException("private-desired-value fixture-token"),
        ):
            response = await self.post(self.payload)
        self.assertEqual(response.status_code, 503, response.text)
        self.assert_redacted(response)
        self.assertFalse(self.updates)

    async def test_failed_stale_receipt_recovers_terminal_refusal_without_false_success(
        self,
    ) -> None:
        payload = self.apply_payload(await self.plan())
        self.change_setting()
        with patch.object(
            self.store,
            "complete_mutation_reservation",
            side_effect=ValueError("fixture receipt storage unavailable"),
        ):
            response = await self.post(payload, "stale-storage")
        self.assertEqual(response.status_code, 503, response.text)
        record = self.store.read_idempotency_record(
            scope=idempotency_scope(self.session.identity),
            route_path="/v1/live-target-runtime/apply",
            idempotency_key="stale-storage",
        )
        assert record is not None
        self.assertFalse(record.provider_effect_started_at)
        transition = self.store.mark_mutation_reconcile_required(
            reservation=record, reconciliation_key=record.reconciliation_key
        )
        self.assertIsNotNone(transition.record)
        self.env = "GOOGLE_ANALYTICS_MEASUREMENT_ID=changed-private-value\n"
        recovered = await self.post(payload, "stale-storage")
        self.assertEqual(recovered.status_code, 409, recovered.text)
        self.assertEqual(recovered.json()["error"]["code"], "stale")
        receipt = self.store.read_idempotency_record(
            scope=idempotency_scope(self.session.identity),
            route_path="/v1/live-target-runtime/apply",
            idempotency_key="stale-storage",
        )
        assert receipt is not None
        self.assertEqual(receipt.response_status_code, 409)
        self.assertEqual(receipt.state, "completed")
        self.assertFalse(self.updates)
        replay = await self.post(payload, "stale-storage")
        self.assertEqual(replay.status_code, 409, replay.text)
        self.assertEqual(replay.json(), recovered.json())

    async def test_other_provider_namespace_does_not_claim_this_physical_target(self) -> None:
        other = self.provider.model_copy(
            update={
                "context": "other-context",
                "instance": "other-lane",
                "provider_id": "other-provider",
            }
        )
        self.store.write_provider_target_record(other)
        response = await self.post(self.payload)
        self.assertEqual(response.status_code, 202, response.text)
        self.assertFalse(self.updates)

    async def test_machine_reads_remain_outside_guard_and_browser_refresh_is_inside(self) -> None:
        for browser in (False, True):
            with self.subTest(browser=browser):
                self.env = "GOOGLE_ANALYTICS_MEASUREMENT_ID=old-private-value\n"
                prepared = runtime.prepare_live_target_runtime_environment(
                    control_plane_root=self.root,
                    database_url=self.store.database_url,
                    product_name=self.product,
                    context_name=self.context,
                    instance_name=self.instance,
                )
                inside = False
                reads: list[bool] = []
                comparisons: list[bool] = []
                writes: list[bool] = []

                @contextmanager
                def guard() -> Iterator[None]:
                    nonlocal inside
                    inside = True
                    try:
                        yield
                    finally:
                        inside = False

                def fetch(**kwargs: Any) -> dict[str, str]:
                    reads.append(inside)
                    return self.fetch(**kwargs)

                def update(**kwargs: Any) -> None:
                    writes.append(inside)
                    self.update(**kwargs)

                with (
                    patch(
                        "control_plane.dokploy.api.fetch_dokploy_target_payload", side_effect=fetch
                    ),
                    patch(
                        "control_plane.dokploy.api.update_dokploy_target_env", side_effect=update
                    ),
                ):
                    options: dict[str, Any] = dict(
                        apply_changes=True,
                        deploy=False,
                        no_cache=False,
                        deploy_timeout_seconds=None,
                        deploy_trigger=runtime.trigger_and_wait_for_dokploy_target_deploy,
                        provider_mutation_guard=guard,
                    )
                    if browser:
                        runtime.apply_prepared_live_target_runtime_environment(
                            prepared,
                            **options,
                            before_provider_env_write=lambda env: comparisons.append(inside),
                        )
                        self.assertTrue(reads[0])
                        self.assertFalse(reads[-1])
                        self.assertTrue(comparisons)
                        self.assertTrue(all(comparisons))
                    else:
                        runtime.apply_live_target_runtime_environment(
                            control_plane_root=self.root,
                            database_url=self.store.database_url,
                            product_name=self.product,
                            context_name=self.context,
                            instance_name=self.instance,
                            **options,
                        )
                        self.assertFalse(any(reads))
                self.assertEqual(writes, [True])
