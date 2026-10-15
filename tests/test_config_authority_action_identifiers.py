from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from control_plane.config_authority_audit import (
    ALLOW_REASON_SCHEMA_ONLY,
    build_config_authority_audit,
    evaluate_config_authority_gate,
)
from tests.test_config_authority_audit import _commit_all, _findings, _git, _init_repo


class AuthorizationActionIdentifierTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        _init_repo(self.root)
        (self.root / "README.md").write_text("Synthetic scanner fixture.\n")
        _commit_all(self.root)
        self.base = _git(self.root, "rev-parse", "HEAD")
        self.source = self.root / "permissions.py"

    def audit(self, source: str) -> dict[str, object]:
        (self.root / "untracked.py").unlink(missing_ok=True)
        self.source.write_text(source)
        _commit_all(self.root)
        head = _git(self.root, "rev-parse", "HEAD")
        # Dirty and untracked declarations must never supply classification proof.
        self.source.write_text('OBSERVATION_AUTHZ_ACTION = "runtime-subject"\n')
        (self.root / "untracked.py").write_text('AUTHZ_SUBJECT = "another-subject"\n')
        return build_config_authority_audit(
            control_plane_root=self.root,
            mode="changed-files-gate",
            base_sha=self.base,
            head_sha=head,
        )

    def test_fixed_action_used_by_authorization_check_is_schema(self) -> None:
        for declaration in (
            'OBSERVATION_AUTHZ_ACTION = "synthetic_service.inspect"',
            'OBSERVATION_AUTHZ_ACTION: str = "synthetic_service.inspect"',
        ):
            with self.subTest(declaration=declaration):
                audit = self.audit(
                    declaration
                    + "\ndef observe(authorization_allows):\n"
                    + "    return authorization_allows(action=OBSERVATION_AUTHZ_ACTION)\n"
                )
                findings = _findings(audit)
                self.assertTrue(findings)
                self.assertTrue(
                    all(finding["allow_reason"] == ALLOW_REASON_SCHEMA_ONLY for finding in findings)
                )
                self.assertEqual(evaluate_config_authority_gate(audit)["status"], "pass")

    def test_action_name_does_not_allow_runtime_authority(self) -> None:
        declaration = 'OBSERVATION_AUTHZ_ACTION = "synthetic_service.inspect"\n'
        check = "authorization_allows(action=OBSERVATION_AUTHZ_ACTION)\n"
        for source in (
            'AUTHZ_SUBJECT = "synthetic_service.inspect"\n',
            'AUTHZ_PRODUCT = "synthetic-product"\n',
            'AUTHZ_GRANTS = {"subject": "synthetic-agent", "actions": ["service.read"]}\n',
            declaration,
            declaration + 'OBSERVATION_AUTHZ_ACTION = "other_service.read"\n' + check,
            declaration + 'grant = {"subject": OBSERVATION_AUTHZ_ACTION}\n' + check,
            declaration + 'grant = {"actions": [OBSERVATION_AUTHZ_ACTION]}\n' + check,
            declaration + "configure(subject=OBSERVATION_AUTHZ_ACTION)\n",
            'OBSERVATION_AUTHZ_ACTION = "synthetic-agent"\n' + check,
            'OBSERVATION_AUTHZ_ACTION = "service.*"\n' + check,
            'OBSERVATION_AUTHZ_ACTION = "owner/repository"\n' + check,
            'class Settings:\n    OBSERVATION_AUTHZ_ACTION = "service.read"\n',
        ):
            with self.subTest(source=source):
                audit = self.audit(source)
                self.assertEqual(evaluate_config_authority_gate(audit)["status"], "fail")
                self.assertTrue(
                    any(
                        finding["classification"] == "needs_classification"
                        for finding in _findings(audit)
                    )
                )


if __name__ == "__main__":
    unittest.main()
