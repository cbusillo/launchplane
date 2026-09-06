from dataclasses import asdict
import unittest

from pydantic import ValidationError

from control_plane.change_impact_generated import resolve_generated_boundaries
from control_plane.change_impact_matching import validate_change_impact_v2_policy
from control_plane.change_impact_service import evaluate_change_impact
from control_plane.contracts.change_impact import (
    ChangeImpactComponentRule,
    ChangeImpactPolicyRecord,
)
from tests.test_change_impact import _policy, _product, _stored_evidence
from tests.test_change_impact_v2 import _repository_evidence
from tests.test_change_impact_v2 import _rule, _v2_policy


def _generated(*generators: str, component: str = "artifact") -> ChangeImpactComponentRule:
    return ChangeImpactComponentRule(
        component=component,
        path_prefixes=(f"generated/{component}",),
        generated_by=generators or ("source",),
        reason="Generator attribution.",
    )


class GeneratedBoundaryTests(unittest.TestCase):
    def test_generated_scope_uses_leaves_and_preserves_all_floor_axes(self) -> None:
        parent = _rule("parent", "src", products=(_product("unrelated"),), governance=True)
        source = _rule("source", "src/api", products=(_product("api"),), production=True)
        artifact_parent = _rule("generated", "generated", products=(_product("also-unrelated"),))
        policy = _v2_policy(parent, source, artifact_parent, _generated())
        result = evaluate_change_impact(
            repository_evidence=_repository_evidence("generated/artifact/schema.json"),
            policies=(policy,),
        )
        self.assertEqual(
            (
                result.status,
                result.owner_impact,
                result.required_engineering_review_count,
                tuple(p.product for p in result.affected_products),
                tuple(p.product for p in result.production_affecting_products),
                result.governance_impact,
            ),
            ("success", "required", 2, ("api",), ("api",), True),
        )
        winner = next(e for e in result.matched_evidence if not e.review_floor_only)
        self.assertEqual(tuple(p.product for p in winner.affected_products), ("api",))

    def test_all_none_and_mixed_fanout_keep_distinct_authority(self) -> None:
        none = _rule("none", "docs")
        direct = _rule("source", "src", products=(_product("api"),))
        for leaves, products, owner in (
            (("none",), (), "not_required"),
            (("none", "source"), ("api",), "required"),
        ):
            with self.subTest(leaves=leaves):
                policy = _v2_policy(none, direct, _generated(*leaves))
                boundary = resolve_generated_boundaries(policy)["artifact"]
                result = evaluate_change_impact(
                    repository_evidence=_repository_evidence("generated/artifact/schema.json"),
                    policies=(policy,),
                )
                self.assertEqual(
                    (boundary.declared_none, result.owner_impact), (not products, owner)
                )
                self.assertEqual(tuple(p.product for p in result.affected_products), products)

    def test_narrow_direct_rule_does_not_expand_generated_ancestor(self) -> None:
        generated = _generated().model_copy(update={"governance_impact": True, "rule_id": ""})
        policy = _v2_policy(
            _rule("source", "src", products=(_product("unused"),), production=True),
            generated,
            _rule("handwritten", "generated/artifact/manual"),
        )
        result = evaluate_change_impact(
            repository_evidence=_repository_evidence("generated/artifact/manual/readme.md"),
            policies=(policy,),
        )
        self.assertEqual(
            (
                result.affected_products,
                result.production_affecting_products,
                result.governance_impact,
                result.required_engineering_review_count,
            ),
            ((), (), True, 2),
        )

    def test_every_generator_prefix_contributes_ancestor_floors(self) -> None:
        source = ChangeImpactComponentRule(
            component="source",
            path_prefixes=("src/api", "contracts/api"),
            affected_products=(_product("api"),),
            reason="Two generator roots.",
        )
        policy = _v2_policy(
            source,
            _generated(),
            _rule("src", "src"),
            _rule("contracts", "contracts", production=True, governance=True),
        )
        boundary = resolve_generated_boundaries(policy)["artifact"]
        self.assertEqual(
            tuple((a.component, a.prefix) for a in boundary.generators[0].ancestor_floors),
            (("contracts", "contracts"), ("src", "src")),
        )
        self.assertEqual(
            (boundary.floors.sensitive, boundary.floors.governance, boundary.floors.production),
            (True, True, True),
        )

    def test_losing_generated_generator_ancestor_does_not_expand(self) -> None:
        policy = _v2_policy(
            _generated(),
            _generated("unused", component="ancestor").model_copy(
                update={"path_prefixes": ("src",), "rule_id": ""}
            ),
            _rule("source", "src/api", products=(_product("api"),)),
            _rule("unused", "other", products=(_product("unused"),), governance=True),
        )
        boundary = resolve_generated_boundaries(policy)["artifact"]
        self.assertEqual(tuple(p.product for p in boundary.affected_products), ("api",))
        self.assertFalse(boundary.floors.governance)

    def test_mapping_contributions_change_even_when_aggregate_is_identical(self) -> None:
        leaves = (
            _rule("a", "a", products=(_product("api"),)),
            _rule("b", "b", products=(_product("api"),)),
        )
        first = resolve_generated_boundaries(_v2_policy(*leaves, _generated("a")))["artifact"]
        second = resolve_generated_boundaries(_v2_policy(*leaves, _generated("b")))["artifact"]
        self.assertEqual(first.affected_products, second.affected_products)
        self.assertNotEqual(asdict(first), asdict(second))

    def test_resolution_ignores_prose_and_input_order_and_is_immutable(self) -> None:
        leaves = (_rule("a", "a", products=(_product("api"),)), _rule("b", "b"))
        policy = _v2_policy(*leaves, _generated("b", "a"))
        original = policy.model_dump(mode="json")
        resolved = resolve_generated_boundaries(policy)
        payload = policy.model_dump(exclude={"policy_digest"})
        payload["component_rules"] = [
            rule.model_copy(update={"reason": "Different prose.", "rule_id": ""}).model_dump()
            for rule in reversed(policy.component_rules)
        ]
        changed = ChangeImpactPolicyRecord.model_validate(payload)
        self.assertEqual(resolved, resolve_generated_boundaries(changed))
        self.assertEqual(resolved, resolve_generated_boundaries(policy))
        self.assertEqual(policy.model_dump(mode="json"), original)
        with self.assertRaises(TypeError):
            resolved["artifact"] = resolved["artifact"]  # type: ignore[index]

    def test_generator_edges_do_not_license_unmatched_stored_evidence(self) -> None:
        policy = _v2_policy(_rule("source", "src", products=(_product("api"),)), _generated())
        result = evaluate_change_impact(
            repository_evidence=_repository_evidence("generated/artifact/schema.json"),
            policies=(policy,),
            stored_evidence=(_stored_evidence("source", products=(_product("extra"),)),),
        )
        self.assertEqual((result.status, result.owner_impact), ("unknown", "required"))
        self.assertEqual(tuple(p.product for p in result.affected_products), ("api",))

    def test_artifact_stored_extension_keeps_generator_production_floor(self) -> None:
        policy = _v2_policy(
            _rule("source", "src", products=(_product("api"),), production=True, governance=True),
            _generated(),
        )
        result = evaluate_change_impact(
            repository_evidence=_repository_evidence("generated/artifact/schema.json"),
            policies=(policy,),
            stored_evidence=(_stored_evidence("artifact", products=(_product("extra"),)),),
        )
        self.assertEqual(result.status, "success")
        self.assertEqual(
            tuple(p.product for p in result.production_affecting_products), ("api", "extra")
        )
        self.assertEqual(
            tuple(
                (row.review_tier, row.governance_impact, row.production_affecting)
                for row in result.matched_evidence
            ),
            (("sensitive", True, True), ("sensitive", True, True)),
        )

    def test_invalid_generator_graphs_fail_before_evaluation(self) -> None:
        cases = (
            ((_generated("missing"),), "missing"),
            ((_generated("artifact"),), "itself"),
            ((_generated(), _generated("artifact", component="source")), "terminal"),
            (
                (
                    _generated(),
                    ChangeImpactComponentRule(
                        component="source", path_prefixes=("src",), reason="Implicit."
                    ),
                ),
                "implicit",
            ),
        )
        for rules, message in cases:
            with self.subTest(message=message):
                policy = _v2_policy(*rules)
                with self.assertRaisesRegex(ValueError, message):
                    resolve_generated_boundaries(policy)
                result = evaluate_change_impact(
                    repository_evidence=_repository_evidence("generated/artifact/file"),
                    policies=(policy,),
                )
                self.assertEqual(
                    (result.status, result.reason_code), ("unknown", "policy_rules_invalid")
                )

    def test_rule_modes_and_fanout_bounds_are_checked(self) -> None:
        payload = _generated().model_dump(exclude={"rule_id"})
        updates: tuple[dict[str, object], ...] = (
            {"generated_by": []},
            {"generated_by": ["source", "source"]},
            {"generated_by": [f"s{i}" for i in range(21)]},
            {"affected_products": [_product("api")]},
            {"product_impact": "declared_none"},
        )
        for update in updates:
            with self.subTest(update=update), self.assertRaises(ValidationError):
                ChangeImpactComponentRule.model_validate(payload | update)
        leaves = tuple(_rule(f"s{i}", f"src/{i}") for i in range(20))
        generators = tuple(rule.component for rule in leaves)
        policy = _v2_policy(*leaves, _generated(*generators))
        self.assertEqual(len(resolve_generated_boundaries(policy)["artifact"].generators), 20)
        artifacts = tuple(_generated(*generators, component=f"a{i}") for i in range(21))
        validate_change_impact_v2_policy(_v2_policy(*leaves, *artifacts[:20]))
        with self.assertRaisesRegex(ValueError, "400 edges"):
            validate_change_impact_v2_policy(_v2_policy(*leaves, *artifacts))

    def test_v1_rejects_generated_authority_and_preserves_legacy_hash(self) -> None:
        policy = _policy()
        self.assertEqual(
            policy.policy_digest, "fec4cc7ea106e369cf2a269d12e4e495a1e0a58d05edffb55a5cc1ab7c81baad"
        )
        self.assertNotIn("generated_by", policy.model_dump_json(exclude_none=True))
        payload = policy.model_dump(exclude={"policy_digest"})
        payload["component_rules"] = [_generated()]
        with self.assertRaisesRegex(ValidationError, "classification_model v2"):
            ChangeImpactPolicyRecord.model_validate(payload)
