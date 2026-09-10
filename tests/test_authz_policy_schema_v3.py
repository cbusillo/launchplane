from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from pydantic import ValidationError

from control_plane.authz_grant_service import (
    AuthzManagedPolicyReconcileEnvelope,
    AuthzPolicyConflictError,
    execute_managed_authz_policy_reconcile,
    plan_managed_authz_policy_reconcile,
    preview_authz_candidate_policy,
    summarize_authz_policy_record,
)
from control_plane.authz_policy_recovery import (
    build_authz_policy_recovery_candidate_reconcile_request,
)
from control_plane.contracts.authz_access_read import (
    AuthzPolicyCandidatePreviewRequest,
    EffectiveAccessEvaluateRequest,
)
from control_plane.contracts.authz_policy_record import (
    AuthzPolicySchemaWriteNotActivatedError,
    LaunchplaneAuthzPolicyRecord,
    authz_policy_sha256,
    build_authz_policy_record_id,
)
from control_plane.contracts.ordinary_agent import (
    OrdinaryAgentPolicySnapshot,
    OrdinaryAgentPrincipal,
)
from control_plane.contracts.privileged_operation import (
    ManagedAuthzPolicySetProposalInput,
    PrivilegedOperationActor,
    PrivilegedOperationConflictError,
    PrivilegedOperationRecord,
)
from control_plane.service_auth import (
    AuthorizationTarget,
    GitHubHumanIdentity,
    GitHubHumanPolicyRule,
    LaunchplaneAuthzPolicy,
    LocalOperatorIdentity,
    LocalOperatorPolicyRule,
    migrate_authz_policy_to_schema_v2,
)
from control_plane.generic_web_preview_authz import (
    GenericWebPreviewAuthzPlanRequest,
    plan_generic_web_preview_authz_reconcile,
)
from control_plane.ordinary_agent_eligibility import evaluate_ordinary_agent_policy
from control_plane.privileged_operation_registry import PrivilegedOperationPlannerError
from control_plane.privileged_operation_service import create_typed_privileged_operation_plan
from control_plane.storage.postgres import PostgresRecordStore


def _ordinary_rule_payload() -> dict[str, object]:
    return {
        "managed_set_id": "ordinary-agent.pilot",
        "managed_rule_id": "agent_one.launchplane.main",
        "principal_id": "agent_one",
        "target": {
            "repository_id": 1001,
            "repository": "example/launchplane",
            "base_branch": "main",
        },
        "actions": ["self_read", "preflight"],
    }


def _schema_v3_policy() -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate_json(
        json.dumps({"schema_version": 3, "ordinary_agents": [_ordinary_rule_payload()]})
    )


def _record(policy: LaunchplaneAuthzPolicy, *, revision: int = 1) -> LaunchplaneAuthzPolicyRecord:
    digest = authz_policy_sha256(policy)
    return LaunchplaneAuthzPolicyRecord(
        record_id=build_authz_policy_record_id(revision=revision, policy_sha256=digest),
        revision=revision,
        source="test:authz-schema-v3",
        updated_at="2026-09-09T00:00:00Z",
        policy=policy,
    )


class _PolicyStore:
    def __init__(self, policy: LaunchplaneAuthzPolicy) -> None:
        self.record = _record(policy)

    def list_authz_policy_records(
        self, *, status: str = "", limit: int | None = None
    ) -> tuple[LaunchplaneAuthzPolicyRecord, ...]:
        records = (self.record,) if status in {"", "active"} else ()
        return records[:limit]


def _human_admin_rule() -> GitHubHumanPolicyRule:
    return GitHubHumanPolicyRule(
        managed_set_id="administration.core",
        managed_rule_id="human-admin",
        github_ids=(101,),
        roles=("admin",),
        products=("launchplane",),
        contexts=("launchplane",),
        actions=("authz_policy_grant.write",),
    )


class AuthzPolicySchemaV3CompatibilityTests(unittest.TestCase):
    def test_legacy_reconcile_plan_digests_remain_byte_compatible(self) -> None:
        cases = (
            (
                1,
                "migrate_v1_to_v2",
                "9e068ccf2099effcc30fd2c488e7eaec038a0512ae2f163468b95c4c068d1d01",
            ),
            (2, "reject", "06dbbfdf22535329e8554537a099f1a4b6959bdebc42d36af8e122a772d80cd1"),
        )
        for active_schema, migration, expected_plan_sha256 in cases:
            with self.subTest(active_schema=active_schema):
                request = AuthzManagedPolicyReconcileEnvelope.model_validate(
                    {
                        "product": "launchplane",
                        "managed_set_id": "test.empty",
                        "schema_migration": migration,
                        "desired_policy": {"schema_version": 2},
                    }
                )
                _, _, candidate, diff = plan_managed_authz_policy_reconcile(
                    record_store=_PolicyStore(
                        LaunchplaneAuthzPolicy.model_validate({"schema_version": active_schema})
                    ),
                    request=request,
                )

                self.assertEqual(
                    candidate.model_dump_json(),
                    '{"schema_version":2,"github_actions":[],"github_humans":[],"terminal_agents":[],"local_operators":[],"local_admins":[]}',
                )
                self.assertEqual(diff.plan_sha256, expected_plan_sha256)

    def test_empty_reader_field_preserves_hard_coded_legacy_digests_and_ids(self) -> None:
        cases = (
            (
                1,
                '{"schema_version":1,"github_actions":[],"github_humans":[],"terminal_agents":[],"local_operators":[],"local_admins":[]}',
                "00acbce577bb9d5b9ebca902203078cef18c4045337643681b73cb0ff5636364",
                "launchplane-authz-policy-r00000000000000000001-00acbce577bb",
            ),
            (
                2,
                '{"schema_version":2,"github_actions":[],"github_humans":[],"terminal_agents":[],"local_operators":[],"local_admins":[]}',
                "2cc07d6a7a67de7f5c30ff2135abce0a1bff1569b624ea3453fc4ee809e3a263",
                "launchplane-authz-policy-r00000000000000000001-2cc07d6a7a67",
            ),
        )
        for schema_version, expected_payload, expected_digest, expected_record_id in cases:
            with self.subTest(schema_version=schema_version):
                policy = LaunchplaneAuthzPolicy.model_validate({"schema_version": schema_version})
                self.assertNotIn("ordinary_agents", policy.model_dump(mode="json"))
                self.assertEqual(policy.model_dump_json(), expected_payload)
                self.assertEqual(authz_policy_sha256(policy), expected_digest)
                self.assertEqual(
                    build_authz_policy_record_id(
                        revision=1,
                        policy_sha256=authz_policy_sha256(policy),
                    ),
                    expected_record_id,
                )

    def test_nonempty_ordinary_rules_require_schema_v3(self) -> None:
        for schema_version in (1, 2):
            with self.subTest(schema_version=schema_version), self.assertRaises(ValidationError):
                LaunchplaneAuthzPolicy.model_validate_json(
                    json.dumps(
                        {
                            "schema_version": schema_version,
                            "ordinary_agents": [_ordinary_rule_payload()],
                        }
                    )
                )

        policy = _schema_v3_policy()
        self.assertEqual(policy.ordinary_agents[0].principal_id, "agent_one")
        self.assertIn("ordinary_agents", policy.model_dump(mode="json"))
        self.assertEqual(
            LaunchplaneAuthzPolicy.model_validate(
                {"schema_version": 3, "ordinary_agents": [_ordinary_rule_payload()]}
            ),
            policy,
        )

        with self.assertRaises(ValidationError):
            LaunchplaneAuthzPolicy.model_validate({"schema_version": 4})

    def test_schema_v3_rejects_malformed_ordinary_rules(self) -> None:
        malformed_cases: tuple[dict[str, object], ...] = (
            {"actions": []},
            {"actions": ["self_read", "self_read"]},
            {"actions": ["admin"]},
            {
                "target": {
                    "repository_id": 1001,
                    "repository": "example/launchplane",
                    "base_branch": "release/*",
                }
            },
        )
        for changes in malformed_cases:
            rule = {**_ordinary_rule_payload(), **changes}
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                LaunchplaneAuthzPolicy.model_validate(
                    {"schema_version": 3, "ordinary_agents": [rule]}
                )

    def test_stored_policy_rejects_duplicate_managed_identity_across_domains(self) -> None:
        ordinary_rule = _ordinary_rule_payload()
        with self.assertRaisesRegex(ValidationError, "unique across the policy"):
            LaunchplaneAuthzPolicy.model_validate_json(
                json.dumps(
                    {
                        "schema_version": 3,
                        "local_operators": [
                            {
                                "managed_set_id": ordinary_rule["managed_set_id"],
                                "managed_rule_id": ordinary_rule["managed_rule_id"],
                                "subjects": ["operator"],
                                "token_labels": ["operator"],
                                "actions": ["product_profile.read"],
                            }
                        ],
                        "ordinary_agents": [ordinary_rule],
                    }
                )
            )

    def test_schema_v3_keeps_v2_identity_semantics_and_no_ordinary_identity(self) -> None:
        policy = LaunchplaneAuthzPolicy(
            schema_version=3,
            local_operators=(
                LocalOperatorPolicyRule(
                    subjects=("operator",),
                    token_labels=("operator",),
                    products=("launchplane",),
                    contexts=("launchplane",),
                    instances=("prod",),
                    actions=("product_environment.read",),
                ),
            ),
            ordinary_agents=_schema_v3_policy().ordinary_agents,
        )
        identity = LocalOperatorIdentity(subject="operator", token_label="operator")

        evaluation = policy.evaluate(
            identity=identity,
            action="product_environment.read",
            product="launchplane",
            context="launchplane",
            target=AuthorizationTarget(scope="instance", instances=("prod",)),
            record_context=False,
        )

        self.assertEqual(evaluation.decision, "allowed")
        self.assertEqual(
            AuthzPolicyCandidatePreviewRequest(candidate_policy=policy).candidate_policy,
            policy,
        )
        self.assertEqual(
            AuthzManagedPolicyReconcileEnvelope(
                product="launchplane",
                managed_set_id="ordinary-agent.pilot",
                desired_policy=_schema_v3_policy(),
            ).desired_policy.schema_version,
            3,
        )

    def test_schema_v3_preserves_v2_instance_scope_validation(self) -> None:
        with self.assertRaisesRegex(ValidationError, "require instances"):
            LaunchplaneAuthzPolicy(
                schema_version=3,
                local_operators=(
                    LocalOperatorPolicyRule(
                        subjects=("operator",),
                        token_labels=("operator",),
                        actions=("deployment.read",),
                    ),
                ),
            )

    def test_schema_v3_is_preserved_by_legacy_migration_helper_and_candidate_reader(self) -> None:
        policy = _schema_v3_policy()
        self.assertIs(migrate_authz_policy_to_schema_v2(policy), policy)
        self.assertEqual(
            AuthzPolicyCandidatePreviewRequest(candidate_policy=policy).candidate_policy,
            policy,
        )

    def test_request_facing_principal_union_rejects_ordinary_agents(self) -> None:
        with self.assertRaises(ValidationError):
            EffectiveAccessEvaluateRequest.model_validate(
                {
                    "principal": {
                        "principal_type": "ordinary_agents",
                        "principal_id": "agent_one",
                    },
                    "action": "product_profile.read",
                    "product": "launchplane",
                    "context": "launchplane",
                    "target_scope": "context",
                }
            )

    def test_summary_reports_nonempty_ordinary_rules_without_changing_legacy_shape(self) -> None:
        legacy_summary = summarize_authz_policy_record(
            _record(LaunchplaneAuthzPolicy(schema_version=2))
        )
        schema_v3_summary = summarize_authz_policy_record(_record(_schema_v3_policy()))

        self.assertNotIn("ordinary_agent_rule_count", legacy_summary)
        self.assertEqual(schema_v3_summary["ordinary_agent_rule_count"], 1)

    def test_v3_generated_recovery_and_preview_requests_preserve_target_schema(self) -> None:
        policy = _schema_v3_policy()
        recovery = build_authz_policy_recovery_candidate_reconcile_request(
            policy=policy,
            github_id=1,
            candidate_id="activate-privileged-policy-operation",
            mode="dry_run",
            reason="compatibility proof",
        )
        generic_web = plan_generic_web_preview_authz_reconcile(
            current_policy=policy,
            request=GenericWebPreviewAuthzPlanRequest(
                target_product="example",
                repository="example/site",
                repository_id="1001",
                repository_owner_id="1002",
                launchplane_sha="b" * 40,
                reason="compatibility proof",
                related_issue="#2363",
            ),
        )

        self.assertEqual(recovery.desired_policy.schema_version, 3)
        self.assertEqual(generic_web.reconcile_request.desired_policy.schema_version, 3)
        for generated_request in (recovery, generic_web.reconcile_request):
            first = plan_managed_authz_policy_reconcile(
                record_store=_PolicyStore(policy), request=generated_request
            )
            second = plan_managed_authz_policy_reconcile(
                record_store=_PolicyStore(policy), request=generated_request
            )
            self.assertEqual(first[2].ordinary_agents, policy.ordinary_agents)
            self.assertEqual(first[3].plan_sha256, second[3].plan_sha256)

    def test_explicit_v2_to_v3_plan_preserves_existing_collections_and_quorum(self) -> None:
        current = LaunchplaneAuthzPolicy.model_validate(
            {
                "schema_version": 2,
                "administrator_quorum": 1,
                "github_actions": [
                    {
                        "managed_set_id": "existing.rules",
                        "managed_rule_id": "workflow",
                        "repository": "example/launchplane",
                        "actions": ["product_profile.read"],
                    }
                ],
                "github_humans": [_human_admin_rule().model_dump(mode="json")],
                "terminal_agents": [
                    {
                        "managed_set_id": "existing.rules",
                        "managed_rule_id": "terminal",
                        "subjects": ["terminal:test"],
                        "token_labels": ["test"],
                        "actions": ["product_profile.read"],
                    }
                ],
                "local_operators": [
                    {
                        "managed_set_id": "existing.rules",
                        "managed_rule_id": "operator",
                        "subjects": ["operator:test"],
                        "token_labels": ["test"],
                        "actions": ["product_profile.read"],
                    }
                ],
                "local_admins": [
                    {
                        "managed_set_id": "existing.rules",
                        "managed_rule_id": "local-admin",
                        "subjects": ["admin:test"],
                        "token_labels": ["test"],
                        "actions": ["product_profile.read"],
                    }
                ],
            }
        )
        request = AuthzManagedPolicyReconcileEnvelope.model_validate(
            {
                "product": "launchplane",
                "managed_set_id": "ordinary-agent.pilot",
                "schema_migration": "migrate_v2_to_v3",
                "desired_policy": {
                    "schema_version": 3,
                    "ordinary_agents": [
                        {
                            **_ordinary_rule_payload(),
                            "target": {
                                "repository_id": 1001,
                                "repository": "example/launchplane",
                                "base_branch": "Release/Main",
                            },
                        }
                    ],
                },
            }
        )

        _, _, candidate, diff = plan_managed_authz_policy_reconcile(
            record_store=_PolicyStore(current), request=request
        )

        self.assertEqual(candidate.schema_version, 3)
        self.assertEqual(candidate.administrator_quorum, 1)
        for collection in (
            "github_actions",
            "github_humans",
            "terminal_agents",
            "local_operators",
            "local_admins",
        ):
            self.assertEqual(getattr(candidate, collection), getattr(current, collection))
        self.assertEqual(candidate.ordinary_agents, request.desired_policy.ordinary_agents)
        self.assertEqual(candidate.ordinary_agents[0].actions, ("preflight", "self_read"))
        self.assertEqual(candidate.ordinary_agents[0].target.base_branch, "Release/Main")
        self.assertTrue(diff.schema_migrated)

    def test_explicit_migration_proposal_persists_as_data_and_binds_replay(self) -> None:
        request = ManagedAuthzPolicySetProposalInput(
            managed_set_id="ordinary-agent.pilot",
            schema_migration="migrate_v2_to_v3",
            desired_policy=_schema_v3_policy(),
            reason="Persist the reviewed policy migration proposal without applying it.",
        )
        actor = PrivilegedOperationActor(
            identity_type="github_human",
            github_id=101,
            login="admin",
        )
        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(
                database_url=f"sqlite+pysqlite:///{Path(directory) / 'launchplane.sqlite3'}"
            )
            store.ensure_schema()
            active = store.seed_authz_policy_if_absent(
                _record(LaunchplaneAuthzPolicy(schema_version=2))
            )
            try:
                written = create_typed_privileged_operation_plan(
                    record_store=store,
                    descriptor_id="managed-authz-policy-set",
                    actor=actor,
                    source_kind="browser_api",
                    source_event_id="policy-v2-to-v3-proposal",
                    request=request,
                    now=lambda: datetime(2026, 9, 10, tzinfo=timezone.utc),
                )
                persisted = store.read_privileged_operation_record(written.record.operation_id)
                stored_active = store.list_authz_policy_records(status="active", limit=2)

                replayed = create_typed_privileged_operation_plan(
                    record_store=store,
                    descriptor_id="managed-authz-policy-set",
                    actor=actor,
                    source_kind="browser_api",
                    source_event_id="policy-v2-to-v3-proposal",
                    request=request,
                    now=lambda: datetime(2026, 9, 10, 0, 5, tzinfo=timezone.utc),
                )

                implicit_reject = request.model_copy(update={"schema_migration": "reject"})
                with self.assertRaises(PrivilegedOperationConflictError):
                    create_typed_privileged_operation_plan(
                        record_store=store,
                        descriptor_id="managed-authz-policy-set",
                        actor=actor,
                        source_kind="browser_api",
                        source_event_id="policy-v2-to-v3-proposal",
                        request=implicit_reject,
                        now=lambda: datetime(2026, 9, 10, tzinfo=timezone.utc),
                    )
                with self.assertRaises(PrivilegedOperationPlannerError) as planning_error:
                    create_typed_privileged_operation_plan(
                        record_store=store,
                        descriptor_id="managed-authz-policy-set",
                        actor=actor,
                        source_kind="browser_api",
                        source_event_id="implicit-policy-v3-proposal",
                        request=implicit_reject,
                        now=lambda: datetime(2026, 9, 10, tzinfo=timezone.utc),
                    )
            finally:
                store.close()

        self.assertEqual(written.write_status, "written")
        self.assertEqual(replayed.write_status, "replayed")
        self.assertEqual(replayed.record, persisted)
        self.assertEqual(persisted.status, "planned")
        self.assertIsInstance(persisted.request, ManagedAuthzPolicySetProposalInput)
        assert isinstance(persisted.request, ManagedAuthzPolicySetProposalInput)
        self.assertEqual(persisted.request.schema_migration, "migrate_v2_to_v3")
        self.assertEqual(persisted.request.reconcile_request().schema_migration, "migrate_v2_to_v3")
        self.assertEqual(stored_active, (active,))
        self.assertIsInstance(planning_error.exception.__cause__, AuthzPolicyConflictError)

        tampered_payload = persisted.model_dump(mode="json")
        tampered_request = tampered_payload["request"]
        assert isinstance(tampered_request, dict)
        tampered_request.pop("schema_migration")
        with self.assertRaisesRegex(ValueError, "request_digest does not match request"):
            PrivilegedOperationRecord.model_validate(tampered_payload)

    def test_v3_reconcile_replaces_only_selected_managed_set_and_keeps_ids_semantic(self) -> None:
        selected_first = _ordinary_rule_payload()
        selected_second = {
            **_ordinary_rule_payload(),
            "managed_rule_id": "agent_two.launchplane.main",
            "principal_id": "agent_two",
        }
        unrelated = {
            **_ordinary_rule_payload(),
            "managed_set_id": "ordinary-agent.unrelated",
            "managed_rule_id": "agent_other.launchplane.main",
            "principal_id": "agent_other",
        }
        active = LaunchplaneAuthzPolicy.model_validate(
            {
                "schema_version": 3,
                "administrator_quorum": 1,
                "github_humans": [_human_admin_rule().model_dump(mode="json")],
                "ordinary_agents": [selected_first, selected_second, unrelated],
            }
        )
        replacement = {
            **selected_first,
            "managed_rule_id": "agent_one.launchplane.trunk",
            "target": {
                "repository_id": 1001,
                "repository": "example/launchplane",
                "base_branch": "trunk",
            },
        }
        request = AuthzManagedPolicyReconcileEnvelope.model_validate(
            {
                "product": "launchplane",
                "managed_set_id": "ordinary-agent.pilot",
                "desired_policy": {
                    "schema_version": 3,
                    "ordinary_agents": [replacement],
                },
            }
        )

        _, _, candidate, diff = plan_managed_authz_policy_reconcile(
            record_store=_PolicyStore(active), request=request
        )

        self.assertEqual(candidate.github_humans, active.github_humans)
        self.assertEqual(
            tuple(
                rule
                for rule in candidate.ordinary_agents
                if rule.managed_set_id == "ordinary-agent.unrelated"
            ),
            tuple(
                rule
                for rule in active.ordinary_agents
                if rule.managed_set_id == "ordinary-agent.unrelated"
            ),
        )
        self.assertEqual(
            {rule.managed_rule_id for rule in candidate.ordinary_agents},
            {"agent_one.launchplane.trunk", "agent_other.launchplane.main"},
        )
        self.assertTrue(diff.authorization_changed)
        self.assertEqual(diff.added_rule_count, 1)
        self.assertEqual(diff.removed_rule_count, 2)
        old_binding = evaluate_ordinary_agent_policy(
            snapshot=OrdinaryAgentPolicySnapshot(
                record_kind="proposed_ordinary_agent_v1",
                authority_state="inert",
                authorizes_execution=False,
                record_id="policy-after-reconcile",
                revision=2,
                policy_digest=authz_policy_sha256(candidate),
                input_domain_id="ordinary-agent-effective-inputs-v1",
                evaluator_semantics_version="ordinary-agent-eligibility-v1",
                rules=candidate.ordinary_agents,
            ),
            principal=OrdinaryAgentPrincipal(
                record_kind="proposed_ordinary_agent_v1",
                authority_state="inert",
                authorizes_execution=False,
                record_id="principal-agent-one",
                principal_id="agent_one",
                execution_profile="guarded_executor",
                status="active",
            ),
            target=active.ordinary_agents[0].target,
            action="self_read",
            managed_set_id="ordinary-agent.pilot",
            managed_rule_id="agent_one.launchplane.main",
        )
        self.assertEqual(old_binding.reason_code, "bound_rule_missing")

    def test_candidate_preview_reports_ordinary_structure_without_admin_effect(self) -> None:
        active = LaunchplaneAuthzPolicy(
            schema_version=3,
            administrator_quorum=1,
            github_humans=(_human_admin_rule(),),
        )
        candidate = active.model_copy(
            update={"ordinary_agents": _schema_v3_policy().ordinary_agents}
        )
        response = preview_authz_candidate_policy(
            active_record=_record(active),
            caller_identity=GitHubHumanIdentity(
                login="admin",
                github_id=101,
                name="Admin",
                email="admin@example.test",
                organizations=frozenset(),
                teams=frozenset(),
                role="admin",
            ),
            request=AuthzPolicyCandidatePreviewRequest.model_validate(
                {
                    "candidate_policy": candidate.model_dump(mode="json"),
                    "probes": [
                        {
                            "principal": {
                                "principal_type": "github_human",
                                "login": "admin",
                                "github_id": 101,
                                "role": "admin",
                            },
                            "action": "authz_policy_grant.write",
                            "product": "launchplane",
                            "context": "launchplane",
                            "target_scope": "context",
                        }
                    ],
                }
            ),
            trace_id="launchplane_req_v3_preview",
        )

        self.assertEqual(response.candidate_policy.schema_version, 3)
        self.assertEqual(response.diff.changed_principal_types, ("ordinary_agents",))
        self.assertEqual(response.diff.candidate_principal_rule_counts.ordinary_agents, 1)
        self.assertEqual(response.candidate_reachable_administrators.rule_count, 1)
        self.assertEqual(
            response.candidate_reachable_administrators.strict_github_human_id_count, 1
        )
        self.assertEqual(response.probes[0].active_evaluation.decision, "allowed")
        self.assertEqual(response.probes[0].candidate_evaluation.decision, "allowed")

    def test_v3_same_identity_principal_family_replacement_remains_one_update(self) -> None:
        active = _schema_v3_policy()
        request = AuthzManagedPolicyReconcileEnvelope.model_validate(
            {
                "product": "launchplane",
                "managed_set_id": "ordinary-agent.pilot",
                "desired_policy": {
                    "schema_version": 3,
                    "local_operators": [
                        {
                            "managed_set_id": "ordinary-agent.pilot",
                            "managed_rule_id": "agent_one.launchplane.main",
                            "subjects": ["operator:test"],
                            "token_labels": ["test"],
                            "actions": ["product_profile.read"],
                        }
                    ],
                },
            }
        )

        _, _, candidate, diff = plan_managed_authz_policy_reconcile(
            record_store=_PolicyStore(active), request=request
        )

        self.assertEqual(candidate.ordinary_agents, ())
        self.assertEqual(len(candidate.local_operators), 1)
        self.assertEqual(diff.updated_rule_count, 1)
        self.assertEqual(diff.added_rule_count, 0)
        self.assertEqual(diff.removed_rule_count, 0)
        self.assertEqual(diff.changes[0].previous_principal_type, "ordinary_agents")
        self.assertEqual(diff.changes[0].desired_principal_type, "local_operators")

    def test_schema_transition_table_rejects_every_unsupported_pair(self) -> None:
        cases = (
            (1, "reject", 2),
            (1, "migrate_v2_to_v3", 3),
            (2, "migrate_v1_to_v2", 2),
            (2, "reject", 3),
            (3, "reject", 2),
            (3, "migrate_v2_to_v3", 3),
        )
        for active_schema, migration, desired_schema in cases:
            with self.subTest(
                active_schema=active_schema,
                migration=migration,
                desired_schema=desired_schema,
            ):
                request = AuthzManagedPolicyReconcileEnvelope.model_validate(
                    {
                        "product": "launchplane",
                        "managed_set_id": "test.empty",
                        "schema_migration": migration,
                        "desired_policy": {"schema_version": desired_schema},
                    }
                )
                with self.assertRaises(AuthzPolicyConflictError):
                    plan_managed_authz_policy_reconcile(
                        record_store=_PolicyStore(
                            LaunchplaneAuthzPolicy.model_validate({"schema_version": active_schema})
                        ),
                        request=request,
                    )

    def test_v3_apply_stays_fenced_after_successful_dry_run(self) -> None:
        active = _schema_v3_policy()
        dry_run = AuthzManagedPolicyReconcileEnvelope(
            product="launchplane",
            managed_set_id="ordinary-agent.pilot",
            desired_policy=active,
        )
        _, _, _, diff = plan_managed_authz_policy_reconcile(
            record_store=_PolicyStore(active), request=dry_run
        )
        apply = AuthzManagedPolicyReconcileEnvelope.model_validate(
            {
                **dry_run.model_dump(mode="json"),
                "mode": "apply",
                "reason": "prove the write fence",
                "reviewed_plan_sha256": diff.plan_sha256,
            }
        )

        store = _PolicyStore(active)
        with self.assertRaisesRegex(
            AuthzPolicySchemaWriteNotActivatedError,
            "authz_policy_schema_v3_write_not_activated",
        ):
            execute_managed_authz_policy_reconcile(
                record_store=store,
                request=apply,
                identity=GitHubHumanIdentity(
                    login="admin",
                    github_id=101,
                    name="Admin",
                    email="admin@example.test",
                    organizations=frozenset(),
                    teams=frozenset(),
                    role="admin",
                ),
                trace_id="launchplane_req_v3_apply_fence",
                now_timestamp=lambda: "2026-09-10T00:00:00Z",
            )
        self.assertEqual(store.record.policy, active)


class AuthzPolicySchemaV3StoreFenceTests(unittest.TestCase):
    def _store(self, directory: str) -> PostgresRecordStore:
        store = PostgresRecordStore(
            database_url=f"sqlite+pysqlite:///{Path(directory) / 'launchplane.sqlite3'}"
        )
        store.ensure_schema()
        return store

    def test_seed_rejects_schema_v3_when_no_active_policy_exists(self) -> None:
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            with self.assertRaisesRegex(
                AuthzPolicySchemaWriteNotActivatedError,
                "authz_policy_schema_v3_write_not_activated",
            ):
                store.seed_authz_policy_if_absent(_record(_schema_v3_policy()))
            self.assertEqual(store.list_authz_policy_records(), ())
            store.close()

    def test_compare_write_rejects_v3_replacement_without_superseding_v2(self) -> None:
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            active = store.seed_authz_policy_if_absent(
                _record(LaunchplaneAuthzPolicy(schema_version=2))
            )
            replacement = _record(_schema_v3_policy(), revision=2)

            with self.assertRaises(AuthzPolicySchemaWriteNotActivatedError):
                store.compare_and_write_authz_policy_record(
                    expected_record=active,
                    replacement_record=replacement,
                )

            self.assertEqual(store.list_authz_policy_records(status="active"), (active,))
            self.assertEqual(store.list_authz_policy_records(status="superseded"), ())
            store.close()

    def test_compare_write_rejects_downgrade_or_delete_of_observed_v3(self) -> None:
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            active = _record(_schema_v3_policy())
            store._write_row(store._authz_policy_row(active))
            replacement = _record(LaunchplaneAuthzPolicy(schema_version=2), revision=2)

            with self.assertRaisesRegex(
                AuthzPolicyConflictError,
                "schema downgrade from version 3 to version 2 is not supported",
            ):
                plan_managed_authz_policy_reconcile(
                    record_store=store,
                    request=AuthzManagedPolicyReconcileEnvelope(
                        product="launchplane",
                        managed_set_id="test.empty",
                        desired_policy=LaunchplaneAuthzPolicy(schema_version=2),
                    ),
                )

            for candidate in (replacement, None):
                with (
                    self.subTest(replacement=candidate is not None),
                    self.assertRaises(AuthzPolicySchemaWriteNotActivatedError),
                ):
                    store.compare_and_write_authz_policy_record(
                        expected_record=active,
                        replacement_record=candidate,
                    )

            self.assertEqual(store.list_authz_policy_records(status="active"), (active,))
            self.assertEqual(store.list_authz_policy_records(status="superseded"), ())
            store.close()
