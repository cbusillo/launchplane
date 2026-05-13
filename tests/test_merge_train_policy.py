import json
import os
import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from click.testing import CliRunner
from pydantic import ValidationError

from control_plane.cli import main
from control_plane.contracts.merge_train_policy import (
    build_sellyouroutboard_main_merge_train_policy,
    load_merge_train_policy_from_default_config,
    parse_merge_train_policy_toml,
)
from control_plane.merge_train_policy_source import load_launchplane_merge_train_policy


class MergeTrainPolicyTests(unittest.TestCase):
    def test_sellyouroutboard_main_policy_is_initial_smoke_target(self) -> None:
        policy = build_sellyouroutboard_main_merge_train_policy()

        repository_policy = policy.find_repository_policy(
            repository="cbusillo/sellyouroutboard", base_branch="main"
        )

        self.assertEqual(repository_policy.enqueue_label, "ready-to-merge")
        self.assertEqual(repository_policy.blocked_label, "merge-blocked")
        self.assertEqual(repository_policy.merge_method, "merge")
        self.assertEqual(repository_policy.failure_policy, "pause_train")
        self.assertTrue(repository_policy.enqueue.label_required)
        self.assertEqual(
            repository_policy.enqueue.allowed_actor_roles, ("repo_owner", "repo_admin")
        )
        self.assertEqual(repository_policy.merge_identity.kind, "github_actions_oidc")
        self.assertEqual(repository_policy.merge_identity.name, "launchplane-merge-train")
        self.assertEqual(repository_policy.service_authz.action, "merge_train.run_once")
        self.assertEqual(repository_policy.service_authz.product, "launchplane")
        self.assertEqual(repository_policy.service_authz.context, "launchplane")
        self.assertEqual(repository_policy.github_token.env_var, "GH_TOKEN")
        self.assertEqual(len(policy.policy_sha256), 64)

    def test_default_config_includes_smoke_and_codex_skills_policies(self) -> None:
        policy = load_merge_train_policy_from_default_config()

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
        self.assertEqual(codex_skills_policy.merge_method, "merge")
        self.assertEqual(codex_skills_policy.github_token.env_var, "GH_TOKEN")
        self.assertEqual(codex_skills_policy.service_authz.action, "merge_train.run_once")
        self.assertEqual(codex_skills_policy.service_authz.product, "launchplane")
        self.assertEqual(codex_skills_policy.service_authz.context, "launchplane")

    def test_launchplane_policy_source_prefers_env_toml(self) -> None:
        with patch.dict(
            os.environ,
            {"LAUNCHPLANE_MERGE_TRAIN_POLICY_TOML": _policy_toml("example/app")},
            clear=True,
        ):
            policy = load_launchplane_merge_train_policy()

        self.assertEqual(len(policy.policies), 1)
        self.assertEqual(policy.policies[0].policy_key, "example/app:main")

    def test_launchplane_policy_source_uses_env_file(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            policy_file = Path(temporary_directory_name) / "merge-train-policies.toml"
            policy_file.write_text(_policy_toml("example/file-app"), encoding="utf-8")
            with patch.dict(
                os.environ,
                {"LAUNCHPLANE_MERGE_TRAIN_POLICY_FILE": str(policy_file)},
                clear=True,
            ):
                policy = load_launchplane_merge_train_policy()

        self.assertEqual(len(policy.policies), 1)
        self.assertEqual(policy.policies[0].policy_key, "example/file-app:main")

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

    def test_work_graph_merge_train_policy_cli_renders_dry_run_contract(self) -> None:
        result = CliRunner().invoke(
            main,
            [
                "work-graph",
                "merge-train-policy",
                "--repository",
                "cbusillo/sellyouroutboard",
                "--base-branch",
                "main",
            ],
        )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["repository_count"], 2)
        self.assertEqual(payload["selected_policy"]["repository"], "cbusillo/sellyouroutboard")
        self.assertEqual(payload["selected_policy"]["base_branch"], "main")


def _policy_toml(repository: str) -> str:
    return textwrap.dedent(
        f"""
        schema_version = 1

        [[policies]]
        repository = "{repository}"
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
        [policies.github_token]
        env_var = "GH_TOKEN"
        """
    ).strip()


if __name__ == "__main__":
    unittest.main()
