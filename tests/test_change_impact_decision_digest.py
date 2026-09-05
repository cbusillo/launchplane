import unittest

from pydantic import ValidationError

from control_plane.change_impact_decision_digest import (
    CHANGE_IMPACT_DECISION_FIELDS,
    build_change_impact_decision_digest,
)
from control_plane.change_impact_service import evaluate_change_impact
from control_plane.contracts.change_impact import (
    ChangeImpactChangedFileEvidence,
    ChangeImpactBaseEvidence,
    ChangeImpactComponentRule,
    ChangeImpactEvaluation,
    ChangeImpactPolicyRecord,
)
from tests.test_change_impact import _product, _stored_evidence
from tests.test_change_impact_generated import _generated
from tests.test_change_impact_rename import _resolve
from tests.test_change_impact_v2 import _repository_evidence, _rule, _v2_policy


def _rule_with(rule: ChangeImpactComponentRule, **changes: object) -> ChangeImpactComponentRule:
    return ChangeImpactComponentRule.model_validate(rule.model_dump(exclude={"rule_id"}) | changes)


def _decision(policy: ChangeImpactPolicyRecord) -> ChangeImpactEvaluation:
    return evaluate_change_impact(
        repository_evidence=_repository_evidence("src/api/file.py"), policies=(policy,)
    )


def _effective(evaluation: ChangeImpactEvaluation) -> object:
    return evaluation.model_dump(
        include={
            "status",
            "reason_code",
            "owner_impact",
            "engineering_review_tier",
            "required_engineering_review_count",
            "affected_products",
            "production_affecting_products",
            "governance_impact",
            "coverage",
        }
    )


class ChangeImpactDecisionDigestTests(unittest.TestCase):
    def test_contract_growth_requires_explicit_hash_or_provenance_disposition(self) -> None:
        # A new contract field must be reviewed against the frozen v2 projection.
        # Target is bound separately; IDs and evidence decorations are provenance;
        # the binding pair is the output. Schema versions are bound explicitly.
        excluded = {
            "target",
            "policy_record_id",
            "policy_revision",
            "policy_digest",
            "matched_evidence",
            "unknown_evidence",
            "binding_hash_version",
            "change_impact_decision_digest",
        }
        self.assertEqual(
            set(ChangeImpactEvaluation.model_fields)
            | set(ChangeImpactEvaluation.model_computed_fields),
            CHANGE_IMPACT_DECISION_FIELDS | excluded,
        )
        # Prefixes are scoped to matches, authority fields to their resolved mode.
        # Only the content-derived rule ID and explanatory reason are excluded.
        scoped = {
            "schema_version",
            "component",
            "path_prefixes",
            "affected_products",
            "review_tier",
            "production_affecting",
            "product_impact",
            "governance_impact",
            "generated_by",
        }
        self.assertEqual(
            set(ChangeImpactComponentRule.model_fields)
            | set(ChangeImpactComponentRule.model_computed_fields),
            scoped | {"rule_id", "reason"},
        )

    def test_policy_contract_rejects_duplicate_components_and_routine_unknown_default(self) -> None:
        with self.assertRaises(ValidationError):
            _v2_policy(_rule("same", "src"), _rule("same", "other"))
        with self.assertRaises(ValidationError):
            ChangeImpactPolicyRecord.model_validate(
                _v2_policy(_rule("source", "src")).model_dump(exclude={"policy_digest"})
                | {"default_unknown_review_tier": "routine"}
            )

    def test_evaluation_schema_version_is_part_of_the_scoped_protocol(self) -> None:
        policy = _v2_policy(_rule("source", "src", products=(_product("a"),)))
        evidence = _repository_evidence("src/api/file.py")
        baseline = evaluate_change_impact(repository_evidence=evidence, policies=(policy,))
        future = ChangeImpactEvaluation.model_validate(
            baseline.model_dump() | {"schema_version": 2}
        )
        digest = build_change_impact_decision_digest(
            repository_evidence=evidence,
            policy=policy,
            stored_evidence=(),
            generated_boundaries={},
            evaluation=future,
        )
        self.assertNotEqual(digest, baseline.change_impact_decision_digest)

    def test_head_tree_and_base_identity_each_require_a_new_digest(self) -> None:
        policy = _v2_policy(_rule("source", "src", products=(_product("a"),)))
        evidence = _repository_evidence("src/api/file.py")
        baseline = evaluate_change_impact(repository_evidence=evidence, policies=(policy,))
        for updated in (
            evidence.model_copy(
                update={"target": evidence.target.model_copy(update={"head_sha": "c" * 40})}
            ),
            evidence.model_copy(
                update={"target": evidence.target.model_copy(update={"tree_sha": "d" * 40})}
            ),
            evidence.model_copy(
                update={"base": ChangeImpactBaseEvidence(base_ref="main", base_sha="e" * 40)}
            ),
        ):
            result = evaluate_change_impact(repository_evidence=updated, policies=(policy,))
            self.assertEqual(_effective(result), _effective(baseline))
            self.assertNotEqual(
                result.change_impact_decision_digest, baseline.change_impact_decision_digest
            )

    def test_mutations_bind_effective_authority_but_ignore_unused_policy_and_prose(self) -> None:
        parent = _rule("parent", "src", products=(_product("parent"),))
        child = _rule("child", "src/api", products=(_product("a"),))
        baseline_policy = _v2_policy(parent, child)
        baseline = _decision(baseline_policy)
        self.assertEqual(baseline.status, "success")
        self.assertEqual(baseline.binding_hash_version, 2)
        self.assertEqual(ChangeImpactEvaluation.model_validate(baseline.model_dump()), baseline)
        cases = [
            ("prose", parent, _rule_with(child, reason="New rationale."), False),
            (
                "unused prefix",
                parent,
                _rule_with(child, path_prefixes=("src/api", "unused")),
                False,
            ),
            (
                "ancestor products",
                _rule_with(parent, affected_products=(_product("other"),)),
                child,
                False,
            ),
            ("winner component", parent, _rule_with(child, component="other"), True),
            ("specificity", parent, _rule_with(child, path_prefixes=("src/api/file.py",)), True),
            ("sensitive floor", _rule_with(parent, review_tier="sensitive"), child, True),
            ("governance floor", _rule_with(parent, governance_impact=True), child, True),
            ("production floor", _rule_with(parent, production_affecting=True), child, True),
            (
                "none",
                parent,
                _rule_with(child, affected_products=(), product_impact="declared_none"),
                True,
            ),
        ]
        for field in ("product", "system", "owner_action", "owner_environment"):
            scope = _product("a").model_copy(update={field: "changed"})
            cases.append((field, parent, _rule_with(child, affected_products=(scope,)), True))
        for name, ancestor, winner, changed in cases:
            with self.subTest(name=name):
                result = _decision(_v2_policy(ancestor, winner))
                self.assertEqual(result.status, "success")
                self.assertEqual(
                    result.change_impact_decision_digest != baseline.change_impact_decision_digest,
                    changed,
                )
                if _effective(result) != _effective(baseline):
                    self.assertNotEqual(
                        result.change_impact_decision_digest, baseline.change_impact_decision_digest
                    )
                if not changed:
                    self.assertEqual(_effective(result), _effective(baseline))
        revised = ChangeImpactPolicyRecord.model_validate(
            baseline_policy.model_dump(exclude={"record_id", "policy_digest"})
            | {
                "policy_revision": 2,
                "supersedes_record_id": baseline_policy.record_id,
                "reason": "Unrelated new revision.",
                "component_rules": (
                    child,
                    _rule("unrelated", "unrelated", governance=True),
                    parent,
                ),
            }
        )
        current = _decision(revised)
        self.assertNotEqual(current.policy_digest, baseline.policy_digest)
        self.assertEqual(
            current.change_impact_decision_digest, baseline.change_impact_decision_digest
        )
        self.assertEqual(_effective(current), _effective(baseline))

    def test_trusted_evidence_semantics_cover_authority_without_provenance_churn(self) -> None:
        policy = _v2_policy(_rule("child", "src/api", products=(_product("a"),)))
        dependency = _stored_evidence("child", products=(_product("b"),))
        reviewer = _stored_evidence("child", kind="reviewer", products=(_product("c"),))
        results = [
            evaluate_change_impact(
                repository_evidence=_repository_evidence("src/api/file.py"),
                policies=(policy,),
                stored_evidence=evidence,
            )
            for evidence in (
                (dependency, reviewer),
                (
                    reviewer,
                    dependency.model_copy(update={"record_id": "reissued", "reason": "New prose."}),
                    dependency,
                ),
                (dependency, reviewer.model_copy(update={"affected_products": (_product("d"),)})),
                (reviewer,),
            )
        ]
        original, reordered, changed, untrusted = results
        self.assertEqual(_effective(original), _effective(reordered))
        self.assertEqual(
            original.change_impact_decision_digest, reordered.change_impact_decision_digest
        )
        self.assertNotEqual(_effective(original), _effective(changed))
        self.assertNotEqual(
            original.change_impact_decision_digest, changed.change_impact_decision_digest
        )
        self.assertEqual(
            (
                untrusted.status,
                untrusted.binding_hash_version,
                untrusted.change_impact_decision_digest,
            ),
            ("unknown", None, None),
        )

    def test_generated_authority_and_ancestor_floors_are_covered(self) -> None:
        parent = _rule("parent", "src", products=(_product("unused"),), governance=True)
        source = _rule("source", "src/api", products=(_product("a"),))
        policies = (
            _v2_policy(parent, source, _generated()),
            _v2_policy(
                _rule_with(parent, affected_products=(_product("other"),)), source, _generated()
            ),
            _v2_policy(_rule_with(parent, governance_impact=None), source, _generated()),
            _v2_policy(
                parent, _rule_with(source, affected_products=(_product("b"),)), _generated()
            ),
            _v2_policy(
                parent,
                _rule_with(source, component="replacement"),
                _generated("artifact", "replacement"),
            ),
        )
        results = [
            evaluate_change_impact(
                repository_evidence=_repository_evidence("generated/artifact/schema.json"),
                policies=(policy,),
            )
            for policy in policies
        ]
        baseline = results[0]
        for index, result in enumerate(results[1:], 1):
            with self.subTest(index=index):
                self.assertEqual(result.status, "success")
                self.assertEqual(
                    result.change_impact_decision_digest == baseline.change_impact_decision_digest,
                    index == 1,
                )
                if index == 1:
                    self.assertEqual(_effective(result), _effective(baseline))

    def test_rename_is_distinct_from_remove_add_and_missing_origin_cannot_bind(self) -> None:
        policy = _v2_policy(_rule("source", "src", products=(_product("a"),)))
        evidence = _repository_evidence("src/old.py", "src/new.py")
        results = []
        for changed_files in (
            (
                ChangeImpactChangedFileEvidence(path="src/old.py", change_kind="removed"),
                ChangeImpactChangedFileEvidence(
                    path="src/new.py", change_kind="renamed", previous_path="src/old.py"
                ),
            ),
            (
                ChangeImpactChangedFileEvidence(path="src/old.py", change_kind="removed"),
                ChangeImpactChangedFileEvidence(path="src/new.py", change_kind="added"),
            ),
            (ChangeImpactChangedFileEvidence(path="src/new.py", change_kind="renamed"),),
            (
                ChangeImpactChangedFileEvidence(
                    path="src/new.py", change_kind="renamed", previous_path="src/missing.py"
                ),
            ),
            (ChangeImpactChangedFileEvidence(path="src/new.py"),),
        ):
            # Validate to canonical path order, like the provider boundary.
            current = type(evidence).model_validate(
                evidence.model_dump() | {"changed_files": changed_files}
            )
            results.append(evaluate_change_impact(repository_evidence=current, policies=(policy,)))
        self.assertEqual(_effective(results[0]), _effective(results[1]))
        self.assertNotEqual(
            results[0].change_impact_decision_digest, results[1].change_impact_decision_digest
        )
        for result in results[2:]:
            self.assertEqual(
                (result.status, result.binding_hash_version, result.change_impact_decision_digest),
                ("unknown", None, None),
            )

    def test_real_provider_swap_pairs_bind_independently_of_evidence_order(self) -> None:
        evidence = _resolve(
            [
                {"filename": "src/a.py", "status": "renamed", "previous_filename": "src/b.py"},
                {"filename": "src/b.py", "status": "renamed", "previous_filename": "src/a.py"},
            ]
        )
        policy = _v2_policy(_rule("source", "src", products=(_product("a"),)))
        forward = evaluate_change_impact(repository_evidence=evidence, policies=(policy,))
        reversed_evidence = evidence.model_copy(
            update={"changed_files": tuple(reversed(evidence.changed_files))}
        )
        backward = evaluate_change_impact(repository_evidence=reversed_evidence, policies=(policy,))
        self.assertEqual(forward.status, "success")
        self.assertEqual(_effective(forward), _effective(backward))
        self.assertEqual(
            forward.change_impact_decision_digest, backward.change_impact_decision_digest
        )

    def test_partial_coverage_retains_known_products_and_sensitive_typed_fallback(self) -> None:
        result = evaluate_change_impact(
            repository_evidence=_repository_evidence("src/api/file.py", "unmapped/file.py"),
            policies=(_v2_policy(_rule("source", "src", products=(_product("a"),))),),
        )
        self.assertEqual(
            (
                result.reason_code,
                result.engineering_review_tier,
                result.required_engineering_review_count,
            ),
            ("policy_coverage_incomplete", "sensitive", 2),
        )
        self.assertEqual(tuple(p.product for p in result.affected_products), ("a",))
        self.assertEqual(
            (result.binding_hash_version, result.change_impact_decision_digest), (None, None)
        )
