from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from control_plane.change_impact_service import ChangeImpactRepositoryEvidenceProvider
from control_plane.contracts.change_impact import (
    ChangeImpactChangedFileEvidence,
    ChangeImpactComponentRule,
    ChangeImpactPolicyRecord,
    ChangeImpactProductScope,
    ChangeImpactRepositoryEvidence,
    ChangeImpactStoredEvidence,
    ChangeImpactTarget,
    ChangeImpactTargetReference,
)
from control_plane.contracts.owner_acceptance import OwnerAcceptanceEventRecord
from control_plane.contracts.product_owner import (
    ProductOwnerGrant,
    ProductOwnerIdentity,
    ProductOwnerPolicyRecord,
    ProductOwnerRequirement,
    ProductOwnerRequirementRecord,
)
from control_plane.owner_acceptance import (
    OwnerAcceptanceAuthorizationError,
    OwnerAcceptanceBindingConflictError,
    OwnerAcceptanceEventConflictError,
    evaluate_owner_acceptance,
    record_owner_acceptance_event,
)
from control_plane.service_auth import GitHubHumanIdentity, TerminalAgentIdentity
from control_plane.storage.filesystem import FilesystemRecordStore


REPOSITORY_ID = "1001"
REPOSITORY_OWNER_ID = "2001"
REPOSITORY = "example/web"
PRODUCT = "generic-web-a"
SECOND_PRODUCT = "generic-web-b"
SYSTEM = "web"
OWNER_GITHUB_ID = 3001
HEAD_SHA = "a" * 40
TREE_SHA = "b" * 40


class _EvidenceProvider(ChangeImpactRepositoryEvidenceProvider):
    def __init__(self, evidence: ChangeImpactRepositoryEvidence) -> None:
        self.evidence = evidence

    def resolve(self, _target: ChangeImpactTargetReference) -> ChangeImpactRepositoryEvidence:
        return self.evidence


def _human(github_id: int = OWNER_GITHUB_ID) -> GitHubHumanIdentity:
    return GitHubHumanIdentity(
        login="owner",
        github_id=github_id,
        name="Owner",
        email="",
        organizations=frozenset(),
        teams=frozenset(),
        role="admin",
    )


def _repository_evidence(
    *, path: str = "src/runtime/app.py", head: str = HEAD_SHA
) -> ChangeImpactRepositoryEvidence:
    return ChangeImpactRepositoryEvidence(
        target=ChangeImpactTarget(
            repository_id=REPOSITORY_ID,
            repository_owner_id=REPOSITORY_OWNER_ID,
            repository=REPOSITORY,
            pull_request_number=2022,
            head_sha=head,
            tree_sha=TREE_SHA,
        ),
        changed_files=(ChangeImpactChangedFileEvidence(path=path),),
    )


def _impact_policy() -> ChangeImpactPolicyRecord:
    return ChangeImpactPolicyRecord(
        repository_id=REPOSITORY_ID,
        repository_owner_id=REPOSITORY_OWNER_ID,
        repository=REPOSITORY,
        policy_revision=1,
        component_rules=(
            ChangeImpactComponentRule(
                component="runtime",
                path_prefixes=("src/runtime",),
                affected_products=(ChangeImpactProductScope(product=PRODUCT, system=SYSTEM),),
                review_tier="routine",
                reason="Runtime paths affect the product.",
            ),
            ChangeImpactComponentRule(
                component="engineering",
                path_prefixes=("control_plane",),
                affected_products=(),
                review_tier="sensitive",
                reason="Control plane only.",
            ),
            ChangeImpactComponentRule(
                component="shared-runtime",
                path_prefixes=("src/shared",),
                affected_products=(
                    ChangeImpactProductScope(product=PRODUCT, system=SYSTEM),
                    ChangeImpactProductScope(product=SECOND_PRODUCT, system=SYSTEM),
                ),
                review_tier="routine",
                reason="Shared runtime affects two products.",
            ),
        ),
        effective_at="2026-08-07T00:00:00Z",
        source="test",
        reason="Owner acceptance policy.",
    )


def _owner_policy(
    *,
    revision: int = 1,
    supersedes_record_id: str | None = None,
) -> ProductOwnerPolicyRecord:
    return ProductOwnerPolicyRecord(
        product=PRODUCT,
        system=SYSTEM,
        policy_revision=revision,
        owners=(
            ProductOwnerGrant(
                identity=ProductOwnerIdentity(
                    provider="github", provider_subject_id=str(OWNER_GITHUB_ID)
                ),
                repository_ids=(REPOSITORY_ID,),
                environments=("pull_request",),
            ),
        ),
        effective_at=f"2026-08-07T0{revision - 1}:00:00Z",
        source="test",
        reason="Current human Owner.",
        supersedes_record_id=supersedes_record_id,
    )


def _owner_requirement(
    *,
    revision: int = 1,
    supersedes_record_id: str | None = None,
) -> ProductOwnerRequirementRecord:
    return ProductOwnerRequirementRecord(
        product=PRODUCT,
        system=SYSTEM,
        requirement_revision=revision,
        requirements=(
            ProductOwnerRequirement(
                action="pull_request.owner_acceptance",
                repository_ids=(REPOSITORY_ID,),
                environments=("pull_request",),
            ),
        ),
        effective_at=f"2026-08-07T0{revision - 1}:00:00Z",
        source="test",
        reason="Require Owner acceptance for product pull requests.",
        supersedes_record_id=supersedes_record_id,
    )


def _store(
    root: Path,
    *,
    include_dependency_evidence: bool = True,
    shared_dependency_evidence: bool = False,
) -> FilesystemRecordStore:
    class _OwnerAcceptanceStore(FilesystemRecordStore):
        def list_change_impact_stored_evidence(
            self,
            *,
            repository_id: str,
            pull_request_number: int,
            head_sha: str,
            tree_sha: str,
        ) -> tuple[ChangeImpactStoredEvidence, ...]:
            if not include_dependency_evidence:
                return ()
            if shared_dependency_evidence:
                return (
                    ChangeImpactStoredEvidence(
                        record_id="dependency-shared",
                        component="shared-runtime",
                        affected_products=(
                            ChangeImpactProductScope(product=PRODUCT, system=SYSTEM),
                            ChangeImpactProductScope(product=SECOND_PRODUCT, system=SYSTEM),
                        ),
                        kind="dependency",
                        confidence="known",
                        reason="Trusted dependency evidence binds shared runtime to both products.",
                    ),
                )
            return (
                ChangeImpactStoredEvidence(
                    record_id="dependency-runtime",
                    component="runtime",
                    affected_products=(ChangeImpactProductScope(product=PRODUCT, system=SYSTEM),),
                    kind="dependency",
                    confidence="known",
                    reason="Trusted dependency evidence binds runtime to the product.",
                ),
            )

    store = _OwnerAcceptanceStore(root)
    store.write_change_impact_policy_record(_impact_policy())
    store.write_product_owner_policy_record(_owner_policy())
    store.write_product_owner_requirement_record(_owner_requirement())
    return store


def _expected_binding_sha256(
    *,
    store: object,
    provider: ChangeImpactRepositoryEvidenceProvider,
) -> str:
    decision = evaluate_owner_acceptance(
        store=store,
        repository_evidence_provider=provider,
        target=ChangeImpactTargetReference(
            repository=REPOSITORY,
            pull_request_number=2022,
        ),
        evaluated_at="2026-08-07T12:00:00Z",
    )
    if decision.binding is None:
        raise AssertionError("Expected Owner acceptance binding")
    return decision.binding.binding_sha256


class OwnerAcceptanceTests(unittest.TestCase):
    def test_acceptance_records_and_replays_for_exact_binding(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            target = ChangeImpactTargetReference(repository=REPOSITORY, pull_request_number=2022)
            provider = _EvidenceProvider(_repository_evidence())
            expected_binding_sha256 = _expected_binding_sha256(store=store, provider=provider)
            result = record_owner_acceptance_event(
                store=store,
                repository_evidence_provider=provider,
                target=target,
                identity=_human(),
                action="accepted",
                expected_binding_sha256=expected_binding_sha256,
                source_event_kind="browser_api",
                source_event_id="accept-1",
                occurred_at="2026-08-07T12:00:00Z",
            )
            self.assertEqual(result.status, "written")
            self.assertEqual(result.decision.status, "accepted")

            replay = record_owner_acceptance_event(
                store=store,
                repository_evidence_provider=provider,
                target=target,
                identity=_human(),
                action="accepted",
                expected_binding_sha256=expected_binding_sha256,
                source_event_kind="browser_api",
                source_event_id="accept-1",
                occurred_at="2026-08-07T12:01:00Z",
            )
            self.assertEqual(replay.status, "replayed")
            self.assertEqual(replay.record, result.record)

            conflicting = OwnerAcceptanceEventRecord.model_validate(
                result.record.model_dump(mode="json") | {"reason": "changed"}
            )
            with self.assertRaises(OwnerAcceptanceEventConflictError):
                store.write_owner_acceptance_event_record(conflicting)

    def test_engineering_only_not_required_writes_no_event(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory), include_dependency_evidence=False)
            target = ChangeImpactTargetReference(repository=REPOSITORY, pull_request_number=2022)
            decision = evaluate_owner_acceptance(
                store=store,
                repository_evidence_provider=_EvidenceProvider(
                    _repository_evidence(path="control_plane/service_auth.py")
                ),
                target=target,
                evaluated_at="2026-08-07T12:00:00Z",
            )
            self.assertEqual(decision.status, "not_required")
            self.assertEqual(store.list_owner_acceptance_event_records(), ())

    def test_non_human_and_non_owner_cannot_author(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            target = ChangeImpactTargetReference(repository=REPOSITORY, pull_request_number=2022)
            provider = _EvidenceProvider(_repository_evidence())
            expected_binding_sha256 = _expected_binding_sha256(store=store, provider=provider)
            with self.assertRaises(OwnerAcceptanceAuthorizationError):
                record_owner_acceptance_event(
                    store=store,
                    repository_evidence_provider=provider,
                    target=target,
                    identity=TerminalAgentIdentity(subject="agent", token_label="local"),
                    action="accepted",
                    expected_binding_sha256=expected_binding_sha256,
                    source_event_kind="browser_api",
                    source_event_id="accept-agent",
                    occurred_at="2026-08-07T12:00:00Z",
                )
            with self.assertRaises(OwnerAcceptanceAuthorizationError):
                record_owner_acceptance_event(
                    store=store,
                    repository_evidence_provider=provider,
                    target=target,
                    identity=_human(github_id=9999),
                    action="accepted",
                    expected_binding_sha256=expected_binding_sha256,
                    source_event_kind="browser_api",
                    source_event_id="accept-other",
                    occurred_at="2026-08-07T12:00:00Z",
                )

    def test_head_change_stales_previous_acceptance(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            target = ChangeImpactTargetReference(repository=REPOSITORY, pull_request_number=2022)
            provider = _EvidenceProvider(_repository_evidence())
            expected_binding_sha256 = _expected_binding_sha256(store=store, provider=provider)
            accepted = record_owner_acceptance_event(
                store=store,
                repository_evidence_provider=provider,
                target=target,
                identity=_human(),
                action="accepted",
                expected_binding_sha256=expected_binding_sha256,
                source_event_kind="browser_api",
                source_event_id="accept-1",
                occurred_at="2026-08-07T12:00:00Z",
            )
            drifted = evaluate_owner_acceptance(
                store=store,
                repository_evidence_provider=_EvidenceProvider(_repository_evidence(head="c" * 40)),
                target=target,
                evaluated_at="2026-08-07T12:30:00Z",
            )
            self.assertEqual(drifted.status, "stale")
            self.assertNotEqual(
                drifted.binding.binding_sha256 if drifted.binding else "",
                accepted.record.binding.binding_sha256,
            )

    def test_revised_owner_policy_and_requirement_histories_remain_usable(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            current_policy = store.list_product_owner_policy_records(status="active")[0]
            current_requirement = store.list_product_owner_requirement_records(status="active")[0]
            successor_policy = _owner_policy(
                revision=2,
                supersedes_record_id=current_policy.record_id,
            )
            successor_requirement = _owner_requirement(
                revision=2,
                supersedes_record_id=current_requirement.record_id,
            )
            store.compare_and_write_product_owner_policy_record(
                successor_policy,
                expected_current_record_id=current_policy.record_id,
                expected_current_policy_digest=current_policy.policy_digest,
            )
            store.compare_and_write_product_owner_requirement_record(
                successor_requirement,
                expected_current_record_id=current_requirement.record_id,
                expected_current_requirement_digest=current_requirement.requirement_digest,
            )
            provider = _EvidenceProvider(_repository_evidence())
            expected_binding_sha256 = _expected_binding_sha256(store=store, provider=provider)

            result = record_owner_acceptance_event(
                store=store,
                repository_evidence_provider=provider,
                target=ChangeImpactTargetReference(
                    repository=REPOSITORY,
                    pull_request_number=2022,
                ),
                identity=_human(),
                action="accepted",
                expected_binding_sha256=expected_binding_sha256,
                source_event_kind="browser_api",
                source_event_id="accept-revision-2",
                occurred_at="2026-08-07T12:00:00Z",
            )

            self.assertEqual(result.status, "written")
            self.assertEqual(result.record.binding.owner_policy_revision, 2)
            self.assertEqual(result.record.binding.owner_requirement_revision, 2)

    def test_write_rejects_binding_changed_after_owner_evaluation(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            target = ChangeImpactTargetReference(repository=REPOSITORY, pull_request_number=2022)
            provider = _EvidenceProvider(_repository_evidence())
            expected_binding_sha256 = _expected_binding_sha256(store=store, provider=provider)
            provider.evidence = _repository_evidence(head="c" * 40)

            with self.assertRaises(OwnerAcceptanceBindingConflictError):
                record_owner_acceptance_event(
                    store=store,
                    repository_evidence_provider=provider,
                    target=target,
                    identity=_human(),
                    action="accepted",
                    expected_binding_sha256=expected_binding_sha256,
                    source_event_kind="browser_api",
                    source_event_id="accept-stale-binding",
                    occurred_at="2026-08-07T12:00:00Z",
                )

            self.assertEqual(store.list_owner_acceptance_event_records(), ())

    def test_evaluation_uses_any_owner_covering_the_exact_scope(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            current_policy = store.list_product_owner_policy_records(status="active")[0]
            successor_policy = ProductOwnerPolicyRecord(
                product=PRODUCT,
                system=SYSTEM,
                policy_revision=2,
                owners=(
                    ProductOwnerGrant(
                        identity=ProductOwnerIdentity(
                            provider="github",
                            provider_subject_id="2999",
                        ),
                        repository_ids=("9999",),
                        environments=("pull_request",),
                    ),
                    ProductOwnerGrant(
                        identity=ProductOwnerIdentity(
                            provider="github",
                            provider_subject_id=str(OWNER_GITHUB_ID),
                        ),
                        repository_ids=(REPOSITORY_ID,),
                        environments=("pull_request",),
                    ),
                ),
                effective_at="2026-08-07T01:00:00Z",
                source="test",
                reason="Only the second Owner covers this repository.",
                supersedes_record_id=current_policy.record_id,
            )
            store.compare_and_write_product_owner_policy_record(
                successor_policy,
                expected_current_record_id=current_policy.record_id,
                expected_current_policy_digest=current_policy.policy_digest,
            )

            decision = evaluate_owner_acceptance(
                store=store,
                repository_evidence_provider=_EvidenceProvider(_repository_evidence()),
                target=ChangeImpactTargetReference(
                    repository=REPOSITORY,
                    pull_request_number=2022,
                ),
                evaluated_at="2026-08-07T12:00:00Z",
            )

            self.assertEqual(decision.status, "pending")
            binding = decision.binding
            self.assertIsNotNone(binding)
            assert binding is not None
            self.assertEqual(binding.owner_policy_revision, 2)

    def test_multi_product_change_fails_closed(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory), shared_dependency_evidence=True)
            decision = evaluate_owner_acceptance(
                store=store,
                repository_evidence_provider=_EvidenceProvider(
                    _repository_evidence(path="src/shared/app.py")
                ),
                target=ChangeImpactTargetReference(
                    repository=REPOSITORY,
                    pull_request_number=2022,
                ),
                evaluated_at="2026-08-07T12:00:00Z",
            )

            self.assertEqual(decision.status, "unavailable")
            self.assertEqual(decision.reason_code, "multi_product_unsupported")
            self.assertEqual(store.list_owner_acceptance_event_records(), ())


if __name__ == "__main__":
    unittest.main()
