from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from pydantic import ValidationError

from control_plane.change_impact_service import (
    ChangeImpactPolicyConflictError,
    ChangeImpactPolicySequenceError,
    ChangeImpactTrustedCallerBinding,
    apply_change_impact_policy,
    evaluate_change_impact,
)
from control_plane.contracts.change_impact import (
    ChangeImpactChangedFileEvidence,
    ChangeImpactComponentRule,
    ChangeImpactPolicyRecord,
    ChangeImpactProductScope,
    ChangeImpactRepositoryEvidence,
    ChangeImpactStoredEvidence,
    ChangeImpactTarget,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore


REPOSITORY_ID = "1001"
REPOSITORY_OWNER_ID = "2001"
REPOSITORY = "example/shared-addons"
HEAD_SHA = "a" * 40
TREE_SHA = "b" * 40
MERGE_SHA = "d" * 40


def _product(product: str) -> ChangeImpactProductScope:
    return ChangeImpactProductScope(product=product, system="web")


def _policy(
    *,
    revision: int = 1,
    supersedes_record_id: str | None = None,
    effective_at: str = "2026-08-06T00:00:00Z",
) -> ChangeImpactPolicyRecord:
    return ChangeImpactPolicyRecord(
        repository_id=REPOSITORY_ID,
        repository_owner_id=REPOSITORY_OWNER_ID,
        repository=REPOSITORY,
        policy_revision=revision,
        component_rules=(
            ChangeImpactComponentRule(
                component="generic-web-runtime",
                path_prefixes=("src/runtime",),
                affected_products=(_product("generic-web-a"),),
                review_tier="routine",
                reason="Runtime code reaches one generic web product.",
            ),
            ChangeImpactComponentRule(
                component="odoo-shared-addon",
                path_prefixes=("addons/shared",),
                affected_products=(_product("cm-odoo"), _product("opw-odoo")),
                review_tier="routine",
                reason="Shared addon reaches multiple Odoo products.",
            ),
            ChangeImpactComponentRule(
                component="billing-policy",
                path_prefixes=("control_plane/billing",),
                affected_products=(),
                review_tier="sensitive",
                reason="Billing/governance policy is sensitive engineering work.",
            ),
            ChangeImpactComponentRule(
                component="engineering-ci",
                path_prefixes=(".github/workflows",),
                affected_products=(),
                review_tier="routine",
                reason="CI-only engineering change has no product runtime effect.",
            ),
        ),
        effective_at=effective_at,
        source="test",
        reason="Exercise change impact policy.",
        supersedes_record_id=supersedes_record_id,
    )


def _repository_evidence(
    *paths: str,
    head_sha: str = HEAD_SHA,
) -> ChangeImpactRepositoryEvidence:
    return ChangeImpactRepositoryEvidence(
        target=ChangeImpactTarget(
            repository_id=REPOSITORY_ID,
            repository_owner_id=REPOSITORY_OWNER_ID,
            repository=REPOSITORY,
            pull_request_number=20,
            head_sha=head_sha,
            tree_sha=TREE_SHA,
        ),
        merge_commit_sha=MERGE_SHA,
        changed_files=tuple(ChangeImpactChangedFileEvidence(path=path) for path in paths),
    )


def _stored_evidence(
    component: str,
    *,
    kind: str = "dependency",
    products: tuple[ChangeImpactProductScope, ...] = (),
    confidence: str = "known",
) -> ChangeImpactStoredEvidence:
    return ChangeImpactStoredEvidence.model_validate(
        {
            "record_id": f"stored-{kind}-{component}",
            "component": component,
            "kind": kind,
            "affected_products": products,
            "confidence": confidence,
            "reason": "Launchplane storage binds this evidence to the exact target.",
        }
    )


class ChangeImpactEvaluationTests(unittest.TestCase):
    def test_policy_declared_product_scope_is_authoritative_and_storage_can_extend_it(self) -> None:
        policy = _policy()
        without_storage = evaluate_change_impact(
            repository_evidence=_repository_evidence("src/runtime/app.py"),
            policies=(policy,),
            evaluated_at="2026-08-06T01:00:00Z",
        )
        self.assertEqual(without_storage.status, "success")
        self.assertEqual(without_storage.engineering_review_tier, "routine")
        self.assertEqual(without_storage.required_engineering_review_count, 1)
        self.assertEqual(without_storage.owner_impact, "required")

        evaluation = evaluate_change_impact(
            repository_evidence=_repository_evidence("src/runtime/app.py"),
            policies=(policy,),
            stored_evidence=(
                _stored_evidence(
                    "generic-web-runtime",
                    products=(_product("generic-web-a"),),
                ),
            ),
            evaluated_at="2026-08-06T01:00:00Z",
        )
        self.assertEqual(evaluation.status, "success")
        self.assertEqual(evaluation.engineering_review_tier, "routine")
        self.assertEqual(evaluation.required_engineering_review_count, 1)
        self.assertEqual(evaluation.owner_impact, "required")
        self.assertEqual(
            tuple(item.product for item in evaluation.affected_products), ("generic-web-a",)
        )
        self.assertEqual(evaluation.policy_revision, policy.policy_revision)
        self.assertEqual(evaluation.policy_digest, policy.policy_digest)

    def test_multi_product_and_engineering_only_paths_remain_deterministic(self) -> None:
        policy = _policy()
        multi_product = evaluate_change_impact(
            repository_evidence=_repository_evidence("addons/shared/models.py"),
            policies=(policy,),
            stored_evidence=(_stored_evidence("odoo-shared-addon"),),
            evaluated_at="2026-08-06T01:00:00Z",
        )
        self.assertEqual(multi_product.status, "success")
        self.assertEqual(
            tuple(item.product for item in multi_product.affected_products),
            ("cm-odoo", "opw-odoo"),
        )

        engineering_only = evaluate_change_impact(
            repository_evidence=_repository_evidence(".github/workflows/ci.yml"),
            policies=(policy,),
            evaluated_at="2026-08-06T01:00:00Z",
        )
        self.assertEqual(engineering_only.status, "success")
        self.assertEqual(engineering_only.engineering_review_tier, "routine")
        self.assertEqual(engineering_only.owner_impact, "not_required")
        self.assertIsNotNone(engineering_only.coverage)
        assert engineering_only.coverage is not None
        self.assertEqual(
            engineering_only.coverage.model_dump(),
            {
                "state": "complete",
                "unmatched_path_count": 0,
                "unmatched_path_samples": (),
                "truncated": False,
            },
        )

    def test_sensitive_path_cannot_be_downgraded_by_stored_evidence(self) -> None:
        policy = _policy()
        evaluation = evaluate_change_impact(
            repository_evidence=_repository_evidence("control_plane/billing/rules.py"),
            policies=(policy,),
            stored_evidence=(
                _stored_evidence("billing-policy", kind="dependency"),
                _stored_evidence("billing-policy", kind="reviewer"),
            ),
            evaluated_at="2026-08-06T01:00:00Z",
        )
        self.assertEqual(evaluation.status, "success")
        self.assertEqual(evaluation.engineering_review_tier, "sensitive")
        self.assertEqual(evaluation.required_engineering_review_count, 2)
        self.assertEqual(
            tuple(item.source for item in evaluation.matched_evidence),
            ("server_diff", "launchplane_dependency", "launchplane_reviewer"),
        )

    def test_reviewer_evidence_cannot_replace_dependency_authority(self) -> None:
        policy = _policy()
        evaluation = evaluate_change_impact(
            repository_evidence=_repository_evidence("src/runtime/app.py"),
            policies=(policy,),
            stored_evidence=(
                _stored_evidence(
                    "generic-web-runtime",
                    kind="reviewer",
                    products=(_product("generic-web-a"),),
                ),
            ),
            evaluated_at="2026-08-06T01:00:00Z",
        )

        self.assertEqual(evaluation.status, "unknown")
        self.assertEqual(evaluation.engineering_review_tier, "sensitive")
        self.assertEqual(evaluation.required_engineering_review_count, 2)
        self.assertIn(
            "reviewer evidence lacks trusted dependency evidence", evaluation.unknown_evidence[0]
        )

    def test_unknown_path_and_ambiguous_storage_fail_closed(self) -> None:
        policy = _policy()
        unknown_path = evaluate_change_impact(
            repository_evidence=_repository_evidence("unmapped/file.py"),
            policies=(policy,),
            evaluated_at="2026-08-06T01:00:00Z",
        )
        self.assertEqual(unknown_path.status, "unknown")
        self.assertEqual(unknown_path.engineering_review_tier, "sensitive")

        ambiguous = evaluate_change_impact(
            repository_evidence=_repository_evidence("src/runtime/app.py"),
            policies=(policy,),
            stored_evidence=(_stored_evidence("generic-web-runtime", confidence="ambiguous"),),
            evaluated_at="2026-08-06T01:00:00Z",
        )
        self.assertEqual(ambiguous.status, "unknown")
        self.assertEqual(ambiguous.required_engineering_review_count, 2)

    def test_trusted_oidc_repository_and_head_binding_fails_closed(self) -> None:
        policy = _policy()
        stale_head = evaluate_change_impact(
            repository_evidence=_repository_evidence(".github/workflows/ci.yml"),
            policies=(policy,),
            trusted_caller_binding=ChangeImpactTrustedCallerBinding(
                repository=REPOSITORY,
                repository_id=REPOSITORY_ID,
                repository_owner_id=REPOSITORY_OWNER_ID,
                head_sha="c" * 40,
            ),
            evaluated_at="2026-08-06T01:00:00Z",
        )
        self.assertEqual(stale_head.status, "stale_head")
        self.assertEqual(stale_head.required_engineering_review_count, 2)
        self.assertIsNone(stale_head.coverage)

        merge_ref = evaluate_change_impact(
            repository_evidence=_repository_evidence(".github/workflows/ci.yml"),
            policies=(policy,),
            trusted_caller_binding=ChangeImpactTrustedCallerBinding(
                repository=REPOSITORY,
                repository_id=REPOSITORY_ID,
                repository_owner_id=REPOSITORY_OWNER_ID,
                head_sha=MERGE_SHA,
                ref="refs/pull/20/merge",
            ),
            evaluated_at="2026-08-06T01:00:00Z",
        )
        self.assertEqual(merge_ref.status, "success")

        wrong_repository = evaluate_change_impact(
            repository_evidence=_repository_evidence(".github/workflows/ci.yml"),
            policies=(policy,),
            trusted_caller_binding=ChangeImpactTrustedCallerBinding(
                repository="example/other",
                repository_id="9999",
                repository_owner_id=REPOSITORY_OWNER_ID,
                head_sha=HEAD_SHA,
            ),
            evaluated_at="2026-08-06T01:00:00Z",
        )
        self.assertEqual(wrong_repository.status, "unknown")
        self.assertEqual(wrong_repository.reason_code, "caller_repository_mismatch")

    def test_change_impact_outputs_are_frozen(self) -> None:
        evaluation = evaluate_change_impact(
            repository_evidence=_repository_evidence(".github/workflows/ci.yml"),
            policies=(_policy(),),
            evaluated_at="2026-08-06T01:00:00Z",
        )
        with self.assertRaises(ValidationError):
            evaluation.status = "unknown"

    def test_policy_storage_revisions_round_trip_filesystem_and_sqlite(self) -> None:
        first = _policy()
        second = _policy(revision=2, supersedes_record_id=first.record_id)
        with TemporaryDirectory() as directory:
            filesystem_store = FilesystemRecordStore(Path(directory) / "records")
            self.assertEqual(
                apply_change_impact_policy(store=filesystem_store, record=first).status,
                "applied",
            )
            self.assertEqual(
                apply_change_impact_policy(
                    store=filesystem_store,
                    record=second,
                    expected_current_record_id=first.record_id,
                    expected_current_policy_digest=first.policy_digest,
                ).status,
                "applied",
            )
            self.assertEqual(
                filesystem_store.list_change_impact_policy_records(status="active")[0].record_id,
                second.record_id,
            )
            with self.assertRaises(ChangeImpactPolicyConflictError):
                apply_change_impact_policy(store=filesystem_store, record=first)
            with self.assertRaises(ChangeImpactPolicyConflictError):
                apply_change_impact_policy(
                    store=filesystem_store,
                    record=_policy(revision=3, supersedes_record_id=second.record_id),
                    expected_current_record_id=first.record_id,
                    expected_current_policy_digest=first.policy_digest,
                )

        with TemporaryDirectory() as directory:
            postgres_store = PostgresRecordStore(
                database_url=f"sqlite+pysqlite:///{Path(directory) / 'records.db'}"
            )
            postgres_store.ensure_schema()
            postgres_store.write_change_impact_policy_record(first)
            self.assertEqual(
                postgres_store.read_change_impact_policy_record(first.record_id).policy_digest,
                first.policy_digest,
            )

    def test_dry_run_rejects_future_effective_policy(self) -> None:
        future_effective_at = (
            (datetime.now(timezone.utc) + timedelta(days=1))
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(Path(directory))
            with self.assertRaises(ChangeImpactPolicySequenceError):
                apply_change_impact_policy(
                    store=store,
                    record=_policy(effective_at=future_effective_at),
                    mode="dry_run",
                )


if __name__ == "__main__":
    unittest.main()
