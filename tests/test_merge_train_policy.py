import json
from pathlib import Path
from tempfile import TemporaryDirectory
import textwrap
import unittest

import click
from click.testing import CliRunner
from pydantic import ValidationError

from control_plane import cli as control_plane_cli
from control_plane.cli import main
from control_plane.contracts.driver_descriptor import DriverContextView, DriverDescriptor
from control_plane.contracts.driver_descriptor import DriverView
from control_plane.contracts.merge_train_policy import (
    MergeTrainPolicyRecord,
    build_merge_train_policy_record_id,
    parse_merge_train_policy_toml,
)
from control_plane.merge_train_policy_source import MergeTrainPolicyStoreMissingError
from control_plane.merge_train_policy_source import resolve_merge_train_policy_record
from tests.merge_train_policy_fixtures import build_test_merge_train_policy
from tests.merge_train_policy_fixtures import build_test_merge_train_policy_with_codex_skills


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

        summary = control_plane_cli._summarize_merge_train_policy_record(record)

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

    def test_cli_choice_normalizers_share_trimmed_case_insensitive_choices(self) -> None:
        self.assertEqual(control_plane_cli._normalize_secret_scope(" Context "), "context")
        self.assertEqual(control_plane_cli._normalize_odoo_apply_status(" PASS "), "pass")
        self.assertEqual(
            control_plane_cli._normalize_odoo_prod_rollback_source_channel(" testing "),
            "testing",
        )
        self.assertEqual(
            control_plane_cli._normalize_dokploy_target_type(" APPLICATION "),
            "application",
        )

    def test_cli_choice_normalizer_preserves_domain_error_messages(self) -> None:
        invalid_cases = [
            (
                control_plane_cli._normalize_secret_scope,
                "environment",
                "Secret scope must be one of global, context, or context_instance.",
            ),
            (
                control_plane_cli._normalize_odoo_apply_status,
                "success",
                "Odoo override apply status must be skipped, pending, pass, or fail.",
            ),
            (
                control_plane_cli._normalize_odoo_prod_rollback_source_channel,
                "prod",
                "Odoo prod rollback source channel must be testing.",
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

        payload = control_plane_cli._first_driver_payload(view, driver_id="verireel")

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

        payload = control_plane_cli._first_driver_payload(view, driver_id="verireel")

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
                main,
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
