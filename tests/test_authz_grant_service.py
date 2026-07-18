from __future__ import annotations

import json
import unittest
from typing import cast

from pydantic import ValidationError

from control_plane.authz_grant_service import (
    AuthzManagedPolicyReconcileEnvelope,
    AuthzPolicyConflictError,
    AuthzPolicyRequestError,
    AuthzPolicyGitHubActionsGrant,
    AuthzPolicyGitHubActionsGrantEnvelope,
    AuthzPolicyGitHubActionsRemovalEnvelope,
    AuthzPolicyGitHubHumanGrant,
    AuthzPolicyGitHubHumanGrantEnvelope,
    AuthzPolicyLocalAdminGrantEnvelope,
    AuthzPolicyLocalOperatorGrantEnvelope,
    AuthzPolicyTerminalAgentGrantEnvelope,
    authz_policy_grant_response_audit_payload,
    build_authz_policy_github_actions_removal_service_result,
    build_authz_policy_grant_service_result,
    execute_authz_policy_route,
    plan_github_actions_authz_policy_removal,
    plan_github_actions_authz_policy_grant,
    plan_github_human_authz_policy_grant,
    plan_managed_authz_policy_reconcile,
    write_github_actions_authz_policy_removal,
    write_github_actions_authz_policy_grant,
    write_github_human_authz_policy_grant,
)
from control_plane.contracts.authz_policy_record import (
    AuthzPolicyCompareWriteResult,
    LaunchplaneAuthzPolicyRecord,
    authz_policy_sha256,
    build_authz_policy_record_id,
)
from control_plane.service_auth import (
    GitHubActionsIdentity,
    GitHubActionsPolicyRule,
    LaunchplaneAuthzPolicy,
    LocalOperatorPolicyRule,
)

_GITHUB_REPOSITORY_IDS = {
    "repository_id": "1001",
    "repository_owner_id": "2001",
}


class _AuthzPolicyStore:
    def __init__(
        self,
        records: tuple[LaunchplaneAuthzPolicyRecord, ...],
        *,
        compare_write_allowed: bool = True,
    ) -> None:
        self.records = records
        self.written_records: list[LaunchplaneAuthzPolicyRecord] = []
        self.compare_write_allowed = compare_write_allowed

    def list_authz_policy_records(
        self,
        *,
        status: str = "",
        limit: int | None = None,
    ) -> tuple[LaunchplaneAuthzPolicyRecord, ...]:
        records = tuple(record for record in self.records if not status or record.status == status)
        if limit is not None:
            return records[:limit]
        return records

    def compare_and_write_authz_policy_record(
        self,
        *,
        expected_record: LaunchplaneAuthzPolicyRecord,
        replacement_record: LaunchplaneAuthzPolicyRecord | None,
    ) -> AuthzPolicyCompareWriteResult:
        active_records = tuple(record for record in self.records if record.status == "active")
        current_record = active_records[0]
        if not self.compare_write_allowed:
            return AuthzPolicyCompareWriteResult("stale", current_record)
        if current_record != expected_record:
            return AuthzPolicyCompareWriteResult("stale", current_record)
        if replacement_record is None:
            return AuthzPolicyCompareWriteResult("unchanged", current_record)
        superseded_ids = {record.record_id for record in active_records}
        superseded_records = tuple(
            record.model_copy(update={"status": "superseded"})
            if record.record_id in superseded_ids
            else record
            for record in self.records
        )
        self.written_records.append(replacement_record)
        self.records = (replacement_record, *superseded_records)
        return AuthzPolicyCompareWriteResult("written", replacement_record)


def _identity() -> GitHubActionsIdentity:
    return GitHubActionsIdentity(
        repository="cbusillo/launchplane",
        repository_owner="cbusillo",
        repository_id="1001",
        repository_owner_id="2001",
        workflow_ref="cbusillo/launchplane/.github/workflows/deploy-launchplane.yml@refs/heads/main",
        job_workflow_ref="",
        event_name="workflow_dispatch",
        ref="refs/heads/main",
        ref_type="branch",
        environment="",
        subject="repo:cbusillo/launchplane:ref:refs/heads/main",
        sha="abc123",
        raw_claims={},
    )


def _active_record() -> LaunchplaneAuthzPolicyRecord:
    policy = LaunchplaneAuthzPolicy.model_validate(
        {
            "github_actions": [
                {
                    "repository": "cbusillo/launchplane",
                    "products": ["launchplane"],
                    "contexts": ["launchplane"],
                    "actions": ["launchplane_service_deploy.execute"],
                }
            ]
        }
    )
    policy_sha256 = authz_policy_sha256(policy)
    return LaunchplaneAuthzPolicyRecord(
        record_id=build_authz_policy_record_id(
            revision=1,
            policy_sha256=policy_sha256,
        ),
        revision=1,
        status="active",
        source="test:bootstrap",
        updated_at="2026-05-07T15:00:00Z",
        policy_sha256=policy_sha256,
        policy=policy,
    )


def _active_record_for_policy(policy: LaunchplaneAuthzPolicy) -> LaunchplaneAuthzPolicyRecord:
    policy_sha256 = authz_policy_sha256(policy)
    return LaunchplaneAuthzPolicyRecord(
        record_id=build_authz_policy_record_id(
            revision=1,
            policy_sha256=policy_sha256,
        ),
        revision=1,
        status="active",
        source="test:managed",
        updated_at="2026-07-18T06:45:00Z",
        policy_sha256=policy_sha256,
        policy=policy,
    )


def _grant_request(mode: str = "apply") -> AuthzPolicyGitHubActionsGrantEnvelope:
    return AuthzPolicyGitHubActionsGrantEnvelope.model_validate(
        {
            "product": "launchplane",
            "mode": mode,
            "reason": "Grant product profile read for SYO promotion diagnostics.",
            "related_issue": "cbusillo/launchplane#83",
            "grant": {
                "repository": "cbusillo/launchplane",
                **_GITHUB_REPOSITORY_IDS,
                "workflow_refs": [
                    "cbusillo/launchplane/.github/workflows/deploy-launchplane.yml@refs/heads/main"
                ],
                "event_names": ["workflow_dispatch"],
                "products": ["sellyouroutboard"],
                "contexts": ["launchplane"],
                "actions": ["product_profile.read"],
                "source_label": "test:audit-grant",
            },
        }
    )


def _removal_request(mode: str = "apply") -> AuthzPolicyGitHubActionsRemovalEnvelope:
    return AuthzPolicyGitHubActionsRemovalEnvelope.model_validate(
        {
            "product": "launchplane",
            "mode": mode,
            "reason": "Remove broad Launchplane deploy authority after narrowing routes.",
            "related_issue": "cbusillo/launchplane#1049",
            "removal": {
                "repository": "cbusillo/launchplane",
                "products": ["launchplane"],
                "contexts": ["launchplane"],
                "actions": ["launchplane_service_deploy.execute"],
                "source_label": "test:authz-removal",
            },
        }
    )


def _human_grant_request(mode: str = "apply") -> AuthzPolicyGitHubHumanGrantEnvelope:
    return AuthzPolicyGitHubHumanGrantEnvelope.model_validate(
        {
            "product": "launchplane",
            "mode": mode,
            "reason": "Grant SYO promotion workflow dispatch to the operator.",
            "related_issue": "cbusillo/launchplane#153",
            "grant": {
                "logins": ["cbusillo"],
                "roles": ["admin"],
                "products": ["sellyouroutboard"],
                "contexts": ["sellyouroutboard"],
                "actions": ["generic_web_prod_promotion.dispatch"],
                "source_label": "test:human-grant",
            },
        }
    )


class AuthzGrantServiceTests(unittest.TestCase):
    def test_github_actions_grant_requires_immutable_repository_ids(self) -> None:
        payload = {
            "product": "launchplane",
            "mode": "dry_run",
            "grant": {
                "repository": "cbusillo/launchplane",
                "actions": ["product_profile.read"],
            },
        }

        with self.assertRaises(ValidationError):
            AuthzPolicyGitHubActionsGrantEnvelope.model_validate(payload)

        grant = cast(dict[str, object], payload["grant"])
        grant.update({"repository_id": "", "repository_owner_id": ""})
        with self.assertRaisesRegex(
            ValidationError,
            "require immutable repository_id and repository_owner_id selectors",
        ):
            AuthzPolicyGitHubActionsGrantEnvelope.model_validate(payload)

        grant.update(_GITHUB_REPOSITORY_IDS)
        envelope = AuthzPolicyGitHubActionsGrantEnvelope.model_validate(payload)
        self.assertEqual(envelope.grant.repository_id, "1001")
        self.assertEqual(envelope.grant.repository_owner_id, "2001")

    def test_route_rejects_policy_change_after_authorization(self) -> None:
        active_record = _active_record()

        with self.assertRaisesRegex(AuthzPolicyConflictError, "after the caller was authorized"):
            execute_authz_policy_route(
                record_store=_AuthzPolicyStore((active_record,)),
                request=_grant_request(mode="dry_run"),
                identity=_identity(),
                trace_id="trace-stale-authorization",
                now_timestamp=lambda: "2026-07-18T00:00:00Z",
                authorized_policy_sha256="different-policy",
            )

    def test_managed_route_rejects_policy_change_after_authorization(self) -> None:
        active_record = _active_record()
        request = AuthzManagedPolicyReconcileEnvelope.model_validate(
            {
                "schema_version": 2,
                "product": "launchplane",
                "mode": "dry_run",
                "managed_set_id": "operator.launchplane",
                "schema_migration": "migrate_v1_to_v2",
                "unmanaged_adoption": "adopt_matching",
                "desired_policy": {
                    "schema_version": 2,
                    "github_actions": [
                        {
                            "managed_set_id": "operator.launchplane",
                            "managed_rule_id": "service.deploy",
                            "repository": "cbusillo/launchplane",
                            "repository_id": "1001",
                            "repository_owner_id": "2001",
                            "products": ["launchplane"],
                            "contexts": ["launchplane"],
                            "actions": ["launchplane_service_deploy.execute"],
                        }
                    ],
                },
            }
        )

        with self.assertRaisesRegex(AuthzPolicyConflictError, "after the caller was authorized"):
            execute_authz_policy_route(
                record_store=_AuthzPolicyStore((active_record,)),
                request=request,
                identity=_identity(),
                trace_id="trace-managed-stale-authorization",
                now_timestamp=lambda: "2026-07-18T00:00:00Z",
                authorized_policy_sha256="different-policy",
            )

    def test_managed_reconcile_adopts_v1_rule_and_migrates_schema(self) -> None:
        current_record = _active_record()
        request = AuthzManagedPolicyReconcileEnvelope.model_validate(
            {
                "schema_version": 2,
                "product": "launchplane",
                "mode": "dry_run",
                "managed_set_id": "operator.launchplane",
                "schema_migration": "migrate_v1_to_v2",
                "unmanaged_adoption": "adopt_matching",
                "desired_policy": {
                    "schema_version": 2,
                    "github_actions": [
                        {
                            "managed_set_id": "operator.launchplane",
                            "managed_rule_id": "service.deploy",
                            "repository": "cbusillo/launchplane",
                            "repository_id": "1001",
                            "repository_owner_id": "2001",
                            "products": ["launchplane"],
                            "contexts": ["launchplane"],
                            "actions": ["launchplane_service_deploy.execute"],
                        }
                    ],
                },
            }
        )

        current_policy, observed_record, updated_policy, diff = plan_managed_authz_policy_reconcile(
            record_store=_AuthzPolicyStore((current_record,)),
            request=request,
        )

        self.assertEqual(current_policy.schema_version, 1)
        self.assertEqual(observed_record, current_record)
        self.assertEqual(updated_policy.schema_version, 2)
        self.assertEqual(len(updated_policy.github_actions), 1)
        self.assertEqual(
            updated_policy.github_actions[0].managed_rule_id,
            "service.deploy",
        )
        self.assertTrue(diff.schema_migrated)
        self.assertTrue(diff.changed)
        self.assertEqual(diff.adopted_rule_count, 1)
        self.assertEqual(diff.added_rule_count, 0)

    def test_managed_reconcile_updates_and_removes_only_its_managed_set(self) -> None:
        current_policy = LaunchplaneAuthzPolicy.model_validate(
            {
                "schema_version": 2,
                "github_actions": [
                    {
                        "managed_set_id": "operator.launchplane",
                        "managed_rule_id": "service.read",
                        "repository": "cbusillo/launchplane",
                        "repository_id": "1001",
                        "repository_owner_id": "2001",
                        "products": ["launchplane"],
                        "contexts": ["launchplane"],
                        "actions": ["launchplane_service.read"],
                    },
                    {
                        "managed_set_id": "operator.launchplane",
                        "managed_rule_id": "stale.rule",
                        "repository": "cbusillo/launchplane",
                        "repository_id": "1001",
                        "repository_owner_id": "2001",
                        "products": ["launchplane"],
                        "contexts": ["launchplane"],
                        "actions": ["product_profile.read"],
                    },
                    {
                        "managed_set_id": "another.manager",
                        "managed_rule_id": "preserved.rule",
                        "repository": "cbusillo/launchplane",
                        "repository_id": "1001",
                        "repository_owner_id": "2001",
                        "products": ["launchplane"],
                        "contexts": ["launchplane"],
                        "actions": ["product_profile.read"],
                    },
                ],
            }
        )
        current_record = _active_record_for_policy(current_policy)
        request = AuthzManagedPolicyReconcileEnvelope.model_validate(
            {
                "schema_version": 2,
                "product": "launchplane",
                "mode": "dry_run",
                "managed_set_id": "operator.launchplane",
                "desired_policy": {
                    "schema_version": 2,
                    "github_actions": [
                        {
                            "managed_set_id": "operator.launchplane",
                            "managed_rule_id": "service.read",
                            "repository": "cbusillo/launchplane",
                            "repository_id": "1001",
                            "repository_owner_id": "2001",
                            "products": ["launchplane"],
                            "contexts": ["launchplane"],
                            "actions": ["product_profile.read"],
                        }
                    ],
                },
            }
        )

        _, _, updated_policy, diff = plan_managed_authz_policy_reconcile(
            record_store=_AuthzPolicyStore((current_record,)),
            request=request,
        )

        rules_by_identity = {
            (rule.managed_set_id, rule.managed_rule_id): rule
            for rule in updated_policy.github_actions
        }
        self.assertEqual(
            set(rules_by_identity),
            {
                ("operator.launchplane", "service.read"),
                ("another.manager", "preserved.rule"),
            },
        )
        self.assertEqual(diff.updated_rule_count, 1)
        self.assertEqual(diff.removed_rule_count, 1)
        self.assertEqual(diff.added_rule_count, 0)

    def test_managed_reconcile_rejects_removing_last_policy_administrator(self) -> None:
        current_policy = LaunchplaneAuthzPolicy.model_validate(
            {
                "schema_version": 2,
                "local_admins": [
                    {
                        "managed_set_id": "operator.owner",
                        "managed_rule_id": "authz.admin",
                        "subjects": ["owner"],
                        "token_labels": ["owner-admin"],
                        "products": ["launchplane"],
                        "contexts": ["launchplane"],
                        "actions": ["authz_policy_grant.write"],
                    }
                ],
            }
        )
        request = AuthzManagedPolicyReconcileEnvelope.model_validate(
            {
                "schema_version": 2,
                "product": "launchplane",
                "mode": "dry_run",
                "managed_set_id": "operator.owner",
                "desired_policy": {"schema_version": 2},
            }
        )

        with self.assertRaisesRegex(AuthzPolicyConflictError, "retain at least one principal"):
            plan_managed_authz_policy_reconcile(
                record_store=_AuthzPolicyStore((_active_record_for_policy(current_policy),)),
                request=request,
            )

    def test_managed_reconcile_rejects_unreviewed_apply_plan(self) -> None:
        current_record = _active_record()
        request = AuthzManagedPolicyReconcileEnvelope.model_validate(
            {
                "schema_version": 2,
                "product": "launchplane",
                "mode": "apply",
                "managed_set_id": "operator.launchplane",
                "schema_migration": "migrate_v1_to_v2",
                "reviewed_plan_sha256": "0" * 64,
                "reason": "Apply the reviewed managed policy.",
                "desired_policy": {"schema_version": 2},
            }
        )

        with self.assertRaisesRegex(
            AuthzPolicyConflictError,
            "reviewed_plan_sha256 no longer matches",
        ):
            plan_managed_authz_policy_reconcile(
                record_store=_AuthzPolicyStore((current_record,)),
                request=request,
            )

    def test_managed_reconcile_requires_managed_desired_rules(self) -> None:
        with self.assertRaisesRegex(
            ValidationError,
            "Every desired managed authz rule must declare",
        ):
            AuthzManagedPolicyReconcileEnvelope.model_validate(
                {
                    "schema_version": 2,
                    "product": "launchplane",
                    "mode": "dry_run",
                    "managed_set_id": "operator.launchplane",
                    "desired_policy": {
                        "schema_version": 2,
                        "github_actions": [
                            {
                                "repository": "cbusillo/launchplane",
                                "repository_id": "1001",
                                "repository_owner_id": "2001",
                                "actions": ["product_profile.read"],
                            }
                        ],
                    },
                }
            )

    def test_managed_reconcile_requires_explicit_migration_and_adoption(self) -> None:
        current_record = _active_record()
        desired_policy = {
            "schema_version": 2,
            "github_actions": [
                {
                    "managed_set_id": "operator.launchplane",
                    "managed_rule_id": "service.deploy",
                    "repository": "cbusillo/launchplane",
                    "repository_id": "1001",
                    "repository_owner_id": "2001",
                    "products": ["launchplane"],
                    "contexts": ["launchplane"],
                    "actions": ["launchplane_service_deploy.execute"],
                }
            ],
        }
        without_migration = AuthzManagedPolicyReconcileEnvelope.model_validate(
            {
                "schema_version": 2,
                "product": "launchplane",
                "managed_set_id": "operator.launchplane",
                "desired_policy": desired_policy,
            }
        )
        with self.assertRaisesRegex(AuthzPolicyConflictError, "explicit schema_migration"):
            plan_managed_authz_policy_reconcile(
                record_store=_AuthzPolicyStore((current_record,)),
                request=without_migration,
            )

        without_adoption = AuthzManagedPolicyReconcileEnvelope.model_validate(
            {
                **without_migration.model_dump(mode="json"),
                "schema_migration": "migrate_v1_to_v2",
            }
        )
        with self.assertRaisesRegex(AuthzPolicyConflictError, "would adopt an unmanaged rule"):
            plan_managed_authz_policy_reconcile(
                record_store=_AuthzPolicyStore((current_record,)),
                request=without_adoption,
            )

    def test_managed_reconcile_rejects_ambiguous_unmanaged_adoption(self) -> None:
        unmanaged_rule = GitHubActionsPolicyRule(
            repository="cbusillo/launchplane",
            products=("launchplane",),
            contexts=("launchplane",),
            actions=("product_profile.read",),
        )
        current_record = _active_record_for_policy(
            LaunchplaneAuthzPolicy(
                schema_version=2,
                github_actions=(unmanaged_rule, unmanaged_rule),
            )
        )
        request = AuthzManagedPolicyReconcileEnvelope.model_validate(
            {
                "schema_version": 2,
                "product": "launchplane",
                "managed_set_id": "operator.launchplane",
                "unmanaged_adoption": "adopt_matching",
                "desired_policy": {
                    "schema_version": 2,
                    "github_actions": [
                        {
                            "managed_set_id": "operator.launchplane",
                            "managed_rule_id": "profile.read",
                            "repository": "cbusillo/launchplane",
                            "repository_id": "1001",
                            "repository_owner_id": "2001",
                            "products": ["launchplane"],
                            "contexts": ["launchplane"],
                            "actions": ["product_profile.read"],
                        }
                    ],
                },
            }
        )

        with self.assertRaisesRegex(AuthzPolicyConflictError, "adoption is ambiguous"):
            plan_managed_authz_policy_reconcile(
                record_store=_AuthzPolicyStore((current_record,)),
                request=request,
            )

    def test_managed_reconcile_adopts_semantically_equal_unordered_rule(self) -> None:
        current_policy = LaunchplaneAuthzPolicy.model_validate(
            {
                "schema_version": 2,
                "github_actions": [
                    {
                        "repository": "cbusillo/launchplane",
                        "repository_id": "1001",
                        "repository_owner_id": "2001",
                        "event_names": ["workflow_dispatch", "push", "workflow_dispatch"],
                        "products": ["launchplane", "launchplane"],
                        "contexts": ["launchplane"],
                        "actions": ["service.read", "product_profile.read", "service.read"],
                    }
                ],
            }
        )
        request = AuthzManagedPolicyReconcileEnvelope.model_validate(
            {
                "schema_version": 2,
                "product": "launchplane",
                "mode": "dry_run",
                "managed_set_id": "operator.launchplane",
                "unmanaged_adoption": "adopt_matching",
                "desired_policy": {
                    "schema_version": 2,
                    "github_actions": [
                        {
                            "managed_set_id": "operator.launchplane",
                            "managed_rule_id": "service.read",
                            "repository": "cbusillo/launchplane",
                            "repository_id": "1001",
                            "repository_owner_id": "2001",
                            "event_names": ["push", "workflow_dispatch"],
                            "products": ["launchplane"],
                            "contexts": ["launchplane"],
                            "actions": ["product_profile.read", "service.read"],
                        }
                    ],
                },
            }
        )

        _, _, adopted_policy, diff = plan_managed_authz_policy_reconcile(
            record_store=_AuthzPolicyStore((_active_record_for_policy(current_policy),)),
            request=request,
        )

        self.assertEqual(diff.adopted_rule_count, 1)
        self.assertEqual(len(adopted_policy.github_actions), 1)
        self.assertEqual(adopted_policy.github_actions[0].managed_rule_id, "service.read")
        removal_request = AuthzManagedPolicyReconcileEnvelope.model_validate(
            {
                **request.model_dump(mode="json"),
                "unmanaged_adoption": "reject",
                "desired_policy": {"schema_version": 2},
            }
        )
        _, _, removed_policy, removal_diff = plan_managed_authz_policy_reconcile(
            record_store=_AuthzPolicyStore((_active_record_for_policy(adopted_policy),)),
            request=removal_request,
        )
        self.assertEqual(removal_diff.removed_rule_count, 1)
        self.assertEqual(removed_policy.github_actions, ())

    def test_managed_reconcile_preserves_other_set_order_without_hash_churn(self) -> None:
        current_policy = LaunchplaneAuthzPolicy(
            schema_version=2,
            local_operators=(
                LocalOperatorPolicyRule(
                    managed_set_id="operator.second",
                    managed_rule_id="second.read",
                    subjects=("owner",),
                    token_labels=("operator",),
                    actions=("product_profile.read",),
                ),
                LocalOperatorPolicyRule(
                    managed_set_id="operator.first",
                    managed_rule_id="first.read",
                    subjects=("owner",),
                    token_labels=("operator",),
                    actions=("product_profile.read",),
                ),
            ),
        )
        current_record = _active_record_for_policy(current_policy)
        request = AuthzManagedPolicyReconcileEnvelope.model_validate(
            {
                "schema_version": 2,
                "product": "launchplane",
                "managed_set_id": "operator.first",
                "desired_policy": {
                    "schema_version": 2,
                    "local_operators": [current_policy.local_operators[1].model_dump(mode="json")],
                },
            }
        )

        _, _, updated_policy, diff = plan_managed_authz_policy_reconcile(
            record_store=_AuthzPolicyStore((current_record,)),
            request=request,
        )

        self.assertEqual(updated_policy, current_policy)
        self.assertFalse(diff.changed)
        self.assertEqual(diff.unchanged_rule_count, 1)

    def test_managed_reconcile_reports_principal_type_move_as_one_update(self) -> None:
        current_policy = LaunchplaneAuthzPolicy(
            schema_version=2,
            local_operators=(
                LocalOperatorPolicyRule(
                    managed_set_id="operator.owner",
                    managed_rule_id="owner.read",
                    subjects=("owner",),
                    token_labels=("operator",),
                    actions=("product_profile.read",),
                ),
            ),
        )
        current_record = _active_record_for_policy(current_policy)
        request = AuthzManagedPolicyReconcileEnvelope.model_validate(
            {
                "schema_version": 2,
                "product": "launchplane",
                "managed_set_id": "operator.owner",
                "desired_policy": {
                    "schema_version": 2,
                    "local_admins": [
                        {
                            "managed_set_id": "operator.owner",
                            "managed_rule_id": "owner.read",
                            "subjects": ["owner"],
                            "token_labels": ["admin"],
                            "actions": ["product_profile.read"],
                        }
                    ],
                },
            }
        )

        _, _, updated_policy, diff = plan_managed_authz_policy_reconcile(
            record_store=_AuthzPolicyStore((current_record,)),
            request=request,
        )

        self.assertEqual(len(updated_policy.local_operators), 0)
        self.assertEqual(len(updated_policy.local_admins), 1)
        self.assertEqual(diff.updated_rule_count, 1)
        self.assertEqual(diff.added_rule_count, 0)
        self.assertEqual(diff.removed_rule_count, 0)
        self.assertEqual(diff.changes[0].previous_principal_type, "local_operators")
        self.assertEqual(diff.changes[0].desired_principal_type, "local_admins")

    def test_managed_reconcile_binds_apply_to_normalized_reviewed_plan(self) -> None:
        current_record = _active_record_for_policy(LaunchplaneAuthzPolicy(schema_version=2))
        dry_run = AuthzManagedPolicyReconcileEnvelope.model_validate(
            {
                "schema_version": 2,
                "product": "launchplane",
                "managed_set_id": "operator.owner",
                "reason": "Review owner read authority.",
                "desired_policy": {
                    "schema_version": 2,
                    "local_operators": [
                        {
                            "managed_set_id": "operator.owner",
                            "managed_rule_id": "owner.read",
                            "subjects": ["owner", "owner"],
                            "token_labels": ["secondary", "primary"],
                            "actions": ["product_profile.read"],
                        }
                    ],
                },
            }
        )
        _, _, _, reviewed_diff = plan_managed_authz_policy_reconcile(
            record_store=_AuthzPolicyStore((current_record,)),
            request=dry_run,
        )
        apply_request = AuthzManagedPolicyReconcileEnvelope.model_validate(
            {
                **dry_run.model_dump(mode="json"),
                "mode": "apply",
                "reviewed_plan_sha256": reviewed_diff.plan_sha256,
            }
        )

        _, _, _, apply_diff = plan_managed_authz_policy_reconcile(
            record_store=_AuthzPolicyStore((current_record,)),
            request=apply_request,
        )

        self.assertEqual(apply_diff.plan_sha256, reviewed_diff.plan_sha256)
        self.assertEqual(
            apply_request.desired_policy.local_operators[0].token_labels,
            ("primary", "secondary"),
        )

    def test_managed_reconcile_allows_only_preexisting_mutable_privileged_overlap(self) -> None:
        old_job_ref = (
            "cbusillo/launchplane/.github/workflows/reusable-product-driver-prod-promotion.yml"
            "@refs/heads/main"
        )
        new_job_ref = (
            "cbusillo/launchplane/.github/workflows/reusable-product-driver-prod-promotion.yml"
            "@" + "a" * 40
        )
        caller_workflow_ref = (
            "cbusillo/odoo-tenant-cm/.github/workflows/odoo-prod-promotion.yml@refs/heads/main"
        )
        current_rule = GitHubActionsPolicyRule(
            repository="cbusillo/odoo-tenant-cm",
            workflow_refs=(caller_workflow_ref,),
            job_workflow_refs=(old_job_ref,),
            products=("odoo-tenant-cm",),
            contexts=("odoo-tenant-cm",),
            instances=("prod",),
            actions=("odoo_prod_promotion.execute",),
        )
        current_record = _active_record_for_policy(
            LaunchplaneAuthzPolicy(schema_version=2, github_actions=(current_rule,))
        )
        overlap_request = AuthzManagedPolicyReconcileEnvelope.model_validate(
            {
                "schema_version": 2,
                "product": "launchplane",
                "managed_set_id": "operator.odoo",
                "unmanaged_adoption": "adopt_matching",
                "desired_policy": {
                    "schema_version": 2,
                    "github_actions": [
                        {
                            "managed_set_id": "operator.odoo",
                            "managed_rule_id": "cm.prod.promotion",
                            "repository": "cbusillo/odoo-tenant-cm",
                            "repository_id": "3001",
                            "repository_owner_id": "2001",
                            "workflow_refs": [caller_workflow_ref],
                            "job_workflow_refs": [old_job_ref, new_job_ref],
                            "products": ["odoo-tenant-cm"],
                            "contexts": ["odoo-tenant-cm"],
                            "instances": ["prod"],
                            "actions": ["odoo_prod_promotion.execute"],
                        }
                    ],
                },
            }
        )

        _, _, overlapped_policy, overlap_diff = plan_managed_authz_policy_reconcile(
            record_store=_AuthzPolicyStore((current_record,)),
            request=overlap_request,
        )

        self.assertEqual(overlap_diff.adopted_rule_count, 1)
        self.assertEqual(
            overlapped_policy.github_actions[0].job_workflow_refs,
            (new_job_ref, old_job_ref),
        )
        mutable_only_payload = overlap_request.model_dump(mode="json")
        mutable_only_payload["desired_policy"]["github_actions"][0]["job_workflow_refs"] = [
            old_job_ref
        ]
        mutable_only_request = AuthzManagedPolicyReconcileEnvelope.model_validate(
            mutable_only_payload
        )
        with self.assertRaisesRegex(AuthzPolicyRequestError, "at least one reusable workflow"):
            plan_managed_authz_policy_reconcile(
                record_store=_AuthzPolicyStore((current_record,)),
                request=mutable_only_request,
            )
        new_only_payload = overlap_request.model_dump(mode="json")
        new_only_payload["desired_policy"]["github_actions"][0]["job_workflow_refs"] = [new_job_ref]
        new_only_request = AuthzManagedPolicyReconcileEnvelope.model_validate(new_only_payload)
        with self.assertRaisesRegex(AuthzPolicyConflictError, "reviewed overlap plan"):
            plan_managed_authz_policy_reconcile(
                record_store=_AuthzPolicyStore((current_record,)),
                request=new_only_request,
            )
        unsafe_request = AuthzManagedPolicyReconcileEnvelope.model_validate(
            {
                **overlap_request.model_dump(mode="json"),
                "unmanaged_adoption": "reject",
            }
        )
        with self.assertRaisesRegex(AuthzPolicyRequestError, "new refs must use a full commit SHA"):
            plan_managed_authz_policy_reconcile(
                record_store=_AuthzPolicyStore(
                    (_active_record_for_policy(LaunchplaneAuthzPolicy(schema_version=2)),)
                ),
                request=unsafe_request,
            )

    def test_route_request_schema_must_match_active_policy_schema(self) -> None:
        request = AuthzPolicyGitHubActionsGrantEnvelope.model_validate(
            {
                "schema_version": 2,
                "product": "launchplane",
                "mode": "dry_run",
                "grant": {
                    "repository": "cbusillo/launchplane",
                    **_GITHUB_REPOSITORY_IDS,
                    "actions": ["product_profile.read"],
                },
            }
        )

        with self.assertRaisesRegex(
            ValueError,
            "must match the active policy schema_version",
        ):
            execute_authz_policy_route(
                record_store=_AuthzPolicyStore((_active_record(),)),
                request=request,
                identity=_identity(),
                trace_id="trace-schema-mismatch",
                now_timestamp=lambda: "2026-07-18T00:00:00Z",
            )

    def test_schema_v2_instance_scoped_grants_require_instances_for_every_principal(
        self,
    ) -> None:
        grants = (
            (
                AuthzPolicyGitHubActionsGrantEnvelope,
                {
                    "repository": "cbusillo/odoo-tenant",
                    **_GITHUB_REPOSITORY_IDS,
                    "actions": ["deployment.write"],
                },
            ),
            (
                AuthzPolicyGitHubHumanGrantEnvelope,
                {
                    "logins": ["alice"],
                    "roles": ["admin"],
                    "actions": ["deployment.write"],
                },
            ),
            (
                AuthzPolicyTerminalAgentGrantEnvelope,
                {
                    "subjects": ["local-owner-agent"],
                    "token_labels": ["local-owner-read"],
                    "actions": ["deployment.write"],
                },
            ),
            (
                AuthzPolicyLocalOperatorGrantEnvelope,
                {
                    "subjects": ["local-owner-agent"],
                    "token_labels": ["local-owner-write"],
                    "actions": ["deployment.write"],
                },
            ),
            (
                AuthzPolicyLocalAdminGrantEnvelope,
                {
                    "subjects": ["local-owner-admin"],
                    "token_labels": ["local-owner-admin"],
                    "actions": ["deployment.write"],
                },
            ),
        )
        for envelope_type, grant in grants:
            with self.subTest(envelope_type=envelope_type.__name__):
                payload = {
                    "schema_version": 2,
                    "product": "launchplane",
                    "mode": "dry_run",
                    "grant": grant,
                }
                with self.assertRaisesRegex(
                    ValidationError,
                    "instance-scoped authz grants require instances",
                ):
                    envelope_type.model_validate(payload)

                payload["grant"] = {**grant, "instances": ["testing"]}
                envelope = envelope_type.model_validate(payload)
                self.assertEqual(envelope.grant.to_policy_rule().instances, ("testing",))

    def test_schema_v1_preserves_legacy_instance_grant_shape(self) -> None:
        envelope = AuthzPolicyGitHubActionsGrantEnvelope.model_validate(
            {
                "schema_version": 1,
                "product": "launchplane",
                "mode": "dry_run",
                "grant": {
                    "repository": "cbusillo/odoo-tenant",
                    **_GITHUB_REPOSITORY_IDS,
                    "actions": ["deployment.write"],
                },
            }
        )

        self.assertEqual(envelope.grant.instances, ())

    def test_schema_v1_rejects_instance_selectors(self) -> None:
        with self.assertRaisesRegex(
            ValidationError,
            "Schema-v1 authz grants cannot declare instances",
        ):
            AuthzPolicyGitHubActionsGrantEnvelope.model_validate(
                {
                    "schema_version": 1,
                    "product": "launchplane",
                    "mode": "dry_run",
                    "grant": {
                        "repository": "cbusillo/odoo-tenant",
                        **_GITHUB_REPOSITORY_IDS,
                        "instances": ["testing"],
                        "actions": ["deployment.write"],
                    },
                }
            )

    def test_schema_v2_instance_scoped_removal_requires_instances(self) -> None:
        payload = {
            "schema_version": 2,
            "product": "launchplane",
            "mode": "dry_run",
            "removal": {
                "repository": "cbusillo/odoo-tenant",
                "actions": ["deployment.write"],
            },
        }

        with self.assertRaisesRegex(
            ValidationError,
            "instance-scoped authz grants require instances",
        ):
            AuthzPolicyGitHubActionsRemovalEnvelope.model_validate(payload)

        removal = cast(dict[str, object], payload["removal"])
        removal["instances"] = ["testing"]
        envelope = AuthzPolicyGitHubActionsRemovalEnvelope.model_validate(payload)
        self.assertEqual(envelope.removal.instances, ("testing",))

    def test_schema_v2_dual_scope_grants_support_context_or_exact_instance(self) -> None:
        context_envelope = AuthzPolicyGitHubActionsGrantEnvelope.model_validate(
            {
                "schema_version": 2,
                "product": "launchplane",
                "mode": "dry_run",
                "grant": {
                    "repository": "cbusillo/launchplane",
                    **_GITHUB_REPOSITORY_IDS,
                    "actions": ["product_environment.read"],
                },
            }
        )
        instance_envelope = AuthzPolicyGitHubActionsGrantEnvelope.model_validate(
            {
                "schema_version": 2,
                "product": "launchplane",
                "mode": "dry_run",
                "grant": {
                    "repository": "cbusillo/launchplane",
                    **_GITHUB_REPOSITORY_IDS,
                    "instances": ["testing"],
                    "actions": ["product_environment.read"],
                },
            }
        )

        self.assertEqual(context_envelope.grant.instances, ())
        self.assertEqual(instance_envelope.grant.instances, ("testing",))

    def test_schema_v2_rejects_instance_selectors_for_context_only_actions(self) -> None:
        with self.assertRaisesRegex(
            ValidationError,
            "can only declare instances for instance-scoped actions",
        ):
            AuthzPolicyGitHubActionsGrantEnvelope.model_validate(
                {
                    "schema_version": 2,
                    "product": "launchplane",
                    "mode": "dry_run",
                    "grant": {
                        "repository": "cbusillo/launchplane",
                        **_GITHUB_REPOSITORY_IDS,
                        "instances": ["testing"],
                        "actions": ["product_profile.read"],
                    },
                }
            )

    def test_unknown_grant_schema_version_fails_closed(self) -> None:
        with self.assertRaises(ValidationError):
            AuthzPolicyGitHubActionsGrantEnvelope.model_validate(
                {
                    "schema_version": 3,
                    "product": "launchplane",
                    "mode": "dry_run",
                    "grant": {
                        "repository": "cbusillo/launchplane",
                        **_GITHUB_REPOSITORY_IDS,
                        "actions": ["product_profile.read"],
                    },
                }
            )

    def test_grant_normalizes_tuple_values(self) -> None:
        grant = AuthzPolicyGitHubActionsGrant.model_validate(
            {
                "repository": " cbusillo/launchplane ",
                "repository_id": " 1001 ",
                "repository_owner_id": " 2001 ",
                "workflow_refs": [" workflow ", ""],
                "actions": [" product_profile.read "],
            }
        )

        self.assertEqual(grant.repository, "cbusillo/launchplane")
        self.assertEqual(grant.repository_id, "1001")
        self.assertEqual(grant.repository_owner_id, "2001")
        self.assertEqual(grant.workflow_refs, ("workflow",))
        self.assertEqual(grant.actions, ("product_profile.read",))

    def test_human_grant_normalizes_tuple_values(self) -> None:
        grant = AuthzPolicyGitHubHumanGrant.model_validate(
            {
                "logins": [" cbusillo ", ""],
                "roles": ["admin"],
                "actions": [" generic_web_prod_promotion.dispatch "],
            }
        )

        self.assertEqual(grant.logins, ("cbusillo",))
        self.assertEqual(grant.roles, ("admin",))
        self.assertEqual(grant.actions, ("generic_web_prod_promotion.dispatch",))

    def test_plan_and_write_grant_records_changed_policy(self) -> None:
        store = _AuthzPolicyStore((_active_record(),))
        request = _grant_request()

        _current_policy, current_record, diff = plan_github_actions_authz_policy_grant(
            record_store=store,
            grant=request.grant,
        )
        self.assertEqual(current_record.record_id, store.records[0].record_id)
        self.assertEqual(diff["changed"], True)
        self.assertEqual(diff["new_github_actions_rule_count"], 2)

        updated_policy, record, changed, write_diff, audit = (
            write_github_actions_authz_policy_grant(
                record_store=store,
                request=request,
                identity=_identity(),
                trace_id="trace-1",
                now_timestamp=lambda: "2026-05-07T16:00:00Z",
            )
        )

        self.assertTrue(changed)
        self.assertEqual(write_diff, diff)
        self.assertEqual(store.written_records, [record])
        self.assertEqual(len(updated_policy.github_actions), 2)
        self.assertEqual(audit["reason"], request.reason)
        self.assertIn("workflow_refs", json.dumps(audit, sort_keys=True))
        self.assertEqual(write_diff["new_github_humans_rule_count"], 0)

        result, driver_result = build_authz_policy_grant_service_result(
            authz_policy_record=record,
            changed=changed,
            mode=request.mode,
            diff=write_diff,
            audit=audit,
        )
        self.assertEqual(result["authz_policy_record_id"], record.record_id)
        authz_policy_summary = cast(dict[str, object], driver_result["authz_policy"])
        self.assertEqual(authz_policy_summary["github_actions_immutable_repository_rule_count"], 1)
        self.assertEqual(authz_policy_summary["github_actions_legacy_name_only_rule_count"], 1)
        driver_audit = cast(dict[str, object], driver_result["audit"])
        self.assertNotIn("requested_grant", driver_audit)
        requested_grant_summary = cast(dict[str, object], driver_audit["requested_grant_summary"])
        self.assertEqual(requested_grant_summary["workflow_ref_count"], 1)

    def test_grant_upgrades_matching_legacy_rule_in_place(self) -> None:
        request = _grant_request()
        desired_rule = request.grant.to_policy_rule()
        legacy_rule = desired_rule.model_copy(
            update={"repository_id": "", "repository_owner_id": ""}
        )
        legacy_policy = LaunchplaneAuthzPolicy(github_actions=(legacy_rule,))
        legacy_policy_sha256 = authz_policy_sha256(legacy_policy)
        legacy_record = LaunchplaneAuthzPolicyRecord(
            record_id=build_authz_policy_record_id(
                revision=1,
                policy_sha256=legacy_policy_sha256,
            ),
            status="active",
            source="test:legacy",
            updated_at="2026-05-07T15:00:00Z",
            policy_sha256=legacy_policy_sha256,
            policy=legacy_policy,
        )
        store = _AuthzPolicyStore((legacy_record,))

        _current_policy, _current_record, diff = plan_github_actions_authz_policy_grant(
            record_store=store,
            grant=request.grant,
        )
        self.assertEqual(diff["changed"], True)
        self.assertEqual(diff["upgraded_legacy_github_actions_rule_count"], 1)
        self.assertEqual(diff["new_github_actions_rule_count"], 1)

        updated_policy, _record, changed, _write_diff, _audit = (
            write_github_actions_authz_policy_grant(
                record_store=store,
                request=request,
                identity=_identity(),
                trace_id="trace-upgrade",
                now_timestamp=lambda: "2026-05-07T16:00:00Z",
            )
        )

        self.assertTrue(changed)
        self.assertEqual(updated_policy.github_actions, (desired_rule,))

    def test_write_rejects_stale_active_policy(self) -> None:
        store = _AuthzPolicyStore((_active_record(),), compare_write_allowed=False)

        with self.assertRaisesRegex(AuthzPolicyConflictError, "authz policy changed"):
            write_github_actions_authz_policy_grant(
                record_store=store,
                request=_grant_request(),
                identity=_identity(),
                trace_id="trace-stale",
                now_timestamp=lambda: "2026-05-07T16:00:00Z",
            )

        self.assertEqual(store.written_records, [])

    def test_plan_and_write_human_grant_records_changed_policy(self) -> None:
        store = _AuthzPolicyStore((_active_record(),))
        request = _human_grant_request()

        _current_policy, current_record, diff = plan_github_human_authz_policy_grant(
            record_store=store,
            grant=request.grant,
        )
        self.assertEqual(current_record.record_id, store.records[0].record_id)
        self.assertEqual(diff["changed"], True)
        self.assertEqual(diff["new_github_actions_rule_count"], 1)
        self.assertEqual(diff["new_github_humans_rule_count"], 1)

        updated_policy, record, changed, write_diff, audit = write_github_human_authz_policy_grant(
            record_store=store,
            request=request,
            identity=_identity(),
            trace_id="trace-human",
            now_timestamp=lambda: "2026-05-07T16:00:00Z",
        )

        self.assertTrue(changed)
        self.assertEqual(write_diff, diff)
        self.assertEqual(store.written_records, [record])
        self.assertEqual(len(updated_policy.github_humans), 1)
        self.assertEqual(audit["reason"], request.reason)
        self.assertIn("logins", json.dumps(audit, sort_keys=True))

        result, driver_result = build_authz_policy_grant_service_result(
            authz_policy_record=record,
            changed=changed,
            mode=request.mode,
            diff=write_diff,
            audit=audit,
        )
        self.assertEqual(result["authz_policy_record_id"], record.record_id)
        driver_audit = cast(dict[str, object], driver_result["audit"])
        requested_grant_summary = cast(dict[str, object], driver_audit["requested_grant_summary"])
        self.assertEqual(requested_grant_summary["principal_type"], "github_human")
        self.assertEqual(requested_grant_summary["login_count"], 1)
        self.assertNotIn("logins", requested_grant_summary)

    def test_repeated_grant_does_not_write_new_record(self) -> None:
        store = _AuthzPolicyStore((_active_record(),))
        request = _grant_request()
        _updated_policy, record, _changed, _diff, _audit = write_github_actions_authz_policy_grant(
            record_store=store,
            request=request,
            identity=_identity(),
            trace_id="trace-1",
            now_timestamp=lambda: "2026-05-07T16:00:00Z",
        )

        _same_policy, same_record, same_changed, _same_diff, same_audit = (
            write_github_actions_authz_policy_grant(
                record_store=store,
                request=request,
                identity=_identity(),
                trace_id="trace-2",
                now_timestamp=lambda: "2026-05-07T16:01:00Z",
            )
        )

        self.assertFalse(same_changed)
        self.assertEqual(same_record.record_id, record.record_id)
        self.assertEqual(len(store.written_records), 1)
        self.assertEqual(same_audit["changed"], False)

    def test_plan_and_write_removal_records_changed_policy(self) -> None:
        store = _AuthzPolicyStore((_active_record(),))
        request = _removal_request()

        _current_policy, current_record, diff = plan_github_actions_authz_policy_removal(
            record_store=store,
            removal=request.removal,
        )
        self.assertEqual(current_record.record_id, store.records[0].record_id)
        self.assertEqual(diff["changed"], True)
        self.assertEqual(diff["matched_rule_count"], 1)
        self.assertEqual(diff["new_github_actions_rule_count"], 0)

        updated_policy, record, changed, write_diff, audit = (
            write_github_actions_authz_policy_removal(
                record_store=store,
                request=request,
                identity=_identity(),
                trace_id="trace-remove",
                now_timestamp=lambda: "2026-05-07T16:00:00Z",
            )
        )

        self.assertTrue(changed)
        self.assertEqual(write_diff, diff)
        self.assertEqual(store.written_records, [record])
        self.assertEqual(updated_policy.github_actions, ())
        self.assertEqual(audit["reason"], request.reason)
        self.assertIn("requested_removal", audit)

        result, driver_result = build_authz_policy_github_actions_removal_service_result(
            authz_policy_record=record,
            changed=changed,
            mode=request.mode,
            diff=write_diff,
            audit=audit,
        )
        self.assertEqual(result["authz_policy_record_id"], record.record_id)
        driver_audit = cast(dict[str, object], driver_result["audit"])
        self.assertNotIn("requested_removal", driver_audit)
        requested_removal_summary = cast(
            dict[str, object], driver_audit["requested_removal_summary"]
        )
        self.assertEqual(
            requested_removal_summary["actions"], ["launchplane_service_deploy.execute"]
        )

    def test_repeated_removal_does_not_write_new_record(self) -> None:
        store = _AuthzPolicyStore((_active_record(),))
        request = _removal_request()
        _updated_policy, record, _changed, _diff, _audit = (
            write_github_actions_authz_policy_removal(
                record_store=store,
                request=request,
                identity=_identity(),
                trace_id="trace-remove",
                now_timestamp=lambda: "2026-05-07T16:00:00Z",
            )
        )

        _same_policy, same_record, same_changed, same_diff, same_audit = (
            write_github_actions_authz_policy_removal(
                record_store=store,
                request=request,
                identity=_identity(),
                trace_id="trace-remove-repeat",
                now_timestamp=lambda: "2026-05-07T16:01:00Z",
            )
        )

        self.assertFalse(same_changed)
        self.assertEqual(same_record.record_id, record.record_id)
        self.assertEqual(same_diff["matched_rule_count"], 0)
        self.assertEqual(len(store.written_records), 1)
        self.assertEqual(same_audit["changed"], False)

    def test_removal_rejects_last_policy_administrator(self) -> None:
        policy = LaunchplaneAuthzPolicy.model_validate(
            {
                "github_actions": [
                    {
                        "repository": "cbusillo/launchplane",
                        "products": ["launchplane"],
                        "contexts": ["launchplane"],
                        "actions": ["authz_policy_grant.write"],
                    }
                ]
            }
        )
        record = LaunchplaneAuthzPolicyRecord(
            record_id="authz-policy-admin",
            source="test:policy-admin",
            updated_at="2026-07-18T00:00:00Z",
            policy=policy,
        )
        request = AuthzPolicyGitHubActionsRemovalEnvelope.model_validate(
            {
                "product": "launchplane",
                "mode": "dry_run",
                "removal": {
                    "repository": "cbusillo/launchplane",
                    "products": ["launchplane"],
                    "contexts": ["launchplane"],
                    "actions": ["authz_policy_grant.write"],
                },
            }
        )

        with self.assertRaisesRegex(AuthzPolicyConflictError, "retain at least one principal"):
            plan_github_actions_authz_policy_removal(
                record_store=_AuthzPolicyStore((record,)),
                removal=request.removal,
            )

    def test_removal_ignores_unusable_policy_administrators(self) -> None:
        for unusable_rules in (
            {
                "terminal_agents": [
                    {
                        "subjects": ["read-only-agent"],
                        "actions": ["authz_policy_grant.write"],
                    }
                ]
            },
            {
                "github_humans": [
                    {
                        "logins": ["read-only-human"],
                        "roles": ["read_only"],
                        "actions": ["authz_policy_grant.write"],
                    }
                ]
            },
        ):
            with self.subTest(unusable_rules=unusable_rules):
                policy = LaunchplaneAuthzPolicy.model_validate(
                    {
                        "github_actions": [
                            {
                                "repository": "cbusillo/launchplane",
                                "products": ["launchplane"],
                                "contexts": ["launchplane"],
                                "actions": ["authz_policy_grant.write"],
                            }
                        ],
                        **unusable_rules,
                    }
                )
                record = LaunchplaneAuthzPolicyRecord(
                    record_id="authz-policy-admin",
                    source="test:policy-admin",
                    updated_at="2026-07-18T00:00:00Z",
                    policy=policy,
                )
                request = AuthzPolicyGitHubActionsRemovalEnvelope.model_validate(
                    {
                        "product": "launchplane",
                        "mode": "dry_run",
                        "removal": {
                            "repository": "cbusillo/launchplane",
                            "products": ["launchplane"],
                            "contexts": ["launchplane"],
                            "actions": ["authz_policy_grant.write"],
                        },
                    }
                )

                with self.assertRaisesRegex(
                    AuthzPolicyConflictError, "retain at least one principal"
                ):
                    plan_github_actions_authz_policy_removal(
                        record_store=_AuthzPolicyStore((record,)),
                        removal=request.removal,
                    )

    def test_removal_requires_exact_rule_match(self) -> None:
        store = _AuthzPolicyStore((_active_record(),))
        request = AuthzPolicyGitHubActionsRemovalEnvelope.model_validate(
            {
                "product": "launchplane",
                "mode": "dry_run",
                "removal": {
                    "repository": "cbusillo/launchplane",
                    "workflow_refs": [
                        "cbusillo/launchplane/.github/workflows/deploy-launchplane.yml@refs/heads/main"
                    ],
                    "products": ["launchplane"],
                    "contexts": ["launchplane"],
                    "actions": ["launchplane_service_deploy.execute"],
                },
            }
        )

        _policy, _record, diff = plan_github_actions_authz_policy_removal(
            record_store=store,
            removal=request.removal,
        )

        self.assertEqual(diff["changed"], False)
        self.assertEqual(diff["matched_rule_count"], 0)
        self.assertEqual(diff["new_github_actions_rule_count"], 1)

    def test_removal_deletes_duplicate_exact_rules(self) -> None:
        record = _active_record()
        duplicate_policy = record.policy.model_copy(
            update={"github_actions": record.policy.github_actions * 2}
        )
        duplicate_record = LaunchplaneAuthzPolicyRecord(
            record_id="launchplane-authz-policy-duplicate-test",
            status="active",
            source="test:duplicates",
            updated_at="2026-05-07T15:30:00Z",
            policy_sha256=authz_policy_sha256(duplicate_policy),
            policy=duplicate_policy,
        )
        store = _AuthzPolicyStore((duplicate_record,))
        request = _removal_request()

        updated_policy, _record, changed, diff, _audit = write_github_actions_authz_policy_removal(
            record_store=store,
            request=request,
            identity=_identity(),
            trace_id="trace-remove-duplicates",
            now_timestamp=lambda: "2026-05-07T16:00:00Z",
        )

        self.assertTrue(changed)
        self.assertEqual(diff["matched_rule_count"], 2)
        self.assertEqual(diff["removed_rule_count"], 2)
        self.assertEqual(updated_policy.github_actions, ())

    def test_response_audit_replaces_requested_grant_with_summary(self) -> None:
        audit: dict[str, object] = {
            "requested_grant": _grant_request().grant.to_policy_rule().model_dump(mode="json"),
            "mode": "dry_run",
            "operator": {
                "type": "github_actions",
                "repository": "cbusillo/launchplane",
                "workflow_ref": "cbusillo/launchplane/.github/workflows/deploy-launchplane.yml@refs/heads/main",
                "sha": "abc123",
            },
        }

        response_audit = authz_policy_grant_response_audit_payload(audit)

        self.assertNotIn("requested_grant", response_audit)
        self.assertEqual(response_audit["operator"], {"type": "github_actions"})
        self.assertNotIn(
            "cbusillo/launchplane/.github/workflows/deploy-launchplane.yml@refs/heads/main",
            json.dumps(response_audit, sort_keys=True),
        )
        requested_grant_summary = cast(dict[str, object], response_audit["requested_grant_summary"])
        self.assertEqual(requested_grant_summary["actions"], ["product_profile.read"])

    def test_response_audit_summarizes_human_grant_without_logins(self) -> None:
        audit: dict[str, object] = {
            "requested_grant": _human_grant_request()
            .grant.to_policy_rule()
            .model_dump(mode="json"),
            "mode": "dry_run",
        }

        response_audit = authz_policy_grant_response_audit_payload(audit)

        self.assertNotIn("requested_grant", response_audit)
        self.assertNotIn("cbusillo", json.dumps(response_audit, sort_keys=True))
        requested_grant_summary = cast(dict[str, object], response_audit["requested_grant_summary"])
        self.assertEqual(requested_grant_summary["principal_type"], "github_human")
        self.assertEqual(requested_grant_summary["login_count"], 1)


if __name__ == "__main__":
    unittest.main()
