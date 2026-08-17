from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import json
import unittest

from control_plane.change_impact_service import ChangeImpactRepositoryEvidenceProvider
from control_plane.change_impact_github import GitHubOpenPullRequest
from control_plane.contracts.change_impact import (
    ChangeImpactAuthorshipEvidence,
    ChangeImpactBaseEvidence,
    ChangeImpactChangedFileEvidence,
    ChangeImpactComponentRule,
    ChangeImpactPolicyRecord,
    ChangeImpactProductScope,
    ChangeImpactRepositoryEvidence,
    ChangeImpactStoredEvidence,
    ChangeImpactTarget,
    ChangeImpactTargetReference,
)
from control_plane.contracts.owner_acceptance import (
    OwnerAcceptanceBinding,
    OwnerAcceptanceEventRecord,
    OwnerAcceptanceResolutionEvidence,
    OwnerAcceptanceTransitionError,
    owner_acceptance_event_replay_digest,
)
from control_plane.contracts.preview_generation_record import (
    PreviewGenerationRecord,
    PreviewPullRequestSummary,
)
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductImageProfile,
    ProductLaneProfile,
    ProductPreviewProfile,
)
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
    OwnerAcceptanceEvaluationUnavailableError,
    OwnerAcceptanceEventConflictError,
    aggregate_owner_acceptance_decision,
    build_owner_acceptance_system_event,
    evaluate_owner_acceptance,
    record_owner_acceptance_event,
)
from control_plane.service_auth import GitHubHumanIdentity, TerminalAgentIdentity
from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore


REPOSITORY_ID = "1001"
REPOSITORY_OWNER_ID = "2001"
REPOSITORY = "example/web"
PRODUCT = "generic-web-a"
SECOND_PRODUCT = "generic-web-b"
SYSTEM = "web"
PREVIEW_CONTEXT = "generic-web-a-preview"
OWNER_GITHUB_ID = 3001
CONTRIBUTOR_GITHUB_ID = 4001
HEAD_SHA = "a" * 40
TREE_SHA = "b" * 40
BASE_SHA = "e" * 40
BASE_REF = "main"


class _EvidenceProvider(ChangeImpactRepositoryEvidenceProvider):
    def __init__(
        self,
        evidence: ChangeImpactRepositoryEvidence,
        open_pull_requests: tuple[GitHubOpenPullRequest, ...] | None = None,
    ) -> None:
        self.evidence = evidence
        self.open_pull_requests = open_pull_requests or (
            GitHubOpenPullRequest(
                repository=evidence.target.repository,
                pull_request_number=evidence.target.pull_request_number,
                title="Owner acceptance test pull request",
                url=(
                    f"https://github.com/{evidence.target.repository}/pull/"
                    f"{evidence.target.pull_request_number}"
                ),
                updated_at="2026-08-07T12:00:00Z",
            ),
        )

    def resolve(self, _target: ChangeImpactTargetReference) -> ChangeImpactRepositoryEvidence:
        return self.evidence

    def resolve_current_item(
        self,
        target: ChangeImpactTargetReference,
    ) -> ChangeImpactRepositoryEvidence:
        return self.resolve(target)

    def list_open_pull_requests(
        self,
        _repository: str,
        *,
        limit: int,
    ) -> tuple[GitHubOpenPullRequest, ...]:
        return self.open_pull_requests[:limit]


def _human(
    github_id: int = OWNER_GITHUB_ID,
    *,
    login: str = "owner",
) -> GitHubHumanIdentity:
    return GitHubHumanIdentity(
        login=login,
        github_id=github_id,
        name="Owner",
        email="",
        organizations=frozenset(),
        teams=frozenset(),
        role="admin",
    )


def _repository_evidence(
    *,
    path: str = "src/runtime/app.py",
    head: str = HEAD_SHA,
    base_sha: str = BASE_SHA,
    authorship: ChangeImpactAuthorshipEvidence | None = None,
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
        base=ChangeImpactBaseEvidence(base_ref=BASE_REF, base_sha=base_sha),
        authorship=authorship
        or ChangeImpactAuthorshipEvidence(
            resolution="resolved",
            contributor_github_ids=(CONTRIBUTOR_GITHUB_ID,),
            commit_count=1,
        ),
    )


def _impact_policy(
    *,
    revision: int = 1,
    supersedes_record_id: str | None = None,
) -> ChangeImpactPolicyRecord:
    return ChangeImpactPolicyRecord(
        repository_id=REPOSITORY_ID,
        repository_owner_id=REPOSITORY_OWNER_ID,
        repository=REPOSITORY,
        policy_revision=revision,
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
        effective_at=f"2026-08-07T0{revision - 1}:00:00Z",
        source="test",
        reason="Owner acceptance policy.",
        supersedes_record_id=supersedes_record_id,
    )


def _owner_policy(
    *,
    product: str = PRODUCT,
    revision: int = 1,
    supersedes_record_id: str | None = None,
    owners: tuple[ProductOwnerGrant, ...] | None = None,
) -> ProductOwnerPolicyRecord:
    return ProductOwnerPolicyRecord(
        product=product,
        system=SYSTEM,
        policy_revision=revision,
        owners=owners
        or (
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
    product: str = PRODUCT,
    revision: int = 1,
    supersedes_record_id: str | None = None,
) -> ProductOwnerRequirementRecord:
    return ProductOwnerRequirementRecord(
        product=product,
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
    shared_dependency_evidence: bool | list[bool] = False,
    include_second_product_authority: bool | None = None,
    invalid_product_profile: bool = False,
    include_product_profile: bool = True,
    product_profile_read_limit: int | None = None,
) -> FilesystemRecordStore:
    class _OwnerAcceptanceStore(FilesystemRecordStore):
        product_profile_read_count = 0

        def read_product_profile_record(self, product: str) -> LaunchplaneProductProfileRecord:
            self.product_profile_read_count += 1
            if invalid_product_profile:
                raise ValueError("invalid product profile")
            if (
                product_profile_read_limit is not None
                and self.product_profile_read_count > product_profile_read_limit
            ):
                raise ValueError("product profile read limit exceeded")
            return super().read_product_profile_record(product)

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
            shared_evidence_enabled = (
                shared_dependency_evidence[0]
                if isinstance(shared_dependency_evidence, list)
                else shared_dependency_evidence
            )
            if shared_evidence_enabled:
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
    if (
        bool(shared_dependency_evidence)
        if include_second_product_authority is None
        else include_second_product_authority
    ):
        store.write_product_owner_policy_record(_owner_policy(product=SECOND_PRODUCT))
        store.write_product_owner_requirement_record(_owner_requirement(product=SECOND_PRODUCT))
    if include_product_profile:
        store.write_product_profile_record(_preview_profile(enabled=False))
        if (
            bool(shared_dependency_evidence)
            if include_second_product_authority is None
            else include_second_product_authority
        ):
            store.write_product_profile_record(
                _preview_profile(product=SECOND_PRODUCT, enabled=False)
            )
    return store


def _preview_profile(
    *,
    product: str = PRODUCT,
    enabled: bool = True,
    data_transport_mode: str = "none",
) -> LaunchplaneProductProfileRecord:
    return LaunchplaneProductProfileRecord(
        product=product,
        display_name=product.replace("-", " ").title(),
        repository=REPOSITORY,
        driver_id="generic-web",
        image=ProductImageProfile(repository="ghcr.io/example/web"),
        runtime_port=3000,
        health_path="/health",
        lanes=(
            ProductLaneProfile(
                instance="testing",
                context="generic-web-a-testing",
                base_url="https://testing.example.test",
                health_url="https://testing.example.test/health",
            ),
        ),
        preview=ProductPreviewProfile(
            enabled=enabled,
            context=PREVIEW_CONTEXT,
            slug_template="pr-{number}",
            app_name_prefix="generic-web-a-preview",
            data_transport_mode=data_transport_mode,  # type: ignore[arg-type]
        ),
        updated_at="2026-08-07T00:00:00Z",
        source="test",
    )


def _write_preview_evidence(
    store: FilesystemRecordStore,
    *,
    head_sha: str = HEAD_SHA,
    preview_id: str = "preview-generic-web-a-pr-2022",
    generation_id: str = "preview-generic-web-a-pr-2022-generation-0001",
    runtime_generation_id: str | None = None,
    artifact_id: str = "artifact-generic-web-a-pr-2022",
    image_digest: str = "a" * 64,
    preview_state: str = "active",
    generation_state: str = "ready",
    verify_status: str = "pass",
    data_transport_mode: str = "none",
) -> None:
    preview = PreviewRecord(
        preview_id=preview_id,
        context=PREVIEW_CONTEXT,
        anchor_repo="web",
        anchor_pr_number=2022,
        anchor_pr_url="https://github.com/example/web/pull/2022",
        preview_label="preview",
        canonical_url="https://pr-2022.example.test",
        state=preview_state,  # type: ignore[arg-type]
        created_at="2026-08-07T00:00:00Z",
        updated_at="2026-08-07T01:00:00Z",
        eligible_at="2026-08-07T00:00:00Z",
        active_generation_id=generation_id,
        serving_generation_id=generation_id,
        latest_generation_id=generation_id,
        latest_manifest_fingerprint=f"manifest-{generation_id}",
    )
    runtime_identity = RuntimeIdentity(
        product=PRODUCT,
        context=PREVIEW_CONTEXT,
        instance="preview-pr-2022",
        environment_kind="preview",
        deployment_record_id=f"deployment-{generation_id}",
        artifact_id=artifact_id,
        source_git_ref=head_sha,
        image_reference=f"ghcr.io/example/web@sha256:{image_digest}",
        preview_id=preview.preview_id,
        preview_generation_id=(
            generation_id if runtime_generation_id is None else runtime_generation_id
        ),
    )
    generation = PreviewGenerationRecord(
        generation_id=generation_id,
        preview_id=preview.preview_id,
        sequence=1,
        state=generation_state,  # type: ignore[arg-type]
        requested_reason="Owner acceptance preview evidence.",
        requested_at="2026-08-07T00:00:00Z",
        ready_at="2026-08-07T01:00:00Z",
        resolved_manifest_fingerprint=preview.latest_manifest_fingerprint,
        artifact_id=artifact_id,
        source_map=(),
        anchor_summary=PreviewPullRequestSummary(
            repo="web",
            pr_number=2022,
            head_sha=head_sha,
            pr_url=preview.anchor_pr_url,
        ),
        deploy_status="pass",
        verify_status=verify_status,  # type: ignore[arg-type]
        overall_health_status="pass",
        runtime_identity=runtime_identity,
    )
    store.write_product_profile_record(_preview_profile(data_transport_mode=data_transport_mode))
    store.write_preview_generation_record(generation)
    store.write_preview_record(preview)


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
    def test_non_preview_binding_digest_remains_backward_compatible(self) -> None:
        """A binding recorded before reviewed context existed keeps its exact digest.

        Reviewed context, like preview evidence before it, is an optional bound
        field. Historical events therefore replay byte-identically, while a current
        server-derived binding legitimately differs because more evidence is bound.
        """
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            historical = OwnerAcceptanceBinding(
                repository_id=REPOSITORY_ID,
                repository_owner_id=REPOSITORY_OWNER_ID,
                repository=REPOSITORY,
                pull_request_number=2022,
                head_sha=HEAD_SHA,
                tree_sha=TREE_SHA,
                change_impact_policy_record_id=_impact_policy().record_id,
                change_impact_policy_revision=1,
                change_impact_policy_digest=_impact_policy().policy_digest,
                product=PRODUCT,
                system=SYSTEM,
                action="pull_request.owner_acceptance",
                environment="pull_request",
                owner_policy_record_id=_owner_policy().record_id,
                owner_policy_revision=1,
                owner_policy_digest=_owner_policy().policy_digest,
                owner_requirement_record_id=_owner_requirement().record_id,
                owner_requirement_revision=1,
                owner_requirement_digest=(
                    "8aadfe5b7b0291143ec9e0f653e1978c9cc081d12e36e86b49d87e4a7015e356"
                ),
            )

            self.assertEqual(
                historical.binding_sha256,
                "6f61c0b708961a549242c3ae7c97b599149648955c8bebd8fb540567c409c04a",
            )
            self.assertNotEqual(
                _expected_binding_sha256(
                    store=store,
                    provider=_EvidenceProvider(_repository_evidence()),
                ),
                historical.binding_sha256,
            )

    def test_preview_backed_acceptance_binds_verified_serving_runtime(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            _write_preview_evidence(store)
            provider = _EvidenceProvider(_repository_evidence())
            target = ChangeImpactTargetReference(repository=REPOSITORY, pull_request_number=2022)

            decision = evaluate_owner_acceptance(
                store=store,
                repository_evidence_provider=provider,
                target=target,
                evaluated_at="2026-08-07T12:00:00Z",
            )

            self.assertEqual(decision.status, "pending")
            self.assertIsNotNone(decision.binding)
            assert decision.binding is not None
            self.assertIsNotNone(decision.binding.preview)
            assert decision.binding.preview is not None
            self.assertEqual(decision.binding.preview.context, PREVIEW_CONTEXT)
            self.assertEqual(
                decision.binding.preview.artifact_image_digest,
                f"sha256:{'a' * 64}",
            )
            result = record_owner_acceptance_event(
                store=store,
                repository_evidence_provider=provider,
                target=target,
                identity=_human(),
                action="accepted",
                expected_binding_sha256=decision.binding.binding_sha256,
                source_event_kind="browser_api",
                source_event_id="accept-preview",
                occurred_at="2026-08-07T12:00:00Z",
            )
            self.assertEqual(result.decision.status, "accepted")
            self.assertIsNotNone(result.record.binding.preview)

    def test_preview_drift_conflicts_without_writing(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            _write_preview_evidence(store)
            provider = _EvidenceProvider(_repository_evidence())
            expected_binding_sha256 = _expected_binding_sha256(store=store, provider=provider)
            _write_preview_evidence(
                store,
                generation_id="preview-generic-web-a-pr-2022-generation-0002",
                artifact_id="artifact-generic-web-a-pr-2022-v2",
                image_digest="b" * 64,
            )

            with self.assertRaises(OwnerAcceptanceBindingConflictError):
                record_owner_acceptance_event(
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
                    source_event_id="accept-preview-drift",
                    occurred_at="2026-08-07T12:00:00Z",
                )

            self.assertEqual(store.list_owner_acceptance_event_records(), ())

    def test_incomplete_preview_fails_closed(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            _write_preview_evidence(store, verify_status="fail")

            decision = evaluate_owner_acceptance(
                store=store,
                repository_evidence_provider=_EvidenceProvider(_repository_evidence()),
                target=ChangeImpactTargetReference(
                    repository=REPOSITORY,
                    pull_request_number=2022,
                ),
                evaluated_at="2026-08-07T12:00:00Z",
            )

            self.assertEqual(decision.status, "unavailable")
            self.assertEqual(decision.reason_code, "preview_evidence_unavailable")
            self.assertEqual(store.list_owner_acceptance_event_records(), ())

    def test_enabled_preview_without_active_evidence_fails_closed(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            store.write_product_profile_record(_preview_profile())

            decision = evaluate_owner_acceptance(
                store=store,
                repository_evidence_provider=_EvidenceProvider(_repository_evidence()),
                target=ChangeImpactTargetReference(
                    repository=REPOSITORY,
                    pull_request_number=2022,
                ),
                evaluated_at="2026-08-07T12:00:00Z",
            )

            self.assertEqual(decision.status, "unavailable")
            self.assertEqual(decision.reason_code, "preview_evidence_unavailable")
            self.assertEqual(store.list_owner_acceptance_event_records(), ())

    def test_malformed_preview_profile_fails_closed(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory), invalid_product_profile=True)
            with self.assertRaisesRegex(ValueError, "invalid product profile"):
                store.read_product_profile_record(PRODUCT)

            decision = evaluate_owner_acceptance(
                store=store,
                repository_evidence_provider=_EvidenceProvider(_repository_evidence()),
                target=ChangeImpactTargetReference(
                    repository=REPOSITORY,
                    pull_request_number=2022,
                ),
                evaluated_at="2026-08-07T12:00:00Z",
            )

            self.assertEqual(decision.status, "unavailable")
            self.assertEqual(decision.reason_code, "preview_evidence_unavailable")
            self.assertEqual(store.list_owner_acceptance_event_records(), ())

    def test_owner_binding_requires_runtime_generation_identity(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            _write_preview_evidence(store, runtime_generation_id="")

            decision = evaluate_owner_acceptance(
                store=store,
                repository_evidence_provider=_EvidenceProvider(_repository_evidence()),
                target=ChangeImpactTargetReference(
                    repository=REPOSITORY,
                    pull_request_number=2022,
                ),
                evaluated_at="2026-08-07T12:00:00Z",
            )

            self.assertEqual(decision.status, "unavailable")
            self.assertEqual(decision.reason_code, "preview_evidence_unavailable")
            self.assertEqual(store.list_owner_acceptance_event_records(), ())

    def test_ambiguous_serving_previews_fail_closed(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            _write_preview_evidence(store)
            _write_preview_evidence(
                store,
                preview_id="preview-generic-web-a-pr-2022-duplicate",
                generation_id="preview-generic-web-a-pr-2022-generation-duplicate",
                artifact_id="artifact-generic-web-a-pr-2022-duplicate",
                image_digest="b" * 64,
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

            self.assertEqual(decision.status, "unavailable")
            self.assertEqual(decision.reason_code, "preview_evidence_unavailable")
            self.assertEqual(store.list_owner_acceptance_event_records(), ())

    def test_destroyed_preview_cannot_downgrade_prior_acceptance(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            _write_preview_evidence(store)
            provider = _EvidenceProvider(_repository_evidence())
            target = ChangeImpactTargetReference(repository=REPOSITORY, pull_request_number=2022)
            expected_binding_sha256 = _expected_binding_sha256(store=store, provider=provider)
            record_owner_acceptance_event(
                store=store,
                repository_evidence_provider=provider,
                target=target,
                identity=_human(),
                action="accepted",
                expected_binding_sha256=expected_binding_sha256,
                source_event_kind="browser_api",
                source_event_id="accept-preview",
                occurred_at="2026-08-07T12:00:00Z",
            )
            _write_preview_evidence(store, preview_state="destroyed")

            decision = evaluate_owner_acceptance(
                store=store,
                repository_evidence_provider=provider,
                target=target,
                evaluated_at="2026-08-07T12:30:00Z",
            )

            self.assertEqual(decision.status, "stale")
            self.assertEqual(decision.reason_code, "preview_evidence_stale")
            self.assertIsNotNone(decision.binding)
            assert decision.binding is not None
            with self.assertRaises(OwnerAcceptanceEvaluationUnavailableError):
                record_owner_acceptance_event(
                    store=store,
                    repository_evidence_provider=provider,
                    target=target,
                    identity=_human(),
                    action="accepted",
                    expected_binding_sha256=decision.binding.binding_sha256,
                    source_event_kind="browser_api",
                    source_event_id="accept-preview-downgrade",
                    occurred_at="2026-08-07T12:30:00Z",
                )

    def test_event_timestamps_do_not_change_preview_history_folding(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            _write_preview_evidence(store)
            provider = _EvidenceProvider(_repository_evidence())
            target = ChangeImpactTargetReference(repository=REPOSITORY, pull_request_number=2022)
            record_owner_acceptance_event(
                store=store,
                repository_evidence_provider=provider,
                target=target,
                identity=_human(),
                action="accepted",
                expected_binding_sha256=_expected_binding_sha256(store=store, provider=provider),
                source_event_kind="browser_api",
                source_event_id="future-preview-acceptance",
                occurred_at="2026-08-07T13:00:00Z",
            )
            _write_preview_evidence(store, preview_state="destroyed")

            decision = evaluate_owner_acceptance(
                store=store,
                repository_evidence_provider=provider,
                target=target,
                evaluated_at="2026-08-07T12:00:00Z",
            )

            self.assertEqual(decision.status, "stale")
            self.assertEqual(decision.reason_code, "preview_evidence_stale")
            self.assertIsNotNone(decision.current_event)

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
            self.assertEqual(result.record.subject_sequence, 1)
            self.assertEqual(
                result.record.acceptance_id,
                "owner-acceptance-0dab839e317015df00d2c5caa0639130",
            )
            self.assertEqual(
                result.record.event_id,
                "owner-acceptance-event-ba52f0656d92980fcf96fa9b2fe905fa",
            )
            self.assertEqual(
                owner_acceptance_event_replay_digest(result.record),
                "715064def80cb17f42b6b9c655afce293c0d2315148d09693f45abb11f140c2b",
            )

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

            renamed_owner_replay = record_owner_acceptance_event(
                store=store,
                repository_evidence_provider=provider,
                target=target,
                identity=_human(login="renamed-owner"),
                action="accepted",
                expected_binding_sha256=expected_binding_sha256,
                source_event_kind="browser_api",
                source_event_id="accept-1",
                occurred_at="2026-08-07T12:02:00Z",
            )
            self.assertEqual(renamed_owner_replay.status, "replayed")
            self.assertEqual(renamed_owner_replay.record, result.record)

            conflicting = OwnerAcceptanceEventRecord.model_validate(
                result.record.model_dump(mode="json") | {"reason": "changed"}
            )
            with self.assertRaises(OwnerAcceptanceEventConflictError):
                store.write_owner_acceptance_event_record(conflicting)

    def test_before_write_can_reject_resolved_binding_before_append(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            target = ChangeImpactTargetReference(repository=REPOSITORY, pull_request_number=2022)
            provider = _EvidenceProvider(_repository_evidence())
            expected_binding_sha256 = _expected_binding_sha256(store=store, provider=provider)
            observed_records: list[OwnerAcceptanceEventRecord] = []

            def reject_before_write(record: OwnerAcceptanceEventRecord) -> None:
                observed_records.append(record)
                raise RuntimeError("resolved binding changed outside the held lock")

            with self.assertRaisesRegex(RuntimeError, "outside the held lock"):
                record_owner_acceptance_event(
                    store=store,
                    repository_evidence_provider=provider,
                    target=target,
                    identity=_human(),
                    action="accepted",
                    expected_binding_sha256=expected_binding_sha256,
                    source_event_kind="browser_api",
                    source_event_id="reject-before-write",
                    occurred_at="2026-08-07T12:00:00Z",
                    before_write=reject_before_write,
                )

            self.assertEqual(len(observed_records), 1)
            self.assertEqual(observed_records[0].binding.repository_id, REPOSITORY_ID)
            self.assertEqual(store.list_owner_acceptance_event_records(), ())

    def test_complete_human_transition_table_and_resolution_evidence(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            target = ChangeImpactTargetReference(repository=REPOSITORY, pull_request_number=2022)
            provider = _EvidenceProvider(_repository_evidence())
            binding_sha256 = _expected_binding_sha256(store=store, provider=provider)

            changes_requested = record_owner_acceptance_event(
                store=store,
                repository_evidence_provider=provider,
                target=target,
                identity=_human(),
                action="changes_requested",
                expected_binding_sha256=binding_sha256,
                source_event_kind="browser_api",
                source_event_id="request-changes",
                reason="Clarify the product behavior.",
                occurred_at="2026-08-07T13:00:00Z",
            )
            self.assertEqual(changes_requested.record.subject_sequence, 1)

            with self.assertRaises(OwnerAcceptanceTransitionError):
                record_owner_acceptance_event(
                    store=store,
                    repository_evidence_provider=provider,
                    target=target,
                    identity=_human(),
                    action="accepted",
                    expected_binding_sha256=binding_sha256,
                    source_event_kind="browser_api",
                    source_event_id="resolve-without-evidence",
                    occurred_at="2026-08-07T12:00:00Z",
                )

            accepted = record_owner_acceptance_event(
                store=store,
                repository_evidence_provider=provider,
                target=target,
                identity=_human(),
                action="accepted",
                expected_binding_sha256=binding_sha256,
                source_event_kind="browser_api",
                source_event_id="resolve-with-evidence",
                resolution=OwnerAcceptanceResolutionEvidence(
                    summary="The requested behavior is now explicit and covered.",
                    resolved_evidence_references=("test:owner-flow", "record:product-spec-17"),
                ),
                occurred_at="2026-08-07T11:00:00Z",
            )
            self.assertEqual(accepted.record.subject_sequence, 2)
            self.assertEqual(accepted.decision.status, "accepted")

            changes_after_acceptance = record_owner_acceptance_event(
                store=store,
                repository_evidence_provider=provider,
                target=target,
                identity=_human(),
                action="changes_requested",
                expected_binding_sha256=binding_sha256,
                source_event_kind="browser_api",
                source_event_id="request-more-changes",
                reason="A later product concern remains.",
                occurred_at="2026-08-07T10:00:00Z",
            )
            self.assertEqual(changes_after_acceptance.record.subject_sequence, 3)
            self.assertEqual(changes_after_acceptance.decision.status, "changes_requested")

            revoked = record_owner_acceptance_event(
                store=store,
                repository_evidence_provider=provider,
                target=target,
                identity=_human(),
                action="revoked",
                expected_binding_sha256=binding_sha256,
                source_event_kind="browser_api",
                source_event_id="revoke-review",
                reason="The Owner withdrew the review after new evidence.",
                occurred_at="2026-08-07T09:00:00Z",
            )
            self.assertEqual(revoked.record.subject_sequence, 4)
            self.assertEqual(revoked.decision.status, "revoked")

            with self.assertRaises(OwnerAcceptanceTransitionError):
                record_owner_acceptance_event(
                    store=store,
                    repository_evidence_provider=provider,
                    target=target,
                    identity=_human(),
                    action="accepted",
                    expected_binding_sha256=binding_sha256,
                    source_event_kind="browser_api",
                    source_event_id="accept-after-revoke",
                    occurred_at="2026-08-07T14:00:00Z",
                )

    def test_identical_reaffirmation_is_rejected_without_consuming_sequence(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            target = ChangeImpactTargetReference(repository=REPOSITORY, pull_request_number=2022)
            provider = _EvidenceProvider(_repository_evidence())
            binding_sha256 = _expected_binding_sha256(store=store, provider=provider)
            accepted = record_owner_acceptance_event(
                store=store,
                repository_evidence_provider=provider,
                target=target,
                identity=_human(),
                action="accepted",
                expected_binding_sha256=binding_sha256,
                source_event_kind="browser_api",
                source_event_id="initial-acceptance",
                occurred_at="2026-08-07T12:00:00Z",
            )
            replay = record_owner_acceptance_event(
                store=store,
                repository_evidence_provider=provider,
                target=target,
                identity=_human(),
                action="accepted",
                expected_binding_sha256=binding_sha256,
                source_event_kind="browser_api",
                source_event_id="initial-acceptance",
                occurred_at="2026-08-07T14:00:00Z",
            )
            self.assertEqual(replay.status, "replayed")
            self.assertEqual(replay.record.subject_sequence, 1)

            with self.assertRaises(OwnerAcceptanceTransitionError):
                record_owner_acceptance_event(
                    store=store,
                    repository_evidence_provider=provider,
                    target=target,
                    identity=_human(),
                    action="accepted",
                    expected_binding_sha256=binding_sha256,
                    source_event_kind="browser_api",
                    source_event_id="deliberate-reaffirmation",
                    occurred_at="2026-08-07T13:00:00Z",
                )

            changes = record_owner_acceptance_event(
                store=store,
                repository_evidence_provider=provider,
                target=target,
                identity=_human(),
                action="changes_requested",
                expected_binding_sha256=binding_sha256,
                source_event_kind="browser_api",
                source_event_id="changes-after-rejected-reaffirmation",
                reason="The product review changed.",
                occurred_at="2026-08-07T11:00:00Z",
            )
            self.assertEqual(accepted.record.subject_sequence, 1)
            self.assertEqual(changes.record.subject_sequence, 2)

    def test_filesystem_store_assigns_sequences_atomically_across_instances(self) -> None:
        with TemporaryDirectory() as directory:
            state_dir = Path(directory)
            store = _store(state_dir)
            binding = evaluate_owner_acceptance(
                store=store,
                repository_evidence_provider=_EvidenceProvider(_repository_evidence()),
                target=ChangeImpactTargetReference(repository=REPOSITORY, pull_request_number=2022),
                evaluated_at="2026-08-07T12:00:00Z",
            ).binding
            assert binding is not None
            first = build_owner_acceptance_system_event(
                binding=binding,
                action="superseded",
                occurred_at="2026-08-07T13:00:00Z",
                source_event_id="concurrent-system-one",
                reason="Concurrent ordering proof.",
            )
            second = build_owner_acceptance_system_event(
                binding=binding,
                action="invalidated",
                occurred_at="2026-08-07T11:00:00Z",
                source_event_id="concurrent-system-two",
                reason="Concurrent ordering proof.",
            )
            barrier = Barrier(2)

            def write(event: OwnerAcceptanceEventRecord) -> str:
                barrier.wait()
                return FilesystemRecordStore(state_dir).write_owner_acceptance_event_record(event)

            with ThreadPoolExecutor(max_workers=2) as executor:
                statuses = tuple(executor.map(write, (first, second)))

            self.assertEqual(sorted(statuses), ["written", "written"])
            persisted = store.list_owner_acceptance_event_records()
            self.assertEqual(sorted(event.subject_sequence for event in persisted), [1, 2])

    def test_filesystem_store_appends_legacy_events_after_sequenced_history(self) -> None:
        with TemporaryDirectory() as directory:
            state_dir = Path(directory)
            store = _store(state_dir)
            binding = evaluate_owner_acceptance(
                store=store,
                repository_evidence_provider=_EvidenceProvider(_repository_evidence()),
                target=ChangeImpactTargetReference(repository=REPOSITORY, pull_request_number=2022),
                evaluated_at="2026-08-07T12:00:00Z",
            ).binding
            assert binding is not None
            sequenced = build_owner_acceptance_system_event(
                binding=binding,
                action="superseded",
                occurred_at="2026-08-07T13:00:00Z",
                source_event_id="sequenced-system-event",
                reason="Initial sequenced history.",
            )
            legacy = build_owner_acceptance_system_event(
                binding=binding,
                action="invalidated",
                occurred_at="2026-08-07T14:00:00Z",
                source_event_id="legacy-system-event",
                reason="Written by a rolled-back runtime.",
            )
            self.assertEqual(store.write_owner_acceptance_event_record(sequenced), "written")
            legacy_payload = legacy.model_dump(mode="json", exclude_none=True)
            legacy_payload.pop("subject_sequence", None)
            legacy_path = store._record_path(
                "launchplane_owner_acceptance_events",
                legacy.event_id,
            )
            legacy_path.parent.mkdir(parents=True, exist_ok=True)
            legacy_path.write_text(json.dumps(legacy_payload), encoding="utf-8")

            records = store.list_owner_acceptance_event_records()

            by_event_id = {record.event_id: record for record in records}
            self.assertEqual(by_event_id[sequenced.event_id].subject_sequence, 1)
            self.assertEqual(by_event_id[legacy.event_id].subject_sequence, 2)

    def test_filesystem_import_preserves_owner_acceptance_sequence_order(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            filesystem_store = _store(root / "filesystem")
            target = ChangeImpactTargetReference(repository=REPOSITORY, pull_request_number=2022)
            provider = _EvidenceProvider(_repository_evidence())
            binding_sha256 = _expected_binding_sha256(
                store=filesystem_store,
                provider=provider,
            )
            requested = record_owner_acceptance_event(
                store=filesystem_store,
                repository_evidence_provider=provider,
                target=target,
                identity=_human(),
                action="changes_requested",
                expected_binding_sha256=binding_sha256,
                source_event_kind="browser_api",
                source_event_id="filesystem-import-requested",
                reason="Clarify the product behavior.",
                occurred_at="2026-08-07T14:00:00Z",
            )
            accepted = record_owner_acceptance_event(
                store=filesystem_store,
                repository_evidence_provider=provider,
                target=target,
                identity=_human(),
                action="accepted",
                expected_binding_sha256=binding_sha256,
                source_event_kind="browser_api",
                source_event_id="filesystem-import-accepted",
                resolution=OwnerAcceptanceResolutionEvidence(
                    summary="The requested behavior is now explicit.",
                    resolved_evidence_references=("test:owner-flow",),
                ),
                occurred_at="2026-08-07T13:00:00Z",
            )
            postgres_store = PostgresRecordStore(
                database_url=f"sqlite+pysqlite:///{root / 'launchplane.sqlite3'}"
            )
            postgres_store.ensure_schema()

            counts = postgres_store.import_core_records_from_filesystem(filesystem_store)

            self.assertEqual(counts["owner_acceptance_events"], 2)
            imported = sorted(
                postgres_store.list_owner_acceptance_event_records(),
                key=lambda event: event.subject_sequence,
            )
            self.assertEqual(
                [(event.subject_sequence, event.action) for event in imported],
                [(1, "changes_requested"), (2, "accepted")],
            )
            self.assertEqual(requested.record.subject_sequence, 1)
            self.assertEqual(accepted.record.subject_sequence, 2)

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

    def test_authority_policy_drift_conflicts_before_owner_authorization(self) -> None:
        for drift in ("change_impact", "owner_policy", "owner_requirement", "owner_membership"):
            with self.subTest(drift=drift), TemporaryDirectory() as directory:
                store = _store(Path(directory))
                target = ChangeImpactTargetReference(
                    repository=REPOSITORY,
                    pull_request_number=2022,
                )
                provider = _EvidenceProvider(_repository_evidence())
                expected_binding_sha256 = _expected_binding_sha256(store=store, provider=provider)

                if drift == "change_impact":
                    current_impact_policy = store.list_change_impact_policy_records(
                        status="active"
                    )[0]
                    successor_impact_policy = _impact_policy(
                        revision=2,
                        supersedes_record_id=current_impact_policy.record_id,
                    )
                    store.compare_and_write_change_impact_policy_record(
                        successor_impact_policy,
                        expected_current_record_id=current_impact_policy.record_id,
                        expected_current_policy_digest=current_impact_policy.policy_digest,
                    )
                elif drift in {"owner_policy", "owner_membership"}:
                    current_owner_policy = store.list_product_owner_policy_records(status="active")[
                        0
                    ]
                    successor_owner_policy = _owner_policy(
                        revision=2,
                        supersedes_record_id=current_owner_policy.record_id,
                        owners=(
                            ProductOwnerGrant(
                                identity=ProductOwnerIdentity(
                                    provider="github",
                                    provider_subject_id="9999",
                                ),
                                repository_ids=(REPOSITORY_ID,),
                                environments=("pull_request",),
                            ),
                        )
                        if drift == "owner_membership"
                        else None,
                    )
                    store.compare_and_write_product_owner_policy_record(
                        successor_owner_policy,
                        expected_current_record_id=current_owner_policy.record_id,
                        expected_current_policy_digest=current_owner_policy.policy_digest,
                    )
                else:
                    current_owner_requirement = store.list_product_owner_requirement_records(
                        status="active"
                    )[0]
                    successor_owner_requirement = _owner_requirement(
                        revision=2,
                        supersedes_record_id=current_owner_requirement.record_id,
                    )
                    store.compare_and_write_product_owner_requirement_record(
                        successor_owner_requirement,
                        expected_current_record_id=current_owner_requirement.record_id,
                        expected_current_requirement_digest=current_owner_requirement.requirement_digest,
                    )

                with self.assertRaises(OwnerAcceptanceBindingConflictError):
                    record_owner_acceptance_event(
                        store=store,
                        repository_evidence_provider=provider,
                        target=target,
                        identity=_human(),
                        action="accepted",
                        expected_binding_sha256=expected_binding_sha256,
                        source_event_kind="browser_api",
                        source_event_id=f"accept-{drift}-drift",
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

    def test_multi_product_change_requires_acceptance_per_product(self) -> None:
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

            self.assertEqual(decision.status, "pending")
            self.assertEqual(decision.reason_code, "acceptance_missing")
            self.assertEqual(
                tuple(product.product for product in decision.products),
                (PRODUCT, SECOND_PRODUCT),
            )
            self.assertEqual(
                tuple(product.status for product in decision.products),
                ("pending", "pending"),
            )
            self.assertIsNotNone(decision.binding)
            assert decision.binding is not None
            self.assertEqual(decision.binding.product, PRODUCT)
            self.assertEqual(store.list_owner_acceptance_event_records(), ())

    def test_multi_product_write_selects_each_server_derived_binding(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory), shared_dependency_evidence=True)
            provider = _EvidenceProvider(_repository_evidence(path="src/shared/app.py"))
            target = ChangeImpactTargetReference(repository=REPOSITORY, pull_request_number=2022)
            initial = evaluate_owner_acceptance(
                store=store,
                repository_evidence_provider=provider,
                target=target,
                evaluated_at="2026-08-07T12:00:00Z",
            )
            bindings = {product.product: product.binding for product in initial.products}
            self.assertTrue(all(binding is not None for binding in bindings.values()))

            second = bindings[SECOND_PRODUCT]
            assert second is not None
            accepted_second = record_owner_acceptance_event(
                store=store,
                repository_evidence_provider=provider,
                target=target,
                identity=_human(),
                action="accepted",
                expected_binding_sha256=second.binding_sha256,
                source_event_kind="browser_api",
                source_event_id="accept-both-products",
                occurred_at="2026-08-07T12:05:00Z",
            )
            self.assertEqual(accepted_second.record.binding.product, SECOND_PRODUCT)
            self.assertEqual(accepted_second.decision.status, "pending")
            self.assertEqual(
                tuple(product.status for product in accepted_second.decision.products),
                ("pending", "accepted"),
            )

            first = bindings[PRODUCT]
            assert first is not None
            accepted_first = record_owner_acceptance_event(
                store=store,
                repository_evidence_provider=provider,
                target=target,
                identity=_human(),
                action="accepted",
                expected_binding_sha256=first.binding_sha256,
                source_event_kind="browser_api",
                source_event_id="accept-both-products",
                occurred_at="2026-08-07T12:10:00Z",
            )
            self.assertEqual(accepted_first.record.binding.product, PRODUCT)
            self.assertEqual(accepted_first.decision.status, "accepted")
            self.assertEqual(
                tuple(product.status for product in accepted_first.decision.products),
                ("accepted", "accepted"),
            )
            events = store.list_owner_acceptance_event_records()
            self.assertEqual(len(events), 2)
            self.assertEqual(len({event.event_id for event in events}), 2)

    def test_write_does_not_re_resolve_aggregate_after_persisting(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory), product_profile_read_limit=3)
            provider = _EvidenceProvider(_repository_evidence())
            target = ChangeImpactTargetReference(repository=REPOSITORY, pull_request_number=2022)
            evaluated = evaluate_owner_acceptance(
                store=store,
                repository_evidence_provider=provider,
                target=target,
                evaluated_at="2026-08-07T12:00:00Z",
            )
            assert evaluated.binding is not None

            result = record_owner_acceptance_event(
                store=store,
                repository_evidence_provider=provider,
                target=target,
                identity=_human(),
                action="accepted",
                expected_binding_sha256=evaluated.binding.binding_sha256,
                source_event_kind="browser_api",
                source_event_id="accept-with-bounded-evidence-reads",
                occurred_at="2026-08-07T12:05:00Z",
            )

            self.assertEqual(result.status, "written")
            self.assertEqual(result.decision.status, "accepted")
            self.assertEqual(getattr(store, "product_profile_read_count"), 3)

    def test_multi_product_aggregate_fails_closed_for_missing_authority(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(
                Path(directory),
                shared_dependency_evidence=True,
                include_second_product_authority=False,
            )
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
            self.assertEqual(decision.reason_code, "owner_authority_unavailable")
            self.assertEqual(
                tuple(product.status for product in decision.products),
                ("pending", "unavailable"),
            )
            self.assertIsNone(decision.binding)

    def test_aggregate_status_precedence_and_tie_break_are_deterministic(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory), shared_dependency_evidence=True)
            initial = evaluate_owner_acceptance(
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
            first, second = initial.products
            cases = (
                ("unavailable", "owner_authority_unavailable", "accepted", "acceptance_valid"),
                ("stale", "acceptance_stale", "revoked", "acceptance_revoked"),
                ("revoked", "acceptance_revoked", "changes_requested", "changes_requested"),
                ("changes_requested", "changes_requested", "pending", "acceptance_missing"),
                ("pending", "acceptance_missing", "accepted", "acceptance_valid"),
                ("accepted", "acceptance_valid", "not_required", "engineering_only"),
            )
            for first_status, first_reason, second_status, second_reason in cases:
                with self.subTest(first_status=first_status, second_status=second_status):
                    aggregate = aggregate_owner_acceptance_decision(
                        products=(
                            first.model_copy(
                                update={"status": first_status, "reason_code": first_reason}
                            ),
                            second.model_copy(
                                update={"status": second_status, "reason_code": second_reason}
                            ),
                        ),
                        evaluated_at="2026-08-07T12:00:00Z",
                    )
                    self.assertEqual(aggregate.status, first_status)
                    self.assertEqual(aggregate.reason_code, first_reason)

            tied = aggregate_owner_acceptance_decision(
                products=(first, second),
                evaluated_at="2026-08-07T12:00:00Z",
            )
            self.assertIsNotNone(tied.binding)
            assert tied.binding is not None
            self.assertEqual(tied.binding.product, PRODUCT)

    def test_product_set_drift_is_deterministic_and_read_only(self) -> None:
        with TemporaryDirectory() as directory:
            shared_evidence_enabled = [False]
            store = _store(
                Path(directory),
                shared_dependency_evidence=shared_evidence_enabled,
                include_second_product_authority=True,
            )
            target = ChangeImpactTargetReference(repository=REPOSITORY, pull_request_number=2022)
            single_provider = _EvidenceProvider(_repository_evidence(path="src/runtime/app.py"))
            single = evaluate_owner_acceptance(
                store=store,
                repository_evidence_provider=single_provider,
                target=target,
                evaluated_at="2026-08-07T12:00:00Z",
            )
            assert single.binding is not None
            accepted = record_owner_acceptance_event(
                store=store,
                repository_evidence_provider=single_provider,
                target=target,
                identity=_human(),
                action="accepted",
                expected_binding_sha256=single.binding.binding_sha256,
                source_event_kind="browser_api",
                source_event_id="accept-before-product-set-drift",
                occurred_at="2026-08-07T12:05:00Z",
            )

            shared_evidence_enabled[0] = True
            expanded_provider = _EvidenceProvider(
                _repository_evidence(path="src/shared/app.py", head="c" * 40)
            )
            expanded = evaluate_owner_acceptance(
                store=store,
                repository_evidence_provider=expanded_provider,
                target=target,
                evaluated_at="2026-08-07T12:10:00Z",
            )
            self.assertEqual(
                tuple(product.status for product in expanded.products),
                ("stale", "pending"),
            )
            self.assertNotEqual(
                expanded.products[0].binding.binding_sha256
                if expanded.products[0].binding is not None
                else "",
                accepted.record.binding.binding_sha256,
            )

            shared_evidence_enabled[0] = False
            contracted_provider = _EvidenceProvider(
                _repository_evidence(path="src/runtime/app.py", head="c" * 40)
            )
            contracted = evaluate_owner_acceptance(
                store=store,
                repository_evidence_provider=contracted_provider,
                target=target,
                evaluated_at="2026-08-07T12:15:00Z",
            )
            self.assertEqual(contracted.status, "stale")
            self.assertEqual(tuple(product.product for product in contracted.products), (PRODUCT,))
            self.assertEqual(len(store.list_owner_acceptance_event_records()), 1)


if __name__ == "__main__":
    unittest.main()
