import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from click.testing import CliRunner

from control_plane.config_authority_audit import (
    MAX_SCANNED_FILE_BYTES,
    build_config_authority_audit,
    evaluate_config_authority_gate,
)
from tests.test_config_authority_audit import CLI_MAIN, _commit_all, _git, _init_repo


INVALID_CONSUMER_SOURCE = (
    'PRODUCT_DOMAIN = "live.example"\n'
    "def read_timestamp(value):\n"
    "    try:\n"
    "        return int(value)\n"
    "    except (OSError, ValueError)\n"
    "        return None\n"
)


class PythonCoverageGateTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        _init_repo(self.root)
        self.source = self.root / "consumer/utils/datetime.py"
        self.source.parent.mkdir(parents=True)
        self.source.write_text("def read_timestamp(value):\n    return int(value)\n")
        _commit_all(self.root)
        self.base = _git(self.root, "rev-parse", "HEAD")

    def commit(self) -> str:
        _commit_all(self.root)
        return _git(self.root, "rev-parse", "HEAD")

    def audit(self, head: str) -> dict[str, object]:
        return build_config_authority_audit(
            control_plane_root=self.root,
            mode="changed-files-gate",
            base_sha=self.base,
            head_sha=head,
        )

    def test_unparsed_committed_authority_fails_both_profiles_despite_dirty_repair(self) -> None:
        self.source.write_text(INVALID_CONSUMER_SOURCE)
        head = self.commit()
        self.source.write_text("# repaired locally, not committed\n")
        audit = self.audit(head)
        self.assertEqual(audit["findings"], [])
        for profile in ("default", "product-repo"):
            with self.subTest(profile=profile):
                gate = evaluate_config_authority_gate(audit, profile=profile)
                self.assertEqual(gate["status"], "fail")
                self.assertEqual(gate["rejected_findings"], [])
                gaps = gate["rejected_coverage_gaps"]
                assert isinstance(gaps, list)
                self.assertEqual([gap["path"] for gap in gaps], ["consumer/utils/datetime.py"])
                self.assertEqual(gaps[0]["reason"], "parse_failure")
                self.assertEqual(
                    gaps[0]["rejection_reason"], "python_authority_coverage_incomplete"
                )

    def test_existing_parse_gap_is_not_grandfathered_in_an_edited_file(self) -> None:
        self.source.write_text(INVALID_CONSUMER_SOURCE)
        self.base = self.commit()
        self.source.write_text(INVALID_CONSUMER_SOURCE + "# intended formatter edit\n")
        gate = evaluate_config_authority_gate(self.audit(self.commit()), profile="product-repo")
        self.assertEqual(gate["status"], "fail")
        self.assertTrue(gate["rejected_coverage_gaps"])

    def test_unchanged_unparsed_source_is_outside_the_changed_file_gate(self) -> None:
        self.source.write_text(INVALID_CONSUMER_SOURCE)
        self.base = self.commit()
        (self.root / "helper.py").write_text("def normalize(value):\n    return str(value)\n")
        gate = evaluate_config_authority_gate(self.audit(self.commit()), profile="product-repo")
        self.assertEqual(gate["status"], "pass")
        self.assertEqual(gate["rejected_coverage_gaps"], [])

    def test_repaired_source_is_scanned_and_real_authority_still_fails(self) -> None:
        self.source.write_text(INVALID_CONSUMER_SOURCE)
        self.base = self.commit()
        for authority in (True, False):
            with self.subTest(authority=authority):
                repaired = INVALID_CONSUMER_SOURCE.replace(
                    "except (OSError, ValueError)\n", "except (OSError, ValueError):\n"
                )
                if not authority:
                    repaired = repaired.split("\n", 1)[1]
                self.source.write_text(repaired)
                gate = evaluate_config_authority_gate(
                    self.audit(self.commit()), profile="product-repo"
                )
                self.assertEqual(gate["status"], "fail" if authority else "pass")
                self.assertEqual(bool(gate["rejected_findings"]), authority)
                self.assertEqual(gate["rejected_coverage_gaps"], [])

    def test_cli_reports_coverage_failure_and_preserves_report_only_mode(self) -> None:
        self.source.write_text(INVALID_CONSUMER_SOURCE)
        head = self.commit()
        args = [
            "service",
            "audit-config-authority",
            "--control-plane-root",
            str(self.root),
            "--mode",
            "changed-files-gate",
            "--base-sha",
            self.base,
            "--head-sha",
            head,
            "--gate-profile",
            "product-repo",
        ]
        for output_format in ("json", "markdown"):
            with self.subTest(output_format=output_format):
                result = CliRunner().invoke(
                    CLI_MAIN, [*args, "--format", output_format, "--fail-on-findings"]
                )
                self.assertNotEqual(result.exit_code, 0, result.output)
                self.assertIn("incomplete Python authority coverage", result.output)
                self.assertIn("python-version", result.output)
                self.assertIn("consumer/utils/datetime.py", result.stdout)
                self.assertIn("parse_failure", result.stdout)
                if output_format == "json":
                    gate = json.loads(result.stdout)["gate"]
                    self.assertEqual(gate["status"], "fail")
                    self.assertEqual(gate["rejected_finding_count"], 0)
                    self.assertEqual(gate["rejected_coverage_gap_count"], 1)
        report = CliRunner().invoke(CLI_MAIN, args)
        self.assertEqual(report.exit_code, 0, report.output)
        self.assertNotIn("gate", json.loads(report.stdout))

    def test_other_documented_coverage_gaps_remain_report_only(self) -> None:
        (self.root / "unsupported.rs").write_text("fn main() {}\n")
        (self.root / "large.py").write_bytes(b" " * (MAX_SCANNED_FILE_BYTES + 1))
        (self.root / "binary.py").write_bytes(b"invalid\x00content")
        (self.root / "encoding.env").write_bytes(b"# invalid \xff\n")
        (self.root / "settings.json").write_text('{"unfinished":')
        (self.root / "settings.toml").write_text("unfinished = [")
        (self.root / "build.yml").write_text("name: Build\n")
        audit = self.audit(self.commit())
        coverage = audit["coverage"]
        assert isinstance(coverage, dict)
        self.assertEqual(
            {gap["reason"] for gap in coverage["gaps"]},
            {
                "unscanned_file_class",
                "skipped_large_file",
                "skipped_binary_file",
                "parse_failure",
                "decode_failure",
                "parser_limitation",
            },
        )
        gate = evaluate_config_authority_gate(audit, profile="product-repo")
        self.assertEqual(gate["status"], "pass")
        self.assertEqual(gate["rejected_coverage_gaps"], [])

    def test_python_encoding_declarations_are_scanned_and_invalid_bytes_fail_closed(self) -> None:
        for content, reason in (
            (b'# coding: latin-1\nPRODUCT_DOMAIN = "live.example"\n# caf\xe9\n', None),
            (b'PRODUCT_DOMAIN = "live.example"\n# invalid \xff\n', "decode_failure"),
            (b'# coding: missing-codec\nPRODUCT_DOMAIN = "live.example"\n', "decode_failure"),
        ):
            with self.subTest(content=content):
                self.source.write_bytes(content)
                head = self.commit()
                for mode in ("changed-files-gate", "full-audit"):
                    with self.subTest(mode=mode):
                        audit = (
                            self.audit(head)
                            if mode == "changed-files-gate"
                            else (build_config_authority_audit(control_plane_root=self.root))
                        )
                        gate = evaluate_config_authority_gate(audit, profile="product-repo")
                        self.assertEqual(gate["status"], "fail")
                        gaps = gate["rejected_coverage_gaps"]
                        assert isinstance(gaps, list)
                        if reason:
                            self.assertEqual(gaps[0]["reason"], reason)
                            self.assertEqual(gate["rejected_findings"], [])
                        else:
                            self.assertEqual(gaps, [])
                            self.assertTrue(gate["rejected_findings"])
