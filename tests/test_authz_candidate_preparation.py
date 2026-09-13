from __future__ import annotations

from dataclasses import dataclass
import unittest

from control_plane.authz_candidate_preparation import (
    ADMINISTRATOR_PRODUCT_EVIDENCE_CONTEXT_RULE_ID,
    ADMINISTRATOR_PRODUCT_EVIDENCE_ENVIRONMENT_RULE_ID,
    ADMINISTRATOR_PRODUCT_EVIDENCE_READ_ACTIONS,
    ADMINISTRATOR_PRODUCT_EVIDENCE_READ_MANAGED_SET_ID,
    AuthorizationCandidatePreparationError,
    ORDINARY_AGENT_DELIVERY_ADMINISTRATION_ACTIONS,
    ORDINARY_AGENT_DELIVERY_ADMINISTRATION_MANAGED_RULE_ID,
    ORDINARY_AGENT_DELIVERY_ADMINISTRATION_MANAGED_SET_ID,
    administrator_product_evidence_read_state,
    compile_administrator_product_evidence_read_candidate,
    compile_ordinary_agent_delivery_administration_candidate,
    is_administrator_product_evidence_read_request,
    is_legacy_administrator_product_evidence_read_request,
    is_ordinary_agent_delivery_administration_request,
    ordinary_agent_delivery_administration_state,
)
from control_plane.service_auth import (
    AuthorizationTarget,
    GitHubHumanIdentity,
    LaunchplaneAuthzPolicy,
    TerminalAgentIdentity,
)
from tests.test_ordinary_agent_activation_storage import _record, _revoked


@dataclass
class _ActivationStore:
    records: tuple[object, ...] = ()

    def list_ordinary_agent_delivery_activation_records(
        self, *, limit: int | None = None
    ) -> tuple[object, ...]:
        return self.records[:limit]


def _policy(*, candidate_rule: dict[str, object] | None = None) -> LaunchplaneAuthzPolicy:
    rules: list[dict[str, object]] = [
        {
            "managed_set_id": "operator.policy-administration",
            "managed_rule_id": "policy-administrator",
            "github_ids": [123],
            "roles": ["admin"],
            "products": ["launchplane"],
            "contexts": ["launchplane"],
            "actions": ["authz_policy_grant.write", "authz_policy_operation.propose"],
        },
        {
            "managed_set_id": "unrelated.preserved",
            "managed_rule_id": "unrelated-rule",
            "github_ids": [456],
            "products": ["elsewhere"],
            "contexts": ["elsewhere"],
            "actions": [],
        },
    ]
    if candidate_rule is not None:
        rules.append(candidate_rule)
    return LaunchplaneAuthzPolicy.model_validate({"schema_version": 2, "github_humans": rules})


def _exact_rule(github_id: int = 123) -> dict[str, object]:
    return {
        "managed_set_id": ORDINARY_AGENT_DELIVERY_ADMINISTRATION_MANAGED_SET_ID,
        "managed_rule_id": ORDINARY_AGENT_DELIVERY_ADMINISTRATION_MANAGED_RULE_ID,
        "github_ids": [github_id],
        "roles": ["admin"],
        "products": ["launchplane"],
        "contexts": ["launchplane"],
        "actions": list(ORDINARY_AGENT_DELIVERY_ADMINISTRATION_ACTIONS),
    }


def _product_evidence_fragment(
    *, github_id: int = 123, schema_version: int = 2
) -> LaunchplaneAuthzPolicy:
    current_payload = _policy().model_dump(mode="json")
    current_payload["schema_version"] = schema_version
    current_policy = LaunchplaneAuthzPolicy.model_validate(current_payload)
    state, request = compile_administrator_product_evidence_read_candidate(
        current_policy=current_policy,
        github_id=github_id,
        intent="add",
    )
    assert state == "planned" and request is not None
    return request.desired_policy


def _policy_with_product_evidence(*, github_id: int = 123) -> LaunchplaneAuthzPolicy:
    payload = _policy().model_dump(mode="json")
    fragment = _product_evidence_fragment(github_id=github_id)
    payload["github_humans"].extend(rule.model_dump(mode="json") for rule in fragment.github_humans)
    return LaunchplaneAuthzPolicy.model_validate(payload)


def _legacy_policy_with_product_evidence(*, github_id: int = 123) -> LaunchplaneAuthzPolicy:
    payload = _policy_with_product_evidence(github_id=github_id).model_dump(mode="json")
    payload["github_humans"][-1]["contexts"] = ["launchplane"]
    return LaunchplaneAuthzPolicy.model_validate(payload)


class AuthorizationCandidateCompilerTests(unittest.TestCase):
    def test_add_compiles_only_exact_closed_rule_and_preserves_schema(self) -> None:
        state, request = compile_ordinary_agent_delivery_administration_candidate(
            current_policy=_policy(),
            github_id=123,
            intent="add",
            record_store=_ActivationStore(),
        )

        self.assertEqual(state, "planned")
        self.assertIsNotNone(request)
        assert request is not None
        self.assertTrue(is_ordinary_agent_delivery_administration_request(request))
        self.assertEqual(request.desired_policy.schema_version, 2)
        self.assertEqual(request.schema_migration, "reject")
        self.assertIsNone(request.administrator_quorum_change)
        self.assertEqual(len(request.desired_policy.github_humans), 1)
        rule = request.desired_policy.github_humans[0]
        self.assertEqual(rule.github_ids, (123,))
        self.assertEqual(rule.roles, ("admin",))
        self.assertEqual(rule.products, ("launchplane",))
        self.assertEqual(rule.contexts, ("launchplane",))
        self.assertEqual(rule.actions, ORDINARY_AGENT_DELIVERY_ADMINISTRATION_ACTIONS)
        self.assertFalse(rule.logins or rule.organizations or rule.teams or rule.instances)
        self.assertFalse(
            is_ordinary_agent_delivery_administration_request(
                request.model_copy(update={"administrator_quorum_change": 2})
            )
        )

    def test_exact_desired_state_is_bounded_noop(self) -> None:
        active = _policy(candidate_rule=_exact_rule())
        self.assertEqual(
            ordinary_agent_delivery_administration_state(active, github_id=123),
            "active",
        )
        add_state, add_request = compile_ordinary_agent_delivery_administration_candidate(
            current_policy=active,
            github_id=123,
            intent="add",
            record_store=_ActivationStore(),
        )
        remove_state, remove_request = compile_ordinary_agent_delivery_administration_candidate(
            current_policy=_policy(),
            github_id=123,
            intent="remove",
            record_store=_ActivationStore(),
        )
        self.assertEqual((add_state, add_request), ("already_satisfied", None))
        self.assertEqual((remove_state, remove_request), ("already_satisfied", None))

    def test_remove_compiles_empty_set_through_standard_planner_shape(self) -> None:
        state, request = compile_ordinary_agent_delivery_administration_candidate(
            current_policy=_policy(candidate_rule=_exact_rule()),
            github_id=123,
            intent="remove",
            record_store=_ActivationStore(),
        )

        self.assertEqual(state, "planned")
        assert request is not None
        self.assertTrue(is_ordinary_agent_delivery_administration_request(request))
        self.assertEqual(request.desired_policy.github_humans, ())

    def test_occupied_malformed_and_explicit_overlap_fail_closed(self) -> None:
        occupied = _policy(candidate_rule=_exact_rule(456))
        overlap_payload = _policy().model_dump(mode="json")
        overlap_payload["terminal_agents"] = [
            {
                "managed_set_id": "other.set",
                "managed_rule_id": "other-rule",
                "subjects": ["agent:test"],
                "actions": [ORDINARY_AGENT_DELIVERY_ADMINISTRATION_ACTIONS[0]],
            }
        ]
        overlap = LaunchplaneAuthzPolicy.model_validate(overlap_payload)

        malformed_rule = _exact_rule()
        malformed_rule["actions"] = [
            *ORDINARY_AGENT_DELIVERY_ADMINISTRATION_ACTIONS,
            "authz_policy_grant.write",
        ]
        malformed = _policy(candidate_rule=malformed_rule)
        for policy, reason in (
            (occupied, "candidate_set_conflict"),
            (malformed, "candidate_set_conflict"),
            (overlap, "candidate_action_overlap"),
        ):
            with self.subTest(reason=reason):
                with self.assertRaises(AuthorizationCandidatePreparationError) as caught:
                    compile_ordinary_agent_delivery_administration_candidate(
                        current_policy=policy,
                        github_id=123,
                        intent="add",
                        record_store=_ActivationStore(),
                    )
                self.assertEqual(caught.exception.reason_code, reason)

        # Other explicit authority must not prevent reducing this isolated set.
        overlap_payload["github_humans"].append(_exact_rule())
        active_with_overlap = LaunchplaneAuthzPolicy.model_validate(overlap_payload)
        state, removal = compile_ordinary_agent_delivery_administration_candidate(
            current_policy=active_with_overlap,
            github_id=123,
            intent="remove",
            record_store=_ActivationStore(),
        )
        self.assertEqual(state, "planned")
        assert removal is not None
        self.assertEqual(removal.desired_policy.github_humans, ())

    def test_remove_requires_bounded_readable_activation_history(self) -> None:
        active = _policy(candidate_rule=_exact_rule())
        for store, expected_reason in (
            (object(), "activation_storage_unavailable"),
            (_ActivationStore(records=(object(),)), "activation_storage_unavailable"),
            (_ActivationStore(records=(object(),) * 1001), "activation_history_truncated"),
        ):
            with self.subTest(reason=expected_reason):
                with self.assertRaises(AuthorizationCandidatePreparationError) as caught:
                    compile_ordinary_agent_delivery_administration_candidate(
                        current_policy=active,
                        github_id=123,
                        intent="remove",
                        record_store=store,
                    )
                self.assertEqual(caught.exception.reason_code, expected_reason)

    def test_removal_needs_stop_even_after_activation_expiry(self) -> None:
        activation = _record(
            operation_id="test-candidate-preparation",
            installed_at="2026-09-10T20:00:00Z",
            expires_at="2026-09-10T21:00:00Z",
        )
        active = _policy(candidate_rule=_exact_rule())
        with self.assertRaises(AuthorizationCandidatePreparationError) as caught:
            compile_ordinary_agent_delivery_administration_candidate(
                current_policy=active,
                github_id=123,
                intent="remove",
                record_store=_ActivationStore(records=(activation,)),
            )
        self.assertEqual(caught.exception.reason_code, "current_activation_requires_stop")
        state, request = compile_ordinary_agent_delivery_administration_candidate(
            current_policy=active,
            github_id=123,
            intent="remove",
            record_store=_ActivationStore(
                records=(_revoked(activation, occurred_at="2026-09-10T22:00:00Z"),)
            ),
        )
        self.assertEqual(state, "planned")
        self.assertIsNotNone(request)

    def test_schema_three_and_sensitive_changes_cannot_get_closed_candidate_summary(self) -> None:
        payload = _policy().model_dump(mode="json")
        payload["schema_version"] = 3
        policy = LaunchplaneAuthzPolicy.model_validate(payload)
        _, request = compile_ordinary_agent_delivery_administration_candidate(
            current_policy=policy,
            github_id=123,
            intent="add",
            record_store=_ActivationStore(),
        )
        assert request is not None
        self.assertEqual(request.desired_policy.schema_version, 3)
        self.assertTrue(is_ordinary_agent_delivery_administration_request(request))
        for change in (
            {"administrator_quorum_change": 1},
            {"schema_migration": "migrate_v2_to_v3"},
        ):
            with self.subTest(change=change):
                modified = type(request).model_validate(
                    {**request.model_dump(mode="json"), **change}
                )
                self.assertFalse(is_ordinary_agent_delivery_administration_request(modified))


class AdministratorProductEvidenceCandidateCompilerTests(unittest.TestCase):
    def test_add_compiles_both_read_scopes_for_all_products_and_preserves_schema(self) -> None:
        for schema_version in (2, 3):
            with self.subTest(schema_version=schema_version):
                current_payload = _policy().model_dump(mode="json")
                current_payload["schema_version"] = schema_version
                state, request = compile_administrator_product_evidence_read_candidate(
                    current_policy=LaunchplaneAuthzPolicy.model_validate(current_payload),
                    github_id=123,
                    intent="add",
                )
                self.assertEqual(state, "planned")
                assert request is not None
                self.assertEqual(request.schema_migration, "reject")
                self.assertIsNone(request.administrator_quorum_change)
                fragment = request.desired_policy
                self.assertEqual(fragment.schema_version, schema_version)
                self.assertEqual(len(fragment.github_humans), 2)
                self.assertFalse(
                    fragment.github_actions
                    or fragment.terminal_agents
                    or fragment.local_operators
                    or fragment.local_admins
                    or fragment.ordinary_agents
                )
                rules = {rule.managed_rule_id: rule for rule in fragment.github_humans}
                self.assertEqual(
                    set(rules),
                    {
                        ADMINISTRATOR_PRODUCT_EVIDENCE_CONTEXT_RULE_ID,
                        ADMINISTRATOR_PRODUCT_EVIDENCE_ENVIRONMENT_RULE_ID,
                    },
                )
                self.assertEqual(
                    rules[ADMINISTRATOR_PRODUCT_EVIDENCE_CONTEXT_RULE_ID].instances,
                    (),
                )
                self.assertEqual(
                    rules[ADMINISTRATOR_PRODUCT_EVIDENCE_ENVIRONMENT_RULE_ID].instances,
                    ("*",),
                )
                context_rule = rules[ADMINISTRATOR_PRODUCT_EVIDENCE_CONTEXT_RULE_ID]
                environment_rule = rules[ADMINISTRATOR_PRODUCT_EVIDENCE_ENVIRONMENT_RULE_ID]
                self.assertEqual(context_rule.contexts, ("launchplane",))
                self.assertEqual(environment_rule.contexts, ())
                for rule in rules.values():
                    self.assertEqual(rule.github_ids, (123,))
                    self.assertEqual(rule.roles, ("admin",))
                    self.assertEqual(rule.products, ())
                    self.assertEqual(rule.actions, ADMINISTRATOR_PRODUCT_EVIDENCE_READ_ACTIONS)
                    self.assertFalse(rule.logins or rule.organizations or rule.teams)

                identity = GitHubHumanIdentity(
                    login="administrator",
                    github_id=123,
                    name="Administrator",
                    email="administrator@example.com",
                    organizations=frozenset(),
                    teams=frozenset(),
                    role="admin",
                )
                self.assertTrue(
                    fragment.allows(
                        identity=identity,
                        action="product_environment.read",
                        product="future-product",
                        context="launchplane",
                        target=AuthorizationTarget(scope="context"),
                    )
                )
                self.assertTrue(
                    fragment.allows(
                        identity=identity,
                        action="product_environment.read",
                        product="future-product",
                        context="launchplane",
                        target=AuthorizationTarget(scope="instance", instances=("prod",)),
                    )
                )
                self.assertTrue(
                    fragment.allows(
                        identity=identity,
                        action="product_environment.read",
                        product="future-product",
                        context="foreign-context",
                        target=AuthorizationTarget(scope="instance", instances=("prod",)),
                    )
                )

                denied_cases = (
                    (
                        GitHubHumanIdentity(
                            login="other",
                            github_id=456,
                            name="Other Administrator",
                            email="other@example.com",
                            organizations=frozenset(),
                            teams=frozenset(),
                            role="admin",
                        ),
                        "product_environment.read",
                        "launchplane",
                    ),
                    (
                        TerminalAgentIdentity(subject="agent:other", token_label="other"),
                        "product_environment.read",
                        "launchplane",
                    ),
                    (
                        GitHubHumanIdentity(
                            login="administrator",
                            github_id=123,
                            name="Administrator",
                            email="administrator@example.com",
                            organizations=frozenset(),
                            teams=frozenset(),
                            role="read_only",
                        ),
                        "product_environment.read",
                        "launchplane",
                    ),
                    (identity, "product_config.apply", "launchplane"),
                )
                for denied_identity, action, context in denied_cases:
                    with self.subTest(action=action, context=context):
                        self.assertFalse(
                            fragment.allows(
                                identity=denied_identity,
                                action=action,
                                product="future-product",
                                context=context,
                                target=AuthorizationTarget(scope="context"),
                            )
                        )

                self.assertFalse(
                    fragment.allows(
                        identity=identity,
                        action="product_environment.read",
                        product="future-product",
                        context="foreign-context",
                        target=AuthorizationTarget(scope="context"),
                    )
                )

    def test_exact_set_add_remove_and_noops_are_isolated(self) -> None:
        active = _policy_with_product_evidence()
        self.assertEqual(
            administrator_product_evidence_read_state(active, github_id=123),
            "active",
        )
        add_state, add_request = compile_administrator_product_evidence_read_candidate(
            current_policy=active,
            github_id=123,
            intent="add",
        )
        absent_state, absent_request = compile_administrator_product_evidence_read_candidate(
            current_policy=_policy(),
            github_id=123,
            intent="remove",
        )
        remove_state, remove_request = compile_administrator_product_evidence_read_candidate(
            current_policy=active,
            github_id=123,
            intent="remove",
        )

        self.assertEqual((add_state, add_request), ("already_satisfied", None))
        self.assertEqual((absent_state, absent_request), ("already_satisfied", None))
        self.assertEqual(remove_state, "planned")
        assert remove_request is not None
        self.assertEqual(
            remove_request.managed_set_id, ADMINISTRATOR_PRODUCT_EVIDENCE_READ_MANAGED_SET_ID
        )
        self.assertEqual(remove_request.desired_policy.github_humans, ())
        self.assertTrue(is_administrator_product_evidence_read_request(remove_request))

    def test_legacy_set_is_corrected_or_removed_without_changing_public_state(self) -> None:
        legacy = _legacy_policy_with_product_evidence()
        self.assertEqual(administrator_product_evidence_read_state(legacy, github_id=123), "active")

        add_state, add_request = compile_administrator_product_evidence_read_candidate(
            current_policy=legacy,
            github_id=123,
            intent="add",
        )
        remove_state, remove_request = compile_administrator_product_evidence_read_candidate(
            current_policy=legacy,
            github_id=123,
            intent="remove",
        )

        self.assertEqual(add_state, "planned")
        assert add_request is not None
        environment_rule = next(
            rule
            for rule in add_request.desired_policy.github_humans
            if rule.managed_rule_id == ADMINISTRATOR_PRODUCT_EVIDENCE_ENVIRONMENT_RULE_ID
        )
        self.assertEqual(environment_rule.contexts, ())
        self.assertTrue(is_administrator_product_evidence_read_request(add_request))
        self.assertFalse(is_legacy_administrator_product_evidence_read_request(add_request))
        self.assertEqual(remove_state, "planned")
        assert remove_request is not None
        self.assertEqual(remove_request.desired_policy.github_humans, ())

    def test_collision_and_schema_one_fail_closed_while_unrelated_overlap_is_allowed(self) -> None:
        occupied = _policy_with_product_evidence(github_id=456)
        malformed_payload = _policy_with_product_evidence().model_dump(mode="json")
        malformed_payload["github_humans"][-1]["contexts"] = ["other-context"]
        malformed = LaunchplaneAuthzPolicy.model_validate(malformed_payload)
        for policy in (occupied, malformed, LaunchplaneAuthzPolicy(schema_version=1)):
            with self.subTest(policy=policy):
                with self.assertRaises(AuthorizationCandidatePreparationError) as caught:
                    compile_administrator_product_evidence_read_candidate(
                        current_policy=policy,
                        github_id=123,
                        intent="add",
                    )
                self.assertEqual(caught.exception.reason_code, "candidate_set_conflict")

        overlap_payload = _policy().model_dump(mode="json")
        overlap_payload["terminal_agents"] = [
            {
                "managed_set_id": "unrelated.product-reader",
                "managed_rule_id": "unrelated-reader",
                "subjects": ["agent:reader"],
                "actions": ["product_environment.read"],
            }
        ]
        overlap = LaunchplaneAuthzPolicy.model_validate(overlap_payload)
        state, request = compile_administrator_product_evidence_read_candidate(
            current_policy=overlap,
            github_id=123,
            intent="add",
        )
        self.assertEqual(state, "planned")
        self.assertIsNotNone(request)

    def test_recognizer_rejects_tampering_and_rules_for_different_humans(self) -> None:
        fragment = _product_evidence_fragment()
        _, request = compile_administrator_product_evidence_read_candidate(
            current_policy=_policy(),
            github_id=123,
            intent="add",
        )
        assert request is not None
        self.assertTrue(is_administrator_product_evidence_read_request(request))
        legacy_request = request.model_copy(
            update={
                "desired_policy": fragment.model_copy(
                    update={
                        "github_humans": tuple(
                            rule.model_copy(update={"contexts": ("launchplane",)})
                            if rule.managed_rule_id
                            == ADMINISTRATOR_PRODUCT_EVIDENCE_ENVIRONMENT_RULE_ID
                            else rule
                            for rule in fragment.github_humans
                        )
                    }
                )
            }
        )
        self.assertFalse(is_administrator_product_evidence_read_request(legacy_request))
        self.assertTrue(is_legacy_administrator_product_evidence_read_request(legacy_request))
        self.assertTrue(
            is_administrator_product_evidence_read_request(
                request.model_copy(
                    update={
                        "reason": "A different explanation for the same read capability.",
                        "related_issue": "#999",
                    }
                )
            )
        )
        for index, change in (
            (0, {"github_ids": (456,)}),
            (0, {"actions": ("driver.read",)}),
            (1, {"contexts": ("other-context",)}),
        ):
            with self.subTest(index=index, change=change):
                changed_rules = list(fragment.github_humans)
                changed_rules[index] = changed_rules[index].model_copy(update=change)
                changed_request = request.model_copy(
                    update={
                        "desired_policy": fragment.model_copy(
                            update={"github_humans": tuple(changed_rules)}
                        )
                    }
                )
                self.assertFalse(is_administrator_product_evidence_read_request(changed_request))
