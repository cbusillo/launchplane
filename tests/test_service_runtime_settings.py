import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
from unittest.mock import patch

from fastapi import FastAPI
from pydantic import ValidationError

from control_plane import secrets
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.dokploy.memory_capacity import (
    PREVIEW_MEMORY_FLOORS_SETTING,
    PreviewMemoryCapacityRefused,
    require_preview_host_memory_capacity,
)
from control_plane.http_app import LaunchplaneErrorResponse, _launchplane_http_error
from control_plane.http_routes.service_runtime_settings import (
    register_service_runtime_settings_routes,
)
from control_plane.http_routes.support import ApiRouteRegistrar, ReadRouteDependencies
from control_plane.service_auth import LaunchplaneAuthzPolicy, LocalOperatorIdentity
from control_plane.service_runtime_settings import (
    SERVICE_RUNTIME_SETTINGS_ROUTE,
    ServiceRuntimeSettingRequest,
    apply_service_runtime_setting,
)
from control_plane.storage.postgres import PostgresRecordStore
from tests.support.http import request as http_request
from tests.support.stores import sqlite_database_url

FLOORS = json.dumps({"synthetic-execution-host": 5368709120})
EXISTING_ENV: dict[str, str | int | float | bool] = {
    "LAUNCHPLANE_ADVISORY_GITHUB_APP_ID": "77",
    "OTHER_SERVICE_SETTING": "kept",
}


def _store(directory: str) -> PostgresRecordStore:
    store = PostgresRecordStore(database_url=sqlite_database_url(Path(directory) / "state.sqlite3"))
    store.ensure_schema()
    store.write_runtime_environment_record(
        RuntimeEnvironmentRecord(
            scope="context",
            context="launchplane",
            env=EXISTING_ENV,
            updated_at="2026-10-24T00:00:00Z",
            source_label="test",
        )
    )
    return store


def _service_env(store: PostgresRecordStore) -> dict[str, object]:
    (record,) = store.list_runtime_environment_records(scope="context", context_name="launchplane")
    return dict(record.env)


class ServiceRuntimeSettingTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        environment = patch.dict(
            os.environ, {secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key"}
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.store = _store(directory.name)
        self.addCleanup(self.store.close)
        self.request = ServiceRuntimeSettingRequest(
            key=PREVIEW_MEMORY_FLOORS_SETTING, value=FLOORS, reason="Activate the preview floor."
        )

    def call(self, setting: ServiceRuntimeSettingRequest, key: str = "") -> dict[str, object]:
        return apply_service_runtime_setting(
            store=self.store,
            request=setting,
            actor="local-operator|director|bootstrap",
            trace_id="test-trace",
            idempotency_key=key,
        )

    def apply_request(self, digest: object) -> ServiceRuntimeSettingRequest:
        return self.request.model_copy(update={"mode": "apply", "expected_plan_digest": digest})

    def test_reviewed_apply_activates_the_floor_and_keeps_other_settings(self) -> None:
        preview = self.call(self.request)
        self.assertEqual(_service_env(self.store), EXISTING_ENV)
        result = self.call(self.apply_request(preview["plan_digest"]), "apply-once")
        self.assertEqual(
            _service_env(self.store), {**EXISTING_ENV, PREVIEW_MEMORY_FLOORS_SETTING: FLOORS}
        )
        self.assertNotIn(FLOORS, json.dumps(result))
        with (
            patch(
                "control_plane.dokploy.memory_capacity.read_host_memory_headroom",
                return_value=5368709119,
            ),
            self.assertRaises(PreviewMemoryCapacityRefused),
        ):
            require_preview_host_memory_capacity(
                host="https://provider.invalid",
                token="unused",
                server_id="synthetic-execution-host",
                record_store=self.store,
            )

    def test_replay_returns_the_original_and_a_changed_request_under_its_key_conflicts(
        self,
    ) -> None:
        preview = self.call(self.request)
        apply = self.apply_request(preview["plan_digest"])
        result = self.call(apply, "apply-once")
        written = self.store.list_runtime_environment_records()
        self.assertEqual(self.call(apply, "apply-once"), result)
        with self.assertRaisesRegex(ValueError, "idempotency conflict"):
            self.call(apply.model_copy(update={"reason": "Different intent."}), "apply-once")
        self.assertEqual(self.store.list_runtime_environment_records(), written)

    def test_record_change_after_review_writes_nothing(self) -> None:
        preview = self.call(self.request)
        (record,) = self.store.list_runtime_environment_records()
        self.store.write_runtime_environment_record(
            record.model_copy(update={"env": {**record.env, "OTHER_SERVICE_SETTING": "changed"}})
        )
        before = self.store.list_runtime_environment_records()
        with self.assertRaisesRegex(ValueError, "changed since"):
            self.call(self.apply_request(preview["plan_digest"]), "stale")
        self.assertEqual(self.store.list_runtime_environment_records(), before)

    def test_apply_requires_an_idempotency_key(self) -> None:
        preview = self.call(self.request)
        with self.assertRaises(ValueError):
            self.call(self.apply_request(preview["plan_digest"]))
        self.assertEqual(_service_env(self.store), EXISTING_ENV)

    def test_undeclared_secret_shaped_or_invalid_settings_are_refused(self) -> None:
        for key, value in (
            ("LAUNCHPLANE_UNDECLARED_SETTING", "1"),
            ("LAUNCHPLANE_GITHUB_TOKEN", "fixture-token"),
            (PREVIEW_MEMORY_FLOORS_SETTING, json.dumps({"synthetic-execution-host": 0})),
            (PREVIEW_MEMORY_FLOORS_SETTING, "not json"),
            ("LAUNCHPLANE_DISCUSSION_REMINDERS", json.dumps({"schedules": []})),
        ):
            with self.subTest(key=key, value=value), self.assertRaises(ValidationError):
                ServiceRuntimeSettingRequest(key=key, value=value, reason="Try it.")
        with self.assertRaises(ValidationError):
            ServiceRuntimeSettingRequest(
                key=PREVIEW_MEMORY_FLOORS_SETTING, value=FLOORS, reason="Apply.", mode="apply"
            )


class ServiceRuntimeSettingHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_director_agent_sets_a_setting_and_an_ungranted_agent_is_refused(self) -> None:
        with (
            TemporaryDirectory() as directory,
            patch.dict(
                os.environ, {secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key"}
            ),
        ):
            store = _store(directory)
            self.addCleanup(store.close)
            caller = [LocalOperatorIdentity("director", "existing-bootstrap", director_agent=True)]
            app = FastAPI()
            register_service_runtime_settings_routes(
                cast(ApiRouteRegistrar, app),
                dependencies=ReadRouteDependencies(
                    read_identity=lambda: caller[0],
                    get_record_store=lambda: store,
                    next_trace_id=lambda: "trace-test",
                    authorization_allows=LaunchplaneAuthzPolicy(schema_version=3).allows,
                    http_error=_launchplane_http_error,
                    error_response_model=LaunchplaneErrorResponse,
                ),
                mutation_identity=lambda: caller[0],
            )
            payload = {
                "key": PREVIEW_MEMORY_FLOORS_SETTING,
                "value": FLOORS,
                "reason": "Activate the preview floor.",
            }
            preview = await http_request(
                app, "POST", SERVICE_RUNTIME_SETTINGS_ROUTE, payload=payload
            )
            self.assertEqual(preview.status_code, 200, preview.text)
            applied = await http_request(
                app,
                "POST",
                SERVICE_RUNTIME_SETTINGS_ROUTE,
                headers={"Idempotency-Key": "set-floor"},
                payload={
                    **payload,
                    "mode": "apply",
                    "expected_plan_digest": preview.json()["plan_digest"],
                },
            )
            self.assertEqual(applied.status_code, 200, applied.text)
            self.assertEqual(_service_env(store)[PREVIEW_MEMORY_FLOORS_SETTING], FLOORS)
            caller[0] = LocalOperatorIdentity("other-agent", "other-token")
            denied = await http_request(
                app, "POST", SERVICE_RUNTIME_SETTINGS_ROUTE, payload=payload
            )
            self.assertEqual(denied.status_code, 403, denied.text)
