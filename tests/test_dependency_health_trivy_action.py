from pathlib import Path
import unittest


ACTION_METADATA = Path(".github/actions/dependency-health-trivy/action.yml")


class DependencyHealthTrivyActionTests(unittest.TestCase):
    def test_action_is_composite_and_uses_immutable_uv_setup(self) -> None:
        metadata = ACTION_METADATA.read_text(encoding="utf-8")

        self.assertIn("using: composite", metadata)
        self.assertIn(
            "astral-sh/setup-uv@37802adc94f370d6bfd71619e3f0bf239e1f3b78 # v7",
            metadata,
        )
        self.assertNotIn("astral-sh/setup-uv@v7", metadata)

    def test_action_normalizes_both_reports_and_runs_comparison(self) -> None:
        metadata = ACTION_METADATA.read_text(encoding="utf-8")

        self.assertEqual(metadata.count("dependency-health trivy-snapshot"), 2)
        self.assertIn("dependency-health compare", metadata)
        self.assertIn("--target-advisory-text-file", metadata)
        self.assertIn("--target-advisory-id", metadata)
        self.assertIn('[[ "${output_directory}" =~ [[:cntrl:]] ]]', metadata)


if __name__ == "__main__":
    unittest.main()
