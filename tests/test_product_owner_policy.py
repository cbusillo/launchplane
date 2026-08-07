from __future__ import annotations

import unittest
from tempfile import TemporaryDirectory
from pathlib import Path

from pydantic import ValidationError

from control_plane.contracts.product_owner import (
    ProductOwnerActionContext,
    ProductOwnerActorIdentity,
    ProductOwnerGrant,
    ProductOwnerIdentity,
    ProductOwnerPolicyRecord,
    ProductOwnerRequirement,
    ProductOwnerRequirementRecord,
    ProductOwnerRoutingRecord,
    build_product_owner_policy_record_id,
)
from control_plane.product_owner_service import (
    ProductOwnerPolicyConflictError,
    ProductOwnerPolicySequenceError,
    ProductOwnerRequirementConflictError,
    ProductOwnerRequirementSequenceError,
    ProductOwnerRoutingConflictError,
    ProductOwnerRoutingSequenceError,
    apply_product_owner_policy,
    apply_product_owner_requirement,
    apply_product_owner_routing,
    evaluate_product_owner_shadow_authority,
    get_product_owner_read_model,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore


PRODUCT = "product-alpha"
SYSTEM = "system-web"
REPOSITORY_ID = "101"
ENVIRONMENT = "prod"
ACTION = "production.authorize"


def _identity(subject_id: str) -> ProductOwnerIdentity:
    return ProductOwnerIdentity(provider="github", provider_subject_id=subject_id)


def _grant(subject_id: str) -> ProductOwnerGrant:
    return ProductOwnerGrant(
        identity=_identity(subject_id),
        repository_ids=(REPOSITORY_ID,),
        environments=(ENVIRONMENT,),
    )


def _policy(
    *,
    product: str = PRODUCT,
    revision: int = 1,
    subjects: tuple[str, ...] = ("1001", "1002"),
    supersedes_record_id: str | None = None,
) -> ProductOwnerPolicyRecord:
    return ProductOwnerPolicyRecord(
        record_id=build_product_owner_policy_record_id(
            product=product,
            system=SYSTEM,
            policy_revision=revision,
        ),
        product=product,
        system=SYSTEM,
        policy_revision=revision,
        owners=tuple(_grant(subject) for subject in subjects),
        quorum=1,
        effective_at="2026-08-05T00:00:00Z",
        source="test",
        reason="Exercise the additive owner policy.",
        supersedes_record_id=supersedes_record_id,
    )


def _requirement(*, product: str = PRODUCT) -> ProductOwnerRequirementRecord:
    return ProductOwnerRequirementRecord(
        product=product,
        system=SYSTEM,
        requirement_revision=1,
        requirements=(
            ProductOwnerRequirement(
                action=ACTION,
                repository_ids=(REPOSITORY_ID,),
                environments=(ENVIRONMENT,),
            ),
        ),
        effective_at="2026-08-05T00:00:00Z",
        source="test",
        reason="Require one Owner for production authorization.",
    )


def _context() -> ProductOwnerActionContext:
    return ProductOwnerActionContext(
        product=PRODUCT,
        system=SYSTEM,
        repository_id=REPOSITORY_ID,
        environment=ENVIRONMENT,
        action=ACTION,
    )


class ProductOwnerPolicyTests(unittest.TestCase):
    def test_filesystem_storage_keeps_product_revision_streams_independent(self) -> None:
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(Path(directory))
            second_product = "product-beta"

            self.assertEqual(store.write_product_owner_policy_record(_policy()), "written")
            self.assertEqual(
                store.write_product_owner_policy_record(_policy(product=second_product)),
                "written",
            )
            self.assertEqual(
                store.write_product_owner_requirement_record(_requirement()),
                "written",
            )
            self.assertEqual(
                store.write_product_owner_requirement_record(_requirement(product=second_product)),
                "written",
            )

            self.assertEqual(
                {
                    record.product
                    for record in store.list_product_owner_policy_records(status="active")
                },
                {PRODUCT, second_product},
            )
            self.assertEqual(
                {
                    record.product
                    for record in store.list_product_owner_requirement_records(status="active")
                },
                {PRODUCT, second_product},
            )

    def test_policy_requires_human_immutable_identity_and_quorum_one(self) -> None:
        policy = _policy()
        self.assertEqual(policy.quorum, 1)
        self.assertEqual(len(policy.owners), 2)
        self.assertEqual(policy.owners[0].identity.owner_class, "owner")
        self.assertEqual(_identity("001").provider_subject_id, "1")
        with self.assertRaises(ValidationError):
            ProductOwnerIdentity(provider="github", provider_subject_id="")
        with self.assertRaises(ValidationError):
            ProductOwnerIdentity(provider="launchplane", provider_subject_id="1001")  # type: ignore[arg-type]
        with self.assertRaises(ValidationError):
            ProductOwnerIdentity(provider="github", provider_subject_id="human-login")
        with self.assertRaises(ValidationError):
            ProductOwnerActorIdentity(
                provider="github-actions",  # type: ignore[arg-type]
                provider_subject_id="1001",
            )
        with self.assertRaises(ValidationError):
            ProductOwnerActorIdentity.model_validate(
                {
                    "provider": "github",
                    "provider_subject_id": "1001",
                    "administrative_roles": ["global_admin"],
                }
            )
        identity = _identity("1001")
        grant = _grant("1001")
        with self.assertRaises(ValidationError):
            identity.provider_subject_id = "1002"
        with self.assertRaises(ValidationError):
            grant.environments = ("testing",)
        copied_identity = identity.model_copy(update={"provider_subject_id": "1002"})
        self.assertEqual(copied_identity, _identity("1002"))
        copied_actor = ProductOwnerActorIdentity(
            provider="github",
            provider_subject_id="1001",
        ).model_copy(update={"provider_subject_id": "1002"})
        self.assertEqual(copied_actor.provider_subject_id, "1002")
        self.assertEqual(copied_actor.identity_id, _identity("1002").identity_id)
        copied_grant = grant.model_copy(update={"environments": ("testing",)})
        self.assertEqual(copied_grant.environments, ("testing",))
        self.assertNotEqual(copied_grant.grant_id, grant.grant_id)
        with self.assertRaises(ValidationError):
            identity.model_copy(update={"identity_id": "stale-owner-identity"})
        with self.assertRaises(ValidationError):
            _policy(subjects=("1001", "1001"))
        with self.assertRaises(ValidationError):
            ProductOwnerPolicyRecord.model_validate(
                {
                    **_policy().model_dump(exclude={"quorum"}),
                    "quorum": 2,
                }
            )

    def test_policy_sequence_removes_prior_owner_and_rejects_stale_tip(self) -> None:
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(Path(directory))
            first = _policy()
            applied = apply_product_owner_policy(store=store, record=first)
            self.assertEqual(applied.status, "applied")
            second = _policy(
                revision=2,
                subjects=("1002",),
                supersedes_record_id=first.record_id,
            )
            applied_second = apply_product_owner_policy(
                store=store,
                record=second,
                expected_current_record_id=first.record_id,
                expected_current_policy_digest=first.policy_digest,
            )
            self.assertEqual(applied_second.status, "applied")
            with self.assertRaises(ProductOwnerPolicyConflictError):
                apply_product_owner_policy(
                    store=store,
                    record=_policy(
                        revision=3,
                        subjects=("1001",),
                        supersedes_record_id=second.record_id,
                    ),
                    expected_current_record_id=first.record_id,
                    expected_current_policy_digest=first.policy_digest,
                )
            with self.assertRaises(ProductOwnerPolicySequenceError):
                apply_product_owner_policy(
                    store=store,
                    record=_policy().model_copy(update={"status": "superseded"}),
                )

    def test_requirement_is_separate_and_routing_is_not_authority(self) -> None:
        policy = _policy()
        actor = ProductOwnerActorIdentity(provider="github", provider_subject_id="1001")
        not_required = evaluate_product_owner_shadow_authority(
            context=_context(),
            actor=actor,
            policies=(policy,),
            requirements=(),
            routings=(
                ProductOwnerRoutingRecord(
                    product=PRODUCT,
                    system=SYSTEM,
                    routing_revision=1,
                    preferred_owner_identity_ids=(actor.identity_id,),
                    effective_at="2026-08-05T00:00:00Z",
                    source="test",
                    reason="Prefer one Owner for routing only.",
                ),
            ),
        )
        self.assertEqual(not_required.decision, "not_required")
        self.assertFalse(not_required.authoritative)

        routed_non_owner = evaluate_product_owner_shadow_authority(
            context=_context(),
            actor=ProductOwnerActorIdentity(provider="github", provider_subject_id="9999"),
            policies=(policy,),
            requirements=(_requirement(),),
            routings=(
                ProductOwnerRoutingRecord(
                    product=PRODUCT,
                    system=SYSTEM,
                    routing_revision=1,
                    preferred_owner_identity_ids=(
                        ProductOwnerIdentity(
                            provider="github", provider_subject_id="9999"
                        ).identity_id,
                    ),
                    effective_at="2026-08-05T00:00:00Z",
                    source="test",
                    reason="Routing cannot create authority.",
                ),
            ),
        )
        self.assertEqual(routed_non_owner.decision, "denied")
        self.assertEqual(routed_non_owner.reason_code, "actor_not_current_owner")

        missing_provenance = evaluate_product_owner_shadow_authority(
            context=_context(),
            actor=actor,
            policies=(policy,),
            requirements=(_requirement(),),
            routings=(),
        )
        self.assertEqual(missing_provenance.decision, "denied")
        self.assertEqual(missing_provenance.reason_code, "missing_authority_provenance")

    def test_stale_removed_cross_product_and_admin_are_denied(self) -> None:
        first = _policy()
        second = _policy(
            revision=2,
            subjects=("1002",),
            supersedes_record_id=first.record_id,
        )
        current = second.model_copy(update={"status": "active"})
        history = first.model_copy(update={"status": "superseded"})

        removed = evaluate_product_owner_shadow_authority(
            context=_context(),
            actor=ProductOwnerActorIdentity(provider="github", provider_subject_id="1001"),
            policies=(history, current),
            requirements=(_requirement(),),
            routings=(),
            claimed_policy_revision=2,
            claimed_policy_digest=current.policy_digest,
            claimed_requirement_revision=1,
            claimed_requirement_digest=_requirement().requirement_digest,
        )
        self.assertEqual(removed.reason_code, "actor_not_current_owner")

        stale = evaluate_product_owner_shadow_authority(
            context=_context(),
            actor=ProductOwnerActorIdentity(provider="github", provider_subject_id="1002"),
            policies=(history, current),
            requirements=(_requirement(),),
            routings=(),
            claimed_policy_revision=1,
            claimed_policy_digest=history.policy_digest,
            claimed_requirement_revision=1,
            claimed_requirement_digest=_requirement().requirement_digest,
        )
        self.assertEqual(stale.reason_code, "stale_policy")

        stale_requirement = evaluate_product_owner_shadow_authority(
            context=_context(),
            actor=ProductOwnerActorIdentity(provider="github", provider_subject_id="1002"),
            policies=(history, current),
            requirements=(_requirement(),),
            routings=(),
            claimed_policy_revision=2,
            claimed_policy_digest=current.policy_digest,
            claimed_requirement_revision=1,
            claimed_requirement_digest="f" * 64,
        )
        self.assertEqual(stale_requirement.reason_code, "stale_requirement")

        cross_product = evaluate_product_owner_shadow_authority(
            context=_context().model_copy(update={"product": "product-beta"}),
            actor=ProductOwnerActorIdentity(provider="github", provider_subject_id="1002"),
            policies=(history, current),
            requirements=(_requirement(product="product-beta"),),
            routings=(),
        )
        self.assertEqual(cross_product.reason_code, "owner_policy_unavailable")

        unlisted_human = evaluate_product_owner_shadow_authority(
            context=_context(),
            actor=ProductOwnerActorIdentity(provider="github", provider_subject_id="9999"),
            policies=(history, current),
            requirements=(_requirement(),),
            routings=(),
        )
        self.assertEqual(unlisted_human.reason_code, "actor_not_current_owner")
        self.assertFalse(unlisted_human.authoritative)

    def test_policy_scope_without_current_owner_is_unavailable(self) -> None:
        uncovered_policy = ProductOwnerPolicyRecord(
            product=PRODUCT,
            system=SYSTEM,
            policy_revision=1,
            owners=(
                ProductOwnerGrant(
                    identity=_identity("1001"),
                    repository_ids=("202",),
                    environments=(ENVIRONMENT,),
                ),
            ),
            effective_at="2026-08-05T00:00:00Z",
            source="test",
            reason="Leave the evaluated repository outside the policy scope.",
        )
        evaluation = evaluate_product_owner_shadow_authority(
            context=_context(),
            actor=ProductOwnerActorIdentity(provider="github", provider_subject_id="1001"),
            policies=(uncovered_policy,),
            requirements=(_requirement(),),
            routings=(),
        )
        self.assertEqual(evaluation.decision, "unavailable")
        self.assertEqual(evaluation.reason_code, "policy_scope_not_covered")

    def test_mutated_record_cannot_reuse_stale_authority_digest(self) -> None:
        policy = _policy(subjects=("1001",))
        original_digest = policy.policy_digest
        policy.owners = (_grant("9999"),)
        actor = ProductOwnerActorIdentity(provider="github", provider_subject_id="9999")
        evaluation = evaluate_product_owner_shadow_authority(
            context=_context(),
            actor=actor,
            policies=(policy,),
            requirements=(_requirement(),),
            routings=(),
            claimed_policy_revision=1,
            claimed_policy_digest=original_digest,
            claimed_requirement_revision=1,
            claimed_requirement_digest=_requirement().requirement_digest,
        )
        self.assertEqual(evaluation.decision, "unavailable")
        self.assertEqual(evaluation.reason_code, "owner_policy_unavailable")

        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(Path(directory))
            with self.assertRaises(ProductOwnerPolicyConflictError):
                apply_product_owner_policy(store=store, record=policy)
            with self.assertRaises(ProductOwnerPolicyConflictError):
                store.write_product_owner_policy_record(policy)

    def test_postgres_storage_round_trips_separate_revision_streams(self) -> None:
        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(
                database_url=f"sqlite+pysqlite:///{Path(directory) / 'launchplane.sqlite3'}"
            )
            store.ensure_schema()
            policy = _policy()
            requirement = _requirement()
            routing = ProductOwnerRoutingRecord(
                product=PRODUCT,
                system=SYSTEM,
                routing_revision=1,
                preferred_owner_identity_ids=(policy.owners[0].identity.identity_id,),
                effective_at="2026-08-05T00:00:00Z",
                source="test",
                reason="Keep preferred routing separate from authority.",
            )
            self.assertEqual(store.write_product_owner_policy_record(policy), "written")
            self.assertEqual(
                store.write_product_owner_requirement_record(requirement),
                "written",
            )
            self.assertEqual(store.write_product_owner_routing_record(routing), "written")
            self.assertEqual(store.read_product_owner_policy_record(policy.record_id), policy)
            self.assertEqual(
                store.read_product_owner_requirement_record(requirement.record_id),
                requirement,
            )
            self.assertEqual(store.read_product_owner_routing_record(routing.record_id), routing)
            successor_policy = _policy(
                revision=2,
                subjects=("1002",),
                supersedes_record_id=policy.record_id,
            )
            successor_requirement = ProductOwnerRequirementRecord(
                product=PRODUCT,
                system=SYSTEM,
                requirement_revision=2,
                requirements=(),
                effective_at="2026-08-05T01:00:00Z",
                source="test",
                reason="Disable the requirement in a successor revision.",
                supersedes_record_id=requirement.record_id,
            )
            successor_routing = ProductOwnerRoutingRecord(
                product=PRODUCT,
                system=SYSTEM,
                routing_revision=2,
                preferred_owner_identity_ids=(),
                effective_at="2026-08-05T01:00:00Z",
                source="test",
                reason="Clear preferred routing in a successor revision.",
                supersedes_record_id=routing.record_id,
            )
            self.assertEqual(store.write_product_owner_policy_record(successor_policy), "written")
            self.assertEqual(
                store.write_product_owner_requirement_record(successor_requirement),
                "written",
            )
            self.assertEqual(
                store.write_product_owner_routing_record(successor_routing),
                "written",
            )
            self.assertEqual(
                [record.status for record in store.list_product_owner_policy_records()],
                ["active", "superseded"],
            )
            self.assertEqual(
                [record.status for record in store.list_product_owner_requirement_records()],
                ["active", "superseded"],
            )
            self.assertEqual(
                [record.status for record in store.list_product_owner_routing_records()],
                ["active", "superseded"],
            )
            future_payload = _policy(
                revision=3,
                subjects=("1002",),
                supersedes_record_id=successor_policy.record_id,
            ).model_dump(exclude={"effective_at", "policy_digest"})
            future_policy = ProductOwnerPolicyRecord(
                **future_payload,
                effective_at="2999-01-01T00:00:00Z",
            )
            with self.assertRaises(ProductOwnerPolicySequenceError):
                store.compare_and_write_product_owner_policy_record(
                    future_policy,
                    expected_current_record_id=successor_policy.record_id,
                    expected_current_policy_digest=successor_policy.policy_digest,
                )
            self.assertEqual(
                store.list_product_owner_policy_records(status="active"),
                (successor_policy,),
            )
            store.close()

    def test_requirement_and_routing_streams_use_independent_linear_tips(self) -> None:
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(Path(directory))
            requirement = _requirement()
            routing = ProductOwnerRoutingRecord(
                product=PRODUCT,
                system=SYSTEM,
                routing_revision=1,
                preferred_owner_identity_ids=(_identity("1001").identity_id,),
                effective_at="2026-08-05T00:00:00Z",
                source="test",
                reason="Initial advisory routing.",
            )
            self.assertEqual(
                apply_product_owner_requirement(store=store, record=requirement).status,
                "applied",
            )
            self.assertEqual(
                apply_product_owner_routing(store=store, record=routing).status,
                "applied",
            )
            with self.assertRaises(ProductOwnerRequirementConflictError):
                apply_product_owner_requirement(
                    store=store,
                    record=ProductOwnerRequirementRecord(
                        product=PRODUCT,
                        system=SYSTEM,
                        requirement_revision=2,
                        requirements=(),
                        effective_at="2026-08-05T01:00:00Z",
                        source="test",
                        reason="Disable the explicit requirement.",
                        supersedes_record_id=requirement.record_id,
                    ),
                    expected_current_record_id="stale-tip",
                    expected_current_requirement_digest=requirement.requirement_digest,
                )
            with self.assertRaises(ProductOwnerRoutingConflictError):
                apply_product_owner_routing(
                    store=store,
                    record=ProductOwnerRoutingRecord(
                        product=PRODUCT,
                        system=SYSTEM,
                        routing_revision=2,
                        preferred_owner_identity_ids=(),
                        effective_at="2026-08-05T01:00:00Z",
                        source="test",
                        reason="Clear preferred routing without changing authority.",
                        supersedes_record_id=routing.record_id,
                    ),
                    expected_current_record_id=routing.record_id,
                    expected_current_routing_digest="f" * 64,
                )

    def test_shadow_evaluation_rejects_noncurrent_or_future_authority(self) -> None:
        requirement = _requirement()
        gap_policy = _policy(
            revision=2,
            subjects=("1001",),
            supersedes_record_id=_policy().record_id,
        )
        gap = evaluate_product_owner_shadow_authority(
            context=_context(),
            actor=ProductOwnerActorIdentity(provider="github", provider_subject_id="1001"),
            policies=(gap_policy,),
            requirements=(requirement,),
            routings=(),
            evaluated_at="2026-08-05T12:00:00Z",
        )
        self.assertEqual(gap.decision, "unavailable")
        self.assertEqual(gap.reason_code, "owner_policy_unavailable")

        future_requirement = ProductOwnerRequirementRecord(
            product=PRODUCT,
            system=SYSTEM,
            requirement_revision=1,
            requirements=requirement.requirements,
            effective_at="2999-01-01T00:00:00Z",
            source="test",
            reason="Future requirement must not become current early.",
        )
        future = evaluate_product_owner_shadow_authority(
            context=_context(),
            actor=ProductOwnerActorIdentity(provider="github", provider_subject_id="1001"),
            policies=(_policy(),),
            requirements=(future_requirement,),
            routings=(),
            evaluated_at="2026-08-05T12:00:00Z",
        )
        self.assertEqual(future.decision, "unavailable")
        self.assertEqual(future.reason_code, "requirement_history_invalid")

        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(Path(directory))
            future_policy_payload = _policy().model_dump(exclude={"effective_at", "policy_digest"})
            future_policy = ProductOwnerPolicyRecord(
                **future_policy_payload,
                effective_at="2999-01-01T00:00:00Z",
            )
            future_routing = ProductOwnerRoutingRecord(
                product=PRODUCT,
                system=SYSTEM,
                routing_revision=1,
                effective_at="2999-01-01T00:00:00Z",
                source="test",
                reason="Future routing is not current yet.",
            )
            with self.assertRaises(ProductOwnerPolicySequenceError):
                store.write_product_owner_policy_record(future_policy)
            with self.assertRaises(ProductOwnerRequirementSequenceError):
                store.write_product_owner_requirement_record(future_requirement)
            with self.assertRaises(ProductOwnerRoutingSequenceError):
                store.write_product_owner_routing_record(future_routing)
            read_model = get_product_owner_read_model(
                store=store,
                product=PRODUCT,
                system=SYSTEM,
            )
            self.assertIsNone(read_model.current_policy)
            self.assertIsNone(read_model.current_requirement)
            self.assertIsNone(read_model.current_routing)

    def test_successor_effective_at_is_monotonic_and_current(self) -> None:
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(Path(directory))
            first = _policy()
            self.assertEqual(
                apply_product_owner_policy(store=store, record=first).status, "applied"
            )

            regressed_payload = _policy(
                revision=2,
                subjects=("1002",),
                supersedes_record_id=first.record_id,
            ).model_dump(exclude={"effective_at", "policy_digest"})
            regressed = ProductOwnerPolicyRecord(
                **regressed_payload,
                effective_at="2026-08-04T23:59:59Z",
            )
            with self.assertRaises(ProductOwnerPolicySequenceError):
                apply_product_owner_policy(
                    store=store,
                    record=regressed,
                    expected_current_record_id=first.record_id,
                    expected_current_policy_digest=first.policy_digest,
                )

            future_payload = _policy(
                revision=2,
                subjects=("1002",),
                supersedes_record_id=first.record_id,
            ).model_dump(exclude={"effective_at", "policy_digest"})
            future = ProductOwnerPolicyRecord(
                **future_payload,
                effective_at="2999-01-01T00:00:00Z",
            )
            with self.assertRaises(ProductOwnerPolicySequenceError):
                store.compare_and_write_product_owner_policy_record(
                    future,
                    expected_current_record_id=first.record_id,
                    expected_current_policy_digest=first.policy_digest,
                )
            self.assertEqual(
                store.list_product_owner_policy_records(status="active"),
                (first,),
            )


if __name__ == "__main__":
    unittest.main()
