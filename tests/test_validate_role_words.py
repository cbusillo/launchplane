import importlib.util
import unittest
from pathlib import Path


_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "validate_role_words.py"
_SPEC = importlib.util.spec_from_file_location("validate_role_words", _SCRIPT_PATH)
if _SPEC is None or _SPEC.loader is None:  # pragma: no cover - importlib guard
    raise RuntimeError(f"Could not load role-words check from {_SCRIPT_PATH}")
validate_role_words = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(validate_role_words)


class RoleWordsMarkdownTests(unittest.TestCase):
    def test_reports_role_sense_words_in_prose(self) -> None:
        text = (
            "# Release\n\n"
            "The site Owner accepts the release.\n"
            "Ask the operator before a grant.\n"
            "Only a policy administrator can apply it.\n"
        )

        self.assertEqual(
            validate_role_words.findings(text),
            [(3, "Owner"), (4, "operator"), (5, "policy administrator")],
        )

    def test_allows_repository_sense_code_links_and_wrapped_qualifiers(self) -> None:
        text = (
            "---\n"
            "argv: owner operator\n"
            "---\n"
            "The repository owner and the code owner review `owner_review` records.\n"
            "Pass `OWNER/REPO` or owner/repo, and read [the guide](docs/owner-acceptance.md).\n"
            "Only the GitHub organization\n"
            "owner can change it. <!-- operator note -->\n"
            "```text\n"
            "operator output\n"
            "```\n"
        )

        self.assertEqual(validate_role_words.findings(text), [])

    def test_reports_role_words_in_frontend_text(self) -> None:
        self.assertEqual(
            validate_role_words.text_findings("No Owner set. Ask the operator for owner/repo."),
            ["Owner", "operator"],
        )


if __name__ == "__main__":
    unittest.main()
