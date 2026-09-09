from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from pydantic import ValidationError

from control_plane.authz_grant_service import (
    AuthzManagedPolicyReconcileEnvelope,
    plan_managed_authz_policy_reconcile,
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
from control_plane.service_auth import (
    AuthorizationTarget,
    LaunchplaneAuthzPolicy,
    LocalOperatorIdentity,
    LocalOperatorPolicyRule,
    migrate_authz_policy_to_schema_v2,
)
from control_plane.generic_web_preview_authz import (
    GenericWebPreviewAuthzPlanRequest,
    plan_generic_web_preview_authz_reconcile,
)
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


class AuthzPolicySchemaV3CompatibilityTests(unittest.TestCase):
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
        with self.assertRaises(ValidationError):
            AuthzPolicyCandidatePreviewRequest(candidate_policy=policy)
        with self.assertRaises(ValidationError):
            AuthzManagedPolicyReconcileEnvelope(
                product="launchplane",
                managed_set_id="ordinary-agent.pilot",
                desired_policy=policy,
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

    def test_schema_v3_is_preserved_by_migration_but_rejected_by_v2_writer(self) -> None:
        policy = _schema_v3_policy()
        self.assertIs(migrate_authz_policy_to_schema_v2(policy), policy)
        with self.assertRaises(ValidationError):
            AuthzPolicyCandidatePreviewRequest(candidate_policy=policy)

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

    def test_v3_fails_closed_at_recovery_and_generated_preview_planners(self) -> None:
        policy = _schema_v3_policy()
        with self.assertRaises(AuthzPolicySchemaWriteNotActivatedError):
            build_authz_policy_recovery_candidate_reconcile_request(
                policy=policy,
                github_id=1,
                candidate_id="activate-privileged-policy-operation",
                mode="dry_run",
                reason="compatibility proof",
            )
        with self.assertRaises(AuthzPolicySchemaWriteNotActivatedError):
            plan_generic_web_preview_authz_reconcile(
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

            with self.assertRaises(AuthzPolicySchemaWriteNotActivatedError):
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
