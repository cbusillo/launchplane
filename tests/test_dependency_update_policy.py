import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import cast
from unittest import TestCase

from tests.support.workflows import load_workflow


class DependencyUpdatePolicyTests(TestCase):
    def test_ruff_policy_preserves_the_pre_016_default_rules(self) -> None:
        pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

        self.assertEqual(
            ["E4", "E7", "E9", "F"],
            pyproject["tool"]["ruff"]["lint"]["select"],
        )

    def test_python_toolchain_updates_are_isolated_from_routine_updates(self) -> None:
        dependabot = load_workflow(".github/dependabot.yml")
        updates_value = dependabot.data.get("updates")
        if not isinstance(updates_value, list):
            self.fail("Dependabot updates must be a list")
        updates = updates_value
        uv_update = next(
            update
            for update in updates
            if isinstance(update, dict) and update.get("package-ecosystem") == "uv"
        )
        groups = cast(Mapping[str, object], uv_update["groups"])

        self.assertEqual(
            {"patterns": ["ruff"], "update-types": ["minor", "patch"]},
            groups["ruff-version-updates"],
        )
        self.assertEqual(
            {"patterns": ["mypy"], "update-types": ["minor", "patch"]},
            groups["mypy-version-updates"],
        )
        routine_group = cast(Mapping[str, object], groups["routine-version-updates"])
        self.assertEqual(["ruff", "mypy"], routine_group["exclude-patterns"])
