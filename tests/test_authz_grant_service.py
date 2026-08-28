from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import unittest
from typing import cast

from pydantic import ValidationError

import control_plane.authz_grant_service as control_plane_authz_grant_service
from control_plane.authz_grant_service import (
    AuthzManagedPolicyReconcileEnvelope,
    AuthzPolicyConflictError,
    AuthzPolicyRequestError,
    AuthzPolicySafetyError,
    build_authz_candidate_policy_structural_diff,
    execute_managed_authz_policy_reconcile,
    plan_managed_authz_policy_reconcile,
    preview_authz_candidate_policy,
    summarize_active_authz_policy_record,
    summarize_active_authz_policy_health_record,
)
from control_plane.contracts.authz_access_read import AuthzPolicyCandidatePreviewRequest
from control_plane.contracts.authz_policy_record import (
    LaunchplaneAuthzPolicyRecord,
    authz_policy_sha256,
    build_authz_policy_record_id,
)
from control_plane.service_auth import (
    GitHubActionsIdentity,
    GitHubActionsPolicyRule,
    GitHubHumanPolicyRule,
    LaunchplaneAuthzPolicy,
    LocalAdminIdentity,
    LocalAdminPolicyRule,
    LocalOperatorPolicyRule,
    TerminalAgentPolicyRule,
)


class _AuthzPolicyStore:
    def __init__(self, records: tuple[LaunchplaneAuthzPolicyRecord, ...]) -> None:
        self.records = records

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


def _managed_service_deploy_request() -> AuthzManagedPolicyReconcileEnvelope:
    return AuthzManagedPolicyReconcileEnvelope.model_validate(
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


def _authz_rollout_fixture(filename: str) -> LaunchplaneAuthzPolicy:
    fixture_path = Path(__file__).parent / "fixtures" / "authz" / filename
    return LaunchplaneAuthzPolicy.model_validate(
        json.loads(fixture_path.read_text(encoding="utf-8"))
    )


def _workflow_admin_identity() -> GitHubActionsIdentity:
    return replace(
        _identity(),
        job_workflow_ref=(
            "cbusillo/launchplane/.github/workflows/reusable-manage-authorization.yml@" + "a" * 40
        ),
    )


def _workflow_admin_rule(
    identity: GitHubActionsIdentity,
    *,
    managed_rule_id: str = "authz.admin",
) -> GitHubActionsPolicyRule:
    return GitHubActionsPolicyRule(
        managed_set_id="operator.launchplane",
        managed_rule_id=managed_rule_id,
        repository=identity.repository,
        repository_id=identity.repository_id,
        repository_owner_id=identity.repository_owner_id,
        workflow_refs=(identity.workflow_ref,),
        job_workflow_refs=(identity.job_workflow_ref,),
        products=("launchplane",),
        contexts=("launchplane",),
        actions=("authz_policy_grant.write",),
    )


class AuthzManagedPolicyServiceTests(unittest.TestCase):
    def test_continuity_requires_exact_immutable_github_human_administrators(self) -> None:
        strict_admin = GitHubHumanPolicyRule(
            github_ids=(101, 102),
            roles=("admin",),
            products=("launchplane",),
            contexts=("launchplane",),
            actions=("authz_policy_grant.write",),
        )
        action_empty = strict_admin.model_copy(update={"github_ids": (103,), "actions": ()})
        wildcard_action = strict_admin.model_copy(
            update={"github_ids": (104,), "actions": ("authz_policy_*",)}
        )
        wildcard_product = strict_admin.model_copy(
            update={"github_ids": (105,), "products": ("*",)}
        )
        mutable_organization = strict_admin.model_copy(
            update={"github_ids": (106,), "organizations": ("mutable-org",)}
        )
        policy = LaunchplaneAuthzPolicy(
            schema_version=2,
            github_humans=(
                strict_admin,
                action_empty,
                wildcard_action,
                wildcard_product,
                mutable_organization,
            ),
            local_admins=(
                LocalAdminPolicyRule(
                    subjects=("recovery-admin",),
                    token_labels=("recovery-admin",),
                    products=("launchplane",),
                    contexts=("launchplane",),
                    actions=("authz_policy_grant.write",),
                ),
            ),
        )

        self.assertTrue(
            control_plane_authz_grant_service._authz_policy_allows_immutable_github_id_administration(
                policy=policy,
                github_id=101,
            )
        )
        for github_id in (103, 104, 105, 106):
            self.assertFalse(
                control_plane_authz_grant_service._authz_policy_allows_immutable_github_id_administration(
                    policy=policy,
                    github_id=github_id,
                )
            )
        self.assertTrue(
            control_plane_authz_grant_service._authz_policy_retains_independent_github_id_administration(
                policy=policy,
                applying_github_id=101,
            )
        )
        self.assertFalse(
            control_plane_authz_grant_service._authz_policy_retains_independent_github_id_administration(
                policy=policy.model_copy(
                    update={
                        "github_humans": (strict_admin.model_copy(update={"github_ids": (101,)}),)
                    }
                ),
                applying_github_id=101,
            )
        )

    def test_health_monitoring_managed_rule_requires_pinned_reusable_workflow(
        self,
    ) -> None:
        current_record = _active_record_for_policy(LaunchplaneAuthzPolicy(schema_version=2))
        desired_rule = {
            "managed_set_id": "operator.odoo.health-monitoring",
            "managed_rule_id": "cm.testing.health-monitoring",
            "repository": "every/verireel",
            "repository_id": "1001",
            "repository_owner_id": "2001",
            "workflow_refs": [
                "every/verireel/.github/workflows/product-health-monitoring.yml@refs/heads/main"
            ],
            "event_names": ["workflow_dispatch"],
            "products": ["odoo-product"],
            "contexts": ["cm"],
            "instances": ["testing"],
            "actions": [
                "product_profile.health_monitoring.plan",
                "product_profile.health_monitoring.apply",
            ],
        }
        request_payload = {
            "schema_version": 2,
            "product": "launchplane",
            "managed_set_id": "operator.odoo.health-monitoring",
            "desired_policy": {
                "schema_version": 2,
                "github_actions": [desired_rule],
            },
        }
        request = AuthzManagedPolicyReconcileEnvelope.model_validate(request_payload)

        with self.assertRaisesRegex(AuthzPolicyRequestError, "reviewed reusable workflow"):
            plan_managed_authz_policy_reconcile(
                record_store=_AuthzPolicyStore((current_record,)),
                request=request,
            )

        desired_rule["job_workflow_refs"] = [
            "cbusillo/launchplane/.github/workflows/"
            "reusable-product-health-monitoring.yml@" + "a" * 40
        ]
        pinned_request = AuthzManagedPolicyReconcileEnvelope.model_validate(request_payload)

        _, _, updated_policy, _ = plan_managed_authz_policy_reconcile(
            record_store=_AuthzPolicyStore((current_record,)),
            request=pinned_request,
        )

        self.assertEqual(
            updated_policy.github_actions[0].job_workflow_refs,
            (
                "cbusillo/launchplane/.github/workflows/"
                "reusable-product-health-monitoring.yml@" + "a" * 40,
            ),
        )

    def test_prelaunch_rebuild_managed_rule_requires_pinned_reusable_workflow(
        self,
    ) -> None:
        current_record = _active_record_for_policy(LaunchplaneAuthzPolicy(schema_version=2))
        desired_rule = {
            "managed_set_id": "operator.odoo.prelaunch-rebuild",
            "managed_rule_id": "cm.prod.prelaunch-rebuild",
            "repository": "every/verireel",
            "repository_id": "1001",
            "repository_owner_id": "2001",
            "workflow_refs": [
                "every/verireel/.github/workflows/"
                "product-prelaunch-rebuild-policy.yml@refs/heads/main"
            ],
            "event_names": ["workflow_dispatch"],
            "products": ["odoo-product"],
            "contexts": ["cm"],
            "instances": ["prod"],
            "actions": [
                "product_profile.prelaunch_rebuild.plan",
                "product_profile.prelaunch_rebuild.apply",
            ],
        }
        request_payload = {
            "schema_version": 2,
            "product": "launchplane",
            "managed_set_id": "operator.odoo.prelaunch-rebuild",
            "desired_policy": {
                "schema_version": 2,
                "github_actions": [desired_rule],
            },
        }
        request = AuthzManagedPolicyReconcileEnvelope.model_validate(request_payload)

        with self.assertRaisesRegex(AuthzPolicyRequestError, "reviewed reusable workflow"):
            plan_managed_authz_policy_reconcile(
                record_store=_AuthzPolicyStore((current_record,)),
                request=request,
            )

        desired_rule["job_workflow_refs"] = [
            "cbusillo/launchplane/.github/workflows/"
            "reusable-product-prelaunch-rebuild-policy.yml@" + "a" * 40
        ]
        pinned_request = AuthzManagedPolicyReconcileEnvelope.model_validate(request_payload)

        _, _, updated_policy, _ = plan_managed_authz_policy_reconcile(
            record_store=_AuthzPolicyStore((current_record,)),
            request=pinned_request,
        )

        self.assertEqual(
            updated_policy.github_actions[0].job_workflow_refs,
            (
                "cbusillo/launchplane/.github/workflows/"
                "reusable-product-prelaunch-rebuild-policy.yml@" + "a" * 40,
            ),
        )

    def test_instance_ingress_managed_rule_requires_pinned_reusable_workflow(self) -> None:
        current_record = _active_record_for_policy(LaunchplaneAuthzPolicy(schema_version=2))
        desired_rule = {
            "managed_set_id": "operator.odoo.testing-ingress-route",
            "managed_rule_id": "opw.testing.ingress-route",
            "repository": "cbusillo/launchplane",
            "repository_id": "1001",
            "repository_owner_id": "2001",
            "workflow_refs": [
                "cbusillo/launchplane/.github/workflows/ingress-route-dry-run.yml@refs/heads/main"
            ],
            "event_names": ["workflow_dispatch"],
            "products": ["odoo-tenant-opw"],
            "contexts": ["opw"],
            "instances": ["testing"],
            "actions": ["ingress_route.plan"],
        }
        request_payload = {
            "schema_version": 2,
            "product": "launchplane",
            "managed_set_id": "operator.odoo.testing-ingress-route",
            "desired_policy": {
                "schema_version": 2,
                "github_actions": [desired_rule],
            },
        }

        with self.assertRaisesRegex(AuthzPolicyRequestError, "reviewed reusable workflow"):
            plan_managed_authz_policy_reconcile(
                record_store=_AuthzPolicyStore((current_record,)),
                request=AuthzManagedPolicyReconcileEnvelope.model_validate(request_payload),
            )

        desired_rule["job_workflow_refs"] = [
            "cbusillo/launchplane/.github/workflows/reusable-ingress-route-dry-run.yml@" + "a" * 40
        ]
        _, _, updated_policy, _ = plan_managed_authz_policy_reconcile(
            record_store=_AuthzPolicyStore((current_record,)),
            request=AuthzManagedPolicyReconcileEnvelope.model_validate(request_payload),
        )

        self.assertEqual(updated_policy.github_actions[0].instances, ("testing",))

    def test_context_ingress_managed_rule_preserves_legacy_unpinned_scope(self) -> None:
        current_record = _active_record_for_policy(LaunchplaneAuthzPolicy(schema_version=2))
        request = AuthzManagedPolicyReconcileEnvelope.model_validate(
            {
                "schema_version": 2,
                "product": "launchplane",
                "managed_set_id": "operator.legacy-ingress-route",
                "desired_policy": {
                    "schema_version": 2,
                    "github_actions": [
                        {
                            "managed_set_id": "operator.legacy-ingress-route",
                            "managed_rule_id": "legacy.context.plan",
                            "repository": "cbusillo/launchplane",
                            "repository_id": "1001",
                            "repository_owner_id": "2001",
                            "workflow_refs": [
                                "cbusillo/launchplane/.github/workflows/"
                                "ingress-route-dry-run.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["legacy-product"],
                            "contexts": ["legacy"],
                            "actions": ["ingress_route.plan"],
                        }
                    ],
                },
            }
        )

        _, _, updated_policy, _ = plan_managed_authz_policy_reconcile(
            record_store=_AuthzPolicyStore((current_record,)),
            request=request,
        )

        self.assertEqual(updated_policy.github_actions[0].instances, ())

    def test_managed_route_rejects_policy_change_after_authorization(self) -> None:
        active_record = _active_record()
        request = _managed_service_deploy_request()

        with self.assertRaisesRegex(AuthzPolicyConflictError, "after the caller was authorized"):
            execute_managed_authz_policy_reconcile(
                record_store=_AuthzPolicyStore((active_record,)),
                request=request,
                identity=_identity(),
                trace_id="trace-managed-stale-authorization",
                now_timestamp=lambda: "2026-07-18T00:00:00Z",
                authorized_policy_sha256="different-policy",
            )

    def test_managed_reconcile_adopts_v1_rule_and_migrates_schema(self) -> None:
        current_record = _active_record()
        request = _managed_service_deploy_request()

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
                "reason": "Verify the last policy administrator cannot be removed.",
                "desired_policy": {"schema_version": 2},
            }
        )

        _, _, _, diff = plan_managed_authz_policy_reconcile(
            record_store=_AuthzPolicyStore((_active_record_for_policy(current_policy),)),
            request=request,
        )

        self.assertEqual(diff.policy_safety_blocker_count, 1)
        self.assertEqual(
            diff.policy_safety_blockers[0].code,
            "authz_policy_admin_unreachable",
        )
        self.assertIn(
            "retain at least one reachable principal",
            diff.policy_safety_blockers[0].message,
        )
        apply_request = AuthzManagedPolicyReconcileEnvelope.model_validate(
            {
                **request.model_dump(mode="json"),
                "mode": "apply",
                "reviewed_plan_sha256": diff.plan_sha256,
            }
        )
        with self.assertRaises(AuthzPolicySafetyError) as raised:
            execute_managed_authz_policy_reconcile(
                record_store=_AuthzPolicyStore((_active_record_for_policy(current_policy),)),
                request=apply_request,
                identity=_workflow_admin_identity(),
                trace_id="trace-last-admin-apply",
                now_timestamp=lambda: "2026-08-17T00:00:00Z",
                authorized_policy_sha256=authz_policy_sha256(current_policy),
            )
        self.assertEqual(raised.exception.code, "authz_policy_admin_unreachable")

    def test_managed_reconcile_counts_terminal_agent_policy_administrator(self) -> None:
        terminal_admin = TerminalAgentPolicyRule(
            managed_set_id="operator.terminal-admin",
            managed_rule_id="terminal.admin",
            subjects=("terminal-agent",),
            token_labels=("terminal-admin",),
            products=("launchplane",),
            contexts=("launchplane",),
            actions=("authz_policy_grant.write",),
        )
        current_record = _active_record_for_policy(
            LaunchplaneAuthzPolicy(schema_version=2, terminal_agents=(terminal_admin,))
        )
        request = AuthzManagedPolicyReconcileEnvelope(
            schema_version=2,
            product="launchplane",
            managed_set_id="operator.terminal-admin",
            desired_policy=LaunchplaneAuthzPolicy(schema_version=2),
        )

        _, _, _, diff = plan_managed_authz_policy_reconcile(
            record_store=_AuthzPolicyStore((current_record,)),
            request=request,
        )

        self.assertEqual(diff.policy_safety_blocker_count, 1)
        self.assertEqual(
            diff.policy_safety_blockers[0].code,
            "authz_policy_admin_unreachable",
        )

    def test_managed_apply_requires_independent_policy_administrator(self) -> None:
        identity = _workflow_admin_identity()
        applying_admin = _workflow_admin_rule(identity)
        retired_rule = applying_admin.model_copy(
            update={
                "managed_rule_id": "service.read",
                "actions": ("product_environment.read",),
            }
        )

        def apply_request(
            policy_record: LaunchplaneAuthzPolicyRecord,
        ) -> AuthzManagedPolicyReconcileEnvelope:
            dry_run = AuthzManagedPolicyReconcileEnvelope(
                schema_version=2,
                product="launchplane",
                managed_set_id="operator.launchplane",
                reason="Retire the obsolete managed read rule.",
                desired_policy=LaunchplaneAuthzPolicy(
                    schema_version=2,
                    github_actions=(applying_admin,),
                ),
            )
            _, _, _, diff = plan_managed_authz_policy_reconcile(
                record_store=_AuthzPolicyStore((policy_record,)),
                request=dry_run,
            )
            return AuthzManagedPolicyReconcileEnvelope.model_validate(
                {
                    **dry_run.model_dump(mode="json"),
                    "mode": "apply",
                    "reviewed_plan_sha256": diff.plan_sha256,
                }
            )

        current_policy = LaunchplaneAuthzPolicy(
            schema_version=2,
            github_actions=(applying_admin, retired_rule),
        )
        current_record = _active_record_for_policy(current_policy)
        dry_run_result = execute_managed_authz_policy_reconcile(
            record_store=_AuthzPolicyStore((current_record,)),
            request=AuthzManagedPolicyReconcileEnvelope(
                schema_version=2,
                product="launchplane",
                managed_set_id="operator.launchplane",
                reason="Retire the obsolete managed read rule.",
                desired_policy=LaunchplaneAuthzPolicy(
                    schema_version=2,
                    github_actions=(applying_admin,),
                ),
            ),
            identity=identity,
            trace_id="trace-independent-admin-dry-run",
            now_timestamp=lambda: "2026-08-17T00:00:00Z",
            authorized_policy_sha256=current_record.policy_sha256,
        )
        dry_run_diff = cast(dict[str, object], dry_run_result.driver_result["diff"])
        self.assertEqual(dry_run_diff["policy_safety_blocker_count"], 1)
        self.assertEqual(
            cast(list[dict[str, object]], dry_run_diff["policy_safety_blockers"])[0]["code"],
            "authz_policy_independent_admin_unreachable",
        )
        with self.assertRaises(AuthzPolicySafetyError) as raised:
            execute_managed_authz_policy_reconcile(
                record_store=_AuthzPolicyStore((current_record,)),
                request=apply_request(current_record),
                identity=identity,
                trace_id="trace-independent-admin-required",
                now_timestamp=lambda: "2026-08-17T00:00:00Z",
                authorized_policy_sha256=current_record.policy_sha256,
            )
        self.assertEqual(
            raised.exception.code,
            "authz_policy_independent_admin_unreachable",
        )

        local_admin = LocalAdminPolicyRule(
            subjects=("recovery-admin",),
            token_labels=("recovery-admin",),
            products=("launchplane",),
            contexts=("launchplane",),
            actions=("authz_policy_grant.write",),
        )
        local_admin_record = _active_record_for_policy(
            current_policy.model_copy(update={"local_admins": (local_admin,)})
        )
        local_admin_result = execute_managed_authz_policy_reconcile(
            record_store=_AuthzPolicyStore((local_admin_record,)),
            request=AuthzManagedPolicyReconcileEnvelope(
                schema_version=2,
                product="launchplane",
                managed_set_id="operator.launchplane",
                reason="Retire the obsolete managed read rule.",
                desired_policy=LaunchplaneAuthzPolicy(
                    schema_version=2,
                    github_actions=(applying_admin,),
                ),
            ),
            identity=identity,
            trace_id="trace-local-admin-not-independent",
            now_timestamp=lambda: "2026-08-17T00:00:00Z",
            authorized_policy_sha256=local_admin_record.policy_sha256,
        )
        local_admin_diff = cast(dict[str, object], local_admin_result.driver_result["diff"])
        self.assertEqual(
            cast(list[dict[str, object]], local_admin_diff["policy_safety_blockers"])[0]["code"],
            "authz_policy_independent_admin_unreachable",
        )

        independent_admin = GitHubHumanPolicyRule(
            github_ids=(2002,),
            roles=("admin",),
            products=("launchplane",),
            contexts=("launchplane",),
            actions=("authz_policy_grant.write",),
        )
        recoverable_record = _active_record_for_policy(
            current_policy.model_copy(update={"github_humans": (independent_admin,)})
        )
        result = execute_managed_authz_policy_reconcile(
            record_store=_AuthzPolicyStore((recoverable_record,)),
            request=apply_request(recoverable_record),
            identity=identity,
            trace_id="trace-independent-admin-retained",
            now_timestamp=lambda: "2026-08-17T00:00:00Z",
            authorized_policy_sha256=recoverable_record.policy_sha256,
        )

        self.assertTrue(result.changed)

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

    def test_managed_reconcile_requires_scoped_github_human_principals(self) -> None:
        base_rule = {
            "managed_set_id": "operator.humans",
            "managed_rule_id": "human.read",
            "products": ["launchplane"],
            "contexts": ["launchplane"],
            "actions": ["product_profile.read"],
        }
        invalid_rules = (
            ({**base_rule, "roles": ["read_only"]}, "at least one principal selector"),
            ({**base_rule, "github_ids": [1001]}, "at least one explicit role"),
            (
                {**base_rule, "logins": ["*"], "roles": ["read_only"]},
                "require exact login, organization, and team selectors",
            ),
            (
                {**base_rule, "logins": ["owner"], "roles": ["admin"]},
                "require immutable github_ids",
            ),
            (
                {
                    **base_rule,
                    "logins": ["owner"],
                    "roles": ["read_only"],
                    "actions": ["authz_policy_grant.write"],
                },
                "require immutable github_ids",
            ),
        )

        for invalid_rule, message in invalid_rules:
            with self.subTest(message=message), self.assertRaisesRegex(ValidationError, message):
                AuthzManagedPolicyReconcileEnvelope.model_validate(
                    {
                        "schema_version": 2,
                        "product": "launchplane",
                        "managed_set_id": "operator.humans",
                        "desired_policy": {
                            "schema_version": 2,
                            "github_humans": [invalid_rule],
                        },
                    }
                )

        request = AuthzManagedPolicyReconcileEnvelope.model_validate(
            {
                "schema_version": 2,
                "product": "launchplane",
                "managed_set_id": "operator.humans",
                "desired_policy": {
                    "schema_version": 2,
                    "github_humans": [
                        {
                            **base_rule,
                            "github_ids": [1001],
                            "roles": ["admin"],
                        }
                    ],
                },
            }
        )

        self.assertEqual(request.desired_policy.github_humans[0].github_ids, (1001,))

    def test_owner_acceptance_managed_set_enforces_minimum_human_boundary(self) -> None:
        valid_rule = {
            "managed_set_id": "operator.owner-acceptance",
            "managed_rule_id": "owner.current",
            "github_ids": [1001],
            "roles": ["read_only"],
            "products": ["launchplane"],
            "contexts": ["owner-acceptance"],
            "actions": ["owner_acceptance.read", "owner_acceptance_event.write"],
        }
        request_payload = {
            "schema_version": 2,
            "product": "launchplane",
            "mode": "dry_run",
            "managed_set_id": "operator.owner-acceptance",
            "desired_policy": {
                "schema_version": 2,
                "github_humans": [valid_rule],
            },
        }

        AuthzManagedPolicyReconcileEnvelope.model_validate(request_payload)
        AuthzManagedPolicyReconcileEnvelope.model_validate(
            {
                **request_payload,
                "desired_policy": {
                    "schema_version": 2,
                    "github_humans": [
                        {
                            **valid_rule,
                            "managed_rule_id": "viewer.current",
                            "roles": ["read_only", "admin"],
                            "actions": ["owner_acceptance.read"],
                        }
                    ],
                },
            }
        )

        invalid_rules = (
            ({**valid_rule, "github_ids": [], "logins": ["owner"]}, "immutable GitHub IDs"),
            (
                {**valid_rule, "roles": ["admin"]},
                "Owner candidate rules require only the read_only role",
            ),
            (
                {
                    **valid_rule,
                    "managed_rule_id": "viewer.invalid",
                    "roles": ["read_only"],
                    "actions": ["owner_acceptance.read"],
                },
                "viewer rules require admin and read_only roles",
            ),
            ({**valid_rule, "products": ["other"]}, "exact Launchplane workbench scope"),
            (
                {**valid_rule, "actions": [*valid_rule["actions"], "product_config.apply"]},
                "read action alone or the read and event-write actions together",
            ),
            (
                {**valid_rule, "actions": ["owner_acceptance_event.write"]},
                "read action alone or the read and event-write actions together",
            ),
        )
        for invalid_rule, message in invalid_rules:
            with self.subTest(message=message), self.assertRaisesRegex(ValidationError, message):
                AuthzManagedPolicyReconcileEnvelope.model_validate(
                    {
                        **request_payload,
                        "desired_policy": {
                            "schema_version": 2,
                            "github_humans": [invalid_rule],
                        },
                    }
                )

        with self.assertRaisesRegex(ValidationError, "only GitHub human rules"):
            AuthzManagedPolicyReconcileEnvelope.model_validate(
                {
                    **request_payload,
                    "desired_policy": {
                        "schema_version": 2,
                        "github_actions": [
                            {
                                "managed_set_id": "operator.owner-acceptance",
                                "managed_rule_id": "worker.invalid",
                                "repository": "cbusillo/launchplane",
                                "repository_id": "1001",
                                "repository_owner_id": "2001",
                                "actions": ["owner_acceptance.read"],
                            }
                        ],
                    },
                }
            )

    def test_product_owner_policy_admin_managed_set_enforces_minimum_operator_boundary(
        self,
    ) -> None:
        valid_rule = {
            "managed_set_id": "operator.product-owner-policy-admin",
            "managed_rule_id": "verireel.policy-admin",
            "subjects": ["operator-subject"],
            "token_labels": ["operator-token"],
            "products": ["verireel"],
            "contexts": ["verireel"],
            "actions": [
                "product_owner_policy.read",
                "product_owner_policy.write",
                "product_owner_requirement.read",
                "product_owner_requirement.write",
            ],
        }
        request_payload = {
            "schema_version": 2,
            "product": "launchplane",
            "mode": "dry_run",
            "managed_set_id": "operator.product-owner-policy-admin",
            "desired_policy": {
                "schema_version": 2,
                "local_operators": [valid_rule],
            },
        }

        AuthzManagedPolicyReconcileEnvelope.model_validate(request_payload)

        invalid_rules = (
            ({**valid_rule, "subjects": ["operator-*"]}, "one exact operator subject"),
            ({**valid_rule, "token_labels": ["operator-*"]}, "one exact operator subject"),
            ({**valid_rule, "token_labels": []}, "one exact operator subject"),
            ({**valid_rule, "products": ["*"]}, "one exact product and system scope"),
            ({**valid_rule, "contexts": ["verireel", "other"]}, "one exact product"),
            ({**valid_rule, "instances": ["*"]}, "can only declare instances"),
            (
                {
                    **valid_rule,
                    "actions": [*valid_rule["actions"], "product_owner_routing.write"],
                },
                "exactly the policy and requirement read/write actions",
            ),
            (
                {
                    **valid_rule,
                    "actions": [*valid_rule["actions"], "authz_policy_grant.write"],
                },
                "exactly the policy and requirement read/write actions",
            ),
        )
        for invalid_rule, message in invalid_rules:
            with self.subTest(message=message), self.assertRaisesRegex(ValidationError, message):
                AuthzManagedPolicyReconcileEnvelope.model_validate(
                    {
                        **request_payload,
                        "desired_policy": {
                            "schema_version": 2,
                            "local_operators": [invalid_rule],
                        },
                    }
                )

        with self.assertRaisesRegex(ValidationError, "only local operator rules"):
            AuthzManagedPolicyReconcileEnvelope.model_validate(
                {
                    **request_payload,
                    "desired_policy": {
                        "schema_version": 2,
                        "local_admins": [
                            {
                                "managed_set_id": "operator.product-owner-policy-admin",
                                "managed_rule_id": "admin.invalid",
                                "subjects": ["admin-subject"],
                                "token_labels": ["admin-token"],
                                "products": ["verireel"],
                                "contexts": ["verireel"],
                                "actions": valid_rule["actions"],
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

    def test_managed_reconcile_retires_one_safe_name_only_compatibility_rule(self) -> None:
        caller_workflow_ref = (
            "cbusillo/odoo-tenant-cm/.github/workflows/odoo-artifact-publish.yml@refs/heads/main"
        )
        managed_rule = GitHubActionsPolicyRule(
            managed_set_id="operator.launchplane",
            managed_rule_id="odoo.cm.artifact-publish.inputs",
            repository="cbusillo/odoo-tenant-cm",
            repository_id="3001",
            repository_owner_id="2001",
            workflow_refs=(caller_workflow_ref,),
            job_workflow_refs=(
                "cbusillo/launchplane/.github/workflows/"
                "reusable-odoo-artifact-publish.yml@" + "a" * 40,
            ),
            event_names=("workflow_dispatch",),
            refs=("refs/heads/main",),
            products=("odoo-tenant-cm",),
            contexts=("cm",),
            instances=("*",),
            actions=("odoo_artifact_publish_inputs.read",),
        )
        compatibility_rule = GitHubActionsPolicyRule(
            repository="cbusillo/odoo-tenant-cm",
            workflow_refs=(caller_workflow_ref,),
            contexts=("*",),
            instances=("*",),
            actions=("odoo_artifact_publish_inputs.read",),
        )
        current_policy = LaunchplaneAuthzPolicy(
            schema_version=2,
            github_actions=(compatibility_rule, managed_rule),
        )
        current_record = _active_record_for_policy(current_policy)
        request_payload = {
            "schema_version": 2,
            "product": "launchplane",
            "managed_set_id": "operator.launchplane",
            "desired_policy": {
                "schema_version": 2,
                "github_actions": [managed_rule.model_dump(mode="json")],
            },
        }

        preserve_request = AuthzManagedPolicyReconcileEnvelope.model_validate(request_payload)
        _, _, preserved_policy, preserve_diff = plan_managed_authz_policy_reconcile(
            record_store=_AuthzPolicyStore((current_record,)),
            request=preserve_request,
        )

        self.assertEqual(preserved_policy, current_policy)
        self.assertFalse(preserve_diff.changed)
        self.assertEqual(preserve_diff.unmanaged_compatibility_candidate_count, 1)
        self.assertEqual(preserve_diff.retired_unmanaged_compatibility_rule_count, 0)

        retirement_request = AuthzManagedPolicyReconcileEnvelope.model_validate(
            {**request_payload, "unmanaged_adoption": "adopt_matching"}
        )
        _, _, retired_policy, retirement_diff = plan_managed_authz_policy_reconcile(
            record_store=_AuthzPolicyStore((current_record,)),
            request=retirement_request,
        )

        self.assertEqual(retired_policy.github_actions, (managed_rule,))
        self.assertTrue(retirement_diff.changed)
        self.assertTrue(retirement_diff.authorization_changed)
        self.assertEqual(retirement_diff.unmanaged_compatibility_candidate_count, 1)
        self.assertEqual(retirement_diff.retired_unmanaged_compatibility_rule_count, 1)
        self.assertEqual(retirement_diff.removed_rule_count, 0)
        self.assertEqual(retirement_diff.unchanged_rule_count, 1)
        retirement = retirement_diff.retired_unmanaged_compatibility_rules[0]
        self.assertEqual(retirement.managed_rule_id, managed_rule.managed_rule_id)
        self.assertEqual(retirement.principal_type, "github_actions")
        self.assertEqual(
            retirement.match_type,
            "github_actions_name_only_authorization_narrowing",
        )

    def test_managed_reconcile_treats_star_as_universal_glob_compatibility(self) -> None:
        managed_rule = GitHubActionsPolicyRule(
            managed_set_id="operator.launchplane",
            managed_rule_id="profile.read",
            repository="cbusillo/launchplane",
            repository_id="1001",
            repository_owner_id="2001",
            products=("launch*",),
            actions=("product_profile.read",),
        )
        compatibility_rule = GitHubActionsPolicyRule(
            repository="cbusillo/launchplane",
            products=("*",),
            contexts=("*",),
            actions=("product_profile.read",),
        )
        current_record = _active_record_for_policy(
            LaunchplaneAuthzPolicy(
                schema_version=2,
                github_actions=(compatibility_rule, managed_rule),
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
                    "github_actions": [managed_rule.model_dump(mode="json")],
                },
            }
        )

        _, _, updated_policy, diff = plan_managed_authz_policy_reconcile(
            record_store=_AuthzPolicyStore((current_record,)),
            request=request,
        )

        self.assertEqual(updated_policy.github_actions, (managed_rule,))
        self.assertEqual(diff.unmanaged_compatibility_candidate_count, 1)
        self.assertEqual(diff.retired_unmanaged_compatibility_rule_count, 1)

    def test_managed_route_requires_retirement_to_retain_applying_admin(self) -> None:
        identity = replace(
            _identity(),
            job_workflow_ref=(
                "cbusillo/launchplane/.github/workflows/reusable-manage-authorization.yml@"
                + "a" * 40
            ),
        )
        compatibility_rule = GitHubActionsPolicyRule(
            repository="cbusillo/launchplane",
            workflow_refs=(identity.workflow_ref,),
            products=("*",),
            contexts=("*",),
            actions=("authz_policy_grant.write",),
        )

        def managed_admin_rule(*, repository_id: str) -> GitHubActionsPolicyRule:
            return GitHubActionsPolicyRule(
                managed_set_id="operator.launchplane",
                managed_rule_id="authz.admin",
                repository="cbusillo/launchplane",
                repository_id=repository_id,
                repository_owner_id=identity.repository_owner_id,
                workflow_refs=(identity.workflow_ref,),
                job_workflow_refs=(identity.job_workflow_ref,),
                products=("launchplane",),
                contexts=("launchplane",),
                actions=("authz_policy_grant.write",),
            )

        def reconcile_request(
            managed_rule: GitHubActionsPolicyRule,
        ) -> AuthzManagedPolicyReconcileEnvelope:
            return AuthzManagedPolicyReconcileEnvelope.model_validate(
                {
                    "schema_version": 2,
                    "product": "launchplane",
                    "managed_set_id": "operator.launchplane",
                    "unmanaged_adoption": "adopt_matching",
                    "reason": "Verify stale managed administration fails closed.",
                    "desired_policy": {
                        "schema_version": 2,
                        "github_actions": [managed_rule.model_dump(mode="json")],
                    },
                }
            )

        stale_managed_rule = managed_admin_rule(repository_id="9999")
        stale_record = _active_record_for_policy(
            LaunchplaneAuthzPolicy(
                schema_version=2,
                github_actions=(compatibility_rule, stale_managed_rule),
            )
        )
        stale_result = execute_managed_authz_policy_reconcile(
            record_store=_AuthzPolicyStore((stale_record,)),
            request=reconcile_request(stale_managed_rule),
            identity=identity,
            trace_id="trace-retirement-lockout-dry-run",
            now_timestamp=lambda: "2026-07-20T00:00:00Z",
            authorized_policy_sha256=stale_record.policy_sha256,
        )
        stale_diff = cast(dict[str, object], stale_result.driver_result["diff"])
        stale_blockers = cast(list[dict[str, object]], stale_diff["policy_safety_blockers"])
        self.assertIn(
            "authz_policy_applying_admin_removed",
            {blocker["code"] for blocker in stale_blockers},
        )
        stale_apply_request = AuthzManagedPolicyReconcileEnvelope.model_validate(
            {
                **reconcile_request(stale_managed_rule).model_dump(mode="json"),
                "mode": "apply",
                "reviewed_plan_sha256": stale_diff["plan_sha256"],
            }
        )
        with self.assertRaises(AuthzPolicySafetyError) as raised:
            execute_managed_authz_policy_reconcile(
                record_store=_AuthzPolicyStore((stale_record,)),
                request=stale_apply_request,
                identity=identity,
                trace_id="trace-retirement-lockout",
                now_timestamp=lambda: "2026-07-20T00:00:00Z",
                authorized_policy_sha256=stale_record.policy_sha256,
            )
        self.assertEqual(raised.exception.code, "authz_policy_applying_admin_removed")

        active_managed_rule = managed_admin_rule(repository_id=identity.repository_id)
        active_record = _active_record_for_policy(
            LaunchplaneAuthzPolicy(
                schema_version=2,
                github_actions=(compatibility_rule, active_managed_rule),
            )
        )
        result = execute_managed_authz_policy_reconcile(
            record_store=_AuthzPolicyStore((active_record,)),
            request=reconcile_request(active_managed_rule),
            identity=identity,
            trace_id="trace-retirement-authorized",
            now_timestamp=lambda: "2026-07-20T00:00:00Z",
            authorized_policy_sha256=active_record.policy_sha256,
        )

        result_diff = cast(dict[str, object], result.driver_result["diff"])
        self.assertEqual(result_diff["retired_unmanaged_compatibility_rule_count"], 1)

    def test_managed_route_requires_every_change_to_retain_applying_admin(self) -> None:
        identity = replace(
            _identity(),
            job_workflow_ref=(
                "cbusillo/launchplane/.github/workflows/reusable-manage-authorization.yml@"
                + "a" * 40
            ),
        )
        current_rule = GitHubActionsPolicyRule(
            managed_set_id="operator.launchplane",
            managed_rule_id="authz.admin",
            repository=identity.repository,
            repository_id=identity.repository_id,
            repository_owner_id=identity.repository_owner_id,
            workflow_refs=(identity.workflow_ref,),
            job_workflow_refs=(identity.job_workflow_ref,),
            products=("launchplane",),
            contexts=("launchplane",),
            actions=("authz_policy_grant.write",),
        )
        replacement_rule = current_rule.model_copy(update={"repository_id": "9999"})
        current_record = _active_record_for_policy(
            LaunchplaneAuthzPolicy(schema_version=2, github_actions=(current_rule,))
        )
        request = AuthzManagedPolicyReconcileEnvelope.model_validate(
            {
                "schema_version": 2,
                "product": "launchplane",
                "managed_set_id": "operator.launchplane",
                "reason": "Verify applying administration cannot be removed.",
                "desired_policy": {
                    "schema_version": 2,
                    "github_actions": [replacement_rule.model_dump(mode="json")],
                },
            }
        )

        dry_run_result = execute_managed_authz_policy_reconcile(
            record_store=_AuthzPolicyStore((current_record,)),
            request=request,
            identity=identity,
            trace_id="trace-managed-lockout-dry-run",
            now_timestamp=lambda: "2026-08-17T00:00:00Z",
            authorized_policy_sha256=current_record.policy_sha256,
        )
        dry_run_diff = cast(dict[str, object], dry_run_result.driver_result["diff"])
        dry_run_blockers = cast(list[dict[str, object]], dry_run_diff["policy_safety_blockers"])
        self.assertIn(
            "authz_policy_applying_admin_removed",
            {blocker["code"] for blocker in dry_run_blockers},
        )
        apply_request = AuthzManagedPolicyReconcileEnvelope.model_validate(
            {
                **request.model_dump(mode="json"),
                "mode": "apply",
                "reviewed_plan_sha256": dry_run_diff["plan_sha256"],
            }
        )

        with self.assertRaises(AuthzPolicySafetyError) as raised:
            execute_managed_authz_policy_reconcile(
                record_store=_AuthzPolicyStore((current_record,)),
                request=apply_request,
                identity=identity,
                trace_id="trace-managed-lockout",
                now_timestamp=lambda: "2026-08-17T00:00:00Z",
                authorized_policy_sha256=current_record.policy_sha256,
            )
        self.assertEqual(raised.exception.code, "authz_policy_applying_admin_removed")
        self.assertIn(
            "retain policy administration authority for the applying identity",
            str(raised.exception),
        )

    def test_managed_reconcile_rejects_ambiguous_compatibility_retirement(self) -> None:
        managed_rule = GitHubActionsPolicyRule(
            managed_set_id="operator.launchplane",
            managed_rule_id="profile.read",
            repository="cbusillo/launchplane",
            repository_id="1001",
            repository_owner_id="2001",
            products=("launchplane",),
            contexts=("launchplane",),
            actions=("product_profile.read",),
        )
        compatibility_rule = GitHubActionsPolicyRule(
            repository="cbusillo/launchplane",
            actions=("product_profile.read",),
        )
        current_record = _active_record_for_policy(
            LaunchplaneAuthzPolicy(
                schema_version=2,
                github_actions=(compatibility_rule, compatibility_rule, managed_rule),
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
                    "github_actions": [managed_rule.model_dump(mode="json")],
                },
            }
        )

        with self.assertRaisesRegex(
            AuthzPolicyConflictError,
            "compatibility retirement is ambiguous",
        ):
            plan_managed_authz_policy_reconcile(
                record_store=_AuthzPolicyStore((current_record,)),
                request=request,
            )

    def test_managed_reconcile_checks_ambiguity_before_unchanged_eligibility(self) -> None:
        compatibility_rule = GitHubActionsPolicyRule(
            repository="cbusillo/launchplane",
            actions=("product_profile.read",),
        )
        unchanged_rule = GitHubActionsPolicyRule(
            managed_set_id="operator.launchplane",
            managed_rule_id="profile.first",
            repository="cbusillo/launchplane",
            repository_id="1001",
            repository_owner_id="2001",
            products=("first",),
            contexts=("first",),
            actions=("product_profile.read",),
        )
        changing_rule = GitHubActionsPolicyRule(
            managed_set_id="operator.launchplane",
            managed_rule_id="profile.second",
            repository="cbusillo/launchplane",
            repository_id="1001",
            repository_owner_id="2001",
            products=("second-old",),
            contexts=("second",),
            actions=("product_profile.read",),
        )
        desired_changing_rule = changing_rule.model_copy(update={"products": ("second-new",)})
        current_record = _active_record_for_policy(
            LaunchplaneAuthzPolicy(
                schema_version=2,
                github_actions=(compatibility_rule, unchanged_rule, changing_rule),
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
                        unchanged_rule.model_dump(mode="json"),
                        desired_changing_rule.model_dump(mode="json"),
                    ],
                },
            }
        )

        with self.assertRaisesRegex(
            AuthzPolicyConflictError,
            "compatibility retirement is ambiguous",
        ):
            plan_managed_authz_policy_reconcile(
                record_store=_AuthzPolicyStore((current_record,)),
                request=request,
            )

    def test_managed_reconcile_preserves_id_bound_unmanaged_rule(self) -> None:
        managed_rule = GitHubActionsPolicyRule(
            managed_set_id="operator.launchplane",
            managed_rule_id="profile.read",
            repository="cbusillo/launchplane",
            repository_id="1001",
            repository_owner_id="2001",
            actions=("product_profile.read",),
        )
        id_bound_rule = GitHubActionsPolicyRule(
            repository="cbusillo/launchplane",
            repository_id="1001",
            repository_owner_id="2001",
            actions=("product_profile.read",),
        )
        current_policy = LaunchplaneAuthzPolicy(
            schema_version=2,
            github_actions=(id_bound_rule, managed_rule),
        )
        request = AuthzManagedPolicyReconcileEnvelope.model_validate(
            {
                "schema_version": 2,
                "product": "launchplane",
                "managed_set_id": "operator.launchplane",
                "unmanaged_adoption": "adopt_matching",
                "desired_policy": {
                    "schema_version": 2,
                    "github_actions": [managed_rule.model_dump(mode="json")],
                },
            }
        )

        _, _, updated_policy, diff = plan_managed_authz_policy_reconcile(
            record_store=_AuthzPolicyStore((_active_record_for_policy(current_policy),)),
            request=request,
        )

        self.assertEqual(updated_policy, current_policy)
        self.assertEqual(diff.unmanaged_compatibility_candidate_count, 0)
        self.assertEqual(diff.retired_unmanaged_compatibility_rule_count, 0)

    def test_managed_reconcile_preserves_non_equivalent_unmanaged_rule(self) -> None:
        managed_rule = GitHubActionsPolicyRule(
            managed_set_id="operator.launchplane",
            managed_rule_id="profile.read",
            repository="cbusillo/launchplane",
            repository_id="1001",
            repository_owner_id="2001",
            products=("launchplane",),
            contexts=("launchplane",),
            actions=("product_profile.read",),
        )
        broader_action_rule = GitHubActionsPolicyRule(
            repository="cbusillo/launchplane",
            actions=("product_profile.read", "service.read"),
        )
        current_policy = LaunchplaneAuthzPolicy(
            schema_version=2,
            github_actions=(broader_action_rule, managed_rule),
        )
        current_record = _active_record_for_policy(current_policy)
        request = AuthzManagedPolicyReconcileEnvelope.model_validate(
            {
                "schema_version": 2,
                "product": "launchplane",
                "managed_set_id": "operator.launchplane",
                "unmanaged_adoption": "adopt_matching",
                "desired_policy": {
                    "schema_version": 2,
                    "github_actions": [managed_rule.model_dump(mode="json")],
                },
            }
        )

        _, _, updated_policy, diff = plan_managed_authz_policy_reconcile(
            record_store=_AuthzPolicyStore((current_record,)),
            request=request,
        )

        self.assertEqual(updated_policy, current_policy)
        self.assertFalse(diff.changed)
        self.assertEqual(diff.unmanaged_compatibility_candidate_count, 0)
        self.assertEqual(diff.retired_unmanaged_compatibility_rule_count, 0)

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

    def test_managed_reconcile_reports_single_rule_worker_overlap_as_not_readiness_final(
        self,
    ) -> None:
        current_record = _active_record_for_policy(LaunchplaneAuthzPolicy(schema_version=2))
        request = AuthzManagedPolicyReconcileEnvelope(
            schema_version=2,
            product="launchplane",
            managed_set_id="test.operational-readiness-rollout",
            desired_policy=_authz_rollout_fixture("operational-readiness-overlap.json"),
        )

        _, _, _, diff = plan_managed_authz_policy_reconcile(
            record_store=_AuthzPolicyStore((current_record,)),
            request=request,
        )

        self.assertEqual(diff.operational_readiness_blocked_rule_count, 1)
        self.assertEqual(len(diff.operational_readiness_blockers), 1)
        self.assertEqual(
            diff.operational_readiness_blockers[0].reason_codes,
            ("job_workflow_refs_not_singleton",),
        )

    def test_managed_reconcile_rejects_apply_with_operational_readiness_blockers(
        self,
    ) -> None:
        identity = _workflow_admin_identity()
        admin_rule = _workflow_admin_rule(identity)
        current_record = _active_record_for_policy(
            LaunchplaneAuthzPolicy(schema_version=2, github_actions=(admin_rule,))
        )
        dry_run = AuthzManagedPolicyReconcileEnvelope(
            schema_version=2,
            product="launchplane",
            managed_set_id="test.operational-readiness-rollout",
            reason="Verify readiness blockers fail closed.",
            desired_policy=_authz_rollout_fixture("operational-readiness-overlap.json"),
        )
        _, _, _, diff = plan_managed_authz_policy_reconcile(
            record_store=_AuthzPolicyStore((current_record,)),
            request=dry_run,
        )
        apply_request = AuthzManagedPolicyReconcileEnvelope.model_validate(
            {
                **dry_run.model_dump(mode="json"),
                "mode": "apply",
                "reason": "Verify readiness blockers fail closed.",
                "reviewed_plan_sha256": diff.plan_sha256,
            }
        )

        with self.assertRaises(AuthzPolicySafetyError) as raised:
            execute_managed_authz_policy_reconcile(
                record_store=_AuthzPolicyStore((current_record,)),
                request=apply_request,
                identity=identity,
                trace_id="trace-readiness-blocked",
                now_timestamp=lambda: "2026-08-17T00:00:00Z",
                authorized_policy_sha256=current_record.policy_sha256,
            )
        self.assertEqual(raised.exception.code, "authz_operational_readiness_blocked")
        self.assertIn(
            "cannot apply while operational-readiness blockers remain",
            str(raised.exception),
        )

    def test_managed_reconcile_allows_noop_apply_with_existing_readiness_blockers(
        self,
    ) -> None:
        identity = _workflow_admin_identity()
        admin_rule = _workflow_admin_rule(identity)
        blocked_policy = _authz_rollout_fixture("operational-readiness-overlap.json")
        current_policy = LaunchplaneAuthzPolicy(
            schema_version=2,
            github_actions=(admin_rule, *blocked_policy.github_actions),
        )
        current_record = _active_record_for_policy(current_policy)
        dry_run = AuthzManagedPolicyReconcileEnvelope(
            schema_version=2,
            product="launchplane",
            managed_set_id="test.operational-readiness-rollout",
            reason="Verify no-op applies remain idempotent.",
            desired_policy=blocked_policy,
        )
        _, _, _, diff = plan_managed_authz_policy_reconcile(
            record_store=_AuthzPolicyStore((current_record,)),
            request=dry_run,
        )
        apply_request = AuthzManagedPolicyReconcileEnvelope.model_validate(
            {
                **dry_run.model_dump(mode="json"),
                "mode": "apply",
                "reviewed_plan_sha256": diff.plan_sha256,
            }
        )

        result = execute_managed_authz_policy_reconcile(
            record_store=_AuthzPolicyStore((current_record,)),
            request=apply_request,
            identity=identity,
            trace_id="trace-readiness-noop",
            now_timestamp=lambda: "2026-08-17T00:00:00Z",
            authorized_policy_sha256=current_record.policy_sha256,
        )

        self.assertFalse(result.changed)
        result_diff = cast(dict[str, object], result.driver_result["diff"])
        self.assertEqual(
            result_diff["operational_readiness_blocked_rule_count"],
            1,
        )

    def test_managed_reconcile_accepts_split_exact_worker_rollout_as_readiness_final(
        self,
    ) -> None:
        current_record = _active_record_for_policy(LaunchplaneAuthzPolicy(schema_version=2))
        request = AuthzManagedPolicyReconcileEnvelope(
            schema_version=2,
            product="launchplane",
            managed_set_id="test.operational-readiness-rollout",
            desired_policy=_authz_rollout_fixture("operational-readiness-split.json"),
        )

        _, _, _, diff = plan_managed_authz_policy_reconcile(
            record_store=_AuthzPolicyStore((current_record,)),
            request=request,
        )

        self.assertEqual(diff.added_rule_count, 2)
        self.assertEqual(diff.operational_readiness_blocked_rule_count, 0)
        self.assertEqual(diff.operational_readiness_blockers, ())

    def test_managed_reconcile_reports_singleton_wildcards_as_not_readiness_final(
        self,
    ) -> None:
        current_record = _active_record_for_policy(LaunchplaneAuthzPolicy(schema_version=2))
        request = AuthzManagedPolicyReconcileEnvelope(
            schema_version=2,
            product="launchplane",
            managed_set_id="test.operational-readiness-rollout",
            desired_policy=_authz_rollout_fixture("operational-readiness-wildcards.json"),
        )

        _, _, _, diff = plan_managed_authz_policy_reconcile(
            record_store=_AuthzPolicyStore((current_record,)),
            request=request,
        )

        self.assertEqual(diff.operational_readiness_blocked_rule_count, 2)
        blockers_by_rule = {
            blocker.managed_rule_id: blocker for blocker in diff.operational_readiness_blockers
        }
        self.assertEqual(
            blockers_by_rule["example.replacement-plan.wildcards"].reason_codes,
            (
                "workflow_ref_not_exact",
                "action_not_exact",
                "product_not_exact",
                "context_not_exact",
                "instances_not_singleton",
            ),
        )
        self.assertEqual(
            blockers_by_rule["example.replacement-plan.instance-wildcard"].reason_codes,
            ("instance_not_exact",),
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
        globbed_immutable_payload = overlap_request.model_dump(mode="json")
        globbed_immutable_payload["desired_policy"]["github_actions"][0]["job_workflow_refs"] = [
            "cbusillo/launchplane/.github/workflows/reusable-*.yml@" + "a" * 40
        ]
        globbed_immutable_request = AuthzManagedPolicyReconcileEnvelope.model_validate(
            globbed_immutable_payload
        )
        with self.assertRaisesRegex(AuthzPolicyRequestError, "full commit SHA"):
            plan_managed_authz_policy_reconcile(
                record_store=_AuthzPolicyStore(
                    (_active_record_for_policy(LaunchplaneAuthzPolicy(schema_version=2)),)
                ),
                request=globbed_immutable_request,
            )

    def test_managed_reconcile_adopts_unconstrained_job_identity_by_narrowing(self) -> None:
        caller_workflow_ref = (
            "cbusillo/launchplane/.github/workflows/deploy-launchplane.yml@refs/heads/main"
        )
        pinned_job_workflow_ref = (
            "cbusillo/launchplane/.github/workflows/reusable-authz-policy-reconcile.yml@" + "a" * 40
        )
        current_rule = GitHubActionsPolicyRule(
            repository="cbusillo/launchplane",
            workflow_refs=(caller_workflow_ref,),
            event_names=("workflow_dispatch",),
            products=("launchplane",),
            contexts=("launchplane",),
            actions=("authz_policy_grant.write",),
        )
        current_record = _active_record_for_policy(
            LaunchplaneAuthzPolicy(schema_version=2, github_actions=(current_rule,))
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
                            "managed_rule_id": "authz.operator.dispatch",
                            "repository": "cbusillo/launchplane",
                            "repository_id": "1001",
                            "repository_owner_id": "2001",
                            "workflow_refs": [caller_workflow_ref],
                            "job_workflow_refs": [pinned_job_workflow_ref],
                            "event_names": ["workflow_dispatch"],
                            "products": ["launchplane"],
                            "contexts": ["launchplane"],
                            "actions": ["authz_policy_grant.write"],
                        }
                    ],
                },
            }
        )

        _, _, updated_policy, diff = plan_managed_authz_policy_reconcile(
            record_store=_AuthzPolicyStore((current_record,)),
            request=request,
        )

        self.assertEqual(diff.adopted_rule_count, 1)
        self.assertEqual(diff.added_rule_count, 0)
        self.assertEqual(len(updated_policy.github_actions), 1)
        self.assertEqual(
            updated_policy.github_actions[0].job_workflow_refs,
            (pinned_job_workflow_ref,),
        )

    def test_managed_reconcile_expands_exact_authz_worker_authority(self) -> None:
        old_job_workflow_ref = (
            "cbusillo/launchplane/.github/workflows/reusable-authz-policy-reconcile.yml@" + "a" * 40
        )
        new_job_workflow_ref = (
            "cbusillo/launchplane/.github/workflows/reusable-authz-policy-reconcile.yml@" + "b" * 40
        )
        deploy_workflow_ref = (
            "cbusillo/launchplane/.github/workflows/deploy-launchplane.yml@refs/heads/main"
        )
        operator_workflow_ref = (
            "cbusillo/launchplane/.github/workflows/authz-policy-reconcile.yml@refs/heads/main"
        )
        current_policy = LaunchplaneAuthzPolicy(
            schema_version=2,
            github_actions=(
                GitHubActionsPolicyRule(
                    managed_set_id="operator.launchplane",
                    managed_rule_id="authz.bootstrap",
                    repository="cbusillo/launchplane",
                    repository_id="1001",
                    repository_owner_id="2001",
                    workflow_refs=(deploy_workflow_ref,),
                    job_workflow_refs=(old_job_workflow_ref,),
                    event_names=("workflow_dispatch",),
                    environments=("launchplane-authz-admin",),
                    products=("launchplane",),
                    contexts=("launchplane",),
                    actions=("authz_policy_grant.write",),
                ),
            ),
        )
        new_identity = GitHubActionsIdentity(
            repository="cbusillo/launchplane",
            repository_owner="cbusillo",
            repository_id="1001",
            repository_owner_id="2001",
            workflow_ref=operator_workflow_ref,
            job_workflow_ref=new_job_workflow_ref,
            event_name="workflow_dispatch",
            ref="refs/heads/main",
            ref_type="branch",
            environment="launchplane-authz-admin",
            subject="repo:cbusillo/launchplane:environment:launchplane-authz-admin",
            sha="abc123",
            raw_claims={},
        )
        self.assertFalse(
            current_policy.allows(
                identity=new_identity,
                action="authz_policy_grant.write",
                product="launchplane",
                context="launchplane",
            )
        )
        request = AuthzManagedPolicyReconcileEnvelope.model_validate(
            {
                "schema_version": 2,
                "product": "launchplane",
                "managed_set_id": "operator.authz-policy-reconcile",
                "desired_policy": {
                    "schema_version": 2,
                    "github_actions": [
                        {
                            "managed_set_id": "operator.authz-policy-reconcile",
                            "managed_rule_id": "worker.current",
                            "repository": "cbusillo/launchplane",
                            "repository_id": "1001",
                            "repository_owner_id": "2001",
                            "workflow_refs": [operator_workflow_ref],
                            "job_workflow_refs": [new_job_workflow_ref],
                            "event_names": ["workflow_dispatch"],
                            "environments": ["launchplane-authz-admin"],
                            "products": ["launchplane"],
                            "contexts": ["launchplane"],
                            "actions": ["authz_policy_grant.write"],
                        }
                    ],
                },
            }
        )

        _, _, updated_policy, diff = plan_managed_authz_policy_reconcile(
            record_store=_AuthzPolicyStore((_active_record_for_policy(current_policy),)),
            request=request,
        )

        self.assertEqual(diff.added_rule_count, 1)
        self.assertEqual(diff.removed_rule_count, 0)
        self.assertTrue(
            updated_policy.allows(
                identity=new_identity,
                action="authz_policy_grant.write",
                product="launchplane",
                context="launchplane",
            )
        )
        self.assertEqual(len(updated_policy.github_actions), 2)

    def test_active_summary_reports_managed_migration_readiness_counts(self) -> None:
        unpinned_privileged_rule = GitHubActionsPolicyRule(
            repository="cbusillo/launchplane",
            workflow_refs=(
                "cbusillo/launchplane/.github/workflows/deploy-launchplane.yml@refs/heads/main",
            ),
            products=("launchplane",),
            contexts=("launchplane",),
            actions=("authz_policy_grant.write",),
        )
        managed_rule = GitHubActionsPolicyRule(
            managed_set_id="operator.launchplane",
            managed_rule_id="profile.read",
            repository="cbusillo/launchplane",
            repository_id="1001",
            repository_owner_id="2001",
            products=("launchplane",),
            contexts=("launchplane",),
            actions=("product_profile.read",),
        )
        record = _active_record_for_policy(
            LaunchplaneAuthzPolicy(
                schema_version=2,
                github_actions=(unpinned_privileged_rule, managed_rule),
                local_operators=(
                    LocalOperatorPolicyRule(
                        subjects=("operator",),
                        token_labels=("routine",),
                        actions=("product_profile.read",),
                    ),
                ),
            )
        )

        summary = summarize_active_authz_policy_record(record)

        self.assertEqual(summary["managed_rule_count"], 1)
        self.assertEqual(summary["unmanaged_rule_count"], 2)
        self.assertEqual(
            summary["unmanaged_rule_counts"],
            {
                "github_actions": 1,
                "github_humans": 0,
                "terminal_agents": 0,
                "local_operators": 1,
                "local_admins": 0,
            },
        )
        self.assertEqual(
            summary["github_actions_privileged_unpinned_reusable_rule_count"],
            1,
        )

    def test_policy_health_summary_bounds_and_sorts_managed_sets(self) -> None:
        policy = LaunchplaneAuthzPolicy(
            schema_version=2,
            local_operators=tuple(
                LocalOperatorPolicyRule(
                    managed_set_id=f"operator.set-{index:03d}",
                    managed_rule_id="reader",
                    subjects=(f"operator-{index:03d}",),
                    token_labels=(f"token-{index:03d}",),
                    products=("launchplane",),
                    contexts=("launchplane",),
                    actions=("product_profile.read",),
                )
                for index in range(100)
            ),
            local_admins=(
                LocalAdminPolicyRule(
                    managed_set_id="operator.admin",
                    managed_rule_id="policy-admin",
                    subjects=("authz-admin",),
                    token_labels=("authz-admin-label",),
                    products=("launchplane",),
                    contexts=("launchplane",),
                    actions=("authz_policy_grant.write",),
                ),
            ),
        )
        summary = summarize_active_authz_policy_health_record(
            record=_active_record_for_policy(policy),
            caller_identity=LocalAdminIdentity(
                subject="authz-admin",
                token_label="authz-admin-label",
            ),
        )

        self.assertEqual(summary.managed_sets.total_count, 101)
        self.assertEqual(summary.managed_sets.returned_count, 100)
        self.assertTrue(summary.managed_sets.truncated)
        returned_ids = tuple(item.managed_set_id for item in summary.managed_sets.items)
        self.assertEqual(returned_ids, tuple(sorted(returned_ids)))
        self.assertEqual(returned_ids[0], "operator.admin")
        self.assertEqual(returned_ids[-1], "operator.set-098")

    def test_policy_health_summary_reports_legacy_unmanaged_github_risks(self) -> None:
        policy = LaunchplaneAuthzPolicy(
            schema_version=1,
            github_actions=(
                GitHubActionsPolicyRule(
                    repository="example/repository",
                    workflow_refs=(
                        "example/repository/.github/workflows/caller.yml@refs/heads/main",
                    ),
                    products=("launchplane",),
                    contexts=("launchplane",),
                    actions=("authz_policy_grant.write",),
                ),
            ),
        )
        summary = summarize_active_authz_policy_health_record(
            record=_active_record_for_policy(policy),
            caller_identity=LocalAdminIdentity(
                subject="authz-admin",
                token_label="authz-admin-label",
            ),
        )

        self.assertEqual(summary.health.state, "attention_required")
        self.assertEqual(
            summary.health.reason_codes,
            (
                "policy_schema_legacy",
                "unmanaged_rules_present",
                "github_actions_legacy_name_only_rules_present",
                "github_actions_privileged_unpinned_reusable_rules_present",
            ),
        )
        self.assertEqual(summary.health.managed_rule_count, 0)
        self.assertEqual(summary.health.unmanaged_rule_count, 1)
        self.assertTrue(summary.reachable_administrators.policy_reachable)
        self.assertTrue(summary.reachable_administrators.independent_from_caller_reachable)

    def test_candidate_policy_structural_diff_is_bounded_and_deterministic(self) -> None:
        active_policy = LaunchplaneAuthzPolicy.model_validate(
            {
                "schema_version": 2,
                "local_admins": [
                    {
                        "managed_set_id": "operator.authz-preview",
                        "managed_rule_id": "reader",
                        "subjects": ["admin-secret"],
                        "token_labels": ["admin-token-secret"],
                        "products": ["launchplane"],
                        "contexts": ["launchplane"],
                        "actions": ["authz_policy_candidate_preview.read"],
                    }
                ],
                "local_operators": [
                    {
                        "managed_set_id": "operator.authz-preview",
                        "managed_rule_id": "removed-rule-secret",
                        "subjects": ["removed-subject-secret"],
                        "token_labels": ["removed-token-secret"],
                        "products": ["example"],
                        "contexts": ["testing"],
                        "actions": ["artifact_protection.read"],
                    }
                ],
            }
        )
        candidate_policy = LaunchplaneAuthzPolicy.model_validate(
            {
                "schema_version": 2,
                "local_admins": [
                    {
                        "managed_set_id": "operator.authz-preview",
                        "managed_rule_id": "reader",
                        "subjects": ["admin-secret"],
                        "token_labels": ["admin-token-secret"],
                        "products": ["launchplane"],
                        "contexts": ["launchplane"],
                        "actions": [
                            "authz_policy_candidate_preview.read",
                            "authz_policy_grant.write",
                        ],
                    }
                ],
                "terminal_agents": [
                    {
                        "managed_set_id": "operator.new-set-secret",
                        "managed_rule_id": "added-rule-secret",
                        "subjects": ["added-subject-secret"],
                        "token_labels": ["added-token-secret"],
                        "products": ["example"],
                        "contexts": ["testing"],
                        "actions": ["artifact_protection.read"],
                    }
                ],
            }
        )

        first = build_authz_candidate_policy_structural_diff(
            active_policy=active_policy,
            candidate_policy=candidate_policy,
        )
        second = build_authz_candidate_policy_structural_diff(
            active_policy=active_policy,
            candidate_policy=candidate_policy,
        )

        self.assertEqual(first, second)
        self.assertTrue(first.changed)
        self.assertEqual(first.added_rule_count, 1)
        self.assertEqual(first.updated_rule_count, 1)
        self.assertEqual(first.removed_rule_count, 1)
        self.assertEqual(first.unchanged_rule_count, 0)
        self.assertEqual(first.added_managed_set_count, 1)
        self.assertEqual(first.removed_managed_set_count, 0)
        self.assertEqual(
            first.changed_principal_types,
            ("terminal_agents", "local_operators", "local_admins"),
        )
        serialized = json.dumps(first.model_dump(mode="json"), sort_keys=True)
        for private_value in (
            "admin-secret",
            "admin-token-secret",
            "removed-rule-secret",
            "removed-subject-secret",
            "removed-token-secret",
            "operator.new-set-secret",
            "added-rule-secret",
            "added-subject-secret",
            "added-token-secret",
        ):
            self.assertNotIn(private_value, serialized)

    def test_candidate_policy_preview_round_trips_exact_noop_policy(self) -> None:
        active_policy = LaunchplaneAuthzPolicy.model_validate(
            {
                "schema_version": 2,
                "local_admins": [
                    {
                        "managed_set_id": "operator.authz-preview",
                        "managed_rule_id": "zzz-reader",
                        "subjects": ["z-authz-admin", "authz-admin", "authz-admin"],
                        "token_labels": ["z-label", "authz-admin-label"],
                        "products": ["launchplane", "launchplane"],
                        "contexts": ["launchplane", "launchplane"],
                        "actions": [
                            "authz_policy_grant.write",
                            "authz_policy_candidate_preview.read",
                            "authz_policy_grant.write",
                        ],
                    },
                    {
                        "subjects": ["independent-admin"],
                        "token_labels": ["independent-admin-label"],
                        "products": ["launchplane"],
                        "contexts": ["launchplane"],
                        "actions": ["authz_policy_grant.write", "authz_policy_grant.write"],
                    },
                ],
            }
        )
        active_record = _active_record_for_policy(active_policy)
        request = AuthzPolicyCandidatePreviewRequest.model_validate(
            {"candidate_policy": active_policy.model_dump(mode="json")}
        )

        response = preview_authz_candidate_policy(
            active_record=active_record,
            caller_identity=LocalAdminIdentity(
                subject="authz-admin",
                token_label="authz-admin-label",
            ),
            request=request,
            trace_id="launchplane_req_noop_preview",
        )

        self.assertFalse(response.diff.changed)
        self.assertEqual(response.diff.added_rule_count, 0)
        self.assertEqual(response.diff.updated_rule_count, 0)
        self.assertEqual(response.diff.removed_rule_count, 0)
        self.assertEqual(response.diff.unchanged_rule_count, 2)
        self.assertEqual(response.diff.changed_principal_types, ())
        self.assertEqual(
            response.candidate_policy.submitted_policy_sha256,
            active_record.policy_sha256,
        )
        self.assertNotEqual(
            response.candidate_policy.evaluated_policy_sha256,
            active_record.policy_sha256,
        )
        self.assertTrue(response.candidate_policy.normalized)

    def test_candidate_policy_preview_reuses_effective_access_without_recording_context(
        self,
    ) -> None:
        active_policy = LaunchplaneAuthzPolicy.model_validate(
            {
                "schema_version": 2,
                "local_admins": [
                    {
                        "managed_set_id": "operator.authz-preview",
                        "managed_rule_id": "reader",
                        "subjects": ["authz-admin"],
                        "token_labels": ["authz-admin-label"],
                        "products": ["launchplane"],
                        "contexts": ["launchplane"],
                        "actions": [
                            "authz_policy_candidate_preview.read",
                            "authz_policy_grant.write",
                        ],
                    },
                    {
                        "managed_set_id": "operator.recovery",
                        "managed_rule_id": "independent-admin",
                        "subjects": ["independent-admin"],
                        "token_labels": ["independent-admin-label"],
                        "products": ["launchplane"],
                        "contexts": ["launchplane"],
                        "actions": ["authz_policy_grant.write"],
                    },
                ],
            }
        )
        candidate_policy = active_policy.model_copy(
            update={
                "local_operators": (
                    LocalOperatorPolicyRule(
                        managed_set_id="operator.probe",
                        managed_rule_id="probe-reader",
                        subjects=("probe-subject",),
                        token_labels=("probe-token",),
                        products=("example",),
                        contexts=("testing",),
                        actions=("artifact_protection.read",),
                    ),
                )
            }
        )
        request = AuthzPolicyCandidatePreviewRequest.model_validate(
            {
                "candidate_policy": candidate_policy.model_dump(mode="json"),
                "probes": [
                    {
                        "principal": {
                            "principal_type": "local_operator",
                            "subject": "probe-subject",
                            "token_label": "probe-token",
                        },
                        "action": "artifact_protection.read",
                        "product": "example",
                        "context": "testing",
                        "target_scope": "context",
                    }
                ],
            }
        )

        response = preview_authz_candidate_policy(
            active_record=_active_record_for_policy(active_policy),
            caller_identity=LocalAdminIdentity(
                subject="authz-admin",
                token_label="authz-admin-label",
            ),
            request=request,
            trace_id="launchplane_req_preview",
        )

        self.assertEqual(response.trace_id, "launchplane_req_preview")
        self.assertEqual(response.probes[0].active_evaluation.decision, "denied")
        self.assertEqual(response.probes[0].candidate_evaluation.decision, "allowed")
        self.assertEqual(response.probes[0].delta, "granted")
        self.assertEqual(response.candidate_readiness.blocked_rule_count, 0)
        self.assertTrue(response.candidate_reachable_administrators.policy_reachable)
        self.assertTrue(
            response.candidate_reachable_administrators.independent_from_caller_reachable
        )

    def test_candidate_policy_preview_reuses_managed_workflow_transition_validation(
        self,
    ) -> None:
        active_policy = LaunchplaneAuthzPolicy.model_validate(
            {
                "schema_version": 2,
                "local_admins": [
                    {
                        "managed_set_id": "operator.authz-preview",
                        "managed_rule_id": "reader",
                        "subjects": ["authz-admin"],
                        "token_labels": ["authz-admin-label"],
                        "products": ["launchplane"],
                        "contexts": ["launchplane"],
                        "actions": ["authz_policy_candidate_preview.read"],
                    }
                ],
            }
        )
        candidate_payload = active_policy.model_dump(mode="json")
        candidate_payload["github_actions"] = [
            {
                "managed_set_id": "operator.workflow",
                "managed_rule_id": "mutable-worker",
                "repository": "example/repository",
                "repository_id": "1001",
                "repository_owner_id": "2001",
                "workflow_refs": [
                    "example/repository/.github/workflows/caller.yml@refs/heads/main"
                ],
                "job_workflow_refs": [
                    "example/repository/.github/workflows/worker.yml@refs/heads/main"
                ],
                "products": ["launchplane"],
                "contexts": ["launchplane"],
                "actions": ["authz_policy_grant.write"],
            }
        ]
        request = AuthzPolicyCandidatePreviewRequest.model_validate(
            {"candidate_policy": candidate_payload}
        )

        with self.assertRaisesRegex(
            AuthzPolicyRequestError,
            "pinned to a full commit SHA",
        ):
            preview_authz_candidate_policy(
                active_record=_active_record_for_policy(active_policy),
                caller_identity=LocalAdminIdentity(
                    subject="authz-admin",
                    token_label="authz-admin-label",
                ),
                request=request,
                trace_id="launchplane_req_invalid_transition",
            )
