import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import textwrap
import unittest
from typing import cast

import click
from click import Command
from click.testing import CliRunner
from pydantic import ValidationError

from control_plane import cli as control_plane_cli
from control_plane.cli import main
from control_plane.cli_odoo import normalize_odoo_apply_status
from control_plane.cli_policy_profiles import summarize_merge_train_policy_record
from control_plane.cli_service import _first_driver_payload
from control_plane.cli_storage_secrets import normalize_secret_scope
from control_plane.contracts.driver_descriptor import DriverContextView, DriverDescriptor
from control_plane.contracts.driver_descriptor import DriverView
from control_plane.contracts.merge_train_policy import (
    MergeTrainPolicy,
    MergeTrainRepositoryPolicy,
    MergeTrainPolicyRecord,
    ProviderCodeScanningToolExpectationV1,
    ProviderDeliveryProtectionExpectationV1,
    ProviderPullRequestExpectationV1,
    ProviderRequiredStatusCheckExpectationV1,
    build_merge_train_policy_record_id,
    merge_train_policy_provider_expectation_projection,
    merge_train_policy_sha256,
    merge_train_repository_policy_delivery_semantics_sha256,
    parse_merge_train_policy_toml,
)
from control_plane.merge_train_policy_source import MergeTrainPolicyStoreMissingError
from control_plane.merge_train_policy_source import resolve_merge_train_policy_record
from control_plane.contracts.merge_train_controller_state import build_merge_train_controller_key
from tests.merge_train_policy_fixtures import build_test_merge_train_policy
from tests.merge_train_policy_fixtures import build_test_merge_train_policy_with_codex_skills


CLI_MAIN = cast(Command, main)


def _provider_delivery_expectation(
    *,
    pull_request: ProviderPullRequestExpectationV1 | None = None,
) -> ProviderDeliveryProtectionExpectationV1:
    return ProviderDeliveryProtectionExpectationV1(
        required_status_checks=(
            ProviderRequiredStatusCheckExpectationV1(context=" unit ", app_id=200),
            ProviderRequiredStatusCheckExpectationV1(context="build", app_id=100),
        ),
        strict_required_status_checks_policy=True,
        code_scanning_tools=(
            ProviderCodeScanningToolExpectationV1(
                tool=" Zeta Scanner ",
                alerts_threshold="errors",
                security_alerts_threshold="high_or_higher",
            ),
            ProviderCodeScanningToolExpectationV1(
                tool="CodeQL",
                alerts_threshold="errors_and_warnings",
                security_alerts_threshold="medium_or_higher",
            ),
        ),
        pull_request=pull_request,
        allowed_merge_methods=("rebase", "merge", "squash"),
    )


class MergeTrainPolicyTests(unittest.TestCase):
    def test_every_repository_spelling_shares_one_controller_key(self) -> None:
        # A case-insensitive policy lookup must not let two spellings hold two
        # leases on the same train.
        self.assertEqual(
            build_merge_train_controller_key(repository="cbusillo/BD_to_AVP", base_branch="main"),
            build_merge_train_controller_key(repository="cbusillo/bd_to_avp", base_branch="main"),
        )

    def test_repository_policy_lookup_ignores_repository_casing(self) -> None:
        # Train records store the repository lowercased; the policy keeps its own
        # casing (cbusillo/BD_to_AVP admission was refused as "not admitted").
        policy = build_test_merge_train_policy(repository="Example/Mixed_Case_Repo")

        found = policy.find_repository_policy(
            repository="example/mixed_case_repo", base_branch="main"
        )

        self.assertEqual(found.repository, "Example/Mixed_Case_Repo")
        with self.assertRaises(ValueError):
            policy.find_repository_policy(repository="example/other_repo", base_branch="main")

        payload = policy.model_dump(mode="json")
        twin = dict(payload["policies"][0], repository="example/mixed_case_repo")
        with self.assertRaisesRegex(ValueError, "unique by repository/base_branch"):
            MergeTrainPolicy.model_validate({**payload, "policies": [payload["policies"][0], twin]})

    def test_token_source_preserves_stored_policy_and_requires_explicit_selection(self) -> None:
        payload = build_test_merge_train_policy().model_dump(mode="json")
        payload["policies"][0]["github_token"] = {"env_var": "GH_TOKEN"}
        historical = MergeTrainPolicyRecord.model_validate(
            {
                "record_id": "historical-env-token",
                "source": "test",
                "updated_at": "2026-05-13T21:00:00Z",
                "policy": payload,
            }
        )
        restored = MergeTrainPolicyRecord.model_validate(historical.model_dump(mode="json"))
        self.assertEqual(restored.policy_sha256, historical.policy_sha256)
        self.assertEqual(restored.model_dump(mode="json"), historical.model_dump(mode="json"))
        with self.assertRaisesRegex(ValidationError, "env_var token source is retired"):
            MergeTrainPolicy.model_validate(payload)
        with self.assertRaisesRegex(ValueError, "env_var token source is retired"):
            historical.policy.require_supported_token_sources()

        token_source = payload["policies"][0]["github_token"]
        token_source["runtime_context"] = "example_context"
        with self.assertRaises(ValidationError):
            MergeTrainPolicy.model_validate(payload)
        token_source["env_var"] = ""
        managed = MergeTrainPolicy.model_validate(payload)
        self.assertNotEqual(managed.policy_sha256, historical.policy_sha256)
        self.assertEqual(
            merge_train_repository_policy_delivery_semantics_sha256(managed.policies[0]),
            merge_train_repository_policy_delivery_semantics_sha256(historical.policy.policies[0]),
        )

    def test_provider_delivery_expectation_normalizes_exact_provider_semantics(self) -> None:
        pull_request = ProviderPullRequestExpectationV1(
            dismiss_stale_reviews_on_push=False,
            require_code_owner_review=False,
            require_last_push_approval=False,
            required_approving_review_count=0,
            required_review_thread_resolution=True,
        )

        expectation = _provider_delivery_expectation(pull_request=pull_request)

        self.assertEqual(
            [(check.context, check.app_id) for check in expectation.required_status_checks],
            [("build", 100), ("unit", 200)],
        )
        self.assertEqual(
            [tool.tool for tool in expectation.code_scanning_tools],
            ["CodeQL", "Zeta Scanner"],
        )
        self.assertEqual(expectation.allowed_merge_methods, ("merge", "squash", "rebase"))
        self.assertEqual(expectation.pull_request, pull_request)

    def test_provider_delivery_expectation_accepts_proven_absent_optional_rule_families(
        self,
    ) -> None:
        expectation = ProviderDeliveryProtectionExpectationV1(
            required_status_checks=(
                ProviderRequiredStatusCheckExpectationV1(context="build", app_id=100),
            ),
            strict_required_status_checks_policy=True,
            code_scanning_tools=(),
            pull_request=None,
            allowed_merge_methods=("merge",),
        )

        self.assertEqual(expectation.code_scanning_tools, ())
        self.assertIsNone(expectation.pull_request)
        self.assertIn("pull_request", expectation.model_dump(mode="json", exclude_none=True))

    def test_provider_delivery_expectation_rejects_ambiguous_or_unsafe_shapes(self) -> None:
        valid_payload = {
            "required_status_checks": [
                {"context": "build", "app_id": 100},
            ],
            "strict_required_status_checks_policy": True,
            "code_scanning_tools": [],
            "pull_request": None,
            "allowed_merge_methods": ["merge"],
        }
        invalid_payloads = {
            "duplicate status identity": {
                **valid_payload,
                "required_status_checks": [
                    {"context": "Build", "app_id": 100},
                    {"context": "build", "app_id": 100},
                ],
            },
            "duplicate scanning tool": {
                **valid_payload,
                "code_scanning_tools": [
                    {
                        "tool": "CodeQL",
                        "alerts_threshold": "errors",
                        "security_alerts_threshold": "high_or_higher",
                    },
                    {
                        "tool": "CodeQL",
                        "alerts_threshold": "none",
                        "security_alerts_threshold": "none",
                    },
                ],
            },
            "merge method unavailable": {
                **valid_payload,
                "allowed_merge_methods": ["squash", "rebase"],
            },
            "duplicate merge method": {
                **valid_payload,
                "allowed_merge_methods": ["merge", "merge"],
            },
            "missing explicit pull request family": {
                key: value for key, value in valid_payload.items() if key != "pull_request"
            },
            "unknown field": {**valid_payload, "unknown": True},
        }

        for case, payload in invalid_payloads.items():
            with self.subTest(case=case), self.assertRaises(ValidationError):
                ProviderDeliveryProtectionExpectationV1.model_validate(payload)
        with self.assertRaises(ValidationError):
            ProviderPullRequestExpectationV1(
                dismiss_stale_reviews_on_push=False,
                require_code_owner_review=False,
                require_last_push_approval=False,
                required_approving_review_count=7,
                required_review_thread_resolution=False,
            )

    def test_absent_provider_delivery_expectation_preserves_legacy_bytes_and_digest(self) -> None:
        legacy_policy = build_test_merge_train_policy()
        explicit_none_payload = legacy_policy.model_dump(mode="json")
        explicit_none_payload["policies"][0]["provider_delivery_protection_expectation"] = None
        explicit_none_policy = MergeTrainPolicy.model_validate(explicit_none_payload)

        self.assertNotIn(
            "provider_delivery_protection_expectation",
            explicit_none_policy.model_dump(mode="json")["policies"][0],
        )
        self.assertEqual(explicit_none_policy.policy_sha256, legacy_policy.policy_sha256)

    def test_provider_delivery_serialization_schema_retains_typed_policy_fields(self) -> None:
        schema = MergeTrainRepositoryPolicy.model_json_schema(mode="serialization")
        properties = schema["properties"]
        expectation_schema = schema["$defs"]["ProviderDeliveryProtectionExpectationV1"]

        self.assertFalse(schema["additionalProperties"])
        self.assertIn("merge_identity", properties)
        self.assertIn("github_token", properties)
        self.assertIn("enqueue", properties)
        self.assertIn("provider_delivery_protection_expectation", properties)
        self.assertNotIn("provider_delivery_protection_expectation", schema["required"])
        self.assertTrue(
            properties["provider_delivery_protection_expectation"][
                "x-launchplane-optional-response"
            ]
        )
        self.assertFalse(expectation_schema["additionalProperties"])
        self.assertEqual(
            set(expectation_schema["required"]),
            {
                "required_status_checks",
                "strict_required_status_checks_policy",
                "code_scanning_tools",
                "pull_request",
                "allowed_merge_methods",
            },
        )
        self.assertIn(
            {"type": "null"},
            expectation_schema["properties"]["pull_request"]["anyOf"],
        )

    def test_provider_expectation_projection_tracks_effective_active_policy_only(self) -> None:
        policy_payload = build_test_merge_train_policy().model_dump(mode="json")
        expectation = _provider_delivery_expectation()
        policy_payload["policies"][0]["provider_delivery_protection_expectation"] = (
            expectation.model_dump(mode="json")
        )
        record = MergeTrainPolicyRecord(
            record_id="merge-train-policy-provider-expectation",
            source="test",
            updated_at="2026-09-11T12:00:00Z",
            policy=MergeTrainPolicy.model_validate(policy_payload),
        )

        self.assertEqual(
            merge_train_policy_provider_expectation_projection(record),
            {"cbusillo/sellyouroutboard:main": expectation.model_dump(mode="json")},
        )
        self.assertEqual(
            merge_train_policy_provider_expectation_projection(
                record.model_copy(update={"status": "superseded"})
            ),
            {},
        )

    def test_delivery_semantics_digest_includes_consumed_fields_and_excludes_credentials(
        self,
    ) -> None:
        repository_policy = build_test_merge_train_policy().policies[0]
        excluded_change_payload = repository_policy.model_dump(mode="json")
        excluded_change_payload["merge_identity"]["name"] = "replacement-identity"
        excluded_change_payload["service_authz"]["context"] = "replacement-context"
        excluded_change_payload["github_token"]["runtime_context"] = "replacement_context"
        excluded_change_payload["scheduler"]["enabled"] = True
        excluded_change = MergeTrainRepositoryPolicy.model_validate(excluded_change_payload)
        selected_change_payload = repository_policy.model_dump(mode="json")
        selected_change_payload["enqueue_label"] = "new-ready-label"
        selected_change = MergeTrainRepositoryPolicy.model_validate(selected_change_payload)
        expectation_change_payload = repository_policy.model_dump(mode="json")
        expectation_change_payload["provider_delivery_protection_expectation"] = (
            _provider_delivery_expectation().model_dump(mode="json")
        )
        expectation_change = MergeTrainRepositoryPolicy.model_validate(expectation_change_payload)

        self.assertEqual(
            merge_train_repository_policy_delivery_semantics_sha256(repository_policy),
            merge_train_repository_policy_delivery_semantics_sha256(excluded_change),
        )
        self.assertNotEqual(
            merge_train_repository_policy_delivery_semantics_sha256(repository_policy),
            merge_train_repository_policy_delivery_semantics_sha256(selected_change),
        )
        self.assertNotEqual(
            merge_train_repository_policy_delivery_semantics_sha256(repository_policy),
            merge_train_repository_policy_delivery_semantics_sha256(expectation_change),
        )

    def test_policy_can_include_multiple_repository_branch_entries(self) -> None:
        policy = build_test_merge_train_policy_with_codex_skills()

        self.assertEqual(len(policy.policies), 2)
        self.assertEqual(
            {repository_policy.policy_key for repository_policy in policy.policies},
            {"cbusillo/sellyouroutboard:main", "cbusillo/codex-skills:main"},
        )
        codex_skills_policy = policy.find_repository_policy(
            repository="cbusillo/codex-skills", base_branch="main"
        )
        self.assertEqual(codex_skills_policy.enqueue_label, "ready-to-merge")
        self.assertEqual(codex_skills_policy.blocked_label, "merge-blocked")
        self.assertEqual(codex_skills_policy.stack_child_disposition_label, "stack-landed")
        self.assertEqual(codex_skills_policy.merge_method, "merge")
        self.assertEqual(codex_skills_policy.github_token.runtime_context, "example_context")
        self.assertEqual(codex_skills_policy.service_authz.action, "merge_train.run_once")
        self.assertEqual(codex_skills_policy.service_authz.product, "launchplane")
        self.assertEqual(codex_skills_policy.service_authz.context, "launchplane")
        self.assertFalse(codex_skills_policy.scheduler.enabled)
        self.assertEqual(codex_skills_policy.scheduler.runner_mode, "controller")
        self.assertFalse(codex_skills_policy.scheduler.mutate)
        self.assertEqual(codex_skills_policy.enqueue.trusted_automation_github_user_ids, ())

    def test_policy_normalizes_trusted_automation_github_user_ids(self) -> None:
        policy = build_test_merge_train_policy(
            trusted_automation_github_user_ids=(279560559, 123456789, 279560559)
        )

        repository_policy = policy.find_repository_policy(
            repository="cbusillo/sellyouroutboard", base_branch="main"
        )
        self.assertEqual(
            repository_policy.enqueue.trusted_automation_github_user_ids,
            (123456789, 279560559),
        )

    def test_policy_rejects_non_positive_trusted_automation_github_user_id(self) -> None:
        with self.assertRaises(ValidationError):
            build_test_merge_train_policy(trusted_automation_github_user_ids=(0,))

    def test_policy_can_enable_db_backed_scheduler_target(self) -> None:
        policy = parse_merge_train_policy_toml(
            textwrap.dedent(
                """
                schema_version = 1

                [[policies]]
                repository = "example/app"
                base_branch = "main"
                enqueue_label = "ready-to-merge"
                blocked_label = "merge-blocked"
                stack_child_disposition_label = "stack-landed"
                merge_method = "merge"
                failure_policy = "pause_train"
                [policies.enqueue]
                label_required = true
                allowed_actor_roles = ["repo_owner"]
                [policies.merge_identity]
                kind = "github_app"
                name = "launchplane"
                [policies.scheduler]
                enabled = true
                runner_mode = "level1"
                mutate = true
                """
            ).strip()
        )

        repository_policy = policy.find_repository_policy(
            repository="example/app", base_branch="main"
        )
        self.assertTrue(repository_policy.scheduler.enabled)
        self.assertEqual(repository_policy.scheduler.runner_mode, "level1")
        self.assertTrue(repository_policy.scheduler.mutate)

    def test_default_scheduler_policy_preserves_legacy_policy_digest(self) -> None:
        legacy_payload = {
            "schema_version": 1,
            "policies": [
                {
                    "repository": "example/app",
                    "base_branch": "main",
                    "enqueue_label": "ready-to-merge",
                    "blocked_label": "merge-blocked",
                    "stack_child_disposition_label": "stack-landed",
                    "merge_method": "merge",
                    "failure_policy": "pause_train",
                    "enqueue": {
                        "label_required": True,
                        "allowed_actor_roles": ["repo_owner"],
                    },
                    "merge_identity": {
                        "kind": "github_app",
                        "name": "launchplane",
                    },
                    "service_authz": {
                        "action": "merge_train.run_once",
                        "product": "launchplane",
                        "context": "launchplane",
                    },
                    "github_token": {"env_var": "GH_TOKEN"},
                }
            ],
        }
        legacy_sha256 = hashlib.sha256(
            json.dumps(legacy_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        record = MergeTrainPolicyRecord.model_validate(
            {
                "record_id": "merge-train-policy-legacy",
                "source": "test",
                "updated_at": "2026-05-13T21:00:00Z",
                "policy_sha256": legacy_sha256,
                "policy": legacy_payload,
            }
        )
        self.assertEqual(merge_train_policy_sha256(record.policy), legacy_sha256)
        self.assertFalse(record.policy.policies[0].scheduler.enabled)

    def test_policy_record_rejects_timezone_naive_updated_at(self) -> None:
        with self.assertRaisesRegex(ValidationError, "timestamp must include a timezone"):
            MergeTrainPolicyRecord(
                record_id="merge-train-policy-naive-time",
                source="test",
                updated_at="2026-09-02T00:00:00",
                policy=build_test_merge_train_policy(),
            )

    def test_enabled_scheduler_policy_changes_policy_digest(self) -> None:
        disabled_policy = build_test_merge_train_policy()
        enabled_policy = build_test_merge_train_policy(scheduler_enabled=True)

        self.assertNotEqual(disabled_policy.policy_sha256, enabled_policy.policy_sha256)

    def test_new_policy_rejects_required_engineering_review(self) -> None:
        with self.assertRaisesRegex(
            ValidationError, "Required engineering-review merge mode is retired"
        ):
            build_test_merge_train_policy(engineering_review_mode="required")

    def test_historical_required_policy_preserves_payload_and_digest(self) -> None:
        payload = json.loads(
            (Path(__file__).parent / "fixtures" / "merge-train-policy-required.json").read_text(
                encoding="utf-8"
            )
        )
        record = MergeTrainPolicyRecord.model_validate(payload)
        self.assertEqual(record.policy.policies[0].engineering_review_mode, "required")
        self.assertEqual(record.policy_sha256, payload["policy_sha256"])
        self.assertEqual(record.model_dump(mode="json"), payload)
        self.assertEqual(
            MergeTrainPolicyRecord.model_validate_json(record.model_dump_json()).policy_sha256,
            payload["policy_sha256"],
        )
        with self.assertRaisesRegex(
            ValidationError, "Required engineering-review merge mode is retired"
        ):
            MergeTrainPolicy.model_validate(record.policy.model_dump(mode="json"))

        payload["policy"]["policies"][0]["engineering_review_mode"] = "advisory"
        with self.assertRaisesRegex(ValidationError, "policy_sha256 does not match"):
            MergeTrainPolicyRecord.model_validate(payload)

    def test_new_imports_reject_historical_required_policy(self) -> None:
        from control_plane.http_app import MergeTrainPolicyImportEnvelope
        from control_plane.http_routes.privileged_operations import (
            OrdinaryAgentMergeTrainTargetPrepareEnvelope,
        )
        from control_plane.privileged_operation_registry import (
            plan_managed_merge_train_policy_import,
        )
        from control_plane.privileged_operation_registry import PrivilegedOperationPlannerError
        from control_plane.contracts.privileged_operation import (
            ManagedMergeTrainPolicyImportProposalInput,
            OrdinaryAgentMergeTrainTargetIntent,
        )

        record = MergeTrainPolicyRecord.model_validate_json(
            (Path(__file__).parent / "fixtures" / "merge-train-policy-required.json").read_text(
                encoding="utf-8"
            )
        )
        for mode in ("dry_run", "apply"):
            with (
                self.subTest(mode=mode),
                self.assertRaisesRegex(ValidationError, "mode is retired"),
            ):
                MergeTrainPolicyImportEnvelope(record=record, mode=mode, reason="Retirement test")
        proposal = ManagedMergeTrainPolicyImportProposalInput(
            record=record, reason="Retirement test"
        )
        with self.assertRaisesRegex(PrivilegedOperationPlannerError, "mode is retired"):
            plan_managed_merge_train_policy_import(_PolicyStore(record), proposal)
        intent_payload = {
            key: value
            for key, value in record.policy.policies[0].model_dump(mode="json").items()
            if key in OrdinaryAgentMergeTrainTargetIntent.model_fields
        }
        intent_payload["repository_id"] = "12345"
        intent_payload["engineering_review_mode"] = "advisory"
        OrdinaryAgentMergeTrainTargetIntent.model_validate(intent_payload)
        intent_payload["engineering_review_mode"] = "required"
        with self.assertRaises(ValidationError):
            OrdinaryAgentMergeTrainTargetPrepareEnvelope.model_validate(
                {"source_event_id": "retirement-test", "intent": intent_payload}
            )

    def test_trusted_automation_ids_change_policy_digest(self) -> None:
        default_policy = build_test_merge_train_policy()
        trusted_policy = build_test_merge_train_policy(
            trusted_automation_github_user_ids=(279560559,)
        )

        self.assertNotEqual(default_policy.policy_sha256, trusted_policy.policy_sha256)

    def test_empty_trusted_automation_ids_are_omitted_from_serialized_policy(self) -> None:
        policy = build_test_merge_train_policy()

        payload = policy.model_dump(mode="json")

        self.assertNotIn(
            "trusted_automation_github_user_ids",
            payload["policies"][0]["enqueue"],
        )

    def test_trusted_automation_ids_are_retained_in_serialized_policy(self) -> None:
        policy = build_test_merge_train_policy(trusted_automation_github_user_ids=(279560559,))

        payload = policy.model_dump(mode="json")

        self.assertEqual(
            payload["policies"][0]["enqueue"]["trusted_automation_github_user_ids"],
            [279560559],
        )

    def test_policy_rejects_multiline_repository_authority_values(self) -> None:
        policy_toml = textwrap.dedent(
            """
            schema_version = 1

            [[policies]]
            repository = '''example/app
            other/app'''
            base_branch = "main"
            enqueue_label = "ready-to-merge"
            blocked_label = "merge-blocked"
            stack_child_disposition_label = "stack-landed"
            merge_method = "merge"
            failure_policy = "pause_train"
            [policies.enqueue]
            label_required = true
            allowed_actor_roles = ["repo_owner"]
            [policies.merge_identity]
            kind = "github_app"
            name = "launchplane"
            """
        ).strip()

        with self.assertRaisesRegex(ValidationError, "single line"):
            parse_merge_train_policy_toml(policy_toml)

    def test_policy_record_validates_digest(self) -> None:
        policy = build_test_merge_train_policy_with_codex_skills()
        record = MergeTrainPolicyRecord(
            record_id=build_merge_train_policy_record_id(
                updated_at="2026-05-13T21:00:00Z",
                policy_sha256=policy.policy_sha256,
            ),
            source="test",
            updated_at="2026-05-13T21:00:00Z",
            policy=policy,
        )

        self.assertEqual(record.policy_sha256, policy.policy_sha256)
        self.assertEqual(
            record.record_id,
            f"merge-train-policy-20260513T210000Z-{policy.policy_sha256[:12]}",
        )

    def test_cli_merge_train_policy_summary_uses_shared_policy_base(self) -> None:
        policy = build_test_merge_train_policy_with_codex_skills()
        record = MergeTrainPolicyRecord(
            record_id=build_merge_train_policy_record_id(
                updated_at="2026-05-13T21:00:00Z",
                policy_sha256=policy.policy_sha256,
            ),
            source="test",
            updated_at="2026-05-13T21:00:00Z",
            policy=policy,
        )

        summary = summarize_merge_train_policy_record(record)

        self.assertEqual(summary["record_id"], record.record_id)
        self.assertEqual(summary["status"], "active")
        self.assertEqual(summary["source"], "test")
        self.assertEqual(summary["updated_at"], "2026-05-13T21:00:00Z")
        self.assertEqual(summary["policy_sha256"], policy.policy_sha256)
        self.assertEqual(summary["repository_count"], 2)
        self.assertEqual(
            summary["policy_keys"],
            ["cbusillo/sellyouroutboard:main", "cbusillo/codex-skills:main"],
        )
        self.assertEqual(summary["scheduler_policy_keys"], [])

    def test_cli_choice_normalizers_share_trimmed_case_insensitive_choices(self) -> None:
        self.assertEqual(normalize_secret_scope(" Context "), "context")
        self.assertEqual(normalize_odoo_apply_status(" PASS "), "pass")
        self.assertEqual(
            control_plane_cli._normalize_dokploy_target_type(" APPLICATION "),
            "application",
        )

    def test_cli_choice_normalizer_preserves_domain_error_messages(self) -> None:
        invalid_cases = [
            (
                normalize_secret_scope,
                "environment",
                "Secret scope must be one of global, context, or context_instance.",
            ),
            (
                normalize_odoo_apply_status,
                "success",
                "Odoo override apply status must be skipped, pending, pass, or fail.",
            ),
            (
                control_plane_cli._normalize_dokploy_target_type,
                "service",
                "Dokploy target type must be compose or application.",
            ),
        ]

        for normalizer, value, expected_message in invalid_cases:
            with self.subTest(value=value):
                with self.assertRaisesRegex(click.ClickException, expected_message):
                    normalizer(value)

    def test_cli_first_driver_payload_uses_typed_driver_context_view(self) -> None:
        view = DriverContextView(
            context="demo",
            drivers=(
                DriverView(
                    driver_id="generic-web",
                    descriptor=DriverDescriptor(
                        driver_id="generic-web",
                        label="Generic Web",
                        product="generic-web",
                        description="Generic web driver",
                        provider_boundary="launchplane",
                    ),
                ),
                DriverView(
                    driver_id="verireel",
                    descriptor=DriverDescriptor(
                        driver_id="verireel",
                        label="Verireel",
                        product="verireel",
                        description="Verireel driver",
                        provider_boundary="launchplane",
                    ),
                ),
            ),
        )

        payload = _first_driver_payload(view, driver_id="verireel")

        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload["driver_id"], "verireel")
        descriptor = payload["descriptor"]
        self.assertIsInstance(descriptor, dict)
        assert isinstance(descriptor, dict)
        self.assertEqual(descriptor["driver_id"], "verireel")
        self.assertEqual(descriptor["label"], "Verireel")
        self.assertEqual(descriptor["product"], "verireel")
        self.assertEqual(descriptor["provider_boundary"], "launchplane")

    def test_cli_first_driver_payload_returns_none_for_missing_driver(self) -> None:
        view = DriverContextView(context="demo")

        payload = _first_driver_payload(view, driver_id="verireel")

        self.assertIsNone(payload)

    def test_policy_record_digest_ignores_missing_optional_stack_child_label(
        self,
    ) -> None:
        policy = parse_merge_train_policy_toml(
            textwrap.dedent(
                """
                schema_version = 1

                [[policies]]
                repository = "example/app"
                base_branch = "main"
                enqueue_label = "ready-to-merge"
                blocked_label = "merge-blocked"
                merge_method = "merge"
                failure_policy = "pause_train"
                [policies.enqueue]
                label_required = true
                allowed_actor_roles = ["repo_owner"]
                [policies.merge_identity]
                kind = "github_app"
                name = "launchplane"
                """
            ).strip()
        )
        explicit_empty_policy = parse_merge_train_policy_toml(
            textwrap.dedent(
                """
                schema_version = 1

                [[policies]]
                repository = "example/app"
                base_branch = "main"
                enqueue_label = "ready-to-merge"
                blocked_label = "merge-blocked"
                stack_child_disposition_label = ""
                merge_method = "merge"
                failure_policy = "pause_train"
                [policies.enqueue]
                label_required = true
                allowed_actor_roles = ["repo_owner"]
                [policies.merge_identity]
                kind = "github_app"
                name = "launchplane"
                """
            ).strip()
        )

        self.assertEqual(policy.policy_sha256, explicit_empty_policy.policy_sha256)

    def test_resolve_merge_train_policy_record_fails_closed_when_missing(self) -> None:
        store = _PolicyStore()

        with self.assertRaisesRegex(MergeTrainPolicyStoreMissingError, "missing"):
            resolve_merge_train_policy_record(store)

        self.assertEqual(store.written_records, [])

    def test_resolve_merge_train_policy_record_prefers_existing_active_record(self) -> None:
        policy = build_test_merge_train_policy(repository="example/app")
        existing_record = MergeTrainPolicyRecord(
            record_id="merge-train-policy-existing",
            source="test",
            updated_at="2026-05-13T21:00:00Z",
            policy=policy,
        )
        store = _PolicyStore(existing_record)

        record = resolve_merge_train_policy_record(store)

        self.assertEqual(record.record_id, "merge-train-policy-existing")
        self.assertEqual(store.written_records, [])

    def test_parse_rejects_duplicate_repository_branch_policy(self) -> None:
        policy_toml = textwrap.dedent(
            """
            schema_version = 1

            [[policies]]
            repository = "example/app"
            base_branch = "main"
            enqueue_label = "ready-to-merge"
            blocked_label = "merge-blocked"
            merge_method = "merge"
            failure_policy = "pause_train"
            [policies.enqueue]
            label_required = true
            allowed_actor_roles = ["repo_owner"]
            [policies.merge_identity]
            kind = "github_app"
            name = "launchplane"

            [[policies]]
            repository = "example/app"
            base_branch = "main"
            enqueue_label = "ready-to-merge"
            blocked_label = "merge-blocked"
            merge_method = "merge"
            failure_policy = "pause_train"
            [policies.enqueue]
            label_required = true
            allowed_actor_roles = ["repo_admin"]
            [policies.merge_identity]
            kind = "github_app"
            name = "launchplane"
            """
        ).strip()

        with self.assertRaisesRegex(ValidationError, "unique by repository/base_branch"):
            parse_merge_train_policy_toml(policy_toml)

    def test_parse_rejects_ambiguous_labels(self) -> None:
        policy_toml = textwrap.dedent(
            """
            schema_version = 1

            [[policies]]
            repository = "example/app"
            base_branch = "main"
            enqueue_label = "ready-to-merge"
            blocked_label = "ready-to-merge"
            stack_child_disposition_label = "stack-landed"
            merge_method = "merge"
            failure_policy = "continue_after_blocking_pr"
            [policies.enqueue]
            label_required = true
            allowed_actor_roles = ["repo_admin"]
            [policies.merge_identity]
            kind = "github_token_secret"
            name = "MERGE_TRAIN_TOKEN"
            """
        ).strip()

        with self.assertRaisesRegex(ValidationError, "must differ"):
            parse_merge_train_policy_toml(policy_toml)

    def test_parse_rejects_stack_child_disposition_label_that_matches_train_label(
        self,
    ) -> None:
        policy_toml = textwrap.dedent(
            """
            schema_version = 1

            [[policies]]
            repository = "example/app"
            base_branch = "main"
            enqueue_label = "ready-to-merge"
            blocked_label = "merge-blocked"
            stack_child_disposition_label = "merge-blocked"
            merge_method = "merge"
            failure_policy = "continue_after_blocking_pr"
            [policies.enqueue]
            label_required = true
            allowed_actor_roles = ["repo_admin"]
            [policies.merge_identity]
            kind = "github_token_secret"
            name = "MERGE_TRAIN_TOKEN"
            """
        ).strip()

        with self.assertRaisesRegex(ValidationError, "stack_child_disposition_label"):
            parse_merge_train_policy_toml(policy_toml)

    def test_work_graph_merge_train_policy_cli_renders_dry_run_contract(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            result = CliRunner().invoke(
                CLI_MAIN,
                [
                    "work-graph",
                    "merge-train-policy",
                    "--policy-file",
                    str(_write_policy_file(temporary_directory_name)),
                    "--repository",
                    "cbusillo/sellyouroutboard",
                    "--base-branch",
                    "main",
                ],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["repository_count"], 1)
        self.assertEqual(payload["selected_policy"]["repository"], "cbusillo/sellyouroutboard")
        self.assertEqual(payload["selected_policy"]["base_branch"], "main")


def _write_policy_file(directory: str) -> Path:
    policy_file = Path(directory) / "merge-train-policy.toml"
    policy_file.write_text(
        "\n\n".join(
            (
                "schema_version = 1",
                """[[policies]]
repository = "cbusillo/sellyouroutboard"
base_branch = "main"
enqueue_label = "ready-to-merge"
blocked_label = "merge-blocked"
stack_child_disposition_label = "stack-landed"
merge_method = "merge"
failure_policy = "pause_train"
[policies.enqueue]
label_required = true
allowed_actor_roles = ["repo_owner", "repo_admin"]
[policies.merge_identity]
kind = "github_actions_oidc"
name = "launchplane"
[policies.github_token]
runtime_context = "example_context"
""",
            )
        ),
        encoding="utf-8",
    )
    return policy_file


class _PolicyStore:
    def __init__(self, *records: MergeTrainPolicyRecord) -> None:
        self.records = list(records)
        self.written_records: list[MergeTrainPolicyRecord] = []

    def list_merge_train_policy_records(
        self, *, status: str = "", limit: int | None = None
    ) -> tuple[MergeTrainPolicyRecord, ...]:
        records = [record for record in self.records if not status or record.status == status]
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def write_merge_train_policy_record(self, record: MergeTrainPolicyRecord) -> None:
        self.records.append(record)
        self.written_records.append(record)


if __name__ == "__main__":
    unittest.main()
