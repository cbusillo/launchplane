from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
from control_plane.contracts.change_impact import (
    ChangeImpactChangedFileEvidence,
    ChangeImpactEvaluation,
    ChangeImpactRepositoryEvidence,
    ChangeImpactTarget,
    ChangeImpactTargetReference,
)
from control_plane.contracts.merge_train_structural_provenance import (
    MergeTrainStructuralEntryObservation,
)
from control_plane.contracts.merge_readiness import MergeReadinessOwnerFacet
from control_plane.contracts.owner_acceptance import (
    OwnerAcceptanceAction,
    OwnerAcceptanceDecision,
)
from control_plane.contracts.product_owner import (
    ProductOwnerGrant,
    ProductOwnerIdentity,
    ProductOwnerPolicyRecord,
)
from control_plane.merge_admission import MergeAdmissionDeniedError, MergeAdmissionEvaluation
from control_plane.merge_admission_live import LiveMergeAdmissionEvaluator
from control_plane.merge_train import (
    MergeTrainDryRunSnapshot,
    MergeTrainPullRequestSnapshot,
)
from control_plane.merge_train_github import RecordingMergeTrainGitHubTransport
from control_plane.owner_acceptance import (
    OwnerAcceptanceWriteResult,
    build_owner_acceptance_system_event,
    evaluate_owner_acceptance,
    record_owner_acceptance_event,
)
from control_plane.service_auth import LaunchplaneAuthzPolicy
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.tenant_admission_controller import (
    TenantAdmissionControllerGitHubClient,
    TenantAdmissionRequiredTechnicalCheck,
    TenantAdmissionTechnicalCheckSignal,
    TenantAdmissionTechnicalChecks,
)
from tests.merge_train_policy_fixtures import build_test_merge_train_policy_record
from tests.test_merge_admission_records import _guard_records
from tests.test_owner_acceptance import (
    BASE_SHA as OWNER_BASE_SHA,
    HEAD_SHA as OWNER_HEAD_SHA,
    OWNER_GITHUB_ID,
    REPOSITORY as OWNER_REPOSITORY,
    TREE_SHA as OWNER_TREE_SHA,
    _EvidenceProvider,
    _human,
    _impact_policy,
    _owner_policy,
    _owner_requirement,
    _preview_profile,
    _repository_evidence,
    _write_preview_evidence,
)
from tests.test_merge_readiness import (
    BASE_SHA,
    HEAD_SHA,
    POLICY_SHA,
    REPOSITORY,
    TREE_SHA,
    _evaluate,
    _owner_decision,
    _owner_product,
    _policy_fingerprints,
)


class _StaticSnapshotReader:
    def __init__(self, snapshot: MergeTrainDryRunSnapshot) -> None:
        self.snapshot = snapshot
        self.read_count = 0

    def read_merge_train_snapshot(
        self,
        *,
        repository: str,
        base_branch: str,
    ) -> MergeTrainDryRunSnapshot:
        self.read_count += 1
        if repository != self.snapshot.repository or base_branch != self.snapshot.base_branch:
            raise AssertionError("unexpected merge queue scope")
        return self.snapshot


class _UnusedRepositoryEvidenceProvider:
    def resolve(
        self,
        target: ChangeImpactTargetReference,
    ) -> ChangeImpactRepositoryEvidence:
        raise AssertionError(f"queue drift should fail before repository evidence: {target}")


class _TechnicalCheckClient(TenantAdmissionControllerGitHubClient):
    def __init__(self) -> None:
        super().__init__(transport=RecordingMergeTrainGitHubTransport())

    def read_technical_checks(
        self,
        *,
        repository: str,
        base_branch: str,
        base_sha: str,
        head_sha: str,
        evaluated_at: str,
    ) -> TenantAdmissionTechnicalChecks:
        return TenantAdmissionTechnicalChecks(
            head_sha=head_sha,
            base_sha=base_sha,
            strict=False,
            status="unavailable",
            evaluated_at=evaluated_at,
        )


class _PassingTechnicalCheckClient(TenantAdmissionControllerGitHubClient):
    def __init__(self) -> None:
        super().__init__(transport=RecordingMergeTrainGitHubTransport())

    def read_technical_checks(
        self,
        *,
        repository: str,
        base_branch: str,
        base_sha: str,
        head_sha: str,
        evaluated_at: str,
    ) -> TenantAdmissionTechnicalChecks:
        return TenantAdmissionTechnicalChecks(
            head_sha=head_sha,
            base_sha=base_sha,
            strict=False,
            status="pass",
            required_checks=(TenantAdmissionRequiredTechnicalCheck(name="ci-gate"),),
            signals=(
                TenantAdmissionTechnicalCheckSignal(
                    source="check_run",
                    name="ci-gate",
                    state="pass",
                ),
            ),
            evaluated_at=evaluated_at,
        )


class _EmptyEngineeringReviewStore:
    @staticmethod
    def list_engineering_review_run_records(**_filters: object) -> tuple[()]:
        return ()

    @staticmethod
    def list_engineering_review_decision_records(**_filters: object) -> tuple[()]:
        return ()

    @staticmethod
    def list_engineering_review_authority_records(**_filters: object) -> tuple[()]:
        return ()


@dataclass(frozen=True)
class _TestEntryEvidence:
    repository_evidence: ChangeImpactRepositoryEvidence
    impact: ChangeImpactEvaluation
    owner_decision: object
    observation: MergeTrainStructuralEntryObservation


def _queued_pull_request(
    *,
    number: int,
    head_sha: str,
    created_at: str,
) -> MergeTrainPullRequestSnapshot:
    return MergeTrainPullRequestSnapshot(
        number=number,
        created_at=created_at,
        labels=("ready-to-merge",),
        actor_role="repo_owner",
        head_sha=head_sha,
        base_sha=BASE_SHA,
        base_ref="main",
        mergeable="mergeable",
        required_checks_status="pass",
    )


def _authz_policy_record() -> LaunchplaneAuthzPolicyRecord:
    return LaunchplaneAuthzPolicyRecord(
        record_id="authz-policy-live-owner-tests",
        source="test",
        updated_at="2026-08-11T03:00:00Z",
        policy=LaunchplaneAuthzPolicy(),
    )


def _owner_repository_evidence(
    *,
    head_sha: str = OWNER_HEAD_SHA,
    tree_sha: str = OWNER_TREE_SHA,
) -> ChangeImpactRepositoryEvidence:
    evidence = _repository_evidence(head=head_sha)
    return evidence.model_copy(
        update={
            "target": evidence.target.model_copy(update={"tree_sha": tree_sha}),
        }
    )


def _seed_owner_store(
    root: Path,
    *,
    owner_policy: ProductOwnerPolicyRecord | None = None,
    include_owner_policy: bool = True,
    preview_enabled: bool = False,
) -> FilesystemRecordStore:
    store = FilesystemRecordStore(state_dir=root)
    store.write_change_impact_policy_record(_impact_policy())
    if include_owner_policy:
        store.write_product_owner_policy_record(owner_policy or _owner_policy())
    store.write_product_owner_requirement_record(_owner_requirement())
    store.write_product_profile_record(_preview_profile(enabled=preview_enabled))
    return store


def _record_owner_action(
    *,
    store: FilesystemRecordStore,
    provider: _EvidenceProvider,
    action: OwnerAcceptanceAction,
    source_event_id: str,
    reason: str = "",
) -> OwnerAcceptanceWriteResult:
    decision = evaluate_owner_acceptance(
        store=store,
        repository_evidence_provider=provider,
        target=ChangeImpactTargetReference(
            repository=OWNER_REPOSITORY,
            pull_request_number=2022,
        ),
        evaluated_at="2026-08-11T03:00:00Z",
    )
    if decision.binding is None:
        raise AssertionError("Expected a current Owner acceptance binding")
    return record_owner_acceptance_event(
        store=store,
        repository_evidence_provider=provider,
        target=ChangeImpactTargetReference(
            repository=OWNER_REPOSITORY,
            pull_request_number=2022,
        ),
        identity=_human(),
        action=action,
        expected_binding_sha256=decision.binding.binding_sha256,
        source_event_kind="browser_api",
        source_event_id=source_event_id,
        reason=reason,
        occurred_at="2026-08-11T03:00:00Z",
    )


def _evaluate_owner_live(
    *,
    store: FilesystemRecordStore,
    provider: _EvidenceProvider,
    evidence: ChangeImpactRepositoryEvidence,
) -> MergeAdmissionEvaluation:
    policy_record = build_test_merge_train_policy_record(repository=OWNER_REPOSITORY)
    candidate_record, landing_record, controller_state, _ = _guard_records(
        policy_sha256=policy_record.policy_sha256,
        repository=OWNER_REPOSITORY,
        pull_request_number=2022,
        base_sha=OWNER_BASE_SHA,
        head_sha=evidence.target.head_sha,
        tree_sha=evidence.target.tree_sha,
    )
    evaluator = LiveMergeAdmissionEvaluator(
        store=store,
        repository_evidence_provider=provider,
        technical_check_client=_PassingTechnicalCheckClient(),
        policy_record_provider=lambda: policy_record,
        snapshot_reader=_StaticSnapshotReader(
            MergeTrainDryRunSnapshot(
                repository=OWNER_REPOSITORY,
                base_branch="main",
                base_sha=OWNER_BASE_SHA,
                pull_requests=(
                    _queued_pull_request(
                        number=2022,
                        head_sha=evidence.target.head_sha,
                        created_at="2026-08-11T03:00:00Z",
                    ),
                ),
            )
        ),
    )
    with (
        patch(
            "control_plane.merge_admission_live.read_active_authz_policy_record",
            return_value=_authz_policy_record(),
        ),
        patch(
            "control_plane.merge_admission_live.require_engineering_review_decision_store",
            return_value=_EmptyEngineeringReviewStore(),
        ),
    ):
        return evaluator.evaluate(
            candidate_record=candidate_record,
            landing_plan_record=landing_record,
            entry=landing_record.landing_plan.entries[0],
            observed_base_sha=OWNER_BASE_SHA,
            observed_base_tree_sha="5" * 40,
            observed_head_sha=evidence.target.head_sha,
            observed_head_tree_sha=evidence.target.tree_sha,
            controller_state=controller_state,
            expected_lease_owner=controller_state.lease_owner,
            stack_collapse_record=None,
            evaluated_at="2026-08-11T03:01:00Z",
        )


def _owner_facet(evaluation: MergeAdmissionEvaluation) -> MergeReadinessOwnerFacet:
    if len(evaluation.readiness.owner_facets) != 1:
        raise AssertionError("Expected one Owner readiness facet")
    return evaluation.readiness.owner_facets[0]


class LiveMergeAdmissionRealStoreTests(unittest.TestCase):
    def test_pending_changes_requested_revoked_and_stale_owner_states(self) -> None:
        scenarios = ("pending", "changes_requested", "revoked", "stale")
        expected_reasons = {
            "pending": "owner_acceptance_missing",
            "changes_requested": "owner_changes_requested",
            "revoked": "owner_acceptance_revoked",
            "stale": "owner_acceptance_stale",
        }
        for scenario in scenarios:
            with self.subTest(scenario=scenario), TemporaryDirectory() as directory:
                store = _seed_owner_store(Path(directory))
                evidence = _owner_repository_evidence()
                provider = _EvidenceProvider(evidence)
                if scenario == "changes_requested":
                    _record_owner_action(
                        store=store,
                        provider=provider,
                        action="changes_requested",
                        source_event_id="changes-requested",
                        reason="Owner requested a product correction.",
                    )
                elif scenario in {"revoked", "stale"}:
                    accepted = _record_owner_action(
                        store=store,
                        provider=provider,
                        action="accepted",
                        source_event_id=f"accepted-before-{scenario}",
                    )
                    if scenario == "revoked":
                        _record_owner_action(
                            store=store,
                            provider=provider,
                            action="revoked",
                            source_event_id="revoked",
                            reason="Owner withdrew product acceptance.",
                        )
                    else:
                        store.write_owner_acceptance_event_record(
                            build_owner_acceptance_system_event(
                                binding=accepted.record.binding,
                                action="invalidated",
                                occurred_at="2026-08-11T03:00:30Z",
                                source_event_id="invalidate-accepted-binding",
                                reason="Current evidence invalidated the prior acceptance.",
                            )
                        )

                evaluation = _evaluate_owner_live(
                    store=store,
                    provider=provider,
                    evidence=evidence,
                )
                facet = _owner_facet(evaluation)

                self.assertEqual(facet.owner_status, scenario)
                self.assertEqual(facet.state, "blocked_owner_evidence")
                self.assertIn(expected_reasons[scenario], facet.reason_codes)
                self.assertNotEqual(evaluation.readiness.state, "ready")
                self.assertIn(expected_reasons[scenario], evaluation.readiness.reason_codes)

    def test_denied_and_unavailable_owner_authority_are_fail_closed(self) -> None:
        scenarios = {
            "denied": (
                _owner_policy(
                    owners=(
                        ProductOwnerGrant(
                            identity=ProductOwnerIdentity(
                                provider="github",
                                provider_subject_id=str(OWNER_GITHUB_ID),
                            ),
                            repository_ids=("9999",),
                            environments=("pull_request",),
                        ),
                    )
                ),
                True,
                "owner_authority_denied",
            ),
            "unavailable": (None, False, "owner_authority_unavailable"),
        }
        for scenario, (owner_policy, include_owner_policy, reason_code) in scenarios.items():
            with self.subTest(scenario=scenario), TemporaryDirectory() as directory:
                store = _seed_owner_store(
                    Path(directory),
                    owner_policy=owner_policy,
                    include_owner_policy=include_owner_policy,
                )
                evidence = _owner_repository_evidence()
                evaluation = _evaluate_owner_live(
                    store=store,
                    provider=_EvidenceProvider(evidence),
                    evidence=evidence,
                )
                facet = _owner_facet(evaluation)

                self.assertEqual(facet.owner_status, "unavailable")
                self.assertEqual(facet.owner_reason_code, reason_code)
                self.assertEqual(facet.state, "blocked_owner_evidence")
                self.assertIn(reason_code, facet.reason_codes)
                self.assertNotEqual(evaluation.readiness.state, "ready")
                self.assertIn(reason_code, evaluation.readiness.reason_codes)
                self.assertEqual(facet.event_id, "")

    def test_accepted_exact_binding_is_live_and_admissible(self) -> None:
        with TemporaryDirectory() as directory:
            store = _seed_owner_store(Path(directory))
            evidence = _owner_repository_evidence()
            provider = _EvidenceProvider(evidence)
            accepted = _record_owner_action(
                store=store,
                provider=provider,
                action="accepted",
                source_event_id="accepted-exact-binding",
            )
            evaluation = _evaluate_owner_live(
                store=store,
                provider=provider,
                evidence=evidence,
            )
            facet = _owner_facet(evaluation)

        self.assertEqual(facet.owner_status, "accepted")
        self.assertEqual(facet.owner_reason_code, "acceptance_valid")
        self.assertEqual(facet.state, "ready")
        self.assertEqual(facet.event_id, accepted.record.event_id)
        self.assertEqual(facet.binding_sha256, accepted.record.binding.binding_sha256)
        self.assertEqual(evaluation.structural_result.status, "exact")
        self.assertEqual(evaluation.readiness.state, "ready")

    def test_head_and_tree_drift_stale_persisted_acceptance(self) -> None:
        drifted_evidence = {
            "head": _owner_repository_evidence(head_sha="c" * 40),
            "tree": _owner_repository_evidence(tree_sha="d" * 40),
        }
        for drift_kind, current_evidence in drifted_evidence.items():
            with self.subTest(drift=drift_kind), TemporaryDirectory() as directory:
                store = _seed_owner_store(Path(directory))
                provider = _EvidenceProvider(_owner_repository_evidence())
                accepted = _record_owner_action(
                    store=store,
                    provider=provider,
                    action="accepted",
                    source_event_id=f"accepted-before-{drift_kind}-drift",
                )
                provider.evidence = current_evidence
                evaluation = _evaluate_owner_live(
                    store=store,
                    provider=provider,
                    evidence=current_evidence,
                )
                facet = _owner_facet(evaluation)

                self.assertEqual(facet.owner_status, "stale")
                self.assertEqual(facet.owner_reason_code, "acceptance_stale")
                self.assertIn("owner_acceptance_stale", facet.reason_codes)
                self.assertEqual(facet.event_id, accepted.record.event_id)
                self.assertNotEqual(
                    facet.binding_sha256,
                    accepted.record.binding.binding_sha256,
                )
                self.assertNotEqual(evaluation.readiness.state, "ready")
                self.assertIn("owner_acceptance_stale", evaluation.readiness.reason_codes)

    def test_preview_generation_drift_stales_persisted_acceptance(self) -> None:
        with TemporaryDirectory() as directory:
            store = _seed_owner_store(Path(directory), preview_enabled=True)
            evidence = _owner_repository_evidence()
            provider = _EvidenceProvider(evidence)
            _write_preview_evidence(store)
            accepted = _record_owner_action(
                store=store,
                provider=provider,
                action="accepted",
                source_event_id="accepted-preview-generation-one",
            )
            accepted_evaluation = _evaluate_owner_live(
                store=store,
                provider=provider,
                evidence=evidence,
            )
            accepted_facet = _owner_facet(accepted_evaluation)
            self.assertEqual(accepted_facet.state, "ready")
            self.assertEqual(accepted_evaluation.readiness.state, "ready")
            self.assertIsNotNone(accepted.record.binding.preview)
            _write_preview_evidence(
                store,
                generation_id="preview-generic-web-a-pr-2022-generation-0002",
                artifact_id="artifact-generic-web-a-pr-2022-v2",
                image_digest="c" * 64,
            )
            evaluation = _evaluate_owner_live(
                store=store,
                provider=provider,
                evidence=evidence,
            )
            facet = _owner_facet(evaluation)

        self.assertEqual(facet.owner_status, "stale")
        self.assertEqual(facet.owner_reason_code, "acceptance_stale")
        self.assertEqual(facet.event_id, accepted.record.event_id)
        self.assertNotEqual(facet.binding_sha256, accepted.record.binding.binding_sha256)
        self.assertNotEqual(evaluation.readiness.state, "ready")
        self.assertIn("owner_acceptance_stale", evaluation.readiness.reason_codes)

    def test_owner_policy_and_requirement_drift_stale_acceptance(self) -> None:
        for drift_kind in ("policy", "requirement"):
            with self.subTest(drift=drift_kind), TemporaryDirectory() as directory:
                store = _seed_owner_store(Path(directory))
                evidence = _owner_repository_evidence()
                provider = _EvidenceProvider(evidence)
                accepted = _record_owner_action(
                    store=store,
                    provider=provider,
                    action="accepted",
                    source_event_id=f"accepted-before-{drift_kind}-drift",
                )
                if drift_kind == "policy":
                    current_policy = store.list_product_owner_policy_records(status="active")[0]
                    store.compare_and_write_product_owner_policy_record(
                        _owner_policy(
                            revision=2,
                            supersedes_record_id=current_policy.record_id,
                        ),
                        expected_current_record_id=current_policy.record_id,
                        expected_current_policy_digest=current_policy.policy_digest,
                    )
                else:
                    current_requirement = store.list_product_owner_requirement_records(
                        status="active"
                    )[0]
                    store.compare_and_write_product_owner_requirement_record(
                        _owner_requirement(
                            revision=2,
                            supersedes_record_id=current_requirement.record_id,
                        ),
                        expected_current_record_id=current_requirement.record_id,
                        expected_current_requirement_digest=(
                            current_requirement.requirement_digest
                        ),
                    )
                evaluation = _evaluate_owner_live(
                    store=store,
                    provider=provider,
                    evidence=evidence,
                )
                facet = _owner_facet(evaluation)

                self.assertEqual(facet.owner_status, "stale")
                self.assertEqual(facet.owner_reason_code, "acceptance_stale")
                self.assertEqual(facet.event_id, accepted.record.event_id)
                self.assertNotEqual(
                    facet.binding_sha256,
                    accepted.record.binding.binding_sha256,
                )
                self.assertNotEqual(evaluation.readiness.state, "ready")
                self.assertIn("owner_acceptance_stale", evaluation.readiness.reason_codes)


class LiveMergeAdmissionEvaluatorTests(unittest.TestCase):
    def test_engineering_only_change_binds_current_impact_policy(self) -> None:
        target = ChangeImpactTarget(
            repository_id="101",
            repository_owner_id="202",
            repository=REPOSITORY,
            pull_request_number=2083,
            head_sha=HEAD_SHA,
            tree_sha=TREE_SHA,
        )
        impact = ChangeImpactEvaluation(
            status="success",
            reason_code="change_impact_classified",
            target=target,
            policy_digest=POLICY_SHA,
        )
        evaluator = LiveMergeAdmissionEvaluator(
            store=object(),
            repository_evidence_provider=_UnusedRepositoryEvidenceProvider(),
            technical_check_client=_TechnicalCheckClient(),
        )

        fingerprints = evaluator._policy_fingerprints(
            impact=impact,
            owner_decision=OwnerAcceptanceDecision(
                status="not_required",
                reason_code="engineering_only",
                evaluated_at="2026-08-11T03:01:00Z",
            ),
            engineering_decision=None,
            engineering_authority_digest=None,
            technical_checks=_PassingTechnicalCheckClient().read_technical_checks(
                repository=REPOSITORY,
                base_branch="main",
                base_sha=BASE_SHA,
                head_sha=HEAD_SHA,
                evaluated_at="2026-08-11T03:01:00Z",
            ),
            landing_policy_sha256=POLICY_SHA,
            current_merge_train_policy_sha256=POLICY_SHA,
        )

        self.assertEqual(fingerprints.impact.expected_sha256, POLICY_SHA)
        self.assertEqual(fingerprints.impact.current_sha256, POLICY_SHA)

    def test_missing_owner_evidence_does_not_adopt_current_impact_policy(self) -> None:
        target = ChangeImpactTarget(
            repository_id="101",
            repository_owner_id="202",
            repository=REPOSITORY,
            pull_request_number=2083,
            head_sha=HEAD_SHA,
            tree_sha=TREE_SHA,
        )
        evaluator = LiveMergeAdmissionEvaluator(
            store=object(),
            repository_evidence_provider=_UnusedRepositoryEvidenceProvider(),
            technical_check_client=_TechnicalCheckClient(),
        )

        fingerprints = evaluator._policy_fingerprints(
            impact=ChangeImpactEvaluation(
                status="success",
                reason_code="change_impact_classified",
                target=target,
                policy_digest=POLICY_SHA,
            ),
            owner_decision=OwnerAcceptanceDecision(
                status="pending",
                reason_code="acceptance_missing",
                evaluated_at="2026-08-11T03:01:00Z",
            ),
            engineering_decision=None,
            engineering_authority_digest=None,
            technical_checks=_PassingTechnicalCheckClient().read_technical_checks(
                repository=REPOSITORY,
                base_branch="main",
                base_sha=BASE_SHA,
                head_sha=HEAD_SHA,
                evaluated_at="2026-08-11T03:01:00Z",
            ),
            landing_policy_sha256=POLICY_SHA,
            current_merge_train_policy_sha256=POLICY_SHA,
        )

        self.assertNotEqual(fingerprints.impact.expected_sha256, POLICY_SHA)
        self.assertEqual(fingerprints.impact.current_sha256, POLICY_SHA)

    def test_repository_policy_controls_engineering_review_authority(self) -> None:
        candidate_record, landing_record, controller_state, structural_result = _guard_records()
        snapshot_reader = _StaticSnapshotReader(
            MergeTrainDryRunSnapshot(
                repository=REPOSITORY,
                base_branch="main",
                base_sha=BASE_SHA,
                pull_requests=(
                    _queued_pull_request(
                        number=2083,
                        head_sha=HEAD_SHA,
                        created_at="2026-08-11T03:00:00Z",
                    ),
                ),
            )
        )
        target = ChangeImpactTarget(
            repository_id="101",
            repository_owner_id="202",
            repository=REPOSITORY,
            pull_request_number=2083,
            head_sha=HEAD_SHA,
            tree_sha=TREE_SHA,
        )
        entry_evidence = _TestEntryEvidence(
            repository_evidence=ChangeImpactRepositoryEvidence(
                target=target,
                changed_files=(ChangeImpactChangedFileEvidence(path="control_plane/example.py"),),
            ),
            impact=ChangeImpactEvaluation(
                status="success",
                reason_code="test_evidence_resolved",
                target=target,
            ),
            owner_decision=_owner_decision(_owner_product()),
            observation=MergeTrainStructuralEntryObservation(
                position=1,
                pull_request_number=2083,
                head_sha=HEAD_SHA,
                head_tree_sha=TREE_SHA,
            ),
        )
        engineering_store = _EmptyEngineeringReviewStore()

        for mode in ("advisory", "required"):
            with self.subTest(mode=mode):
                captured: dict[str, object] = {}

                def evaluate_readiness(**kwargs: object):  # type: ignore[no-untyped-def]
                    captured.update(kwargs)
                    return _evaluate(engineering_review_authority=mode)

                evaluator = LiveMergeAdmissionEvaluator(
                    store=object(),
                    repository_evidence_provider=_UnusedRepositoryEvidenceProvider(),
                    technical_check_client=_TechnicalCheckClient(),
                    policy_record_provider=lambda: build_test_merge_train_policy_record(
                        repository=REPOSITORY,
                        engineering_review_mode=mode,
                    ),
                    snapshot_reader=snapshot_reader,
                )
                with (
                    patch.object(
                        LiveMergeAdmissionEvaluator,
                        "_entry_evidence",
                        return_value=entry_evidence,
                    ),
                    patch.object(
                        LiveMergeAdmissionEvaluator,
                        "_combined_owner_review",
                        return_value=None,
                    ),
                    patch.object(
                        LiveMergeAdmissionEvaluator,
                        "_policy_fingerprints",
                        return_value=_policy_fingerprints(),
                    ),
                    patch(
                        "control_plane.merge_admission_live.evaluate_merge_train_structural_candidate",
                        return_value=structural_result,
                    ),
                    patch(
                        "control_plane.merge_admission_live.require_engineering_review_decision_store",
                        return_value=engineering_store,
                    ),
                    patch(
                        "control_plane.merge_admission_live.evaluate_merge_readiness_from_live_evidence",
                        side_effect=evaluate_readiness,
                    ),
                ):
                    evaluator.evaluate(
                        candidate_record=candidate_record,
                        landing_plan_record=landing_record,
                        entry=landing_record.landing_plan.entries[0],
                        observed_base_sha=BASE_SHA,
                        observed_base_tree_sha="5" * 40,
                        observed_head_sha=HEAD_SHA,
                        observed_head_tree_sha=TREE_SHA,
                        controller_state=controller_state,
                        expected_lease_owner=controller_state.lease_owner,
                        stack_collapse_record=None,
                        evaluated_at="2026-08-11T03:01:00Z",
                    )

                self.assertEqual(captured["engineering_review_authority"], mode)

    def test_live_queue_is_rediscovered_and_inserted_pr_refuses_admission(self) -> None:
        candidate_record, landing_record, controller_state, _ = _guard_records()
        snapshot_reader = _StaticSnapshotReader(
            MergeTrainDryRunSnapshot(
                repository=REPOSITORY,
                base_branch="main",
                base_sha=BASE_SHA,
                pull_requests=(
                    _queued_pull_request(
                        number=2082,
                        head_sha="d" * 40,
                        created_at="2026-08-11T02:59:00Z",
                    ),
                    _queued_pull_request(
                        number=2083,
                        head_sha=HEAD_SHA,
                        created_at="2026-08-11T03:00:00Z",
                    ),
                ),
            )
        )
        policy_reads = 0

        def read_policy():  # type: ignore[no-untyped-def]
            nonlocal policy_reads
            policy_reads += 1
            return build_test_merge_train_policy_record(repository=REPOSITORY)

        evaluator = LiveMergeAdmissionEvaluator(
            store=object(),
            repository_evidence_provider=_UnusedRepositoryEvidenceProvider(),
            technical_check_client=TenantAdmissionControllerGitHubClient(
                transport=RecordingMergeTrainGitHubTransport()
            ),
            policy_record_provider=read_policy,
            snapshot_reader=snapshot_reader,
        )
        entry = landing_record.landing_plan.entries[0]

        for _ in range(2):
            with self.assertRaisesRegex(MergeAdmissionDeniedError, "Live merge queue"):
                evaluator.evaluate(
                    candidate_record=candidate_record,
                    landing_plan_record=landing_record,
                    entry=entry,
                    observed_base_sha=BASE_SHA,
                    observed_base_tree_sha="4" * 40,
                    observed_head_sha=HEAD_SHA,
                    observed_head_tree_sha="2" * 40,
                    controller_state=controller_state,
                    expected_lease_owner=controller_state.lease_owner,
                    stack_collapse_record=None,
                    evaluated_at="2026-08-11T03:01:00Z",
                )

        self.assertEqual(policy_reads, 2)
        self.assertEqual(snapshot_reader.read_count, 2)

    def test_active_policy_removal_is_rediscovered_and_refuses_admission(self) -> None:
        candidate_record, landing_record, controller_state, _ = _guard_records()
        snapshot_reader = _StaticSnapshotReader(
            MergeTrainDryRunSnapshot(
                repository=REPOSITORY,
                base_branch="main",
                base_sha=BASE_SHA,
                pull_requests=(
                    _queued_pull_request(
                        number=2083,
                        head_sha=HEAD_SHA,
                        created_at="2026-08-11T03:00:00Z",
                    ),
                ),
            )
        )
        evaluator = LiveMergeAdmissionEvaluator(
            store=object(),
            repository_evidence_provider=_UnusedRepositoryEvidenceProvider(),
            technical_check_client=TenantAdmissionControllerGitHubClient(
                transport=RecordingMergeTrainGitHubTransport()
            ),
            policy_record_provider=lambda: build_test_merge_train_policy_record(
                repository="example/other-repository"
            ),
            snapshot_reader=snapshot_reader,
        )

        with self.assertRaisesRegex(MergeAdmissionDeniedError, "Active merge-train policy"):
            evaluator.evaluate(
                candidate_record=candidate_record,
                landing_plan_record=landing_record,
                entry=landing_record.landing_plan.entries[0],
                observed_base_sha=BASE_SHA,
                observed_base_tree_sha="4" * 40,
                observed_head_sha=HEAD_SHA,
                observed_head_tree_sha="2" * 40,
                controller_state=controller_state,
                expected_lease_owner=controller_state.lease_owner,
                stack_collapse_record=None,
                evaluated_at="2026-08-11T03:01:00Z",
            )

        self.assertEqual(snapshot_reader.read_count, 1)


if __name__ == "__main__":
    unittest.main()
