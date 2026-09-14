from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from pydantic import ValidationError

from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
from control_plane.contracts.merge_train_historical_completion import (
    MergeTrainHistoricalCompletionEvidence,
)
from control_plane.http_routes.mutation_support import idempotency_scope
from control_plane.merge_train_github import GitHubMergeTrainClient
from control_plane.merge_train_historical_completion import read_historical_completion_snapshot
from control_plane.merge_train_historical_disposition import (
    HistoricalDispositionAuthority,
    HistoricalDispositionRequest,
    build_historical_disposition,
)
from control_plane.workflows.merge_train_controller import (
    decide_merge_train_controller_record_action,
)
from tests.support.merge_train import _merge_train_service_identity, _merge_train_service_policy
from tests.test_merge_train_historical_completion import (
    BASE_BRANCH,
    OBSERVED_AT,
    REPOSITORY,
    _HistoricalCompletionFixture,
    _ReadOnlyTransport,
    _provider_responses,
)


class HistoricalDispositionRecordTests(unittest.TestCase):
    def test_observation_retires_candidate_selection_without_claiming_a_landing(self) -> None:
        with TemporaryDirectory() as directory:
            fixture = _HistoricalCompletionFixture(Path(directory))
            identity = _merge_train_service_identity()
            request = HistoricalDispositionRequest(
                repository=REPOSITORY,
                base_branch=BASE_BRANCH,
                selector=fixture.selector,
                identity=identity,
                scope=idempotency_scope(identity),
                route_path="/v1/work-graph/merge-train/controller/run-once",
                idempotency_key="historical-disposition-test",
                request_fingerprint="disposition-request",
            )
            snapshot = read_historical_completion_snapshot(
                store=fixture.store,
                repository=REPOSITORY,
                base_branch=BASE_BRANCH,
                selector=fixture.selector,
            )
            authority = HistoricalDispositionAuthority(
                policy=fixture.policy_record,
                authz=LaunchplaneAuthzPolicyRecord(
                    record_id="test-authz-record",
                    source="test:historical-disposition",
                    updated_at=OBSERVED_AT,
                    policy=_merge_train_service_policy(),
                ),
            )
            transport = _ReadOnlyTransport(responses=_provider_responses())
            evidence = GitHubMergeTrainClient(
                transport=transport
            ).observe_historical_batch_completion(
                landing_plan=fixture.landing_plan, observed_at=OBSERVED_AT
            )
            bundle = build_historical_disposition(
                request=request,
                authority=authority,
                snapshot=snapshot,
                provider_evidence=evidence,
                recorded_at=OBSERVED_AT,
                trace_id="historical-disposition-trace",
            )
            self.assertTrue(
                all(entry.status == "stale" for entry in bundle.successor.landing_plan.entries)
            )
            self.assertTrue(
                all(not entry.merge_commit_sha for entry in bundle.successor.landing_plan.entries)
            )
            self.assertEqual(bundle.controller.status, "idle")
            self.assertEqual(bundle.controller.last_record_id, bundle.successor.record_id)
            self.assertEqual(bundle.controller.active_record_id, "")
            decision = decide_merge_train_controller_record_action(
                candidate_records=(fixture.candidate_record,),
                landing_plan_records=(bundle.successor,),
                stack_collapse_plan_records=(),
            )
            self.assertEqual(decision.action, "idle")
            self.assertEqual(decision.candidate_record_id, "")
            historical = bundle.successor.historical_completion
            assert historical is not None
            assert historical.disposition_authorization is not None
            self.assertEqual(historical.disposition_authorization.actor_scope, request.scope)
            self.assertEqual(historical.provider_evidence, evidence)
            with self.assertRaisesRegex(ValueError, "historical completion"):
                fixture.store.write_merge_train_batch_landing_plan_record(bundle.successor)
            self.assertEqual(
                fixture.store.list_merge_train_controller_state_records()[0], fixture.controller
            )

            # Older observation records remain readable; new service dispositions
            # cannot silently omit the authority that performed the recovery.
            legacy = historical.model_dump(mode="json")
            legacy["schema_version"] = 1
            legacy.pop("disposition_authorization")
            self.assertIsNone(
                MergeTrainHistoricalCompletionEvidence.model_validate(
                    legacy
                ).disposition_authorization
            )
            legacy["schema_version"] = 2
            with self.assertRaisesRegex(ValidationError, "disposition authorization"):
                MergeTrainHistoricalCompletionEvidence.model_validate(legacy)


if __name__ == "__main__":
    unittest.main()
