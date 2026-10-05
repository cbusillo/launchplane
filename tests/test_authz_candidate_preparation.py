from __future__ import annotations

from dataclasses import dataclass
import json
import unittest

from control_plane.authz_candidate_preparation import (
    AGENT_PRODUCT_SETUP_MANAGED_SET_ID,
    ADMINISTRATOR_PRODUCT_EVIDENCE_CONTEXT_RULE_ID,
    ADMINISTRATOR_PRODUCT_EVIDENCE_ENVIRONMENT_RULE_ID,
    ADMINISTRATOR_PRODUCT_EVIDENCE_READ_ACTIONS,
    ADMINISTRATOR_PRODUCT_EVIDENCE_READ_MANAGED_SET_ID,
    AuthorizationCandidatePreparationError,
    ORDINARY_AGENT_DELIVERY_ADMINISTRATION_ACTIONS,
    ORDINARY_AGENT_DELIVERY_ADMINISTRATION_MANAGED_RULE_ID,
    ORDINARY_AGENT_DELIVERY_ADMINISTRATION_MANAGED_SET_ID,
    TERMINAL_ENROLLMENT_POLICY_MANAGED_SET_ID,
    administrator_product_evidence_read_state,
    agent_product_setup_grants_match_records,
    agent_product_setup_products,
    agent_product_setup_request_grants,
    agent_product_setup_state,
    authorization_candidate_request_matches,
    compile_agent_product_setup_candidate,
    compile_administrator_product_evidence_read_candidate,
    compile_terminal_enrollment_policy_candidate,
    is_terminal_enrollment_requester_request,
    compile_ordinary_agent_delivery_administration_candidate,
    is_administrator_product_evidence_read_request,
    is_legacy_administrator_product_evidence_read_request,
    is_ordinary_agent_delivery_administration_request,
    ordinary_agent_delivery_administration_state,
    terminal_enrollment_capability_state,
)
from control_plane.contracts.ordinary_agent import (
    OrdinaryAgentPolicyRule,
    OrdinaryAgentTarget,
)
from control_plane.contracts.privileged_operation import ManagedAuthzPolicySetProposalInput
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductImageProfile,
)
from control_plane.service_auth import (
    AuthorizationTarget,
    GitHubHumanIdentity,
    LaunchplaneAuthzPolicy,
    LocalOperatorIdentity,
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
    def test_terminal_enrollment_candidate_is_narrow_and_preserves_schema(self) -> None:
        identity = TerminalAgentIdentity(subject="trusted-terminal", token_label="owner-terminal")
        state, request = compile_terminal_enrollment_policy_candidate(
            current_policy=_policy(), identity=identity
        )

        self.assertEqual(state, "planned")
        assert request is not None
        self.assertTrue(is_terminal_enrollment_requester_request(request, intent="add"))
        self.assertEqual(request.desired_policy.schema_version, 2)
        self.assertEqual(len(request.desired_policy.terminal_agents), 1)
        rule = request.desired_policy.terminal_agents[0]
        self.assertEqual(rule.subjects, (identity.subject,))
        self.assertEqual(rule.token_labels, (identity.token_label,))
        self.assertEqual(rule.products, ("launchplane",))
        self.assertEqual(rule.contexts, ("launchplane",))
        self.assertEqual(rule.actions, ("ordinary_agent_enrollment.propose",))
        self.assertFalse(request.desired_policy.ordinary_agents)

    def test_terminal_enrollment_readiness_matches_ingress_predicate(self) -> None:
        identity = TerminalAgentIdentity(subject="trusted-terminal", token_label="owner-terminal")
        payload = _policy().model_dump(mode="json")
        payload["terminal_agents"] = [
            {
                "managed_set_id": "existing.terminal",
                "managed_rule_id": "broader-rule",
                "subjects": [identity.subject],
                "token_labels": [identity.token_label],
                "actions": ["ordinary_agent_enrollment.propose", "product_environment.read"],
            }
        ]
        current = LaunchplaneAuthzPolicy.model_validate(payload)

        self.assertEqual(
            terminal_enrollment_capability_state(policy=current, identity=identity), "ready"
        )
        self.assertEqual(
            compile_terminal_enrollment_policy_candidate(current_policy=current, identity=identity),
            ("already_satisfied", None),
        )

    def test_terminal_enrollment_conflicts_are_classified_without_adoption(self) -> None:
        identity = TerminalAgentIdentity(subject="trusted-terminal", token_label="owner-terminal")
        cases = {
            "unmanaged": [{"subjects": [identity.subject], "token_labels": [identity.token_label]}],
            "mismatched": [
                {
                    "managed_set_id": TERMINAL_ENROLLMENT_POLICY_MANAGED_SET_ID,
                    "managed_rule_id": "different-action",
                    "subjects": [identity.subject],
                    "token_labels": [identity.token_label],
                    "actions": ["product_environment.read"],
                }
            ],
            "ambiguous": [
                {
                    "managed_set_id": "terminal.one",
                    "managed_rule_id": "one",
                    "subjects": [identity.subject],
                    "token_labels": [identity.token_label],
                    "actions": ["ordinary_agent_enrollment.propose"],
                },
                {
                    "managed_set_id": "terminal.two",
                    "managed_rule_id": "two",
                    "subjects": [identity.subject],
                    "token_labels": [identity.token_label],
                    "actions": ["ordinary_agent_enrollment.propose"],
                },
            ],
        }
        for expected, terminal_agents in cases.items():
            with self.subTest(expected=expected):
                payload = _policy().model_dump(mode="json")
                payload["terminal_agents"] = terminal_agents
                current = LaunchplaneAuthzPolicy.model_validate(payload)
                self.assertEqual(
                    terminal_enrollment_capability_state(policy=current, identity=identity),
                    expected,
                )
                with self.assertRaises(ValueError):
                    compile_terminal_enrollment_policy_candidate(
                        current_policy=current, identity=identity
                    )

    def test_terminal_context_read_rule_does_not_block_enrollment_preparation(self) -> None:
        identity = TerminalAgentIdentity(subject="trusted-terminal", token_label="owner-terminal")
        payload = _policy().model_dump(mode="json")
        payload["terminal_agents"] = [
            {
                "managed_set_id": "terminal.context-read",
                "managed_rule_id": "redacted-context",
                "subjects": [identity.subject],
                "token_labels": [identity.token_label],
                "products": ["launchplane"],
                "contexts": ["launchplane"],
                "actions": ["product_environment.read"],
            }
        ]
        current = LaunchplaneAuthzPolicy.model_validate(payload)
        state, request = compile_terminal_enrollment_policy_candidate(
            current_policy=current, identity=identity
        )

        self.assertEqual(state, "planned")
        assert request is not None
        self.assertEqual(len(request.desired_policy.terminal_agents), 1)
        self.assertNotEqual(
            request.desired_policy.terminal_agents[0].managed_set_id,
            "terminal.context-read",
        )

    def test_terminal_enrollment_rejects_wildcard_identity_metadata(self) -> None:
        identity = TerminalAgentIdentity(subject="trusted-*", token_label="owner-terminal")

        self.assertEqual(
            terminal_enrollment_capability_state(policy=_policy(), identity=identity),
            "unavailable",
        )
        with self.assertRaises(ValueError):
            compile_terminal_enrollment_policy_candidate(
                current_policy=_policy(), identity=identity
            )

    def test_terminal_enrollment_removal_survives_config_change_and_preserves_client_access(
        self,
    ) -> None:
        identity = TerminalAgentIdentity(subject="trusted-terminal", token_label="owner-terminal")
        _, addition = compile_terminal_enrollment_policy_candidate(
            current_policy=_policy(), identity=identity
        )
        assert addition is not None
        payload = _policy().model_dump(mode="json")
        payload["schema_version"] = 3
        payload["terminal_agents"] = [
            rule.model_dump(mode="json") for rule in addition.desired_policy.terminal_agents
        ]
        payload["ordinary_agents"] = [
            OrdinaryAgentPolicyRule(
                managed_set_id="ordinary-client.existing",
                managed_rule_id="delivery",
                principal_id="agent_existing",
                target=OrdinaryAgentTarget(
                    repository_id=9001,
                    repository="example/project",
                    base_branch="main",
                ),
                actions=("self_read",),
            ).model_dump(mode="json")
        ]
        current = LaunchplaneAuthzPolicy.model_validate(payload)
        state, removal = compile_terminal_enrollment_policy_candidate(
            current_policy=current, identity=None, intent="remove"
        )

        self.assertEqual(state, "planned")
        assert removal is not None
        self.assertTrue(is_terminal_enrollment_requester_request(removal, intent="remove"))
        self.assertFalse(removal.desired_policy.terminal_agents)
        self.assertFalse(removal.desired_policy.ordinary_agents)
        self.assertEqual(len(current.ordinary_agents), 1)

    def test_terminal_enrollment_removal_rejects_foreign_broad_or_ambiguous_sets(self) -> None:
        identity = TerminalAgentIdentity(subject="trusted-terminal", token_label="owner-terminal")
        _, addition = compile_terminal_enrollment_policy_candidate(
            current_policy=_policy(), identity=identity
        )
        assert addition is not None
        exact = addition.desired_policy.terminal_agents[0].model_dump(mode="json")
        cases = {
            "foreign": [{**exact, "managed_rule_id": "foreign"}],
            "broad": [{**exact, "subjects": ["*"]}],
            "ambiguous": [exact, {**exact, "managed_rule_id": "another"}],
        }
        for name, terminal_agents in cases.items():
            with self.subTest(name=name):
                payload = _policy().model_dump(mode="json")
                payload["terminal_agents"] = terminal_agents
                with self.assertRaises(ValueError):
                    compile_terminal_enrollment_policy_candidate(
                        current_policy=LaunchplaneAuthzPolicy.model_validate(payload),
                        identity=None,
                        intent="remove",
                    )

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


def _setup_profile(
    product: str,
    contexts: tuple[tuple[str, str], ...] | None = None,
    production_use: str = "live",
    historical: tuple[str, ...] = (),
) -> LaunchplaneProductProfileRecord:
    lanes = contexts if contexts is not None else (("prod", product),)
    return LaunchplaneProductProfileRecord.model_validate(
        {
            "historical_contexts": list(historical),
            "product": product,
            "production_use": production_use,
            "display_name": product.title(),
            "repository": f"example/{product}",
            "driver_id": "generic-web",
            "image": ProductImageProfile().model_dump(mode="json"),
            "lanes": [{"instance": instance, "context": context} for instance, context in lanes],
            "updated_at": "2026-10-01T12:00:00+00:00",
            "source": "test:agent-product-setup",
        }
    )


@dataclass
class _ProductProfileStore:
    profiles: tuple[LaunchplaneProductProfileRecord, ...] = (
        _setup_profile("example-shop"),
        _setup_profile("example-docs", (("testing", "docs-ctx"), ("prod", "docs-ctx"))),
        _setup_profile("example-split", (("testing", "split-a"), ("prod", "split-b"))),
        _setup_profile("example-laneless", ()),
        _setup_profile("example-service", (("prod", "launchplane"),)),
        _setup_profile("example-upper", (("prod", "Launchplane"),)),
        _setup_profile("example-mixed", (("prod", "Mixed-Case"),)),
        _setup_profile("example-shared-a", (("prod", "shared-ctx"),)),
        _setup_profile("example-shared-b", (("prod", "shared-ctx"),)),
        _setup_profile("example-history", (("prod", "history-ctx"),)),
        _setup_profile("example-heir", (("prod", "heir-ctx"),), historical=("history-ctx",)),
    )

    def read_product_profile_record(self, product: str) -> LaunchplaneProductProfileRecord:
        for profile in self.profiles:
            if profile.product == product:
                return profile
        raise FileNotFoundError(product)

    def list_product_profile_records(self) -> tuple[LaunchplaneProductProfileRecord, ...]:
        return self.profiles


_OPERATOR = LocalOperatorIdentity(subject="operator-agent", token_label="operator-agent-token")


def _setup_rules(
    product: str = "example-shop",
    context: str = "example-shop",
    *,
    subject: str = "operator-agent",
    token_label: str = "operator-agent-token",
) -> list[dict[str, object]]:
    common = {
        "managed_set_id": AGENT_PRODUCT_SETUP_MANAGED_SET_ID,
        "subjects": [subject],
        "token_labels": [token_label],
        "contexts": [context],
    }
    return [
        {
            **common,
            "managed_rule_id": f"{product}.testing-config",
            "products": [product],
            "instances": ["testing"],
            "actions": ["product_config.plan", "product_config.apply"],
        },
        {
            **common,
            "managed_rule_id": f"{product}.prod-backup-policy",
            "products": [product],
            "instances": ["prod"],
            "actions": ["production_backup_authority.write"],
        },
        {
            **common,
            "managed_rule_id": f"{product}.testing-target",
            "products": [product],
            "instances": ["testing"],
            "actions": ["dokploy_target.lane_setup"],
        },
    ]


def _setup_policy(*rules: dict[str, object], schema_version: int = 2) -> LaunchplaneAuthzPolicy:
    payload = _policy().model_dump(mode="json")
    payload["schema_version"] = schema_version
    payload["local_operators"] = [
        {
            "managed_set_id": "operator.standing-read",
            "managed_rule_id": "operator-agent-reader",
            "subjects": ["operator-agent"],
            "token_labels": ["operator-agent-token"],
            "actions": ["product_profile.read"],
        },
        *rules,
    ]
    return LaunchplaneAuthzPolicy.model_validate(payload)


class AgentProductSetupCandidateCompilerTests(unittest.TestCase):
    def _compile(
        self,
        policy: LaunchplaneAuthzPolicy,
        *,
        intent: str = "add",
        products: tuple[str, ...] = ("example-shop",),
        identity: LocalOperatorIdentity | None = _OPERATOR,
    ) -> tuple[str, ManagedAuthzPolicySetProposalInput | None]:
        return compile_agent_product_setup_candidate(
            current_policy=policy,
            identity=identity,
            intent=intent,  # type: ignore[arg-type]
            products=products,
            record_store=_ProductProfileStore(),
        )

    def test_add_compiles_three_lane_bound_rules_per_selected_product(self) -> None:
        state, candidate = self._compile(
            _setup_policy(schema_version=3),
            products=(" example-shop", "example-docs", "example-shop"),
        )

        self.assertEqual(state, "planned")
        assert candidate is not None
        self.assertEqual(candidate.managed_set_id, AGENT_PRODUCT_SETUP_MANAGED_SET_ID)
        self.assertEqual(candidate.desired_policy.schema_version, 3)
        self.assertEqual(candidate.schema_migration, "reject")
        self.assertIsNone(candidate.administrator_quorum_change)
        self.assertEqual(candidate.desired_policy.github_humans, ())
        rules = {rule.managed_rule_id: rule for rule in candidate.desired_policy.local_operators}
        self.assertEqual(
            sorted(rules),
            [
                "example-docs.prod-backup-policy",
                "example-docs.testing-config",
                "example-docs.testing-target",
                "example-shop.prod-backup-policy",
                "example-shop.testing-config",
                "example-shop.testing-target",
            ],
        )
        config = rules["example-docs.testing-config"]
        self.assertEqual(
            (config.products, config.contexts, config.instances, set(config.actions)),
            (
                ("example-docs",),
                ("docs-ctx",),
                ("testing",),
                {"product_config.plan", "product_config.apply"},
            ),
        )
        backup = rules["example-docs.prod-backup-policy"]
        self.assertEqual(
            (backup.products, backup.contexts, backup.instances, backup.actions),
            (("example-docs",), ("docs-ctx",), ("prod",), ("production_backup_authority.write",)),
        )
        target = rules["example-docs.testing-target"]
        self.assertEqual(
            (target.products, target.contexts, target.instances, target.actions),
            (("example-docs",), ("docs-ctx",), ("testing",), ("dokploy_target.lane_setup",)),
        )
        granted = {action for rule in rules.values() for action in rule.actions}
        self.assertNotIn("product_profile.write", granted)
        self.assertNotIn("dokploy_target.setup", granted)
        self.assertEqual(
            agent_product_setup_request_grants(candidate, intent="add"),
            (
                ("example-docs", "docs-ctx", "operator-agent", "operator-agent-token"),
                ("example-shop", "example-shop", "operator-agent", "operator-agent-token"),
            ),
        )
        self.assertIsNone(agent_product_setup_request_grants(candidate, intent="remove"))
        self.assertTrue(
            authorization_candidate_request_matches(
                candidate_id="agent-product-setup",
                request=candidate,
                github_id=123,
                intent="add",
                products=("example-shop", "example-docs"),
                configured_local_operator_identity=_OPERATOR,
                record_store=_ProductProfileStore(),
            )
        )
        self.assertFalse(
            authorization_candidate_request_matches(
                candidate_id="agent-product-setup",
                request=candidate,
                github_id=123,
                intent="add",
                products=("example-shop",),
                configured_local_operator_identity=_OPERATOR,
                record_store=_ProductProfileStore(),
            )
        )

    def test_live_product_is_accepted(self) -> None:
        state, _candidate = self._compile(_setup_policy(), products=("example-shop",))
        self.assertEqual(state, "planned")

    def test_add_refuses_unknown_empty_glob_and_ambiguous_products(self) -> None:
        policy = _setup_policy()
        for products, reason in (
            ((), "candidate_products_required"),
            (("  ",), "candidate_products_required"),
            (("missing-product",), "candidate_product_unavailable"),
            (("example-*",), "candidate_product_unavailable"),
            (("example-split",), "candidate_product_unavailable"),
            (("example-laneless",), "candidate_product_unavailable"),
            (("example-service",), "candidate_product_unavailable"),
            (("example-upper",), "candidate_product_unavailable"),
            (("example-mixed",), "candidate_product_unavailable"),
            (("example-shared-a",), "candidate_product_unavailable"),
            (("example-history",), "candidate_product_unavailable"),
        ):
            with self.subTest(products=products):
                with self.assertRaises(AuthorizationCandidatePreparationError) as raised:
                    self._compile(policy, products=products)
                self.assertEqual(raised.exception.reason_code, reason)

    def test_add_requires_exact_configured_local_operator(self) -> None:
        for identity in (
            None,
            LocalOperatorIdentity(subject="operator-*", token_label="operator-agent-token"),
            LocalOperatorIdentity(subject="operator-agent", token_label=" "),
        ):
            with self.subTest(identity=identity):
                with self.assertRaises(AuthorizationCandidatePreparationError) as raised:
                    self._compile(_setup_policy(), identity=identity)
                self.assertEqual(raised.exception.reason_code, "candidate_principal_unavailable")

    def test_matching_set_is_noop_and_new_selection_replaces_it(self) -> None:
        policy = _setup_policy(*_setup_rules())
        self.assertEqual(self._compile(policy), ("already_satisfied", None))
        self.assertEqual(agent_product_setup_state(policy, identity=_OPERATOR), "active")
        self.assertEqual(agent_product_setup_products(policy), ("example-shop",))
        state, candidate = self._compile(policy, products=("example-shop", "example-docs"))
        self.assertEqual(state, "planned")
        assert candidate is not None
        self.assertEqual(len(candidate.desired_policy.local_operators), 2 * len(_setup_rules()))

    def test_remove_proposes_empty_fragment_and_absent_remove_is_noop(self) -> None:
        self.assertEqual(
            self._compile(_setup_policy(), intent="remove", products=()),
            ("already_satisfied", None),
        )
        state, candidate = self._compile(
            _setup_policy(*_setup_rules(subject="other-operator")),
            intent="remove",
            products=(),
            identity=None,
        )
        self.assertEqual(state, "planned")
        assert candidate is not None
        self.assertEqual(candidate.desired_policy.local_operators, ())
        self.assertEqual(agent_product_setup_request_grants(candidate, intent="remove"), ())
        self.assertTrue(
            authorization_candidate_request_matches(
                candidate_id="agent-product-setup",
                request=candidate,
                github_id=123,
                intent="remove",
            )
        )
        self.assertFalse(
            authorization_candidate_request_matches(
                candidate_id="agent-product-setup",
                request=candidate,
                github_id=123,
                intent="add",
                configured_local_operator_identity=_OPERATOR,
                record_store=_ProductProfileStore(),
            )
        )

    def test_foreign_or_malformed_set_is_conflict(self) -> None:
        shop = _setup_rules()
        widened = json.loads(json.dumps(shop))
        widened[0]["instances"] = ["testing", "prod"]
        extra_action = json.loads(json.dumps(shop))
        extra_action[1]["actions"] = ["production_backup_authority.write", "promotion.write"]
        service_target = json.loads(json.dumps(shop))
        service_target[2]["actions"] = ["dokploy_target.setup"]
        service_target[2]["instances"] = []
        missing_rule = shop[:2]
        cases = (
            _setup_policy(*_setup_rules(subject="other-operator")),
            _setup_policy(*widened),
            _setup_policy(*extra_action),
            _setup_policy(*service_target),
            _setup_policy(*missing_rule),
            _setup_policy(*_setup_rules(), *_setup_rules("example-docs", "docs-ctx", subject="x")),
        )
        for policy in cases:
            with self.subTest(policy=policy.local_operators[1:]):
                self.assertEqual(agent_product_setup_state(policy, identity=_OPERATOR), "conflict")
                if policy is not cases[0]:
                    self.assertEqual(agent_product_setup_products(policy), ())
                with self.assertRaises(AuthorizationCandidatePreparationError) as raised:
                    self._compile(policy)
                self.assertEqual(raised.exception.reason_code, "candidate_set_conflict")
        human_occupied = LaunchplaneAuthzPolicy.model_validate(
            {
                **_setup_policy().model_dump(mode="json"),
                "github_humans": [
                    {
                        "managed_set_id": AGENT_PRODUCT_SETUP_MANAGED_SET_ID,
                        "managed_rule_id": "example-shop.testing-config",
                        "github_ids": [123],
                        "products": ["example-shop"],
                        "contexts": ["example-shop"],
                        "instances": ["testing"],
                        "actions": ["product_config.plan"],
                    }
                ],
            }
        )
        with self.assertRaises(AuthorizationCandidatePreparationError):
            self._compile(human_occupied, intent="remove", products=())

    def test_recognizer_rejects_tampered_requests(self) -> None:
        _state, candidate = self._compile(_setup_policy(schema_version=3))
        assert candidate is not None
        payload = candidate.model_dump(mode="json")
        tampered: list[dict[str, object]] = []
        for suffix, key, value in (
            ("testing-config", "actions", ["product_config.apply", "product_profile.write"]),
            ("testing-config", "instances", ["*"]),
            ("prod-backup-policy", "instances", ["testing"]),
            ("testing-target", "products", ["launchplane"]),
            ("testing-target", "contexts", ["other-context"]),
            ("testing-config", "subjects", ["operator-agent", "other"]),
            ("prod-backup-policy", "managed_rule_id", "example-shop.other-rule"),
        ):
            mutated = json.loads(json.dumps(payload))
            (rule,) = (
                rule
                for rule in mutated["desired_policy"]["local_operators"]
                if rule["managed_rule_id"] == f"example-shop.{suffix}"
            )
            rule[key] = value
            tampered.append(mutated)
        extra_principal = json.loads(json.dumps(payload))
        extra_principal["desired_policy"]["github_humans"] = [
            {
                "managed_set_id": AGENT_PRODUCT_SETUP_MANAGED_SET_ID,
                "managed_rule_id": "extra",
                "github_ids": [123],
                "roles": ["admin"],
            }
        ]
        tampered.append(extra_principal)
        migration = json.loads(json.dumps(payload))
        migration["schema_migration"] = "migrate_v2_to_v3"
        migration["desired_policy"]["schema_version"] = 3
        tampered.append(migration)
        for mutated in tampered:
            with self.subTest(mutated=mutated):
                try:
                    request = ManagedAuthzPolicySetProposalInput.model_validate(mutated)
                except ValueError:
                    continue
                self.assertIsNone(agent_product_setup_request_grants(request, intent="add"))

    def test_record_check_rejects_a_context_that_is_not_the_products_own(self) -> None:
        store = _ProductProfileStore()
        _state, candidate = self._compile(_setup_policy(), products=("example-shop",))
        assert candidate is not None
        grants = agent_product_setup_request_grants(candidate, intent="add")
        assert grants is not None
        self.assertTrue(agent_product_setup_grants_match_records(record_store=store, grants=grants))
        swapped = _setup_policy(*_setup_rules("example-shop", "docs-ctx"))
        swapped_request = ManagedAuthzPolicySetProposalInput(
            managed_set_id=AGENT_PRODUCT_SETUP_MANAGED_SET_ID,
            desired_policy=LaunchplaneAuthzPolicy(
                schema_version=2,
                local_operators=tuple(
                    rule
                    for rule in swapped.local_operators
                    if rule.managed_set_id == AGENT_PRODUCT_SETUP_MANAGED_SET_ID
                ),
            ),
            schema_migration="reject",
            administrator_quorum_change=None,
            reason="test",
            related_issue="#2766",
        )
        swapped_grants = agent_product_setup_request_grants(swapped_request, intent="add")
        assert swapped_grants is not None
        self.assertFalse(
            agent_product_setup_grants_match_records(record_store=store, grants=swapped_grants)
        )
        self.assertFalse(
            authorization_candidate_request_matches(
                candidate_id="agent-product-setup",
                request=swapped_request,
                github_id=123,
                intent="add",
                products=("example-shop",),
                configured_local_operator_identity=_OPERATOR,
                record_store=store,
            )
        )


class AgentPolicyProposerCandidateTests(unittest.TestCase):
    def test_compiler_add_remove_and_replay_keep_the_reviewed_identity(self) -> None:
        from control_plane.authz_candidate_preparation import (
            compile_agent_policy_proposer_candidate,
            agent_policy_proposer_request_matches,
        )

        identity = LocalOperatorIdentity(subject="operator-agent", token_label="write")
        policy = _policy()
        state, request = compile_agent_policy_proposer_candidate(
            current_policy=policy, identity=identity, intent="add"
        )
        self.assertEqual(state, "planned")
        assert request is not None
        self.assertTrue(
            agent_policy_proposer_request_matches(request, identity=identity, intent="add")
        )
        self.assertFalse(
            agent_policy_proposer_request_matches(
                request,
                identity=LocalOperatorIdentity(subject="other", token_label="write"),
                intent="add",
            )
        )
        installed = policy.model_copy(
            update={"local_operators": request.desired_policy.local_operators}
        )
        self.assertEqual(
            compile_agent_policy_proposer_candidate(
                current_policy=installed, identity=identity, intent="add"
            ),
            ("already_satisfied", None),
        )
        state, removal = compile_agent_policy_proposer_candidate(
            current_policy=installed, identity=None, intent="remove"
        )
        self.assertEqual(state, "planned")
        assert removal is not None
        self.assertFalse(removal.desired_policy.local_operators)
        self.assertEqual(removal.managed_set_id, request.managed_set_id)
        self.assertEqual(
            compile_agent_policy_proposer_candidate(
                current_policy=policy, identity=None, intent="remove"
            ),
            ("already_satisfied", None),
        )
        self.assertEqual(installed.github_humans, policy.github_humans)

    def test_compiler_refuses_missing_globbed_colliding_or_overlapping_principals(self) -> None:
        from control_plane.authz_candidate_preparation import (
            compile_agent_policy_proposer_candidate,
        )

        identity = LocalOperatorIdentity(subject="operator-agent", token_label="write")
        policy = _policy()
        _, request = compile_agent_policy_proposer_candidate(
            current_policy=policy, identity=identity, intent="add"
        )
        assert request is not None
        rule = request.desired_policy.local_operators[0]
        for current, principal in (
            (policy, None),
            (policy, LocalOperatorIdentity(subject="*", token_label="write")),
            (
                policy.model_copy(
                    update={"local_operators": (rule.model_copy(update={"subjects": ("other",)}),)}
                ),
                identity,
            ),
            (
                policy.model_copy(
                    update={
                        "local_operators": (
                            rule.model_copy(
                                update={
                                    "actions": (*rule.actions, "authz_policy_operation.approve")
                                }
                            ),
                        )
                    }
                ),
                identity,
            ),
            (
                policy.model_copy(
                    update={
                        "local_operators": (
                            rule.model_copy(update={"managed_set_id": "other.set"}),
                        )
                    }
                ),
                identity,
            ),
        ):
            with (
                self.subTest(principal=principal),
                self.assertRaises(AuthorizationCandidatePreparationError),
            ):
                compile_agent_policy_proposer_candidate(
                    current_policy=current, identity=principal, intent="add"
                )
