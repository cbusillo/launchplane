from __future__ import annotations

from contextlib import ExitStack
from tempfile import TemporaryDirectory
from pathlib import Path
import unittest
from unittest.mock import patch

from control_plane.privileged_operation_service import (
    create_typed_privileged_operation_plan,
    approve_privileged_operation,
)
from control_plane.privileged_operation_worker import execute_approved_privileged_operations_once
from control_plane.contracts.ordinary_agent_activation import (
    OrdinaryAgentDeliveryActivationExecutionEvidence,
)
from control_plane.contracts.privileged_operation import (
    ManagedMergeTrainPolicyImportProposalInput,
    ManagedMergeTrainPolicyPreparationContext,
    OrdinaryAgentMergeTrainTargetIntent,
)
from tests import test_ordinary_agent_activation_worker as worker_fixture
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
                        ("POST", "/v1/agent/ordinary-agent-session-proposals/missing/cancel"),
                        ("GET", "/v1/ordinary-agent-jobs/agent_test/missing"),
                        ("POST", "/v1/ordinary-agent-operations/agent_test/missing/approve"),
                        ("POST", "/v1/ordinary-agent-operations/agent_test/missing/cancel"),
                        ("POST", "/v1/ordinary-agent-sessions/agent_test/missing/revoke"),
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
        with ExitStack() as stack:
            # Seed legacy persisted state; the real schema-3 write fence remains disabled.
            with patch(
                "control_plane.storage.postgres.require_authz_policy_schema_write_activated"
            ):
                store, _, app = stack.enter_context(_database_app(policy))
            store.write_privileged_operation_plan(record, event)
            before_events = store.list_privileged_operation_event_records(
                operation_id=record.operation_id
            )
            with patch.object(store, "write_privileged_operation_plan", side_effect=AssertionError):
                for source_event_id in ("retired-new-plan", record.source_event_id):
                    with self.assertRaises(ValueError):
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
                    if suffix == "/review":
                        self.assertFalse(response.json()["review"]["can_approve"])
                        self.assertFalse(response.json()["review"]["can_revoke"])
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

    async def test_retired_worker_records_terminal_refusal_before_authorization_or_effect(
        self,
    ) -> None:
        record = historical.PrivilegedOperationContractTests()._ordinary_agent_activation_record()
        with _database_app(_reader_policy()) as (store, policy_record, _):
            store.write_privileged_operation_plan(record, historical._planned_event(record))
            approve_privileged_operation(
                record_store=store,
                operation_id=record.operation_id,
                approval=worker_fixture._approval(record, policy_record=policy_record),
                source_event_id="historical-approval",
            )
            with patch(
                "control_plane.privileged_operation_worker._authorize_approved_operation",
                side_effect=AssertionError,
            ):
                results = execute_approved_privileged_operations_once(record_store=store)
            self.assertEqual(len(results), 1)
            outcome = store.read_privileged_operation_record(record.operation_id)
            self.assertEqual(outcome.status, "execution_failed")
            assert isinstance(outcome.execution, OrdinaryAgentDeliveryActivationExecutionEvidence)
            self.assertEqual(outcome.execution.failure_code, "privileged_operation_retired")
            self.assertFalse(outcome.execution.changed)
            self.assertFalse(outcome.execution.reconciliation_required)

    async def test_generic_policy_ingress_rejects_historical_server_preparation_context(
        self,
    ) -> None:
        record = historical.PrivilegedOperationContractTests()._merge_train_policy_import_record()
        request = record.request
        assert isinstance(request, ManagedMergeTrainPolicyImportProposalInput)
        target = request.record.policy.policies[0]
        values = target.model_dump(mode="json")
        intent = OrdinaryAgentMergeTrainTargetIntent.model_validate(
            {
                "repository_id": "12345",
                **{
                    k: v
                    for k, v in values.items()
                    if k in OrdinaryAgentMergeTrainTargetIntent.model_fields
                },
            }
        )
        context = ManagedMergeTrainPolicyPreparationContext(
            intent=intent,
            expected_active_record_id=request.record.record_id,
            expected_active_policy_sha256=request.record.policy_sha256,
            expected_active_updated_at=request.record.updated_at,
            target_policy_key=target.policy_key,
        )
        prepared = request.model_copy(update={"preparation_context": context})
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(Path(directory))
            app = http_fixture.PrivilegedOperationHttpTests()._app(
                store=store, policy=http_fixture._policy()
            )
            async with lifespan_client(app) as client:
                for path in (
                    "/v1/privileged-operations/plans",
                    "/v1/agent/privileged-operations/plans",
                ):
                    response = await client.post(
                        path,
                        json={
                            "descriptor_id": record.descriptor_id,
                            "source_event_id": "forged-server-context",
                            "request": prepared.model_dump(mode="json"),
                        },
                    )
                    self.assertEqual(response.status_code, 422, response.text)
            self.assertEqual(store.list_privileged_operation_records(), ())

    async def test_historical_activation_read_guards_still_reject_broad_or_inexact_rules(
        self,
    ) -> None:
        record = historical.PrivilegedOperationContractTests()._ordinary_agent_activation_record()
        good_rule = {
            "managed_set_id": "history.activation",
            "managed_rule_id": "reader",
            "github_ids": [123],
            "roles": ["admin"],
            "products": ["launchplane"],
            "contexts": ["launchplane"],
            "actions": ["ordinary_agent_delivery_activation.read"],
        }
        cases = (
            [good_rule],
            [{**good_rule, "actions": []}],
            [{**good_rule, "products": ["another-product"]}],
            [{**good_rule, "github_ids": [456]}],
            [good_rule, {**good_rule, "managed_rule_id": "overlapping-reader"}],
        )
        for rules in cases:
            payload = http_fixture._policy().model_dump(mode="json")
            payload["github_humans"].extend(rules)
            policy = LaunchplaneAuthzPolicy.model_validate(payload)
            with TemporaryDirectory() as directory:
                store = FilesystemRecordStore(Path(directory))
                store.write_privileged_operation_plan(record, historical._planned_event(record))
                app = http_fixture.PrivilegedOperationHttpTests()._app(store=store, policy=policy)
                async with lifespan_client(app) as client:
                    response = await client.get(
                        f"/v1/privileged-operations/plans/{record.operation_id}/review"
                    )
                    self.assertEqual(
                        response.status_code, 200 if rules == [good_rule] else 403, response.text
                    )
                    with patch(
                        "control_plane.http_routes.privileged_operations.require_single_managed_rule_identity",
                        side_effect=AssertionError,
                    ):
                        summary = await client.get(
                            f"/v1/agent/privileged-operations/plans/{record.operation_id}"
                        )
                    self.assertEqual(summary.status_code, 404, summary.text)
