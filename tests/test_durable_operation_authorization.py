import json
import unittest

from pydantic import ValidationError

from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
from control_plane.contracts.durable_operation_authorization import (
    DurableOperationAuthorization,
)
from control_plane.durable_operation_authorization import (
    DurableOperationAuthorizationCaptureError,
    capture_durable_operation_authorization,
    durable_operation_authorization_allows,
    managed_github_id_action_allows,
    managed_github_id_rule_allows,
    require_single_managed_github_id_rule_identity,
)
from control_plane.service_auth import (
    AuthorizationTarget,
    GitHubActionsIdentity,
    GitHubHumanIdentity,
    LaunchplaneAuthzPolicy,
)
from tests.support.auth import _identity


_LEGACY_V2_POLICY_AUTHORIZATION_JSON = (
    b'{"schema_version":1,"action":"odoo_stable_bootstrap.execute",'
    b'"product":"odoo-tenant-cm","context":"cm","instances":["testing"],'
    b'"managed_set_id":"operator.odoo-stable-operations",'
    b'"managed_rule_id":"cm-testing-bootstrap",'
    b'"policy_record_id":"launchplane-authz-policy-r00000000000000000041-example",'
    b'"policy_revision":41,"policy_schema_version":2,'
    b'"policy_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
    b'"policy_source":"service:test","authorized_at":"2026-07-23T03:31:00Z",'
    b'"caller":{"schema_version":1,"identity_type":"github_actions",'
    b'"subject":"repo:cbusillo/launchplane:ref:refs/heads/main","token_label":"",'
    b'"repository":"cbusillo/launchplane","repository_owner":"cbusillo",'
    b'"repository_id":"1001","repository_owner_id":"2001",'
    b'"workflow_ref":"cbusillo/launchplane/.github/workflows/'
    b'odoo-stable-operation.yml@refs/heads/main",'
    b'"job_workflow_ref":"cbusillo/launchplane/.github/workflows/'
    b"reusable-odoo-stable-operation.yml@"
    b'0123456789abcdef0123456789abcdef01234567",'
    b'"ref":"refs/heads/main","ref_type":"branch",'
    b'"event_name":"workflow_dispatch","environment":"",'
    b'"sha":"0123456789abcdef0123456789abcdef01234567","login":"",'
    b'"github_id":0,"organizations":[],"teams":[],"role":""}}'
)


class DurableOperationAuthorizationTests(unittest.TestCase):
    def _policy(
        self,
        *,
        managed_rule_id: str = "cm-testing-bootstrap",
        instances: tuple[str, ...] = ("testing",),
        include_unmanaged_overlap: bool = False,
        schema_version: int = 2,
        actions: tuple[str, ...] = ("odoo_stable_bootstrap.execute",),
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
                "actions": list(actions),
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
                "schema_version": schema_version,
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

        for active_policy_schema_version in (2, 3):
            with self.subTest(active_policy_schema_version=active_policy_schema_version):
                updated_policy_record = self._policy_record(
                    self._policy(
                        include_unmanaged_overlap=True,
                        schema_version=active_policy_schema_version,
                    ),
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
                            self._policy(
                                managed_rule_id="replacement-rule",
                                schema_version=active_policy_schema_version,
                            ),
                            revision=43,
                        ),
                    )
                )
                self.assertFalse(
                    durable_operation_authorization_allows(
                        authorization=authorization,
                        policy_record=self._policy_record(
                            self._policy(
                                instances=("prod",),
                                schema_version=active_policy_schema_version,
                            ),
                            revision=44,
                        ),
                    )
                )

    def test_v2_and_v3_action_selectors_deny_nonmatches_and_preserve_empty_actions(
        self,
    ) -> None:
        for policy_schema_version in (2, 3):
            with self.subTest(policy_schema_version=policy_schema_version):
                matching_record = self._policy_record(
                    self._policy(schema_version=policy_schema_version),
                    revision=45,
                )
                matching_authorization = capture_durable_operation_authorization(
                    identity=self._identity(),
                    action="odoo_stable_bootstrap.execute",
                    product="odoo-tenant-cm",
                    context="cm",
                    instances=("testing",),
                    policy_record=matching_record,
                    authorized_at="2026-07-23T03:31:00Z",
                )
                nonmatching_action_record = self._policy_record(
                    self._policy(
                        schema_version=policy_schema_version,
                        actions=("odoo_target_replacement_apply.execute",),
                    ),
                    revision=46,
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
                        policy_record=nonmatching_action_record,
                        authorized_at="2026-07-23T03:31:00Z",
                    )
                self.assertFalse(
                    durable_operation_authorization_allows(
                        authorization=matching_authorization,
                        policy_record=nonmatching_action_record,
                    )
                )

                empty_action_record = self._policy_record(
                    self._policy(schema_version=policy_schema_version, actions=()),
                    revision=47,
                )
                empty_action_authorization = capture_durable_operation_authorization(
                    identity=self._identity(),
                    action="odoo_stable_bootstrap.execute",
                    product="odoo-tenant-cm",
                    context="cm",
                    instances=("testing",),
                    policy_record=empty_action_record,
                    authorized_at="2026-07-23T03:31:00Z",
                )
                self.assertTrue(
                    durable_operation_authorization_allows(
                        authorization=empty_action_authorization,
                        policy_record=empty_action_record,
                    )
                )

    def test_v3_managed_github_id_helpers_preserve_immutable_id_requirements(self) -> None:
        identity = GitHubHumanIdentity(
            login="operator",
            github_id=123,
            name="",
            email="",
            organizations=frozenset(),
            teams=frozenset(),
            role="admin",
        )
        target = AuthorizationTarget(scope="global")
        rule = {
            "managed_set_id": "privileged-operations.secret-execution",
            "managed_rule_id": "human-secret-approver",
            "github_ids": [123],
            "roles": ["admin"],
            "products": ["launchplane"],
            "contexts": ["launchplane"],
            "actions": ["privileged_secret_operation.approve"],
        }
        policy = LaunchplaneAuthzPolicy.model_validate(
            {"schema_version": 3, "github_humans": [rule]}
        )

        managed_identity = require_single_managed_github_id_rule_identity(
            policy=policy,
            identity=identity,
            action="privileged_secret_operation.approve",
            product="launchplane",
            context="launchplane",
            target=target,
        )
        self.assertEqual(managed_identity.managed_rule_id, "human-secret-approver")
        self.assertTrue(
            managed_github_id_rule_allows(
                policy=policy,
                github_id=123,
                managed_set_id=managed_identity.managed_set_id,
                managed_rule_id=managed_identity.managed_rule_id,
                action="privileged_secret_operation.approve",
                product="launchplane",
                context="launchplane",
                target=target,
            )
        )
        self.assertTrue(
            managed_github_id_action_allows(
                policy=policy,
                github_id=123,
                action="privileged_secret_operation.approve",
                product="launchplane",
                context="launchplane",
                target=target,
            )
        )

        mutable_only_policy = LaunchplaneAuthzPolicy.model_validate(
            {
                "schema_version": 3,
                "github_humans": [{**rule, "github_ids": [], "logins": ["operator"]}],
            }
        )
        with self.assertRaisesRegex(
            ValueError,
            "immutable GitHub-ID selector",
        ):
            require_single_managed_github_id_rule_identity(
                policy=mutable_only_policy,
                identity=identity,
                action="privileged_secret_operation.approve",
                product="launchplane",
                context="launchplane",
                target=target,
            )
        self.assertFalse(
            managed_github_id_rule_allows(
                policy=mutable_only_policy,
                github_id=123,
                managed_set_id=managed_identity.managed_set_id,
                managed_rule_id=managed_identity.managed_rule_id,
                action="privileged_secret_operation.approve",
                product="launchplane",
                context="launchplane",
                target=target,
            )
        )

    def test_legacy_v2_authorization_serialization_remains_byte_compatible(self) -> None:
        authorization = DurableOperationAuthorization.model_validate_json(
            _LEGACY_V2_POLICY_AUTHORIZATION_JSON
        )

        serialized = authorization.model_dump_json().encode("utf-8")

        self.assertEqual(serialized, _LEGACY_V2_POLICY_AUTHORIZATION_JSON)
        self.assertEqual(
            DurableOperationAuthorization.model_validate_json(serialized), authorization
        )

    def test_policy_schema_provenance_is_required_and_rejects_unsupported_versions(self) -> None:
        payload = json.loads(_LEGACY_V2_POLICY_AUTHORIZATION_JSON)
        missing_version = dict(payload)
        missing_version.pop("policy_schema_version")
        with self.assertRaises(ValidationError):
            DurableOperationAuthorization.model_validate(missing_version)

        for unsupported_version in (1, 4):
            with self.subTest(unsupported_version=unsupported_version):
                with self.assertRaises(ValidationError):
                    DurableOperationAuthorization.model_validate(
                        {**payload, "policy_schema_version": unsupported_version}
                    )

        with self.assertRaisesRegex(
            DurableOperationAuthorizationCaptureError,
            "schema-v2 or schema-v3",
        ):
            capture_durable_operation_authorization(
                identity=self._identity(),
                action="odoo_stable_bootstrap.execute",
                product="odoo-tenant-cm",
                context="cm",
                instances=("testing",),
                policy_record=self._policy_record(LaunchplaneAuthzPolicy(schema_version=1)),
                authorized_at="2026-07-23T03:31:00Z",
            )

    def test_v3_capture_records_active_policy_provenance_and_round_trips(self) -> None:
        policy_record = self._policy_record(self._policy(schema_version=3), revision=51)

        authorization = capture_durable_operation_authorization(
            identity=self._identity(),
            action="odoo_stable_bootstrap.execute",
            product="odoo-tenant-cm",
            context="cm",
            instances=("testing",),
            policy_record=policy_record,
            authorized_at="2026-07-23T03:31:00Z",
        )
        round_tripped = DurableOperationAuthorization.model_validate_json(
            authorization.model_dump_json()
        )

        self.assertEqual(round_tripped, authorization)
        self.assertEqual(authorization.policy_schema_version, 3)
        self.assertEqual(authorization.policy_record_id, policy_record.record_id)
        self.assertEqual(authorization.policy_revision, policy_record.revision)
        self.assertEqual(authorization.policy_sha256, policy_record.policy_sha256)
        self.assertTrue(
            durable_operation_authorization_allows(
                authorization=round_tripped,
                policy_record=policy_record,
            )
        )
        self.assertFalse(
            durable_operation_authorization_allows(
                authorization=round_tripped,
                policy_record=self._policy_record(self._policy(schema_version=2), revision=52),
            )
        )


if __name__ == "__main__":
    unittest.main()
