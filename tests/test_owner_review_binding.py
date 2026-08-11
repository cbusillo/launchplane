"""Owner product-review identity, age, policy, and preview-trust binding.

These tests cover the fail-closed admissibility contract: bound reviewed base and
impact context, server-resolved numeric contributing identities, self-review
denial, finite review age, scoped policy fingerprints, preview isolation, and the
non-authority API projection.
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from control_plane.contracts.change_impact import (
    ChangeImpactAuthorshipEvidence,
    ChangeImpactComponentRule,
    ChangeImpactPolicyRecord,
    ChangeImpactProductScope,
    ChangeImpactTargetReference,
)
from control_plane.contracts.owner_acceptance import (
    OwnerAcceptanceContributionBinding,
    OwnerAcceptanceDecision,
    owner_acceptance_human_action_semantics,
)
from control_plane.contracts.product_owner import (
    PRODUCT_OWNER_ELEVATED_REVIEW_MAX_AGE_SECONDS,
    PRODUCT_OWNER_ROUTINE_REVIEW_MAX_AGE_SECONDS,
    ProductOwnerGrant,
    ProductOwnerIdentity,
    ProductOwnerPolicyRecord,
    ProductOwnerPreviewTrustPolicy,
    ProductOwnerReviewAgePolicy,
    ProductOwnerSelfReviewPolicy,
    product_owner_scoped_policy_fingerprint,
    ProductOwnerActionContext,
)
from control_plane.owner_acceptance import (
    OwnerAcceptanceSelfReviewDeniedError,
    OwnerAcceptanceWriteResult,
    evaluate_owner_acceptance,
    evaluate_owner_acceptance_for_binding,
    evaluate_owner_acceptance_viewer_eligibility,
    record_owner_acceptance_event,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from tests.test_owner_acceptance import (
    BASE_REF,
    BASE_SHA,
    CONTRIBUTOR_GITHUB_ID,
    OWNER_GITHUB_ID,
    PRODUCT,
    REPOSITORY,
    REPOSITORY_ID,
    REPOSITORY_OWNER_ID,
    SYSTEM,
    _EvidenceProvider,
    _human,
    _owner_policy,
    _repository_evidence,
    _store,
    _write_preview_evidence,
)


TARGET = ChangeImpactTargetReference(repository=REPOSITORY, pull_request_number=2022)
REVIEWED_AT = "2026-08-07T12:00:00Z"


def _evaluate(
    store: object,
    provider: _EvidenceProvider,
    at: str = REVIEWED_AT,
) -> OwnerAcceptanceDecision:
    return evaluate_owner_acceptance(
        store=store,
        repository_evidence_provider=provider,
        target=TARGET,
        evaluated_at=at,
    )


def _accept(
    *,
    store: object,
    provider: _EvidenceProvider,
    github_id: int = OWNER_GITHUB_ID,
    occurred_at: str = REVIEWED_AT,
    source_event_id: str = "idem-2082",
) -> OwnerAcceptanceWriteResult:
    decision = _evaluate(store, provider, occurred_at)
    assert decision.binding is not None
    return record_owner_acceptance_event(
        store=store,
        repository_evidence_provider=provider,
        target=TARGET,
        identity=_human(github_id),
        action="accepted",
        expected_binding_sha256=decision.binding.binding_sha256,
        source_event_kind="browser_api",
        source_event_id=source_event_id,
        occurred_at=occurred_at,
    )


def _self_review_policy(
    *,
    routine_exception_enabled: bool = False,
    revision: int = 2,
    supersedes_record_id: str | None = None,
) -> ProductOwnerPolicyRecord:
    return ProductOwnerPolicyRecord(
        product=PRODUCT,
        system=SYSTEM,
        policy_revision=revision,
        owners=(
            ProductOwnerGrant(
                identity=ProductOwnerIdentity(
                    provider="github",
                    provider_subject_id=str(CONTRIBUTOR_GITHUB_ID),
                ),
                repository_ids=(REPOSITORY_ID,),
                environments=("pull_request",),
            ),
        ),
        self_review=ProductOwnerSelfReviewPolicy(
            routine_exception_enabled=routine_exception_enabled,
            exception_revision=7 if routine_exception_enabled else 0,
            exception_reason=(
                "Single-maintainer routine product changes." if routine_exception_enabled else ""
            ),
        ),
        effective_at=f"2026-08-07T0{revision - 1}:00:00Z",
        source="test",
        reason="Contributor is the only current Owner.",
        supersedes_record_id=supersedes_record_id or _owner_policy().record_id,
    )


def _component_impact_policy(
    *,
    review_tier: str = "routine",
    production_affecting: bool | None = None,
    revision: int = 2,
) -> ChangeImpactPolicyRecord:
    from tests.test_owner_acceptance import _impact_policy

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
                review_tier=review_tier,  # type: ignore[arg-type]
                production_affecting=production_affecting,
                reason="Runtime paths affect the product.",
            ),
        ),
        effective_at="2026-08-07T01:00:00Z",
        source="test",
        reason="Scoped component policy for review classification.",
        supersedes_record_id=_impact_policy().record_id,
    )


class OwnerReviewContextBindingTests(unittest.TestCase):
    def test_missing_product_profile_fails_closed(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory), include_product_profile=False)

            decision = _evaluate(store, _EvidenceProvider(_repository_evidence()))

            self.assertFalse(decision.admissible)
            self.assertEqual(decision.status, "unavailable")
            self.assertEqual(decision.reason_code, "preview_evidence_unavailable")

    def test_binding_carries_reviewed_base_impact_identity_and_policy_fingerprints(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))

            decision = _evaluate(store, _EvidenceProvider(_repository_evidence()))

            assert decision.binding is not None
            context = decision.binding.review_context
            assert context is not None
            self.assertEqual(context.base_ref, BASE_REF)
            self.assertEqual(context.base_sha, BASE_SHA)
            self.assertEqual(context.change_class, "routine")
            self.assertEqual(context.engineering_review_tier, "routine")
            self.assertEqual(
                context.review_max_age_seconds,
                PRODUCT_OWNER_ROUTINE_REVIEW_MAX_AGE_SECONDS,
            )
            self.assertEqual(context.contributions.resolution, "resolved")
            self.assertEqual(
                context.contributions.contributor_github_ids,
                (CONTRIBUTOR_GITHUB_ID,),
            )
            self.assertEqual(context.preview_isolation.isolation_class, "not_applicable")
            self.assertEqual(
                context.policy_fingerprints.owner_membership_fingerprint,
                product_owner_scoped_policy_fingerprint(
                    dimension="owner_membership",
                    context=ProductOwnerActionContext(
                        product=PRODUCT,
                        system=SYSTEM,
                        repository_id=REPOSITORY_ID,
                        environment="pull_request",
                        action="pull_request.owner_acceptance",
                    ),
                    record_id=_owner_policy().record_id,
                    revision=1,
                    payload=_owner_policy().policy_digest,
                ),
            )

    def test_policy_fingerprints_are_scoped_to_the_product_and_action(self) -> None:
        first = ProductOwnerActionContext(
            product=PRODUCT,
            system=SYSTEM,
            repository_id=REPOSITORY_ID,
            environment="pull_request",
            action="pull_request.owner_acceptance",
        )
        second = first.model_copy(update={"product": "other-product"})
        third = first.model_copy(update={"action": "pull_request.other_action"})

        fingerprints = {
            product_owner_scoped_policy_fingerprint(
                dimension="review_age",
                context=context,
                record_id="product-owner-policy-abc-r1",
                revision=1,
                payload={"routine_max_age_seconds": 60},
            )
            for context in (first, second, third)
        }

        self.assertEqual(len(fingerprints), 3)

    def test_conflicting_authorship_invalidates_a_previously_accepted_review(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            provider = _EvidenceProvider(_repository_evidence())
            written = _accept(store=store, provider=provider)
            self.assertEqual(written.decision.status, "accepted")
            self.assertTrue(written.decision.admissible)

            provider.evidence = _repository_evidence(
                authorship=ChangeImpactAuthorshipEvidence(
                    resolution="conflicting",
                    reason="login maps to two numeric identities",
                )
            )
            decision = _evaluate(store, provider)

            self.assertNotEqual(decision.status, "accepted")
            self.assertFalse(decision.admissible)
            self.assertIsNotNone(store.read_owner_acceptance_event_record(written.record.event_id))

    def test_a_binding_with_unknown_contributions_is_never_admissible(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            provider = _EvidenceProvider(_repository_evidence())
            written = _accept(store=store, provider=provider)
            assert written.record.binding.review_context is not None
            unknown_binding = written.record.binding.model_copy(
                update={
                    "review_context": written.record.binding.review_context.model_copy(
                        update={
                            "contributions": OwnerAcceptanceContributionBinding(
                                resolution="unknown",
                                reason_code="identity_evidence_conflicting",
                            )
                        }
                    ),
                    "binding_sha256": "",
                }
            )
            historical = written.record.model_copy(
                update={"binding": unknown_binding, "event_id": "", "acceptance_id": ""}
            )

            decision = evaluate_owner_acceptance_for_binding(
                binding=unknown_binding,
                events=(historical,),
                evaluated_at=REVIEWED_AT,
                policy=_owner_policy(),
            )

            self.assertEqual(decision.status, "unavailable")
            self.assertEqual(decision.reason_code, "contributing_identity_unknown")
            self.assertFalse(decision.admissible)

    def test_missing_base_evidence_fails_closed_without_a_binding(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            evidence = _repository_evidence()
            provider = _EvidenceProvider(evidence.model_copy(update={"base": None}))

            decision = _evaluate(store, provider)

            self.assertEqual(decision.status, "unavailable")
            self.assertEqual(decision.reason_code, "review_context_missing")
            self.assertIsNone(decision.binding)


class OwnerReviewSelfReviewTests(unittest.TestCase):
    def _contributor_store(
        self,
        directory: str,
        *,
        routine_exception_enabled: bool = False,
    ) -> FilesystemRecordStore:
        store = _store(Path(directory))
        store.write_product_owner_policy_record(
            _self_review_policy(routine_exception_enabled=routine_exception_enabled)
        )
        return store

    def test_self_review_is_denied_by_default(self) -> None:
        with TemporaryDirectory() as directory:
            store = self._contributor_store(directory)
            provider = _EvidenceProvider(_repository_evidence())

            with self.assertRaises(OwnerAcceptanceSelfReviewDeniedError):
                _accept(store=store, provider=provider, github_id=CONTRIBUTOR_GITHUB_ID)

    def test_self_reviewer_can_request_changes_or_revoke(self) -> None:
        with TemporaryDirectory() as directory:
            store = self._contributor_store(directory)
            decision = _evaluate(store, _EvidenceProvider(_repository_evidence()))

            eligibility = evaluate_owner_acceptance_viewer_eligibility(
                store=store,
                decisions=(decision,),
                identity=_human(CONTRIBUTOR_GITHUB_ID),
            )

            self.assertEqual(len(eligibility), 1)
            self.assertIs(eligibility[0].can_submit_event, True)
            self.assertIs(eligibility[0].can_accept, False)
            self.assertIs(eligibility[0].can_request_changes, True)
            self.assertIs(eligibility[0].can_revoke, True)
            self.assertEqual(eligibility[0].reason_code, "self_review_denied")

    def test_revisioned_routine_exception_permits_self_review(self) -> None:
        with TemporaryDirectory() as directory:
            store = self._contributor_store(directory, routine_exception_enabled=True)
            provider = _EvidenceProvider(_repository_evidence())

            written = _accept(store=store, provider=provider, github_id=CONTRIBUTOR_GITHUB_ID)

            assert written.record.authorization is not None
            self.assertTrue(written.record.authorization.self_review)
            self.assertEqual(written.record.authorization.self_review_exception_revision, 7)
            self.assertTrue(written.decision.admissible)

    def test_self_review_exception_revision_drift_is_inadmissible(self) -> None:
        with TemporaryDirectory() as directory:
            store = self._contributor_store(directory, routine_exception_enabled=True)
            provider = _EvidenceProvider(_repository_evidence())
            written = _accept(store=store, provider=provider, github_id=CONTRIBUTOR_GITHUB_ID)
            assert written.record.authorization is not None
            drifted_event = written.record.model_copy(
                update={
                    "authorization": written.record.authorization.model_copy(
                        update={"self_review_exception_revision": 8}
                    )
                }
            )

            decision = evaluate_owner_acceptance_for_binding(
                binding=written.record.binding,
                events=(drifted_event,),
                policy=_self_review_policy(routine_exception_enabled=True),
                evaluated_at=REVIEWED_AT,
            )

            self.assertFalse(decision.admissible)
            self.assertEqual(decision.status, "unavailable")
            self.assertEqual(decision.reason_code, "self_review_denied")

    def test_production_affecting_change_denies_self_review_despite_exception(self) -> None:
        with TemporaryDirectory() as directory:
            store = self._contributor_store(directory, routine_exception_enabled=True)
            store.write_change_impact_policy_record(
                _component_impact_policy(production_affecting=True)
            )
            provider = _EvidenceProvider(_repository_evidence())

            decision = _evaluate(store, provider)
            assert decision.binding is not None
            assert decision.binding.review_context is not None
            self.assertEqual(decision.binding.review_context.change_class, "production_affecting")

            with self.assertRaises(OwnerAcceptanceSelfReviewDeniedError):
                _accept(store=store, provider=provider, github_id=CONTRIBUTOR_GITHUB_ID)

    def test_a_contributor_may_still_request_changes_or_revoke(self) -> None:
        """Withdrawing judgment is never blocked; denying it would trap stale evidence."""
        with TemporaryDirectory() as directory:
            store = self._contributor_store(directory)
            provider = _EvidenceProvider(_repository_evidence())
            decision = _evaluate(store, provider)
            assert decision.binding is not None

            written = record_owner_acceptance_event(
                store=store,
                repository_evidence_provider=provider,
                target=TARGET,
                identity=_human(CONTRIBUTOR_GITHUB_ID),
                action="changes_requested",
                expected_binding_sha256=decision.binding.binding_sha256,
                source_event_kind="browser_api",
                source_event_id="idem-2082-changes",
                reason="Contributor withdraws pending product judgment.",
                occurred_at=REVIEWED_AT,
            )

            assert written.record.authorization is not None
            self.assertTrue(written.record.authorization.self_review)
            self.assertEqual(written.record.authorization.self_review_exception_revision, 0)
            self.assertEqual(written.decision.status, "changes_requested")
            self.assertFalse(written.decision.admissible)

    def test_unknown_authorship_denies_every_actor_at_write_time(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            provider = _EvidenceProvider(
                _repository_evidence(
                    authorship=ChangeImpactAuthorshipEvidence(
                        resolution="unresolved",
                        reason="commit has no linked numeric GitHub identity",
                    )
                )
            )

            with self.assertRaises(OwnerAcceptanceSelfReviewDeniedError):
                _accept(store=store, provider=provider)


class OwnerReviewAgeTests(unittest.TestCase):
    def test_routine_review_expires_after_the_policy_age_without_rewriting_history(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            provider = _EvidenceProvider(_repository_evidence())
            written = _accept(store=store, provider=provider)

            still_current = _evaluate(store, provider, "2026-09-05T12:00:00Z")
            expired = _evaluate(store, provider, "2026-09-07T12:00:01Z")

            self.assertEqual(still_current.status, "accepted")
            self.assertTrue(still_current.admissible)
            self.assertEqual(expired.status, "stale")
            self.assertEqual(expired.reason_code, "owner_review_expired")
            self.assertFalse(expired.admissible)
            assert expired.current_event is not None
            self.assertEqual(expired.current_event.event_id, written.record.event_id)
            self.assertEqual(expired.current_event.action, "accepted")

    def test_sensitive_change_uses_the_seven_day_migration_default(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            store.write_change_impact_policy_record(
                _component_impact_policy(review_tier="sensitive")
            )
            provider = _EvidenceProvider(_repository_evidence())

            decision = _evaluate(store, provider)

            assert decision.binding is not None
            context = decision.binding.review_context
            assert context is not None
            self.assertEqual(context.change_class, "sensitive")
            self.assertEqual(
                context.review_max_age_seconds,
                PRODUCT_OWNER_ELEVATED_REVIEW_MAX_AGE_SECONDS,
            )

    def test_production_affecting_change_uses_the_seven_day_migration_default(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            store.write_change_impact_policy_record(
                _component_impact_policy(production_affecting=True)
            )
            provider = _EvidenceProvider(_repository_evidence())

            decision = _evaluate(store, provider)

            assert decision.binding is not None
            context = decision.binding.review_context
            assert context is not None
            self.assertEqual(context.change_class, "production_affecting")
            self.assertEqual(
                context.review_max_age_seconds,
                PRODUCT_OWNER_ELEVATED_REVIEW_MAX_AGE_SECONDS,
            )

    def test_policy_cannot_extend_beyond_the_migration_defaults(self) -> None:
        with self.assertRaises(ValueError):
            ProductOwnerReviewAgePolicy(
                routine_max_age_seconds=PRODUCT_OWNER_ROUTINE_REVIEW_MAX_AGE_SECONDS + 1
            )
        with self.assertRaises(ValueError):
            ProductOwnerReviewAgePolicy(
                elevated_max_age_seconds=PRODUCT_OWNER_ELEVATED_REVIEW_MAX_AGE_SECONDS + 1
            )


class OwnerReviewPreviewTrustTests(unittest.TestCase):
    def test_cloned_product_data_preview_is_historical_but_inadmissible(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            _write_preview_evidence(store, data_transport_mode="clone")
            provider = _EvidenceProvider(_repository_evidence())
            written = _accept(store=store, provider=provider)

            decision = _evaluate(store, provider)

            self.assertEqual(written.record.action, "accepted")
            self.assertEqual(decision.status, "unavailable")
            self.assertEqual(decision.reason_code, "preview_isolation_insufficient")
            self.assertFalse(decision.admissible)

    def test_synthetic_seeded_preview_satisfies_the_default_minimum(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            _write_preview_evidence(store, data_transport_mode="migrate_seed")
            provider = _EvidenceProvider(_repository_evidence())

            written = _accept(store=store, provider=provider)

            assert written.record.binding.review_context is not None
            self.assertEqual(
                written.record.binding.review_context.preview_isolation.isolation_class,
                "synthetic_seeded",
            )
            self.assertTrue(written.decision.admissible)

    def test_driver_owned_transport_is_unknown_isolation_and_never_satisfies_policy(self) -> None:
        trust = ProductOwnerPreviewTrustPolicy()

        self.assertFalse(trust.satisfied_by("unknown"))
        self.assertFalse(trust.satisfied_by("cloned_product_data"))
        self.assertTrue(trust.satisfied_by("synthetic_seeded"))
        self.assertTrue(trust.satisfied_by("no_product_data"))
        self.assertTrue(trust.satisfied_by("not_applicable"))


class OwnerReviewAuthorityLossTests(unittest.TestCase):
    def test_owner_authority_loss_invalidates_admissibility_without_deleting_history(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            provider = _EvidenceProvider(_repository_evidence())
            written = _accept(store=store, provider=provider)

            store.write_product_owner_policy_record(
                _owner_policy(
                    revision=2,
                    supersedes_record_id=_owner_policy().record_id,
                    owners=(
                        ProductOwnerGrant(
                            identity=ProductOwnerIdentity(
                                provider="github",
                                provider_subject_id=str(OWNER_GITHUB_ID + 500),
                            ),
                            repository_ids=(REPOSITORY_ID,),
                            environments=("pull_request",),
                        ),
                    ),
                )
            )
            decision = _evaluate(store, provider)

            self.assertFalse(decision.admissible)
            self.assertNotEqual(decision.status, "accepted")
            self.assertIsNotNone(
                store.read_owner_acceptance_event_record(written.record.event_id),
            )


class OwnerReviewNonAuthorityProjectionTests(unittest.TestCase):
    def test_decision_projects_product_review_semantics_and_authorizes_nothing(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            provider = _EvidenceProvider(_repository_evidence())
            written = _accept(store=store, provider=provider)

            self.assertEqual(
                written.decision.human_action_semantics,
                "product_review_accepted",
            )
            self.assertEqual(written.decision.authorizes, ())
            self.assertEqual(written.record.action, "accepted")
            self.assertEqual(
                owner_acceptance_human_action_semantics("accepted"),
                "product_review_accepted",
            )

    def test_decision_cannot_claim_authority(self) -> None:
        with self.assertRaises(ValueError):
            OwnerAcceptanceDecision(
                status="accepted",
                reason_code="acceptance_valid",
                evaluated_at=REVIEWED_AT,
                authorizes=("merge",),
            )

    def test_only_a_currently_accepted_review_can_be_admissible(self) -> None:
        with self.assertRaises(ValueError):
            OwnerAcceptanceDecision(
                status="stale",
                reason_code="owner_review_expired",
                evaluated_at=REVIEWED_AT,
                admissible=True,
            )


if __name__ == "__main__":
    unittest.main()
