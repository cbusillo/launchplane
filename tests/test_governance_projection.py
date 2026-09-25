from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

from control_plane.contracts.merge_train_policy import MergeTrainGitHubTokenSource
from control_plane.contracts.change_impact import ChangeImpactTargetReference
from control_plane.contracts.governance_projection import GovernanceMergeReadinessFacet
from control_plane.contracts.merge_train_structural_provenance import (
    MergeTrainStackCollapseRootProof,
)
from control_plane.contracts.ordinary_agent_session_lifecycle import OrdinaryAgentJobBinding
from control_plane.governance_projection import build_governance_projection
from control_plane.governance_projection import LiveGovernanceCurrentReadinessProvider
from control_plane.merge_admission import MergeAdmissionEvaluation
from tests.test_merge_readiness import _evaluate as _merge_readiness
from tests.test_merge_admission_records import _guard_records
from tests.test_merge_train_admission import _stack_collapse_record
from tests.support.repository_evidence import (
    REPOSITORY,
    _repository_evidence,
    _store,
)


TARGET = ChangeImpactTargetReference(repository=REPOSITORY, pull_request_number=2022)
NOW = "2026-08-12T05:00:00Z"


def _not_active_readiness(**_: object) -> GovernanceMergeReadinessFacet:
    return GovernanceMergeReadinessFacet(
        availability="not_active",
        reason_code="no_active_merge_lineage",
    )


class GovernanceProjectionTests(unittest.TestCase):
    def test_live_readiness_provider_uses_active_candidate_and_landing_lineage(self) -> None:
        candidate_record, landing_record, controller_state, structural_result = _guard_records()

        class _Evaluator:
            def evaluate(self, **kwargs: object) -> MergeAdmissionEvaluation:
                self.kwargs = kwargs
                return MergeAdmissionEvaluation(
                    readiness=_merge_readiness(),
                    structural_result=structural_result,
                )

        evaluator = _Evaluator()
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            store.write_merge_train_batch_candidate_record(candidate_record)
            store.write_merge_train_batch_landing_plan_record(landing_record)
            store.write_merge_train_controller_state_record(controller_state)
            evidence = _repository_evidence(
                head=candidate_record.candidate.entries[0].head_sha,
                base_sha=candidate_record.candidate.base_sha,
            ).model_copy(
                update={
                    "target": _repository_evidence().target.model_copy(
                        update={
                            "repository": candidate_record.candidate.repository,
                            "pull_request_number": candidate_record.candidate.entries[
                                0
                            ].pull_request_number,
                            "head_sha": candidate_record.candidate.entries[0].head_sha,
                            "tree_sha": candidate_record.candidate.entries[0].head_tree_sha,
                        }
                    )
                }
            )
            provider = LiveGovernanceCurrentReadinessProvider(
                github_token=lambda source, _repository: (
                    "test-token" if source.env_var == "GH_TOKEN" else ""
                ),
                evaluator_factory=lambda _store, _provider, _token: evaluator,
            )

            result = provider(
                store=store,
                repository_evidence=evidence,
                base_branch="main",
                evaluated_at=NOW,
                github_token_source=MergeTrainGitHubTokenSource(env_var="GH_TOKEN"),
            )

        self.assertEqual(result.availability, "available")
        self.assertIsNotNone(result.result)
        self.assertEqual(
            evaluator.kwargs["candidate_record"],
            candidate_record,
        )
        self.assertEqual(
            evaluator.kwargs["landing_plan_record"],
            landing_record,
        )
        self.assertEqual(
            evaluator.kwargs["expected_lease_owner"],
            controller_state.lease_owner,
        )

    def test_live_readiness_provider_requires_running_controller_lease(self) -> None:
        candidate_record, landing_record, controller_state, _structural_result = _guard_records()
        evidence = _repository_evidence(
            head=candidate_record.candidate.entries[0].head_sha,
            base_sha=candidate_record.candidate.base_sha,
        ).model_copy(
            update={
                "target": _repository_evidence().target.model_copy(
                    update={
                        "repository": candidate_record.candidate.repository,
                        "pull_request_number": candidate_record.candidate.entries[
                            0
                        ].pull_request_number,
                        "head_sha": candidate_record.candidate.entries[0].head_sha,
                        "tree_sha": candidate_record.candidate.entries[0].head_tree_sha,
                    }
                )
            }
        )
        idle_controller_state = controller_state.model_copy(
            update={
                "status": "idle",
                "lease_owner": "",
                "lease_acquired_at": "",
                "lease_expires_at": "",
                "heartbeat_at": "",
            }
        )
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            store.write_merge_train_batch_candidate_record(candidate_record)
            store.write_merge_train_batch_landing_plan_record(landing_record)
            store.write_merge_train_controller_state_record(idle_controller_state)
            provider = LiveGovernanceCurrentReadinessProvider(
                github_token=lambda _source, _repository: self.fail("idle controller used a token")
            )

            result = provider(
                store=store,
                repository_evidence=evidence,
                base_branch="main",
                evaluated_at=NOW,
                github_token_source=MergeTrainGitHubTokenSource(env_var="GH_TOKEN"),
            )

        self.assertEqual(result.availability, "unavailable")
        self.assertEqual(result.reason_code, "current_evidence_unavailable")

    def test_live_readiness_provider_rejects_mixed_ordinary_job_bindings_before_token(self) -> None:
        candidate_record, landing_record, controller_state, _structural_result = _guard_records()
        first_binding = OrdinaryAgentJobBinding(
            request_id="request_one",
            scope_sha256="a" * 64,
            binding_revision=1,
        )
        second_binding = first_binding.model_copy(update={"request_id": "request_two"})
        evidence = _repository_evidence(
            head=candidate_record.candidate.entries[0].head_sha,
            base_sha=candidate_record.candidate.base_sha,
        ).model_copy(
            update={
                "target": _repository_evidence().target.model_copy(
                    update={
                        "repository": candidate_record.candidate.repository,
                        "pull_request_number": candidate_record.candidate.entries[
                            0
                        ].pull_request_number,
                        "head_sha": candidate_record.candidate.entries[0].head_sha,
                        "tree_sha": candidate_record.candidate.entries[0].head_tree_sha,
                    }
                )
            }
        )
        cases = (
            (
                "landing",
                candidate_record.model_copy(update={"ordinary_job_binding": first_binding}),
                landing_record.model_copy(update={"ordinary_job_binding": second_binding}),
                controller_state.model_copy(update={"ordinary_job_binding": first_binding}),
            ),
            (
                "controller",
                candidate_record.model_copy(update={"ordinary_job_binding": first_binding}),
                landing_record.model_copy(update={"ordinary_job_binding": first_binding}),
                controller_state.model_copy(update={"ordinary_job_binding": second_binding}),
            ),
            (
                "mixed_legacy",
                candidate_record.model_copy(update={"ordinary_job_binding": first_binding}),
                landing_record.model_copy(update={"ordinary_job_binding": first_binding}),
                controller_state,
            ),
        )
        for name, candidate, landing, controller in cases:
            with self.subTest(name=name):
                store = Mock()
                store.list_merge_train_batch_landing_plan_records.return_value = (landing,)
                store.list_merge_train_batch_candidate_records.return_value = (candidate,)
                store.list_merge_train_controller_state_records.return_value = (controller,)
                token = Mock(return_value="test-token")
                evaluator_factory = Mock()
                provider = LiveGovernanceCurrentReadinessProvider(
                    github_token=token,
                    evaluator_factory=evaluator_factory,
                )

                result = provider(
                    store=store,
                    repository_evidence=evidence,
                    base_branch="main",
                    evaluated_at=NOW,
                    github_token_source=MergeTrainGitHubTokenSource(env_var="GH_TOKEN"),
                )

                self.assertEqual(result.availability, "unavailable")
                self.assertEqual(result.reason_code, "current_evidence_unavailable")
                token.assert_not_called()
                evaluator_factory.assert_not_called()

    def test_live_readiness_provider_accepts_independently_equal_job_bindings(self) -> None:
        candidate_record, landing_record, controller_state, structural_result = _guard_records()

        def binding() -> OrdinaryAgentJobBinding:
            return OrdinaryAgentJobBinding(
                request_id="request_one",
                scope_sha256="a" * 64,
                binding_revision=1,
            )

        candidate_record = candidate_record.model_copy(update={"ordinary_job_binding": binding()})
        landing_record = landing_record.model_copy(update={"ordinary_job_binding": binding()})
        controller_state = controller_state.model_copy(update={"ordinary_job_binding": binding()})
        evidence = _repository_evidence(
            head=candidate_record.candidate.entries[0].head_sha,
            base_sha=candidate_record.candidate.base_sha,
        ).model_copy(
            update={
                "target": _repository_evidence().target.model_copy(
                    update={
                        "repository": candidate_record.candidate.repository,
                        "pull_request_number": candidate_record.candidate.entries[
                            0
                        ].pull_request_number,
                        "head_sha": candidate_record.candidate.entries[0].head_sha,
                        "tree_sha": candidate_record.candidate.entries[0].head_tree_sha,
                    }
                )
            }
        )
        store = Mock()
        store.list_merge_train_batch_landing_plan_records.return_value = (landing_record,)
        store.list_merge_train_batch_candidate_records.return_value = (candidate_record,)
        store.list_merge_train_controller_state_records.return_value = (controller_state,)
        token = Mock(return_value="test-token")
        evaluator = Mock()
        evaluator.evaluate.return_value = MergeAdmissionEvaluation(
            readiness=_merge_readiness(),
            structural_result=structural_result,
        )
        evaluator_factory = Mock(return_value=evaluator)
        provider = LiveGovernanceCurrentReadinessProvider(
            github_token=token,
            evaluator_factory=evaluator_factory,
        )

        result = provider(
            store=store,
            repository_evidence=evidence,
            base_branch="main",
            evaluated_at=NOW,
            github_token_source=MergeTrainGitHubTokenSource(runtime_context="example_context"),
        )

        self.assertEqual(result.availability, "available")
        token.assert_called_once_with(
            MergeTrainGitHubTokenSource(runtime_context="example_context"),
            evidence.target.repository,
        )
        evaluator_factory.assert_called_once()
        evaluator.evaluate.assert_called_once()

    def test_live_readiness_provider_rejects_mixed_collapse_binding_before_token(self) -> None:
        candidate_record, landing_record, controller_state, _structural_result = _guard_records()
        first_binding = OrdinaryAgentJobBinding(
            request_id="request_one",
            scope_sha256="a" * 64,
            binding_revision=1,
        )
        second_binding = first_binding.model_copy(update={"request_id": "request_two"})
        entry = candidate_record.candidate.entries[0]
        stack_collapse_root = MergeTrainStackCollapseRootProof(
            collapse_record_id="collapse-record-one",
            collapse_id="collapse-one",
            root_pull_request_number=entry.pull_request_number,
            original_root_head_sha=entry.head_sha,
            collapsed_root_head_sha=entry.head_sha,
            collapsed_root_tree_sha=entry.head_tree_sha,
        )
        structural_provenance = candidate_record.candidate.structural_provenance
        assert structural_provenance is not None
        candidate = candidate_record.candidate.model_copy(
            update={
                "stack_collapse_root": stack_collapse_root,
                "structural_provenance": structural_provenance.model_copy(
                    update={"stack_collapse_root": stack_collapse_root}
                ),
            }
        )
        candidate_record = candidate_record.model_copy(
            update={"ordinary_job_binding": first_binding, "candidate": candidate}
        )
        landing_record = landing_record.model_copy(update={"ordinary_job_binding": first_binding})
        controller_state = controller_state.model_copy(
            update={"ordinary_job_binding": first_binding}
        )
        collapse_template = _stack_collapse_record(status="planned")
        collapse_record = type(collapse_template).model_validate(
            {
                **collapse_template.model_dump(),
                "record_id": stack_collapse_root.collapse_record_id,
                "ordinary_job_binding": second_binding.model_dump(),
            }
        )
        store = Mock()
        store.list_merge_train_batch_landing_plan_records.return_value = (landing_record,)
        store.list_merge_train_batch_candidate_records.return_value = (candidate_record,)
        store.list_merge_train_controller_state_records.return_value = (controller_state,)
        store.list_merge_train_stack_collapse_plan_records.return_value = (collapse_record,)
        token = Mock(return_value="test-token")
        evaluator_factory = Mock()
        provider = LiveGovernanceCurrentReadinessProvider(
            github_token=token,
            evaluator_factory=evaluator_factory,
        )
        evidence = _repository_evidence(
            head=entry.head_sha,
            base_sha=candidate_record.candidate.base_sha,
        ).model_copy(
            update={
                "target": _repository_evidence().target.model_copy(
                    update={
                        "repository": candidate_record.candidate.repository,
                        "pull_request_number": entry.pull_request_number,
                        "head_sha": entry.head_sha,
                        "tree_sha": entry.head_tree_sha,
                    }
                )
            }
        )

        result = provider(
            store=store,
            repository_evidence=evidence,
            base_branch="main",
            evaluated_at=NOW,
            github_token_source=MergeTrainGitHubTokenSource(env_var="GH_TOKEN"),
        )

        self.assertEqual(result.availability, "unavailable")
        self.assertEqual(result.reason_code, "current_evidence_unavailable")
        token.assert_not_called()
        evaluator_factory.assert_not_called()

    def test_live_readiness_provider_treats_terminal_lineage_as_inactive(self) -> None:
        candidate_record, landing_record, controller_state, _structural_result = _guard_records()
        evidence = _repository_evidence(
            head=candidate_record.candidate.entries[0].head_sha,
            base_sha=candidate_record.candidate.base_sha,
        ).model_copy(
            update={
                "target": _repository_evidence().target.model_copy(
                    update={
                        "repository": candidate_record.candidate.repository,
                        "pull_request_number": candidate_record.candidate.entries[
                            0
                        ].pull_request_number,
                        "head_sha": candidate_record.candidate.entries[0].head_sha,
                        "tree_sha": candidate_record.candidate.entries[0].head_tree_sha,
                    }
                )
            }
        )
        for status in ("stale", "blocked"):
            with self.subTest(status=status), TemporaryDirectory() as directory:
                store = _store(Path(directory))
                terminal_record = landing_record.model_copy(
                    update={
                        "landing_plan": landing_record.landing_plan.model_copy(
                            update={
                                "entries": tuple(
                                    entry.model_copy(update={"status": status})
                                    for entry in landing_record.landing_plan.entries
                                )
                            }
                        )
                    }
                )
                store.write_merge_train_batch_candidate_record(candidate_record)
                store.write_merge_train_batch_landing_plan_record(terminal_record)
                store.write_merge_train_controller_state_record(controller_state)
                provider = LiveGovernanceCurrentReadinessProvider(
                    github_token=lambda _source, _repository: self.fail(
                        "terminal lineage used a token"
                    )
                )

                result = provider(
                    store=store,
                    repository_evidence=evidence,
                    base_branch="main",
                    evaluated_at=NOW,
                    github_token_source=MergeTrainGitHubTokenSource(env_var="GH_TOKEN"),
                )

            self.assertEqual(result.availability, "not_active")

    def test_projection_reuses_one_repository_evidence_snapshot(self) -> None:
        class _ChangingProvider:
            def __init__(self) -> None:
                self.calls = 0

            def resolve(self, target: ChangeImpactTargetReference) -> object:
                self.calls += 1
                if self.calls > 1:
                    return _repository_evidence(head="c" * 40)
                return _repository_evidence()

        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            provider = _ChangingProvider()

            projection = build_governance_projection(
                store=store,
                repository_evidence_provider=provider,  # type: ignore[arg-type]
                current_readiness_provider=_not_active_readiness,
                target=TARGET,
                base_branch="main",
                generated_at=NOW,
            )

        self.assertEqual(provider.calls, 1)
        self.assertEqual(projection.target.head_sha, _repository_evidence().target.head_sha)
        self.assertIsNone(projection.owner_judgment)


if __name__ == "__main__":
    unittest.main()
