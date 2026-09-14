from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi import FastAPI

from control_plane.contracts.merge_train_batch import (
    MergeTrainBatchCandidate,
    MergeTrainBatchCandidateRecord,
    MergeTrainBatchEntry,
    MergeTrainBatchLandingPlan,
    MergeTrainBatchLandingPlanRecord,
    build_merge_train_batch_landing_plan,
)
from control_plane.contracts.merge_train_controller_state import (
    MergeTrainControllerStateRecord,
    build_merge_train_controller_key,
)
from control_plane.contracts.merge_train_historical_completion import (
    MergeTrainHistoricalCompletionSelector,
    MergeTrainHistoricalCompletionEntrySelector,
)
from control_plane.contracts.merge_train_structural_provenance import (
    MergeTrainRollingStep,
    MergeTrainStructuralEntryBinding,
    MergeTrainStructuralProvenance,
    MergeTrainStructuralSubject,
)
from control_plane.contracts.merge_train_stack_collapse import (
    MergeTrainStackCollapseEntry,
    MergeTrainStackCollapseMutation,
    MergeTrainStackCollapsePlan,
    MergeTrainStackCollapsePlanRecord,
)
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.merge_train_historical_completion import (
    MergeTrainHistoricalCompletionPreflight,
    assess_merge_train_historical_completion,
)
from control_plane.service_auth import GitHubActionsIdentity
from control_plane.merge_train_controller_run_once import (
    MergeTrainControllerRequestError,
    MergeTrainControllerRunOnceEnvelope,
    execute_merge_train_controller_with_client,
)
from control_plane.merge_train_github import (
    GitHubMergeTrainClient,
    RecordingMergeTrainGitHubTransport,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore
from tests.http_app_test_support import _post_merge_train_controller_run_once
from tests.merge_train_policy_fixtures import build_test_merge_train_policy_record
from tests.support.auth import _StubVerifier, _identity
from tests.support.merge_train import (
    _merge_train_service_identity,
    _merge_train_service_policy,
)


REPOSITORY = "acme/widget"
BASE_BRANCH = "main"
OBSERVED_AT = "2026-09-13T12:00:00Z"


def _branch(sha: str, tree_sha: str) -> dict[str, object]:
    return {"commit": {"sha": sha, "commit": {"tree": {"sha": tree_sha}}}}


def _pull_request(
    number: int,
    *,
    head_sha: str,
    merge_commit_sha: str,
    state: str = "closed",
    merged: bool = True,
) -> dict[str, object]:
    return {
        "number": number,
        "state": state,
        "merged": merged,
        "base": {"ref": BASE_BRANCH},
        "head": {"sha": head_sha},
        "merge_commit_sha": merge_commit_sha,
    }


def _commit(
    sha: str,
    tree_sha: str,
    *,
    parents: tuple[str, ...] = (),
) -> dict[str, object]:
    return {
        "sha": sha,
        "tree": {"sha": tree_sha},
        "parents": [{"sha": parent} for parent in parents],
    }


def _compare(merge_sha: str) -> dict[str, object]:
    return {"status": "ahead", "merge_base_commit": {"sha": merge_sha}}


def _provider_responses(
    *,
    first_pull_request: dict[str, object] | None = None,
    first_base: tuple[str, str] = ("pinned-base", "pinned-tree"),
    final_base: tuple[str, str] | None = None,
) -> tuple[object, ...]:
    final_base = final_base or first_base
    return (
        _branch(*first_base),
        first_pull_request or _pull_request(1, head_sha="head-1", merge_commit_sha="merge-1"),
        _commit("head-1", "tree-head-1"),
        _commit("merge-1", "tree-merge-1", parents=("pinned-base", "head-1")),
        _commit("pinned-base", "tree-pinned-base"),
        _compare("merge-1"),
        _pull_request(2, head_sha="head-2", merge_commit_sha="merge-2"),
        _commit("head-2", "tree-head-2"),
        _commit("merge-2", "tree-merge-2", parents=("merge-1", "head-2")),
        _commit("merge-1", "tree-merge-1"),
        _compare("merge-2"),
        _branch(*final_base),
    )


class _ReadOnlyTransport(RecordingMergeTrainGitHubTransport):
    def request(
        self,
        *,
        method: str,
        path: str,
        body: dict[str, object] | None = None,
    ) -> object:
        if method != "GET":
            raise AssertionError(f"historical preflight attempted provider {method}")
        return super().request(method=method, path=path, body=body)


class _StoreProxy:
    def __init__(self, store: FilesystemRecordStore) -> None:
        self.store = store

    def __getattr__(self, name: str) -> object:
        return getattr(self.store, name)


class _OrdinaryTargetStore(_StoreProxy):
    def has_ordinary_merge_train_target_fence(self, *, repository: str, base_branch: str) -> bool:
        return True


class _ChangingStore(_StoreProxy):
    def __init__(self, store: FilesystemRecordStore) -> None:
        super().__init__(store)
        self.controller_reads = 0

    def list_merge_train_controller_state_records(
        self,
        *,
        repository: str = "",
        base_branch: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[MergeTrainControllerStateRecord, ...]:
        self.controller_reads += 1
        records = self.store.list_merge_train_controller_state_records(
            repository=repository,
            base_branch=base_branch,
            status=status,
            limit=limit,
        )
        if self.controller_reads < 2:
            return records
        return tuple(
            record.model_copy(update={"last_transition_at": "changed-after-read"})
            for record in records
        )


class _AdmissionPresentStore(_StoreProxy):
    def list_merge_admission_records(self, **kwargs: object) -> tuple[object, ...]:
        return (object(),)


class _HistoricalCompletionFixture:
    def __init__(self, state_dir: Path) -> None:
        self.store = FilesystemRecordStore(state_dir=state_dir)
        self.policy_record = build_test_merge_train_policy_record(repository=REPOSITORY)
        self.store.write_merge_train_policy_record(self.policy_record)
        entries = (
            MergeTrainStructuralEntryBinding(
                position=1,
                pull_request_number=1,
                head_sha="head-1",
                head_tree_sha="tree-head-1",
                impact_status="known",
                affected_subjects=(MergeTrainStructuralSubject(product="acme", system="widget"),),
            ),
            MergeTrainStructuralEntryBinding(
                position=2,
                pull_request_number=2,
                head_sha="head-2",
                head_tree_sha="tree-head-2",
                impact_status="known",
                affected_subjects=(MergeTrainStructuralSubject(product="acme", system="widget"),),
            ),
        )
        provenance = MergeTrainStructuralProvenance(
            repository=REPOSITORY,
            base_branch=BASE_BRANCH,
            base_sha="pinned-base",
            base_tree_sha="tree-pinned-base",
            policy_key=self.policy_record.policy.policies[0].policy_key,
            policy_sha256=self.policy_record.policy_sha256,
            entries=entries,
            steps=(
                MergeTrainRollingStep(
                    position=1,
                    pull_request_number=1,
                    parent_sha="pinned-base",
                    parent_tree_sha="tree-pinned-base",
                    head_sha="head-1",
                    head_tree_sha="tree-head-1",
                    result_sha="merge-1",
                    result_tree_sha="tree-merge-1",
                    kind="merge_commit",
                ),
                MergeTrainRollingStep(
                    position=2,
                    pull_request_number=2,
                    parent_sha="merge-1",
                    parent_tree_sha="tree-merge-1",
                    head_sha="head-2",
                    head_tree_sha="tree-head-2",
                    result_sha="merge-2",
                    result_tree_sha="tree-merge-2",
                    kind="merge_commit",
                ),
            ),
            candidate_sha="merge-2",
            candidate_tree_sha="tree-merge-2",
        )
        candidate = MergeTrainBatchCandidate(
            batch_id="historical-batch",
            repository=REPOSITORY,
            base_branch=BASE_BRANCH,
            base_sha="pinned-base",
            policy_key=self.policy_record.policy.policies[0].policy_key,
            policy_sha256=self.policy_record.policy_sha256,
            candidate_ref="refs/heads/launchplane/train/acme/widget/historical-batch",
            candidate_sha="merge-2",
            candidate_tree_sha="tree-merge-2",
            candidate_sha256=provenance.candidate_sha256,
            status="passed",
            entries=tuple(
                MergeTrainBatchEntry(
                    pull_request_number=entry.pull_request_number,
                    position=entry.position,
                    head_sha=entry.head_sha,
                    head_tree_sha=entry.head_tree_sha,
                    impact_status=entry.impact_status,
                    affected_subjects=entry.affected_subjects,
                )
                for entry in entries
            ),
            structural_provenance=provenance,
            required_checks_status="pass",
            created_at=OBSERVED_AT,
            updated_at=OBSERVED_AT,
        )
        self.candidate_record = MergeTrainBatchCandidateRecord(
            record_id="historical-candidate-record",
            source="test:historical",
            updated_at=OBSERVED_AT,
            candidate=candidate,
        )
        self.store.write_merge_train_batch_candidate_record(self.candidate_record)
        self.landing_plan = build_merge_train_batch_landing_plan(
            candidate=candidate, merge_method="merge", created_at=OBSERVED_AT
        )
        self.landing_record = MergeTrainBatchLandingPlanRecord(
            record_id="historical-landing-record",
            source="test:historical",
            updated_at=OBSERVED_AT,
            landing_plan=self.landing_plan,
        )
        self.store.write_merge_train_batch_landing_plan_record(self.landing_record)
        self.controller = MergeTrainControllerStateRecord(
            controller_key=build_merge_train_controller_key(
                repository=REPOSITORY, base_branch=BASE_BRANCH
            ),
            repository=REPOSITORY,
            base_branch=BASE_BRANCH,
            policy_key=self.policy_record.policy.policies[0].policy_key,
            policy_sha256=self.policy_record.policy_sha256,
            status="reconcile_required",
            updated_at=OBSERVED_AT,
            active_action="land_batch",
            active_phase="merge_batch_entries",
            active_record_id=self.landing_record.record_id,
            step_payload={
                "landing_plan_record_id": self.landing_record.record_id,
                "landing_plan_id": self.landing_plan.plan_id,
                "expected_effect_sha": self.landing_plan.candidate_sha,
            },
            reconciliation_status="required",
            reconciliation_detail="legacy controller state",
        )
        self.store.write_merge_train_controller_state_record(self.controller)

    @property
    def selector(self) -> MergeTrainHistoricalCompletionSelector:
        return MergeTrainHistoricalCompletionSelector(
            expected_active_record_id=self.landing_record.record_id,
            expected_effect_sha=self.landing_plan.candidate_sha,
            expected_policy_sha256=self.policy_record.policy_sha256,
            expected_landing_plan_id=self.landing_plan.plan_id,
            expected_entries=tuple(
                MergeTrainHistoricalCompletionEntrySelector(
                    position=entry.position,
                    pull_request_number=entry.pull_request_number,
                    expected_head_sha=entry.expected_head_sha,
                    expected_head_tree_sha=entry.expected_head_tree_sha,
                )
                for entry in self.landing_plan.entries
            ),
        )

    def request_payload(self, *, mutate: bool = False) -> dict[str, object]:
        return {
            "repository": REPOSITORY,
            "base_branch": BASE_BRANCH,
            "mutate": mutate,
            "historical_completion": self.selector.model_dump(mode="json"),
        }


def _historical_completion_payload(payload: dict[str, object]) -> dict[str, object]:
    historical_completion = payload.get("historical_completion")
    if not isinstance(historical_completion, dict):
        raise AssertionError("request payload must contain a historical completion object")
    return historical_completion


def _assess(
    fixture: _HistoricalCompletionFixture,
    *,
    transport: RecordingMergeTrainGitHubTransport | None = None,
    store: object | None = None,
    selector: MergeTrainHistoricalCompletionSelector | None = None,
) -> MergeTrainHistoricalCompletionPreflight:

    resolved_transport = transport or _ReadOnlyTransport(responses=_provider_responses())
    return assess_merge_train_historical_completion(
        store=store or fixture.store,
        repository=REPOSITORY,
        base_branch=BASE_BRANCH,
        selector=selector or fixture.selector,
        generated_at=OBSERVED_AT,
        github_client=GitHubMergeTrainClient(transport=resolved_transport),
    )


def seed_crowded_scoped_history(
    store: FilesystemRecordStore | PostgresRecordStore,
    fixture: _HistoricalCompletionFixture,
    *,
    landing_count: int = 0,
    candidate_count: int = 0,
    stack_count: int = 0,
    candidate_batch_id: str = "",
    stack_root_pull_request_number: int | None = None,
) -> None:
    """Seed newer same-target records that exact scoped reads must ignore."""
    for index in range(landing_count):
        store.write_merge_train_batch_landing_plan_record(
            fixture.landing_record.model_copy(
                update={
                    "record_id": f"zz-crowded-landing-{index:03d}",
                    "updated_at": f"2026-09-13T13:{index // 60:02d}:{index % 60:02d}Z",
                }
            )
        )
    for index in range(candidate_count):
        batch_id = candidate_batch_id or f"zz-crowded-batch-{index:03d}"
        candidate_updates: dict[str, object] = {"batch_id": batch_id}
        if not candidate_batch_id:
            candidate_updates.update(
                {
                    "candidate_sha256": "",
                    "structural_provenance": None,
                    "status": "planned",
                    "required_checks_status": "unknown",
                }
            )
        candidate = fixture.candidate_record.candidate.model_copy(update=candidate_updates)
        candidate = MergeTrainBatchCandidate.model_validate(candidate.model_dump(mode="python"))
        store.write_merge_train_batch_candidate_record(
            fixture.candidate_record.model_copy(
                update={
                    "record_id": f"zz-crowded-candidate-{index:03d}",
                    "updated_at": f"2026-09-13T13:{index // 60:02d}:{index % 60:02d}Z",
                    "candidate": candidate,
                }
            )
        )
    for index in range(stack_count):
        root_number = stack_root_pull_request_number or 900 + index
        stack_plan = MergeTrainStackCollapsePlan(
            collapse_id=f"zz-crowded-collapse-{index:03d}",
            repository=fixture.landing_plan.repository,
            base_branch=fixture.landing_plan.base_branch,
            root_pull_request_number=root_number,
            root_initial_head_sha="zz-root-head",
            root_head_ref="refs/heads/zz-root",
            policy_key=fixture.landing_plan.policy_key,
            policy_sha256=fixture.landing_plan.policy_sha256,
            entries=(
                MergeTrainStackCollapseEntry(
                    pull_request_number=root_number,
                    position=1,
                    head_sha="zz-root-head",
                    head_ref="refs/heads/zz-root",
                    base_ref=fixture.landing_plan.base_branch,
                ),
                MergeTrainStackCollapseEntry(
                    pull_request_number=root_number + 1,
                    position=2,
                    head_sha="zz-child-head",
                    head_ref="refs/heads/zz-child",
                    base_ref=fixture.landing_plan.base_branch,
                ),
            ),
            mutations=(
                MergeTrainStackCollapseMutation(
                    child_pull_request_number=root_number + 1,
                    parent_pull_request_number=root_number,
                    child_head_sha="zz-child-head",
                    expected_parent_head_sha="zz-root-head",
                    parent_head_ref="refs/heads/zz-root",
                ),
            ),
            created_at=f"2026-09-13T13:{index // 60:02d}:{index % 60:02d}Z",
            updated_at=f"2026-09-13T13:{index // 60:02d}:{index % 60:02d}Z",
        )
        store.write_merge_train_stack_collapse_plan_record(
            MergeTrainStackCollapsePlanRecord(
                record_id=f"zz-crowded-stack-{index:03d}",
                source="test:scoped-history",
                updated_at=stack_plan.updated_at,
                plan=stack_plan,
            )
        )


class HistoricalCompletionCoreTests(unittest.TestCase):
    def test_eligible_preflight_is_read_only_and_contains_bound_provider_evidence(self) -> None:
        with TemporaryDirectory() as directory:
            fixture = _HistoricalCompletionFixture(Path(directory))
            before = {
                path.relative_to(directory): path.read_bytes()
                for path in Path(directory).rglob("*.json")
            }
            transport = _ReadOnlyTransport(responses=_provider_responses())
            result = _assess(fixture, transport=transport)
            after = {
                path.relative_to(directory): path.read_bytes()
                for path in Path(directory).rglob("*.json")
            }

        self.assertEqual(result.status, "eligible")
        self.assertTrue(result.evidence_eligible)
        self.assertEqual(result.reason_code, "exact_historical_completion")
        provider_evidence = result.provider_evidence
        self.assertIsNotNone(provider_evidence)
        assert provider_evidence is not None
        self.assertEqual(provider_evidence.entries[1].observed_merge_commit_sha, "merge-2")
        self.assertEqual(before, after)
        self.assertEqual(tuple(request.method for request in transport.requests), ("GET",) * 12)

    def test_crowded_unrelated_history_cannot_hide_selected_records(self) -> None:
        with TemporaryDirectory() as directory:
            fixture = _HistoricalCompletionFixture(Path(directory))
            seed_crowded_scoped_history(
                fixture.store,
                fixture,
                landing_count=101,
                candidate_count=101,
                stack_count=101,
            )
            transport = _ReadOnlyTransport(responses=_provider_responses())
            result = _assess(fixture, transport=transport)

        self.assertEqual(result.status, "eligible")

    def test_same_batch_candidate_history_overflow_fails_closed_before_provider(self) -> None:
        with TemporaryDirectory() as directory:
            fixture = _HistoricalCompletionFixture(Path(directory))
            seed_crowded_scoped_history(
                fixture.store,
                fixture,
                candidate_count=101,
                candidate_batch_id=fixture.candidate_record.candidate.batch_id,
            )
            transport = _ReadOnlyTransport(responses=_provider_responses())
            result = _assess(fixture, transport=transport)

        self.assertEqual(
            (result.status, result.reason_code),
            ("indeterminate", "candidate_history_limit_exceeded"),
        )
        self.assertEqual(transport.requests, [])

    def test_relevant_stack_root_blocks_before_provider(self) -> None:
        with TemporaryDirectory() as directory:
            fixture = _HistoricalCompletionFixture(Path(directory))
            seed_crowded_scoped_history(
                fixture.store,
                fixture,
                stack_count=1,
                stack_root_pull_request_number=1,
            )
            transport = _ReadOnlyTransport(responses=_provider_responses())
            result = _assess(fixture, transport=transport)

        self.assertEqual(
            (result.status, result.reason_code), ("unsupported", "stack_batch_unsupported")
        )
        self.assertEqual(transport.requests, [])

    def test_exact_landing_record_status_scope_and_identity_fail_before_provider(self) -> None:
        cases = ("inactive", "missing", "foreign")
        for case in cases:
            with self.subTest(case=case), TemporaryDirectory() as directory:
                fixture = _HistoricalCompletionFixture(Path(directory))
                if case == "inactive":
                    fixture.store.write_merge_train_batch_landing_plan_record(
                        fixture.landing_record.model_copy(update={"status": "superseded"})
                    )
                elif case == "missing":
                    next(
                        (Path(directory) / "launchplane_merge_train_batch_landing_plans").glob(
                            "*.json"
                        )
                    ).unlink()
                else:
                    foreign_payload = fixture.landing_plan.model_dump(mode="python")
                    foreign_payload["repository"] = "other/repository"
                    foreign_payload["landing_plan_sha256"] = ""
                    foreign_plan = MergeTrainBatchLandingPlan.model_validate(foreign_payload)
                    fixture.store.write_merge_train_batch_landing_plan_record(
                        fixture.landing_record.model_copy(update={"landing_plan": foreign_plan})
                    )
                transport = _ReadOnlyTransport(responses=_provider_responses())
                result = _assess(fixture, transport=transport)

            expected = (
                "landing_plan_binding_changed" if case == "inactive" else "landing_plan_unavailable"
            )
            self.assertEqual(
                (result.status, result.reason_code),
                ("unsupported" if case == "inactive" else "indeterminate", expected),
            )
            self.assertEqual(transport.requests, [])

    def test_assessment_normalizes_repository_and_base_branch(self) -> None:
        with TemporaryDirectory() as directory:
            fixture = _HistoricalCompletionFixture(Path(directory))
            transport = _ReadOnlyTransport(responses=_provider_responses())
            result = assess_merge_train_historical_completion(
                store=fixture.store,
                repository="  ACME/WIDGET  ",
                base_branch="  main  ",
                selector=fixture.selector,
                generated_at=OBSERVED_AT,
                github_client=GitHubMergeTrainClient(transport=transport),
            )

        self.assertEqual(result.status, "eligible")
        self.assertEqual((result.repository, result.base_branch), (REPOSITORY, BASE_BRANCH))

    def test_normalization_preserves_scoped_negative_reads(self) -> None:
        with TemporaryDirectory() as directory:
            admission_fixture = _HistoricalCompletionFixture(Path(directory) / "admission")
            admission_transport = _ReadOnlyTransport(responses=_provider_responses())
            admission = assess_merge_train_historical_completion(
                store=_AdmissionPresentStore(admission_fixture.store),
                repository="  ACME/WIDGET  ",
                base_branch="  main  ",
                selector=admission_fixture.selector,
                generated_at=OBSERVED_AT,
                github_client=GitHubMergeTrainClient(transport=admission_transport),
            )
            stack_fixture = _HistoricalCompletionFixture(Path(directory) / "stack")
            seed_crowded_scoped_history(
                stack_fixture.store,
                stack_fixture,
                stack_count=1,
                stack_root_pull_request_number=1,
            )
            stack_transport = _ReadOnlyTransport(responses=_provider_responses())
            stack = assess_merge_train_historical_completion(
                store=stack_fixture.store,
                repository="  ACME/WIDGET  ",
                base_branch="  main  ",
                selector=stack_fixture.selector,
                generated_at=OBSERVED_AT,
                github_client=GitHubMergeTrainClient(transport=stack_transport),
            )

        self.assertEqual(
            (admission.status, admission.reason_code), ("unsupported", "admission_present")
        )
        self.assertEqual(admission_transport.requests, [])
        self.assertEqual(
            (stack.status, stack.reason_code), ("unsupported", "stack_batch_unsupported")
        )
        self.assertEqual(stack_transport.requests, [])

    def test_empty_sha_prebuild_candidate_is_allowed_but_materialized_conflict_is_not(self) -> None:
        with TemporaryDirectory() as directory:
            fixture = _HistoricalCompletionFixture(Path(directory) / "prebuild")
            prebuild = fixture.candidate_record.candidate.model_copy(
                update={
                    "candidate_sha": "",
                    "candidate_tree_sha": "",
                    "candidate_sha256": "",
                    "status": "planned",
                    "required_checks_status": "unknown",
                    "structural_provenance": None,
                    "updated_at": "2026-09-13T11:59:00Z",
                }
            )
            fixture.store.write_merge_train_batch_candidate_record(
                fixture.candidate_record.model_copy(
                    update={"record_id": "prebuild-candidate", "candidate": prebuild}
                )
            )
            transport = _ReadOnlyTransport(responses=_provider_responses())
            result = _assess(fixture, transport=transport)
            self.assertEqual(result.status, "eligible")

            fixture = _HistoricalCompletionFixture(Path(directory) / "conflict")
            conflict = fixture.candidate_record.candidate.model_copy(
                update={
                    "candidate_sha": "conflicting-candidate-sha",
                    "candidate_tree_sha": "conflicting-candidate-tree",
                    "candidate_sha256": "conflicting-candidate-digest",
                    "structural_provenance": None,
                    "status": "planned",
                    "required_checks_status": "unknown",
                    "updated_at": "2026-09-13T13:00:00Z",
                }
            )
            fixture.store.write_merge_train_batch_candidate_record(
                fixture.candidate_record.model_copy(
                    update={"record_id": "conflicting-candidate", "candidate": conflict}
                )
            )
            transport = _ReadOnlyTransport(responses=_provider_responses())
            result = _assess(fixture, transport=transport)

        self.assertEqual(
            (result.status, result.reason_code), ("indeterminate", "candidate_ambiguous")
        )
        self.assertEqual(transport.requests, [])

    def test_selector_policy_and_ordinary_fence_refuse_before_provider(self) -> None:
        with TemporaryDirectory() as directory:
            fixture = _HistoricalCompletionFixture(Path(directory))
            cases = (
                (
                    "entry",
                    fixture.selector.model_copy(
                        update={
                            "expected_entries": (
                                fixture.selector.expected_entries[0].model_copy(
                                    update={"expected_head_sha": "moved-head"}
                                ),
                                fixture.selector.expected_entries[1],
                            )
                        }
                    ),
                    fixture.store,
                    "selector_mismatch",
                ),
                (
                    "policy",
                    fixture.selector.model_copy(update={"expected_policy_sha256": "f" * 64}),
                    fixture.store,
                    "policy_changed",
                ),
                (
                    "fence",
                    fixture.selector,
                    _OrdinaryTargetStore(fixture.store),
                    "ordinary_target_unsupported",
                ),
            )
            for name, selector, store, reason in cases:
                with self.subTest(name=name):
                    transport = _ReadOnlyTransport(responses=_provider_responses())
                    result = _assess(fixture, transport=transport, store=store, selector=selector)
                    self.assertEqual(result.status, "unsupported")
                    self.assertEqual(result.reason_code, reason)
                    self.assertEqual(transport.requests, [])

    def test_history_gaps_busy_admission_and_store_change_fail_closed(self) -> None:
        with TemporaryDirectory() as directory:
            fixture = _HistoricalCompletionFixture(Path(directory))
            fixture.store = FilesystemRecordStore(state_dir=Path(directory) / "missing")
            missing = _assess(fixture)
            self.assertEqual(missing.status, "indeterminate")
            self.assertEqual(missing.reason_code, "controller_unavailable")

            fixture = _HistoricalCompletionFixture(Path(directory) / "busy")
            busy = fixture.controller.model_copy(
                update={
                    "status": "running",
                    "lease_owner": "controller-owner",
                    "lease_acquired_at": OBSERVED_AT,
                    "lease_expires_at": "2026-09-13T12:05:00Z",
                    "heartbeat_at": OBSERVED_AT,
                }
            )
            fixture.store.write_merge_train_controller_state_record(busy)
            result = _assess(fixture)
            self.assertEqual(
                (result.status, result.reason_code), ("indeterminate", "controller_busy")
            )

            admission_fixture = _HistoricalCompletionFixture(Path(directory) / "admission")
            admission = _assess(
                admission_fixture, store=_AdmissionPresentStore(admission_fixture.store)
            )
            self.assertEqual(
                (admission.status, admission.reason_code), ("unsupported", "admission_present")
            )

            fixture = _HistoricalCompletionFixture(Path(directory) / "changed")
            changed_transport = _ReadOnlyTransport(responses=_provider_responses())
            changed = _assess(
                fixture, store=_ChangingStore(fixture.store), transport=changed_transport
            )
            self.assertEqual(
                (changed.status, changed.reason_code), ("indeterminate", "store_state_changed")
            )

    def test_provider_unmerged_and_unavailable_candidate_are_not_completion(self) -> None:
        with TemporaryDirectory() as directory:
            fixture = _HistoricalCompletionFixture(Path(directory) / "unmerged")
            responses = _provider_responses(
                first_pull_request=_pull_request(
                    1,
                    head_sha="head-1",
                    merge_commit_sha="merge-1",
                    state="open",
                    merged=False,
                )
            )
            result = _assess(
                fixture,
                transport=_ReadOnlyTransport(responses=responses),
            )
            self.assertEqual(
                (result.status, result.reason_code), ("unsupported", "pull_request_not_merged")
            )

            fixture = _HistoricalCompletionFixture(Path(directory) / "no-candidate")
            for record_path in (Path(directory) / "no-candidate").glob(
                "launchplane_merge_train_batch_candidates/*.json"
            ):
                record_path.unlink()
            unavailable = _assess(fixture)
            self.assertEqual(
                (unavailable.status, unavailable.reason_code),
                ("indeterminate", "candidate_unavailable"),
            )

            fixture = _HistoricalCompletionFixture(Path(directory) / "unreadable")
            candidate_path = next(
                (Path(directory) / "unreadable" / "launchplane_merge_train_batch_candidates").glob(
                    "*.json"
                )
            )
            candidate_path.write_text("{not-json", encoding="utf-8")
            unreadable = _assess(fixture)
            self.assertEqual(
                (unreadable.status, unreadable.reason_code), ("indeterminate", "store_unavailable")
            )

    def test_ordinary_core_rejects_selector_before_transport_or_lease(self) -> None:
        request = MergeTrainControllerRunOnceEnvelope(
            repository=REPOSITORY,
            historical_completion=MergeTrainHistoricalCompletionSelector(
                expected_active_record_id="record",
                expected_effect_sha="effect",
                expected_policy_sha256="policy",
                expected_landing_plan_id="plan",
                expected_entries=(
                    MergeTrainHistoricalCompletionEntrySelector(
                        position=1,
                        pull_request_number=1,
                        expected_head_sha="head",
                        expected_head_tree_sha="tree",
                    ),
                ),
            ),
        )
        transport = _ReadOnlyTransport()
        client = GitHubMergeTrainClient(transport=transport)
        with self.assertRaisesRegex(
            MergeTrainControllerRequestError,
            "historical_completion_requires_legacy_service_preflight",
        ):
            execute_merge_train_controller_with_client(
                request=request,
                policy=None,  # type: ignore[arg-type]
                policy_sha256="policy",
                repository_policy=None,  # type: ignore[arg-type]
                github_client=client,
                trace_id="trace",
                recorded_at=OBSERVED_AT,
                candidate_store=None,  # type: ignore[arg-type]
                landing_store=None,  # type: ignore[arg-type]
                stack_collapse_store=None,  # type: ignore[arg-type]
                controller_state_store=None,  # type: ignore[arg-type]
                admission_store=None,  # type: ignore[arg-type]
                admission_evaluator=None,  # type: ignore[arg-type]
            )
        self.assertEqual(transport.requests, [])


class HistoricalCompletionHttpTests(unittest.IsolatedAsyncioTestCase):
    async def _app(
        self,
        store: FilesystemRecordStore,
        *,
        identity: GitHubActionsIdentity | None = None,
    ) -> FastAPI:
        return create_launchplane_fastapi_app(
            verifier=_StubVerifier(identity or _merge_train_service_identity()),
            authz_policy=_merge_train_service_policy(),
            record_store_factory=lambda: store,
        )

    async def test_http_preflight_uses_real_provider_validator_and_preserves_store(self) -> None:
        with TemporaryDirectory() as directory:
            fixture = _HistoricalCompletionFixture(Path(directory))
            before = {
                path.relative_to(directory): path.read_bytes()
                for path in Path(directory).rglob("*.json")
            }
            transport = _ReadOnlyTransport(responses=_provider_responses())
            app = await self._app(fixture.store)
            with (
                patch.dict(os.environ, {"GH_TOKEN": "test-token"}, clear=False),
                patch(
                    "control_plane.http_app.UrllibMergeTrainGitHubTransport",
                    return_value=transport,
                ),
            ):
                response = await _post_merge_train_controller_run_once(
                    app, fixture.request_payload(), idempotency_key="historical-read-1"
                )
            after = {
                path.relative_to(directory): path.read_bytes()
                for path in Path(directory).rglob("*.json")
            }

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        preflight = payload["result"]["historical_completion_preflight"]
        self.assertEqual(preflight["status"], "eligible")
        self.assertTrue(preflight["evidence_eligible"])
        self.assertEqual(
            preflight["provider_evidence"]["entries"][1]["observed_merge_commit_sha"], "merge-2"
        )
        self.assertEqual(before, after)
        self.assertEqual(len(transport.requests), 12)
        self.assertTrue(all(request.method == "GET" for request in transport.requests))

    async def test_http_auth_denial_has_no_provider_call(self) -> None:
        with TemporaryDirectory() as directory:
            fixture = _HistoricalCompletionFixture(Path(directory))
            app = await self._app(
                fixture.store,
                identity=_identity(
                    repository="other/repository",
                    workflow_ref="other/repository/.github/workflows/other.yml@refs/heads/main",
                ),
            )
            with (
                patch.dict(os.environ, {"GH_TOKEN": "test-token"}, clear=False),
                patch(
                    "control_plane.http_app.UrllibMergeTrainGitHubTransport",
                    side_effect=AssertionError("denied request reached provider"),
                ),
            ):
                response = await _post_merge_train_controller_run_once(
                    app, fixture.request_payload()
                )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_http_mutation_selector_is_nonretryable_before_lease_or_provider(self) -> None:
        with TemporaryDirectory() as directory:
            fixture = _HistoricalCompletionFixture(Path(directory))
            app = await self._app(fixture.store)
            with (
                patch.dict(os.environ, {"GH_TOKEN": "test-token"}, clear=False),
                patch(
                    "control_plane.http_app.UrllibMergeTrainGitHubTransport",
                    side_effect=AssertionError("mutation selector reached provider"),
                ),
            ):
                response = await _post_merge_train_controller_run_once(
                    app, fixture.request_payload(mutate=True), idempotency_key="retry-key"
                )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()["error"]["code"], "historical_completion_recovery_not_enabled"
        )
        self.assertFalse(response.json()["details"]["retryable"])

    async def test_http_selector_mismatch_skips_provider_and_same_key_does_not_replay(self) -> None:
        with TemporaryDirectory() as directory:
            fixture = _HistoricalCompletionFixture(Path(directory))
            app = await self._app(fixture.store)
            mismatch_payload = fixture.request_payload()
            _historical_completion_payload(mismatch_payload)["expected_effect_sha"] = "other-effect"
            mismatch_transport = _ReadOnlyTransport(responses=_provider_responses())
            with (
                patch.dict(os.environ, {"GH_TOKEN": "test-token"}, clear=False),
                patch(
                    "control_plane.http_app.UrllibMergeTrainGitHubTransport",
                    return_value=mismatch_transport,
                ),
            ):
                mismatch = await _post_merge_train_controller_run_once(app, mismatch_payload)
            self.assertEqual(mismatch.status_code, 202)
            self.assertEqual(
                mismatch.json()["result"]["historical_completion_preflight"]["reason_code"],
                "selector_mismatch",
            )
            self.assertEqual(mismatch_transport.requests, [])

            replay_transport = _ReadOnlyTransport(
                responses=_provider_responses() + _provider_responses()
            )
            with (
                patch.dict(os.environ, {"GH_TOKEN": "test-token"}, clear=False),
                patch(
                    "control_plane.http_app.UrllibMergeTrainGitHubTransport",
                    return_value=replay_transport,
                ),
            ):
                first = await _post_merge_train_controller_run_once(
                    app, fixture.request_payload(), idempotency_key="same-preflight-key"
                )
                second = await _post_merge_train_controller_run_once(
                    app, fixture.request_payload(), idempotency_key="same-preflight-key"
                )
        self.assertEqual((first.status_code, second.status_code), (202, 202))
        self.assertEqual(len(replay_transport.requests), 24)
        self.assertEqual(
            first.json()["result"]["historical_completion_preflight"]["status"],
            "eligible",
        )
        self.assertEqual(
            second.json()["result"]["historical_completion_preflight"]["status"],
            "eligible",
        )


if __name__ == "__main__":
    unittest.main()
