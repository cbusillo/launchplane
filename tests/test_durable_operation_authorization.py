import unittest

from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
from control_plane.durable_operation_authorization import (
    DurableOperationAuthorizationCaptureError,
    capture_durable_operation_authorization,
    durable_operation_authorization_allows,
)
from control_plane.service_auth import GitHubActionsIdentity, LaunchplaneAuthzPolicy
from tests.support.auth import _identity


class DurableOperationAuthorizationTests(unittest.TestCase):
    def _policy(
        self,
        *,
        managed_rule_id: str = "cm-testing-bootstrap",
        instances: tuple[str, ...] = ("testing",),
        include_unmanaged_overlap: bool = False,
    ) -> LaunchplaneAuthzPolicy:
        rules: list[dict[str, object]] = [
            {
                "managed_set_id": "operator.odoo-stable-operations",
                "managed_rule_id": managed_rule_id,
                "repository": "cbusillo/launchplane",
                "repository_id": "1001",
                "repository_owner_id": "2001",
                "workflow_refs": [
                    "cbusillo/launchplane/.github/workflows/odoo-stable-bootstrap.yml@refs/heads/main"
                ],
                "job_workflow_refs": [
                    "cbusillo/launchplane/.github/workflows/reusable-odoo-stable-bootstrap.yml@0123456789abcdef0123456789abcdef01234567"
                ],
                "event_names": ["workflow_dispatch"],
                "refs": ["refs/heads/main"],
                "products": ["odoo-tenant-cm"],
                "contexts": ["cm"],
                "instances": list(instances),
                "actions": ["odoo_stable_bootstrap.execute"],
            }
        ]
        if include_unmanaged_overlap:
            rules.append(
                {
                    "repository": "cbusillo/launchplane",
                    "products": ["odoo-tenant-cm"],
                    "contexts": ["cm"],
                    "instances": ["testing"],
                    "actions": ["odoo_stable_bootstrap.execute"],
                }
            )
        return LaunchplaneAuthzPolicy.model_validate(
            {
                "schema_version": 2,
                "github_actions": rules,
            }
        )

    def _identity(self) -> GitHubActionsIdentity:
        return _identity(
            repository="cbusillo/launchplane",
            repository_id="1001",
            repository_owner_id="2001",
            workflow_ref=(
                "cbusillo/launchplane/.github/workflows/odoo-stable-bootstrap.yml@refs/heads/main"
            ),
            job_workflow_ref=(
                "cbusillo/launchplane/.github/workflows/reusable-odoo-stable-bootstrap.yml@0123456789abcdef0123456789abcdef01234567"
            ),
            event_name="workflow_dispatch",
        )

    def _policy_record(
        self,
        policy: LaunchplaneAuthzPolicy,
        *,
        revision: int = 41,
    ) -> LaunchplaneAuthzPolicyRecord:
        return LaunchplaneAuthzPolicyRecord(
            record_id=f"launchplane-authz-policy-r{revision}",
            revision=revision,
            status="active",
            source="service:test",
            updated_at="2026-07-23T03:30:00Z",
            policy=policy,
        )

    def test_capture_records_rule_policy_target_and_reusable_identity(self) -> None:
        policy_record = self._policy_record(self._policy())

        authorization = capture_durable_operation_authorization(
            identity=self._identity(),
            action="odoo_stable_bootstrap.execute",
            product="odoo-tenant-cm",
            context="cm",
            instances=("testing",),
            policy_record=policy_record,
            authorized_at="2026-07-23T03:31:00Z",
        )

        self.assertEqual(authorization.managed_set_id, "operator.odoo-stable-operations")
        self.assertEqual(authorization.managed_rule_id, "cm-testing-bootstrap")
        self.assertEqual(authorization.policy_revision, 41)
        self.assertEqual(authorization.policy_sha256, policy_record.policy_sha256)
        self.assertEqual(authorization.policy_schema_version, 2)
        self.assertEqual(authorization.instances, ("testing",))
        self.assertEqual(authorization.caller.identity_type, "github_actions")
        self.assertEqual(
            authorization.caller.job_workflow_ref,
            "cbusillo/launchplane/.github/workflows/reusable-odoo-stable-bootstrap.yml@0123456789abcdef0123456789abcdef01234567",
        )
        self.assertNotIn("raw_claims", authorization.caller.model_dump(mode="json"))

    def test_capture_ignores_unmanaged_overlap_but_rejects_multiple_managed_matches(self) -> None:
        policy = self._policy(include_unmanaged_overlap=True)
        authorization = capture_durable_operation_authorization(
            identity=self._identity(),
            action="odoo_stable_bootstrap.execute",
            product="odoo-tenant-cm",
            context="cm",
            instances=("testing",),
            policy_record=self._policy_record(policy),
            authorized_at="2026-07-23T03:31:00Z",
        )
        self.assertEqual(authorization.managed_rule_id, "cm-testing-bootstrap")

        ambiguous_policy = policy.model_copy(
            update={
                "github_actions": (
                    *policy.github_actions,
                    policy.github_actions[0].model_copy(
                        update={"managed_rule_id": "cm-testing-bootstrap-overlap"}
                    ),
                )
            }
        )
        with self.assertRaisesRegex(
            DurableOperationAuthorizationCaptureError,
            "exactly one managed authz rule",
        ):
            capture_durable_operation_authorization(
                identity=self._identity(),
                action="odoo_stable_bootstrap.execute",
                product="odoo-tenant-cm",
                context="cm",
                instances=("testing",),
                policy_record=self._policy_record(ambiguous_policy),
                authorized_at="2026-07-23T03:31:00Z",
            )

    def test_reauthorization_requires_the_same_managed_rule_to_still_allow_target(self) -> None:
        original_policy_record = self._policy_record(self._policy())
        authorization = capture_durable_operation_authorization(
            identity=self._identity(),
            action="odoo_stable_bootstrap.execute",
            product="odoo-tenant-cm",
            context="cm",
            instances=("testing",),
            policy_record=original_policy_record,
            authorized_at="2026-07-23T03:31:00Z",
        )

        updated_policy_record = self._policy_record(
            self._policy(include_unmanaged_overlap=True),
            revision=42,
        )
        self.assertNotEqual(
            original_policy_record.policy_sha256,
            updated_policy_record.policy_sha256,
        )
        self.assertTrue(
            durable_operation_authorization_allows(
                authorization=authorization,
                policy_record=updated_policy_record,
            )
        )
        self.assertFalse(
            durable_operation_authorization_allows(
                authorization=authorization,
                policy_record=self._policy_record(
                    self._policy(managed_rule_id="replacement-rule"),
                    revision=43,
                ),
            )
        )
        self.assertFalse(
            durable_operation_authorization_allows(
                authorization=authorization,
                policy_record=self._policy_record(
                    self._policy(instances=("prod",)),
                    revision=44,
                ),
            )
        )


if __name__ == "__main__":
    unittest.main()
