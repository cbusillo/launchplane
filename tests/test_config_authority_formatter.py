import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from control_plane.config_authority_audit import (
    build_config_authority_audit,
    evaluate_config_authority_gate,
)
from tests.test_config_authority_audit import _commit_all, _git, _init_repo


class FormatterTargetGateTests(unittest.TestCase):
    @staticmethod
    def gate(text: str, path: str = "pyproject.toml") -> dict[str, object]:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _init_repo(root)
            (root / path).parent.mkdir(parents=True, exist_ok=True)
            (root / path).write_text("# baseline\n")
            _commit_all(root)
            base = _git(root, "rev-parse", "HEAD")
            (root / path).write_text(text)
            _commit_all(root)
            audit = build_config_authority_audit(
                control_plane_root=root,
                mode="changed-files-gate",
                base_sha=base,
                head_sha=_git(root, "rev-parse", "HEAD"),
            )
            return evaluate_config_authority_gate(audit, profile="product-repo")

    def test_black_target_list_is_formatter_configuration(self) -> None:
        for path in ("pyproject.toml", "package/pyproject.toml"):
            with self.subTest(path=path):
                result = self.gate('[tool.black]\ntarget-version = ["py313", "py314"]\n', path)
                self.assertEqual(result["status"], "pass", result)

    def test_ruff_python_targets_are_developer_configuration(self) -> None:
        for text, path in (
            ('[tool.ruff]\ntarget-version = "py314"\n', "pyproject.toml"),
            ('target-version = "py314"\n', "ruff.toml"),
            ('target-version = "py314"\n', ".ruff.toml"),
            ('[tool.ruff.per-file-target-version]\n"app.py" = "py314"\n', "pyproject.toml"),
            ('[per-file-target-version]\n"app.py" = "py314"\n', "ruff.toml"),
            ('[tool.ruff]\ntarget-version = "py314"\n', "package/pyproject.toml"),
            ('target-version = "py314"\n', "package/.ruff.toml"),
        ):
            with self.subTest(text=text, path=path):
                self.assertEqual(self.gate(text, path)["status"], "pass")

    def test_malformed_and_runtime_targets_remain_rejected(self) -> None:
        for value in (
            "py399",
            "py314-extra",
            " py314",
            "dokploy-production",
            "example/product",
            "https://runtime.example.invalid",
            "${{ secrets.RUNTIME_TOKEN }}",
            314,
            True,
        ):
            with self.subTest(value=value):
                literal = json.dumps(value)
                result = self.gate(f'[tool.black]\ntarget-version = ["py314", {literal}]\n')
                self.assertEqual(result["status"], "fail", result)

    def test_allowance_requires_exact_file_and_list_field(self) -> None:
        for text, path in (
            ('[tool.black]\ntarget-version = ["py314"]\n', "settings.toml"),
            ('[tool.other]\ntarget-version = ["py314"]\n', "pyproject.toml"),
            ('[tool.black.runtime]\ntarget-version = ["py314"]\n', "pyproject.toml"),
            ('[tool.black]\nprovider-target = ["py314"]\n', "pyproject.toml"),
            ('[tool.black]\ntarget-version = "py314"\n', "pyproject.toml"),
        ):
            with self.subTest(text=text, path=path):
                result = self.gate(text, path)
                self.assertEqual(result["status"], "fail", result)

    def test_formatter_allowance_does_not_hide_sibling_authority(self) -> None:
        result = self.gate(
            '[tool.black]\ntarget-version = ["py314"]\n'
            'provider-target = "dokploy-production"\ncredential-token = "private-value"\n'
        )
        self.assertEqual(result["status"], "fail", result)
        rejected = json.dumps(result["rejected_findings"])
        self.assertIn("provider-target", rejected)
        self.assertIn("credential-token", rejected)

    def test_ruff_allowance_keeps_runtime_values_and_other_fields_rejected(self) -> None:
        for text, path in (
            ('[tool.ruff]\ntarget-version = "dokploy-production"\n', "pyproject.toml"),
            ('[per-file-target-version]\n"app.py" = "example/product"\n', ".ruff.toml"),
        ):
            with self.subTest(text=text, path=path):
                self.assertEqual(self.gate(text, path)["status"], "fail")
        for text, path, rejected_key in (
            (
                '[tool.ruff]\ntarget-version = "py314"\nprovider-target = "live"\n',
                "pyproject.toml",
                "tool.ruff.provider-target",
            ),
            (
                'target-version = "py314"\ncredential-token = "private-value"\n',
                "ruff.toml",
                "credential-token",
            ),
        ):
            with self.subTest(path=path, rejected_key=rejected_key):
                result = self.gate(text, path)
                self.assertEqual(result["status"], "fail")
                rejected = result["rejected_findings"]
                assert isinstance(rejected, list)
                self.assertEqual([finding["key"] for finding in rejected], [rejected_key])
