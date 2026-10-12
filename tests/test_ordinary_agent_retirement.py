from __future__ import annotations

from tempfile import TemporaryDirectory
from pathlib import Path
import unittest
from unittest.mock import patch

from control_plane.privileged_operation_service import create_typed_privileged_operation_plan
from control_plane.service_auth import LaunchplaneAuthzPolicy
from control_plane.storage.filesystem import FilesystemRecordStore
from tests.support.http import lifespan_client
from tests.test_authz_administration_read import _database_app
from tests import test_privileged_operation as historical
from tests import test_privileged_operation_http as http_fixture
from tests.test_privileged_operation_record_read import _reader_policy, _HEADERS


class OrdinaryAgentRetirementTests(unittest.IsolatedAsyncioTestCase):
    async def test_removed_ingress_cannot_reach_storage(self) -> None:
        with _database_app(_reader_policy()) as (store, _, app):
            with patch.object(store, "claim_ordinary_agent_credential", side_effect=AssertionError):
                async with lifespan_client(app) as client:
                    for method, path in (
                        ("POST", "/v1/agent/ordinary-agent-enrollments"),
                        ("POST", "/v1/agent/ordinary-agent-enrollments/missing/claim"),
                        ("POST", "/v1/agent/ordinary-agent-session-proposals"),
                        ("POST", "/v1/agent/ordinary-agent-jobs"),
                        ("GET", "/v1/ordinary-agent-operations/agent_test/missing"),
                        ("POST", "/v1/ordinary-agent-connections/agent_test/disconnect"),
                        (
                            "GET",
                            "/v1/privileged-operations/ordinary-agent-delivery-activation/options",
                        ),
                        (
                            "POST",
                            "/v1/privileged-operations/ordinary-agent-delivery-activation/plans",
                        ),
                        (
                            "POST",
                            "/v1/privileged-operations/authorization-candidates/ordinary-agent-delivery/prepare",
                        ),
                        ("GET", "/v1/privileged-operations/merge-train-targets/inputs"),
                        ("POST", "/v1/privileged-operations/merge-train-targets/prepare"),
                    ):
                        with self.subTest(path=path):
                            response = await client.request(method, path, headers=_HEADERS)
                            self.assertEqual(response.status_code, 404, response.text)
            self.assertEqual(store.list_privileged_operation_records(), ())

    async def test_schema_three_and_historical_activation_reads_survive(self) -> None:
        payload = _reader_policy().model_dump(mode="json")
        payload["schema_version"] = 3
        policy = LaunchplaneAuthzPolicy.model_validate(payload)
        record = historical.PrivilegedOperationContractTests()._ordinary_agent_activation_record()
        event = historical._planned_event(record)
        self.assertEqual(
            LaunchplaneAuthzPolicy.model_validate(policy.model_dump()).schema_version,
            payload["schema_version"],
        )
        with _database_app(_reader_policy()) as (store, _, app):
            store.write_privileged_operation_plan(record, event)
            before_events = store.list_privileged_operation_event_records(
                operation_id=record.operation_id
            )
            with patch.object(store, "write_privileged_operation_plan", side_effect=AssertionError):
                for source_event_id in ("retired-new-plan", record.source_event_id):
                    with self.assertRaisesRegex(ValueError, "retired"):
                        create_typed_privileged_operation_plan(
                            record_store=store,
                            descriptor_id=record.descriptor_id,
                            actor=record.requested_by,
                            source_kind="browser_api",
                            source_event_id=source_event_id,
                            request=record.request,
                        )
            async with lifespan_client(app) as client:
                path = f"/v1/privileged-operations/plans/{record.operation_id}"
                for suffix in ("", "/review"):
                    response = await client.get(path + suffix, headers=_HEADERS)
                    self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(
                    (await client.get(path, headers=_HEADERS)).json()["record"],
                    record.model_dump(mode="json"),
                )
            self.assertEqual(store.read_privileged_operation_record(record.operation_id), record)
            self.assertEqual(
                store.list_privileged_operation_event_records(operation_id=record.operation_id),
                before_events,
            )

    async def test_generic_browser_transitions_cannot_reactivate_retired_history(self) -> None:
        record = historical.PrivilegedOperationContractTests()._ordinary_agent_activation_record()
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(Path(directory))
            store.write_privileged_operation_plan(record, historical._planned_event(record))
            app = http_fixture.PrivilegedOperationHttpTests()._app(
                store=store, policy=http_fixture._policy()
            )
            async with lifespan_client(app) as client:
                for action in ("approve", "revoke", "cancel"):
                    response = await client.post(
                        f"/v1/privileged-operations/plans/{record.operation_id}/{action}",
                        json={
                            "source_event_id": f"retired-{action}",
                            "reason": "Historical operation",
                        },
                    )
                    self.assertEqual(response.status_code, 410, response.text)
            self.assertEqual(store.read_privileged_operation_record(record.operation_id), record)
