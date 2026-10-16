from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from typing import Any

from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import BearerIdentityConfig, LaunchplaneAuthzPolicy
from control_plane.service_human_auth import HumanSessionManager, InMemoryHumanSessionStore
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.privileged_operation_worker import execute_approved_privileged_operations_once
from tests.http_app_test_support import (
    _browser_mutation_headers,
    _github_human_identity,
    _github_human_product_config_policy,
    _github_oauth_config,
    _RejectingVerifier,
)
from tests.support.http import request as http_request, get as http_get
from tests.test_privileged_operation_http import _policy_record
from tests.test_production_backup_authority import _dry_run_envelope, _profile


class BackupPolicyProposalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = PostgresRecordStore(
            database_url=f"sqlite+pysqlite:///{Path(self.directory.name) / 'records.sqlite3'}"
        )
        self.store.ensure_schema()
        self.addCleanup(self.store.close)
        self.store.write_product_profile_record(_profile())
        self.sessions = HumanSessionManager(
            config=_github_oauth_config(), session_store=InMemoryHumanSessionStore()
        )
        self.session = self.sessions.issue(_github_human_identity())
        policy = _github_human_product_config_policy(
            actions=("production_backup_authority.read", "production_backup_authority.write"),
            product="example-product",
            context="example-product",
            instances=("prod",),
            schema_version=2,
        ).model_dump(mode="json")
        policy["github_humans"][0].update(
            {
                "managed_set_id": "test.director-backups",
                "managed_rule_id": "director-backup-admin",
                "github_ids": [_github_human_identity().github_id],
            }
        )
        policy["local_operators"] = [
            {
                "managed_set_id": "test.backup-planning",
                "managed_rule_id": "planner",
                "subjects": ["backup-planner"],
                "token_labels": ["backup-planner"],
                "products": ["example-product"],
                "contexts": ["example-product"],
                "instances": ["prod"],
                "actions": [
                    "production_backup_authority.plan",
                    "production_backup_authority.read",
                    "production_backup_authority.write",
                ],
            }
        ]
        self.policy = LaunchplaneAuthzPolicy.model_validate(policy)
        self.store.seed_authz_policy_if_absent(_policy_record(self.policy))
        self.app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=self.policy,
            record_store_factory=lambda: self.store,
            human_session_manager=self.sessions,
            bearer_identity_config=BearerIdentityConfig(
                local_operator_token="test-planner",
                local_operator_subject="backup-planner",
                local_operator_token_label="backup-planner",
            ),
        )
        self.agent_headers = {"Authorization": "Bearer test-planner"}
        self.payload: dict[str, Any] = {
            "descriptor_id": "production-backup-policy",
            "source_event_id": "backup-proposal-1",
            "request": {
                "authority": _dry_run_envelope().model_dump(mode="json"),
                "reason": "Review protection.",
            },
        }

    async def propose(self) -> str:
        result = await http_request(
            self.app,
            "POST",
            "/v1/agent/privileged-operations/plans",
            headers=self.agent_headers,
            payload=self.payload,
        )
        self.assertEqual(result.status_code, 200, result.text)
        self.assertNotIn("proxmox.example.invalid", result.text)
        return str(result.json()["summary"]["operation_id"])

    async def decide(self, operation_id: str, decision: str = "approve") -> Any:
        return await http_request(
            self.app,
            "POST",
            f"/v1/production-backup-authority/proposals/{operation_id}/decision",
            headers=_browser_mutation_headers(self.sessions, self.session),
            payload={"decision": decision, "source_event_id": f"ui:{decision}:{operation_id}"},
        )

    async def test_inert_durable_redacted_then_single_human_click_executes(self) -> None:
        operation_id = await self.propose()
        self.assertEqual(self.store.list_production_backup_policy_records(), ())
        self.assertEqual(execute_approved_privileged_operations_once(record_store=self.store), ())
        self.assertEqual(operation_id, await self.propose())
        # Separate sessions/devices see the same service-held proposal.
        second = self.sessions.issue(_github_human_identity())
        for session in (self.session, second):
            response = await http_get(
                self.app,
                "/v1/production-backup-authority/proposals?product=example-product&context=example-product&instance=prod",
                headers=_browser_mutation_headers(self.sessions, session),
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["proposals"][0]["operation_id"], operation_id)
            for private in (
                "proxmox.example.invalid",
                "backup-operator",
                "pbs-production",
                "guest_id",
                "destination",
                "reason",
            ):
                self.assertNotIn(private, response.text)
        approved = await self.decide(operation_id)
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertEqual(approved.json()["proposal"]["status"], "approved")
        result = execute_approved_privileged_operations_once(record_store=self.store)
        self.assertEqual(result[0].status, "executed", result)
        self.assertEqual(self.store.list_production_backup_policy_records()[0].policy_revision, 1)
        self.assertEqual(execute_approved_privileged_operations_once(record_store=self.store), ())

    async def test_proposer_cannot_decide_or_directly_apply_even_with_write_grant(self) -> None:
        operation_id = await self.propose()
        for route in (
            f"/v1/production-backup-authority/proposals/{operation_id}/decision",
            f"/v1/privileged-operations/plans/{operation_id}/approve",
        ):
            response = await http_request(
                self.app,
                "POST",
                route,
                headers=self.agent_headers,
                payload={
                    "decision": "approve",
                    "source_event_id": "agent-approve",
                    "reason": "try",
                },
            )
            self.assertEqual(response.status_code, 403, response.text)
        record = self.store.read_privileged_operation_record(operation_id)
        direct = await http_request(
            self.app,
            "POST",
            "/v1/production-backup-authority/apply",
            headers=self.agent_headers | {"Idempotency-Key": "agent-direct-apply"},
            payload=_dry_run_envelope().model_dump(mode="json")
            | {"mode": "apply", "reviewed_authority_digest": record.evidence.plan_digest},
        )
        self.assertEqual(direct.status_code, 403, direct.text)
        self.assertEqual(self.store.list_production_backup_policy_records(), ())

    async def test_decline_is_durable_and_never_executed(self) -> None:
        operation_id = await self.propose()
        response = await self.decide(operation_id, "decline")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["proposal"]["status"], "cancelled")
        self.assertEqual((await self.decide(operation_id, "decline")).status_code, 200)
        self.assertEqual((await self.decide(operation_id)).status_code, 409)
        self.assertEqual(execute_approved_privileged_operations_once(record_store=self.store), ())

    async def test_stale_authority_refuses_before_approval(self) -> None:
        operation_id = await self.propose()
        self.store.write_production_backup_target_record(_dry_run_envelope().targets[0])
        response = await self.decide(operation_id)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(
            self.store.read_privileged_operation_record(operation_id).status, "planned"
        )

    async def test_worker_rechecks_authority_after_human_approval(self) -> None:
        operation_id = await self.propose()
        self.assertEqual((await self.decide(operation_id)).status_code, 200)
        self.store.write_production_backup_target_record(_dry_run_envelope().targets[0])
        result = execute_approved_privileged_operations_once(record_store=self.store)
        self.assertEqual(result[0].status, "execution_failed")
        self.assertEqual(self.store.list_production_backup_policy_records(), ())

    async def test_browser_raw_record_is_unavailable_and_csrf_is_required(self) -> None:
        operation_id = await self.propose()
        raw = await http_get(
            self.app,
            f"/v1/privileged-operations/plans/{operation_id}",
            headers=_browser_mutation_headers(self.sessions, self.session),
        )
        self.assertIn(raw.status_code, (403, 404))
        self.assertNotIn("proxmox.example.invalid", raw.text)
        no_csrf = await http_request(
            self.app,
            "POST",
            f"/v1/production-backup-authority/proposals/{operation_id}/decision",
            headers={
                key: value
                for key, value in _browser_mutation_headers(self.sessions, self.session).items()
                if key.lower() != "x-csrf-token"
            },
            payload={"decision": "approve", "source_event_id": "missing-csrf"},
        )
        self.assertEqual(no_csrf.status_code, 403, no_csrf.text)
        self.assertEqual(
            self.store.read_privileged_operation_record(operation_id).status, "planned"
        )

    async def test_changed_source_event_payload_and_wrong_lane_are_refused(self) -> None:
        await self.propose()
        self.payload["request"]["reason"] = "Changed request"
        conflict = await http_request(
            self.app,
            "POST",
            "/v1/agent/privileged-operations/plans",
            headers=self.agent_headers,
            payload=self.payload,
        )
        self.assertEqual(conflict.status_code, 409, conflict.text)
        foreign = await http_get(
            self.app,
            "/v1/production-backup-authority/proposals?product=other-product&context=other-product&instance=prod",
            headers=_browser_mutation_headers(self.sessions, self.session),
        )
        self.assertEqual(foreign.status_code, 403, foreign.text)

    async def test_wrong_promotion_action_is_never_reviewable(self) -> None:
        self.payload["request"]["authority"]["policy"]["promotion_action"] = (
            "unknown_promotion.execute"
        )
        self.payload["request"]["authority"]["policy"]["policy_id"] = ""
        self.payload["request"]["authority"]["policy"]["record_id"] = ""
        self.payload["request"]["authority"]["policy"]["policy_digest"] = ""
        response = await http_request(
            self.app,
            "POST",
            "/v1/agent/privileged-operations/plans",
            headers=self.agent_headers,
            payload=self.payload,
        )
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(self.store.list_privileged_operation_records(), ())

    async def test_worker_rechecks_human_authority_before_any_write(self) -> None:
        from unittest.mock import patch

        operation_id = await self.propose()
        self.assertEqual((await self.decide(operation_id)).status_code, 200)
        removed = _policy_record(LaunchplaneAuthzPolicy(schema_version=2))
        with patch(
            "control_plane.privileged_operation_worker.read_active_authz_policy_record",
            return_value=removed,
        ):
            result = execute_approved_privileged_operations_once(record_store=self.store)
        self.assertEqual(result[0].status, "execution_failed")
        self.assertEqual(self.store.list_production_backup_policy_records(), ())

    async def test_existing_shared_targets_can_be_referenced_without_revision(self) -> None:
        draft = _dry_run_envelope()
        for target in draft.targets:
            self.store.write_production_backup_target_record(target)
        self.payload["request"]["authority"]["targets"] = []
        operation_id = await self.propose()
        self.assertEqual((await self.decide(operation_id)).status_code, 200)
        result = execute_approved_privileged_operations_once(record_store=self.store)
        self.assertEqual(result[0].status, "executed", result)
        self.assertCountEqual(self.store.list_production_backup_target_records(), draft.targets)

    async def test_committed_backup_write_has_one_recoverable_receipt(self) -> None:
        from control_plane.privileged_operation_worker import _execute_backup_policy
        from unittest.mock import patch

        operation_id = await self.propose()
        self.assertEqual((await self.decide(operation_id)).status_code, 200)
        compare = self.store.compare_and_apply_production_backup_authority

        def commit_then_crash(**kwargs: Any) -> Any:
            compare(**kwargs)
            raise SystemExit("simulated process exit after commit")

        with patch.object(
            self.store,
            "compare_and_apply_production_backup_authority",
            side_effect=commit_then_crash,
        ):
            with self.assertRaises(SystemExit):
                execute_approved_privileged_operations_once(record_store=self.store)
        interrupted = self.store.read_privileged_operation_record(operation_id)
        self.assertEqual(interrupted.status, "executing")
        recovered = _execute_backup_policy(store=self.store, record=interrupted)
        self.assertEqual(recovered.result_status, "ok")
        self.assertEqual(len(self.store.list_production_backup_policy_records()), 1)
        self.assertCountEqual(
            self.store.list_production_backup_target_records(), _dry_run_envelope().targets
        )
