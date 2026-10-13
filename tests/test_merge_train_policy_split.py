import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from typing import cast

from fastapi import FastAPI
from pydantic import ValidationError

from control_plane.contracts.merge_train_batch import (
    build_merge_train_batch_landing_plan,
    build_merge_train_batch_landing_plan_record,
)
from control_plane.contracts.merge_train_policy import (
    MergeTrainPolicy,
    MergeTrainPolicyRecord,
    merge_train_repository_authority_sha256,
    merge_train_repository_execution_sha256,
)
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.merge_train_admission import build_merge_train_controller_status_read_model
from control_plane.merge_train_controller_run_once import (
    MergeTrainControllerRunOnceEnvelope,
    execute_merge_train_controller_with_client,
    validate_merge_train_candidate_record_for_controller,
    validate_merge_train_landing_record_for_controller,
    validate_merge_train_stack_collapse_record_for_controller,
)
from control_plane.merge_train_github import GitHubMergeTrainClient
from control_plane.privileged_operation_service import create_typed_privileged_operation_plan
from control_plane.privileged_operation_worker import execute_approved_privileged_operations_once
from control_plane.contracts.privileged_operation import (
    ManagedMergeTrainPolicyImportProposalInput,
    ManagedMergeTrainPolicyImportHumanEvidence,
    PrivilegedOperationActor,
)
from control_plane.merge_train_policy_operations import (
    MERGE_TRAIN_OPERATIONAL_POLICY_ROUTE,
    MERGE_TRAIN_OPERATIONAL_POLICY_SCOPE,
    MergeTrainOperationalUpdate,
)
from control_plane.service_auth import BearerIdentityConfig
from control_plane.storage.postgres import DbOnlyMutationRequest, PostgresRecordStore
from tests.http_app_test_support import _asgi_request, _RejectingVerifier, _AsgiResponse
from tests.merge_train_policy_fixtures import build_test_merge_train_policy
from tests.support.auth import local_operator_policy
from tests.support.merge_train import _seed_merge_train_batch_candidate_record
from tests.support.merge_train import _FakeMergeTrainGitHubClient
from tests.test_merge_train_controller import _stack_collapse_record
from control_plane.merge_admission import MergeAdmissionEvaluation


class _UnexpectedAdmissionEvaluator:
    def evaluate(self, **_: object) -> MergeAdmissionEvaluation:
        raise AssertionError("A dry-run must not admit a merge")


class MergeTrainPolicySplitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = PostgresRecordStore(
            database_url=f"sqlite+pysqlite:///{self.root / 'state.sqlite'}"
        )
        self.store.ensure_schema()
        self.addCleanup(self.store.close)
        self.policy = MergeTrainPolicy(
            policies=(
                *build_test_merge_train_policy(repository="example/first").policies,
                *build_test_merge_train_policy(repository="cbusillo/sellyouroutboard").policies,
            )
        )
        self.original = MergeTrainPolicyRecord(
            record_id="original",
            source="test",
            updated_at="2026-05-13T21:00:00Z",
            policy=self.policy,
        )
        self.store.write_merge_train_policy_record(self.original)
        self.app = self._app(("merge_train_policy_operation.propose",))

    def _app(self, actions: tuple[str, ...]) -> FastAPI:
        return create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=local_operator_policy(actions=actions),
            record_store_factory=lambda: self.store,
            bearer_identity_config=BearerIdentityConfig(
                local_operator_token="test-token",
                local_operator_subject="local-owner-agent",
                local_operator_token_label="local-owner-write",
            ),
        )

    def _request(
        self,
        patch: dict[str, object],
        *,
        repository: str = "example/first",
        event: str = "update-1",
    ) -> dict[str, object]:
        return {
            "repository": repository,
            "base_branch": "main",
            "expected_record_id": self.original.record_id,
            "expected_policy_sha256": self.original.policy_sha256,
            "source_event_id": event,
            "reason": "Tune the queue",
            "patch": patch,
        }

    async def _post(self, payload: dict[str, object], app: FastAPI | None = None) -> _AsgiResponse:
        return await _asgi_request(
            app or self.app,
            "POST",
            MERGE_TRAIN_OPERATIONAL_POLICY_ROUTE,
            headers={"Authorization": "Bearer test-token"},
            payload=payload,
        )

    async def test_operational_http_update_audit_replay_and_unrelated_landing(self) -> None:
        candidate = _seed_merge_train_batch_candidate_record(
            self.root / "fixtures",
            policy=self.policy,
            status="passed",
            required_checks_status="pass",
            candidate_sha="candidate-built",
        )
        self.store.write_merge_train_batch_candidate_record(candidate)
        landing = build_merge_train_batch_landing_plan_record(
            landing_plan=build_merge_train_batch_landing_plan(
                candidate=candidate.candidate, merge_method="merge", created_at=candidate.updated_at
            ),
            source="test",
            updated_at=candidate.updated_at,
        )
        self.store.write_merge_train_batch_landing_plan_record(landing)
        payload = self._request(
            {"max_batch_size": 3, "failure_policy": "continue_after_blocking_pr"}
        )
        response = await self._post(payload)
        self.assertEqual(response.status_code, 200, response.text)
        receipt = response.json()
        self.assertTrue(receipt["changed"])
        current = self.store.list_merge_train_policy_records(status="active")[0]
        self.assertNotEqual(current.policy_sha256, self.original.policy_sha256)
        validate_merge_train_candidate_record_for_controller(
            candidate_record=candidate,
            policy_key=candidate.candidate.policy_key,
            policy_sha256=current.policy_sha256,
            record_store=self.store,
        )
        validate_merge_train_landing_record_for_controller(
            landing_record=landing,
            policy_key=landing.landing_plan.policy_key,
            policy_sha256=current.policy_sha256,
            record_store=self.store,
        )
        progression = execute_merge_train_controller_with_client(
            request=MergeTrainControllerRunOnceEnvelope(
                repository=candidate.candidate.repository, base_branch="main", mutate=False
            ),
            policy=current.policy,
            policy_sha256=current.policy_sha256,
            repository_policy=current.policy.policies[1],
            github_client=cast(
                GitHubMergeTrainClient, _FakeMergeTrainGitHubClient(transport=object())
            ),
            trace_id="split-controller-proof",
            recorded_at=current.updated_at,
            candidate_store=self.store,
            landing_store=self.store,
            stack_collapse_store=self.store,
            controller_state_store=self.store,
            admission_store=self.store,
            admission_evaluator=_UnexpectedAdmissionEvaluator(),
        )
        self.assertEqual(progression.accepted_result["controller_action"], "land_batch")
        status = build_merge_train_controller_status_read_model(
            store=self.store,
            repository=candidate.candidate.repository,
            base_branch="main",
            generated_at=current.updated_at,
            current_policy_key=candidate.candidate.policy_key,
            current_policy_sha256=current.policy_sha256,
            policy_record=current,
        )
        self.assertTrue(status.controller_records)
        self.assertTrue(
            all(record.policy_status == "current" for record in status.controller_records)
        )
        replay = await self._post(payload)
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertEqual(replay.json(), receipt)
        self.assertEqual(self.store.read_merge_train_policy_record("original").status, "superseded")
        conflict = await self._post(self._request({"max_batch_size": 4}))
        self.assertEqual(conflict.status_code, 409, conflict.text)
        stale = await self._post(self._request({"max_batch_size": 4}, event="new-event"))
        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertEqual(self.store.list_merge_train_policy_records(status="active")[0], current)

    async def test_operational_patch_cannot_supply_authority(self) -> None:
        for field, value in (
            ("merge_identity", {"kind": "github_app", "name": "other"}),
            ("github_token", {}),
            ("service_authz", {}),
            ("enqueue", {"label_required": False}),
            ("repository", "example/other"),
            ("base_branch", "release"),
            ("enqueue_label", "different"),
            ("blocked_label", "different"),
            ("provider_delivery_protection_expectation", None),
            ("scheduler", {"mutation_authorized": True}),
        ):
            with self.subTest(field=field):
                response = await self._post(self._request({field: value}))
                self.assertEqual(response.status_code, 400, response.text)
        denied = await self._post(
            self._request({"max_batch_size": 2}), self._app(("merge_train.run_once",))
        )
        self.assertEqual(denied.status_code, 403, denied.text)
        self.assertEqual(
            self.store.list_merge_train_policy_records(status="active")[0], self.original
        )

    async def test_scheduler_cannot_grant_mutation_but_authorized_lane_can_pause_and_resume(
        self,
    ) -> None:
        denied = await self._post(self._request({"scheduler": {"enabled": True, "mutate": True}}))
        self.assertEqual(denied.status_code, 409, denied.text)
        payload = self.original.model_dump(mode="json")
        payload["record_id"] = "authorized"
        payload["policy_sha256"] = ""
        payload["policy"]["policies"][0]["scheduler"] = {
            "enabled": True,
            "mutate": True,
            "runner_mode": "controller",
        }
        authorized = MergeTrainPolicyRecord.model_validate(payload)
        self.store.compare_and_write_merge_train_policy_record(
            expected_record=self.original, replacement_record=authorized
        )
        self.original = authorized
        response = await self._post(
            self._request({"scheduler": {"enabled": False, "mutate": False}})
        )
        self.assertEqual(response.status_code, 200, response.text)
        paused = self.store.list_merge_train_policy_records(status="active")[0]
        self.original = paused
        response = await self._post(
            self._request(
                {"scheduler": {"enabled": True, "mutate": True, "runner_mode": "level1"}},
                event="resume",
            )
        )
        self.assertEqual(response.status_code, 200, response.text)
        resumed = self.store.list_merge_train_policy_records(status="active")[0]
        self.assertTrue(resumed.policy.policies[0].scheduler.mutate)
        self.assertEqual(
            merge_train_repository_authority_sha256(authorized.policy.policies[0]),
            merge_train_repository_authority_sha256(resumed.policy.policies[0]),
        )

    async def test_storage_operational_identity_rejects_full_authority_replacement(self) -> None:
        for field, value in (
            ("enqueue_label", "new-admission"),
            ("blocked_label", "new-hold"),
            ("merge_identity", {"kind": "github_app", "name": "other"}),
            (
                "service_authz",
                {"action": "other", "product": "launchplane", "context": "launchplane"},
            ),
            ("enqueue", {"label_required": False, "allowed_actor_roles": ["repo_owner"]}),
        ):
            with self.subTest(field=field):
                payload = self.original.model_dump(mode="json")
                payload["record_id"] = "unsafe"
                payload["policy_sha256"] = ""
                payload["policy"]["policies"][0][field] = value
                candidate = MergeTrainPolicyRecord.model_validate(payload)
                with self.assertRaisesRegex(ValueError, "changes_authority"):
                    self.store.compare_and_write_merge_train_policy_record(
                        expected_record=self.original,
                        replacement_record=candidate,
                        mutation=DbOnlyMutationRequest(
                            scope=MERGE_TRAIN_OPERATIONAL_POLICY_SCOPE,
                            route_path=MERGE_TRAIN_OPERATIONAL_POLICY_ROUTE,
                            idempotency_key=field,
                            request_fingerprint=field,
                            lease_owner="test",
                            response_status_code=200,
                            response_trace_id="test",
                            response_payload={},
                        ),
                    )
        self.assertEqual(
            self.store.list_merge_train_policy_records(status="active")[0], self.original
        )

    async def test_same_target_annotation_preserves_collapse_but_merge_method_stales_it(
        self,
    ) -> None:
        raw = _stack_collapse_record(status="waiting_for_root_checks").model_dump(mode="json")
        raw["plan"]["repository"] = "example/first"
        raw["plan"]["policy_key"] = "example/first:main"
        raw["plan"]["policy_sha256"] = self.original.policy_sha256
        collapse = type(_stack_collapse_record(status="waiting_for_root_checks")).model_validate(
            raw
        )
        response = await self._post(
            self._request({"stack_child_disposition_label": "finished-stack", "max_batch_size": 1})
        )
        self.assertEqual(response.status_code, 200, response.text)
        current = self.store.list_merge_train_policy_records(status="active")[0]
        validate_merge_train_stack_collapse_record_for_controller(
            collapse_record=collapse,
            policy_key=collapse.plan.policy_key,
            policy_sha256=current.policy_sha256,
            record_store=self.store,
        )
        self.assertEqual(
            merge_train_repository_execution_sha256(self.original.policy.policies[0]),
            merge_train_repository_execution_sha256(current.policy.policies[0]),
        )
        self.original = current
        response = await self._post(
            self._request({"merge_method": "squash"}, event="method-change")
        )
        self.assertEqual(response.status_code, 200, response.text)
        changed = self.store.list_merge_train_policy_records(status="active")[0]
        with self.assertRaisesRegex(ValueError, "digest no longer matches"):
            validate_merge_train_stack_collapse_record_for_controller(
                collapse_record=collapse,
                policy_key=collapse.plan.policy_key,
                policy_sha256=changed.policy_sha256,
                record_store=self.store,
            )

    def test_historical_policy_bytes_digest_still_verify(self) -> None:
        original_bytes = self.original.model_dump_json()
        restored = MergeTrainPolicyRecord.model_validate_json(original_bytes)
        self.assertEqual(restored.model_dump_json(), original_bytes)
        self.assertNotIn(
            "mutation_authorized", json.loads(original_bytes)["policy"]["policies"][0]["scheduler"]
        )
        corrupted = json.loads(original_bytes)
        corrupted["policy"]["policies"][0]["max_batch_size"] = 2
        with self.assertRaises(ValidationError):
            MergeTrainPolicyRecord.model_validate(corrupted)
        with self.assertRaises(ValidationError):
            MergeTrainOperationalUpdate.model_validate(self._request({"schema_version": 2}))

    def test_authority_proposal_is_classified_and_cannot_execute_without_approval(self) -> None:
        payload = self.original.model_dump(mode="json")
        payload["record_id"] = "authority-candidate"
        payload["policy_sha256"] = ""
        payload["policy"]["policies"][0]["enqueue_label"] = "new-admission"
        candidate = MergeTrainPolicyRecord.model_validate(payload)
        planned = create_typed_privileged_operation_plan(
            record_store=self.store,
            descriptor_id="managed-merge-train-policy-import",
            actor=PrivilegedOperationActor(
                identity_type="github_human", github_id=123, login="director"
            ),
            source_kind="browser_api",
            source_event_id="authority-proposal",
            request=ManagedMergeTrainPolicyImportProposalInput(
                record=candidate, reason="Change admission"
            ),
        ).record
        self.assertIsNone(planned.approval)
        assert isinstance(planned.evidence, ManagedMergeTrainPolicyImportHumanEvidence)
        self.assertEqual(
            planned.evidence.authority_changed_policy_keys,
            (candidate.policy.policies[0].policy_key,),
        )
        self.assertEqual(execute_approved_privileged_operations_once(record_store=self.store), ())
        self.assertEqual(
            self.store.list_merge_train_policy_records(status="active")[0], self.original
        )

    def test_different_digest_without_history_cannot_validate_work(self) -> None:
        candidate = _seed_merge_train_batch_candidate_record(
            self.root / "fixtures", policy=self.policy
        )
        with self.assertRaisesRegex(ValueError, "digest no longer matches"):
            validate_merge_train_candidate_record_for_controller(
                candidate_record=candidate,
                policy_key=candidate.candidate.policy_key,
                policy_sha256="unresolved-current-policy",
            )
