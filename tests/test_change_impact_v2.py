from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from pydantic import ValidationError

from control_plane.change_impact_matching import select_change_impact_path_rule
from control_plane.change_impact_service import (
    ChangeImpactPolicySequenceError,
    apply_change_impact_policy,
    evaluate_change_impact,
)
from control_plane.contracts.change_impact import (
    ChangeImpactComponentRule,
    ChangeImpactPolicyRecord,
    ChangeImpactProductScope,
    ChangeImpactRepositoryEvidence,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from tests.test_change_impact import _policy, _product, _stored_evidence
from tests.test_change_impact import _repository_evidence as _legacy_evidence


def _repository_evidence(*paths: str) -> ChangeImpactRepositoryEvidence:
    evidence = _legacy_evidence(*paths)
    return evidence.model_copy(
        update={
            "changed_files": tuple(
                file.model_copy(update={"change_kind": "modified"})
                for file in evidence.changed_files
            )
        }
    )


def _v2_policy(*rules: ChangeImpactComponentRule) -> ChangeImpactPolicyRecord:
    payload = _policy().model_dump(mode="json", exclude={"policy_digest"})
    payload.update(classification_model="v2", component_rules=list(rules))
    return ChangeImpactPolicyRecord.model_validate(payload)


def _rule(
    component: str,
    prefix: str,
    *,
    products: tuple[ChangeImpactProductScope, ...] = (),
    governance: bool = False,
    production: bool = False,
) -> ChangeImpactComponentRule:
    return ChangeImpactComponentRule(
        component=component,
        path_prefixes=(prefix,),
        affected_products=products,
        product_impact=None if products else "declared_none",
        governance_impact=governance,
        production_affecting=production,
        reason="Explicit test authority.",
    )


class ChangeImpactV2Tests(unittest.TestCase):
    def test_legacy_stored_policy_digest_and_rule_ids_are_unchanged(self) -> None:
        policy = _policy()
        self.assertEqual(
            policy.policy_digest,
            "fec4cc7ea106e369cf2a269d12e4e495a1e0a58d05edffb55a5cc1ab7c81baad",
        )
        self.assertEqual(
            tuple(rule.rule_id for rule in policy.component_rules),
            (
                "change-impact-rule-0f4099000cbeb1f38fe55f12",
                "change-impact-rule-5f5187f2ca18348a40c9fbff",
                "change-impact-rule-8713eef0d83c98f3c91ab9f3",
                "change-impact-rule-c4680340405bc752e3fe684d",
            ),
        )

    def test_legacy_nested_rules_still_accumulate_products(self) -> None:
        policy = _v2_policy(
            _rule("parent", "src", products=(_product("a"),)),
            _rule("child", "src/child", products=(_product("b"),)),
        )
        payload = policy.model_dump(exclude={"policy_digest", "classification_model"})
        legacy = ChangeImpactPolicyRecord.model_validate(payload)
        result = evaluate_change_impact(
            repository_evidence=_repository_evidence("src/child/code.py"), policies=(legacy,)
        )
        self.assertEqual(tuple(p.product for p in result.affected_products), ("a", "b"))
        self.assertIsNone(result.classification_model)
        self.assertIsNone(result.governance_impact)

    def test_winner_can_declare_engineering_only_without_lowering_ancestor_floor(self) -> None:
        parent = _rule("parent", "src", products=(_product("a"),), governance=True)
        child = _rule("docs", "src/docs")
        policy = _v2_policy(parent, child)
        result = evaluate_change_impact(
            repository_evidence=_repository_evidence("src/docs/guide.md"), policies=(policy,)
        )
        self.assertEqual(
            (
                result.status,
                result.owner_impact,
                result.affected_products,
                result.required_engineering_review_count,
            ),
            ("success", "not_required", (), 2),
        )
        self.assertTrue(result.governance_impact)
        self.assertEqual(
            [
                (e.component, e.review_floor_only, e.affected_products)
                for e in result.matched_evidence
            ],
            [("parent", True, ()), ("docs", False, ())],
        )
        selection = select_change_impact_path_rule(
            path="src/docs/guide.md", rules=policy.component_rules
        )
        assert selection is not None
        self.assertEqual((selection.winner.rule, selection.winner.prefix), (child, "src/docs"))

    def test_legacy_production_extensions_require_a_changed_file_match(self) -> None:
        policy = _v2_policy(
            _rule("runtime", "src", products=(_product("a"),), production=True),
            _rule("docs", "docs", products=(_product("b"),)),
        )
        legacy = ChangeImpactPolicyRecord.model_validate(
            policy.model_dump(exclude={"policy_digest", "classification_model"})
        )
        for path, status, products in (
            ("src/code.py", "success", ("a", "c")),
            ("docs/guide.md", "unknown", ()),
        ):
            with self.subTest(path=path):
                result = evaluate_change_impact(
                    repository_evidence=_repository_evidence(path),
                    policies=(legacy,),
                    stored_evidence=(_stored_evidence("runtime", products=(_product("c"),)),),
                )
                self.assertEqual(
                    (result.status, tuple(p.product for p in result.production_affecting_products)),
                    (status, products),
                )

    def test_inherited_production_floor_applies_to_winner_and_trusted_extensions(self) -> None:
        result = evaluate_change_impact(
            repository_evidence=_repository_evidence("src/child/code.py"),
            policies=(
                _v2_policy(
                    _rule("parent", "src", products=(_product("a"),), production=True),
                    _rule("child", "src/child", products=(_product("b"),)),
                ),
            ),
            stored_evidence=(_stored_evidence("child", products=(_product("c"),)),),
        )
        self.assertEqual(result.status, "success")
        self.assertEqual(tuple(p.product for p in result.affected_products), ("b", "c"))
        self.assertEqual(tuple(p.product for p in result.production_affecting_products), ("b", "c"))

    def test_floor_component_cannot_license_stored_product_extension(self) -> None:
        result = evaluate_change_impact(
            repository_evidence=_repository_evidence("src/docs/guide.md"),
            policies=(_v2_policy(_rule("parent", "src"), _rule("docs", "src/docs")),),
            stored_evidence=(_stored_evidence("parent", products=(_product("a"),)),),
        )
        self.assertEqual(
            (result.status, result.owner_impact, result.affected_products),
            ("unknown", "unknown", ()),
        )

    def test_old_and_new_rename_paths_union_product_and_governance_impact(self) -> None:
        result = evaluate_change_impact(
            repository_evidence=_repository_evidence("src/code.py", "docs/code.py"),
            policies=(
                _v2_policy(
                    _rule("product", "src", products=(_product("a"),)),
                    _rule("docs", "docs", governance=True),
                ),
            ),
        )
        self.assertEqual(
            (result.owner_impact, result.required_engineering_review_count), ("required", 2)
        )
        self.assertEqual(tuple(p.product for p in result.affected_products), ("a",))

    def test_invalid_v2_rule_sets_fail_dry_run_and_evaluation(self) -> None:
        invalid_sets = (
            (_rule("first", "src"), _rule("second", "src")),
            (_rule("dot", "src/./docs"),),
            (
                ChangeImpactComponentRule(
                    component="implicit", path_prefixes=("src",), reason="No authority."
                ),
            ),
        )
        for rules in invalid_sets:
            with self.subTest(rules=rules), TemporaryDirectory() as directory:
                policy = _v2_policy(*rules)
                with self.assertRaises(ValueError):
                    apply_change_impact_policy(
                        store=FilesystemRecordStore(Path(directory)), record=policy, mode="dry_run"
                    )
                result = evaluate_change_impact(
                    repository_evidence=_repository_evidence("src/file.py"), policies=(policy,)
                )
                self.assertEqual(
                    (result.status, result.reason_code), ("unknown", "policy_rules_invalid")
                )

    def test_root_prefix_cannot_become_a_v2_repository_default(self) -> None:
        payload = _policy().model_dump(exclude={"policy_digest"})
        payload.update(
            classification_model="v2",
            component_rules=[
                {
                    "component": "root",
                    "path_prefixes": ["/"],
                    "product_impact": "declared_none",
                    "reason": "Attempted root default.",
                }
            ],
        )
        policy = ChangeImpactPolicyRecord.model_validate(payload)
        result = evaluate_change_impact(
            repository_evidence=_repository_evidence("src/file.py"), policies=(policy,)
        )
        self.assertEqual((result.status, result.reason_code), ("unknown", "policy_rules_invalid"))

    def test_v2_dry_run_succeeds_but_apply_cannot_activate_incomplete_support(self) -> None:
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(Path(directory))
            policy = _v2_policy(_rule("docs", "docs"))
            self.assertEqual(
                apply_change_impact_policy(store=store, record=policy, mode="dry_run").status,
                "would_apply",
            )
            with self.assertRaises(ChangeImpactPolicySequenceError):
                apply_change_impact_policy(store=store, record=policy)
            self.assertEqual(store.list_change_impact_policy_records(), ())

    def test_v2_fields_cannot_hide_in_legacy_policy(self) -> None:
        payload = _policy().model_dump(mode="json", exclude={"policy_digest"})
        payload["component_rules"] = [_rule("docs", "docs").model_dump()]
        with self.assertRaises(ValidationError):
            ChangeImpactPolicyRecord.model_validate(payload)

    def test_missing_mapping_stays_unknown_despite_explicit_engineering_rule(self) -> None:
        result = evaluate_change_impact(
            repository_evidence=_repository_evidence("unknown/file.py"),
            policies=(_v2_policy(_rule("docs", "docs")),),
        )
        self.assertEqual(
            (result.reason_code, result.owner_impact), ("policy_coverage_incomplete", "unknown")
        )
