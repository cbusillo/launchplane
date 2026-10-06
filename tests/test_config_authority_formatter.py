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
        result = self.gate('[tool.black]\ntarget-version = ["py313", "py314"]\n')
        self.assertEqual(result["status"], "pass", result)

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
