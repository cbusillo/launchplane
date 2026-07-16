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
    MergeTrainPolicyRecord,
    build_merge_train_policy_record_id,
    merge_train_policy_sha256,
    parse_merge_train_policy_toml,
)
from control_plane.merge_train_policy_source import MergeTrainPolicyStoreMissingError
from control_plane.merge_train_policy_source import resolve_merge_train_policy_record
from tests.merge_train_policy_fixtures import build_test_merge_train_policy
from tests.merge_train_policy_fixtures import build_test_merge_train_policy_with_codex_skills


CLI_MAIN = cast(Command, main)


class MergeTrainPolicyTests(unittest.TestCase):
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
        self.assertEqual(codex_skills_policy.github_token.env_var, "GH_TOKEN")
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
                [policies.service_authz]
                action = "merge_train.run_once"
                product = "launchplane"
                context = "launchplane"
                [policies.github_token]
                env_var = "GH_TOKEN"
                """
            ).strip()
        )

        self.assertEqual(merge_train_policy_sha256(policy), legacy_sha256)
        record = MergeTrainPolicyRecord(
            record_id="merge-train-policy-legacy",
            source="test",
            updated_at="2026-05-13T21:00:00Z",
            policy_sha256=merge_train_policy_sha256(policy),
            policy=policy,
        )
        self.assertFalse(record.policy.policies[0].scheduler.enabled)

    def test_enabled_scheduler_policy_changes_policy_digest(self) -> None:
        disabled_policy = build_test_merge_train_policy()
        enabled_policy = build_test_merge_train_policy(scheduler_enabled=True)

        self.assertNotEqual(disabled_policy.policy_sha256, enabled_policy.policy_sha256)

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
env_var = "GH_TOKEN"
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
