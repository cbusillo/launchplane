from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from control_plane.config_authority_audit import (
    ALLOW_REASON_SCHEMA_ONLY,
    build_config_authority_audit,
    evaluate_config_authority_gate,
)
from tests.test_config_authority_audit import _commit_all, _findings, _git, _init_repo


ERROR_CLASS = """
class DiagnosticError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)
"""


class ProtocolIdentifierTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        _init_repo(self.root)
        (self.root / "README.md").write_text("Synthetic protocol scanner fixture.\n")
        _commit_all(self.root)
        self.base = _git(self.root, "rev-parse", "HEAD")

    def audit(self, source: str) -> dict[str, object]:
        path = self.root / "diagnostics.py"
        path.write_text(source)
        _commit_all(self.root)
        head = _git(self.root, "rev-parse", "HEAD")
        path.write_text('DIAGNOSTIC_SECRET_INTEGRATION = "dirty_runtime"\n')
        return build_config_authority_audit(
            control_plane_root=self.root,
            mode="changed-files-gate",
            base_sha=self.base,
            head_sha=head,
        )

    def test_protocol_uses_classify_committed_declarations(self) -> None:
        for declaration in (" = ", ": str = "):
            for name, value, usage in (
                (
                    "DIAGNOSTIC_SECRET_INTEGRATION",
                    "synthetic_diagnostics",
                    "store.list_secret_records(integration=DIAGNOSTIC_SECRET_INTEGRATION)\n"
                    "store.list_secret_bindings(integration=DIAGNOSTIC_SECRET_INTEGRATION)\n"
                    "record.integration == DIAGNOSTIC_SECRET_INTEGRATION\n"
                    "secret.get('integration') != DIAGNOSTIC_SECRET_INTEGRATION\n",
                ),
                (
                    "DIAGNOSTIC_IDENTITY_KEY",
                    "SYNTHETIC_DIAGNOSTIC_IDENTITY",
                    "binding.binding_key == DIAGNOSTIC_IDENTITY_KEY\n"
                    "secret.get('binding_key') != DIAGNOSTIC_IDENTITY_KEY\n",
                ),
                (
                    "ODOO_READ_REJECTED",
                    "synthetic_read_rejected",
                    ERROR_CLASS + "\nerror.code != ODOO_READ_REJECTED\n"
                    "raise DiagnosticError(ODOO_READ_REJECTED, 'Rejected')\n",
                ),
            ):
                with self.subTest(name=name, declaration=declaration):
                    audit = self.audit(f'{name}{declaration}"{value}"\n' + usage)
                    findings = _findings(audit)
                    self.assertTrue(findings)
                    self.assertTrue(
                        all(f["allow_reason"] == ALLOW_REASON_SCHEMA_ONLY for f in findings)
                    )
                    self.assertEqual(evaluate_config_authority_gate(audit)["status"], "pass")

    def test_runtime_and_grant_collisions_remain_rejected(self) -> None:
        declaration = 'DIAGNOSTIC_SECRET_INTEGRATION = "synthetic_diagnostics"\n'
        comparison = "record.integration == DIAGNOSTIC_SECRET_INTEGRATION\n"
        for extra in (
            'grant = {"subject": DIAGNOSTIC_SECRET_INTEGRATION}\n',
            'grant = {"actions": [DIAGNOSTIC_SECRET_INTEGRATION]}\n',
            "configure(product=DIAGNOSTIC_SECRET_INTEGRATION)\n",
            "configure(database=DIAGNOSTIC_SECRET_INTEGRATION)\n",
            "configure(login=DIAGNOSTIC_SECRET_INTEGRATION)\n",
            "configure(password=DIAGNOSTIC_SECRET_INTEGRATION)\n",
            "configure(integration=DIAGNOSTIC_SECRET_INTEGRATION)\n",
            "record.code == DIAGNOSTIC_SECRET_INTEGRATION\n",
            "alias = DIAGNOSTIC_SECRET_INTEGRATION\n",
            'DIAGNOSTIC_SECRET_INTEGRATION = "different"\n',
            "def shadow(DIAGNOSTIC_SECRET_INTEGRATION):\n    pass\n",
            "from settings import DIAGNOSTIC_SECRET_INTEGRATION\n",
        ):
            with self.subTest(extra=extra):
                audit = self.audit(declaration + comparison + extra)
                self.assertEqual(evaluate_config_authority_gate(audit)["status"], "fail")
                self.assertEqual(_findings(audit)[0]["classification"], "needs_classification")

    def test_name_and_identifier_shape_alone_supply_no_proof(self) -> None:
        for source in (
            'DIAGNOSTIC_SECRET_INTEGRATION = "synthetic_diagnostics"\n',
            'DIAGNOSTIC_IDENTITY_KEY = "SYNTHETIC_IDENTITY"\n',
            'ODOO_READ_REJECTED = "synthetic_rejected"\n',
            'DIAGNOSTIC_IDENTITY_KEY = "https://runtime.example.test"\n'
            "binding.binding_key == DIAGNOSTIC_IDENTITY_KEY\n",
            'DIAGNOSTIC_IDENTITY_KEY = "owner/repository"\n'
            "binding.binding_key == DIAGNOSTIC_IDENTITY_KEY\n",
            'ODOO_READ_REJECTED = "synthetic_rejected"\nconfigure(code=ODOO_READ_REJECTED)\n',
            'class Settings:\n    DIAGNOSTIC_IDENTITY_KEY = "SYNTHETIC_IDENTITY"\n'
            "binding.binding_key == Settings.DIAGNOSTIC_IDENTITY_KEY\n",
        ):
            with self.subTest(source=source):
                audit = self.audit(source)
                # Completion is not the nested enforcement verdict.
                self.assertEqual(audit["status"], "ok")
                self.assertEqual(evaluate_config_authority_gate(audit)["status"], "fail")

    def test_same_named_runtime_assignments_do_not_inherit_proof(self) -> None:
        for target in ("settings.DIAGNOSTIC_IDENTITY_KEY", 'os.environ["DIAGNOSTIC_IDENTITY_KEY"]'):
            with self.subTest(target=target):
                audit = self.audit(
                    'DIAGNOSTIC_IDENTITY_KEY = "SYNTHETIC_IDENTITY"\n'
                    + f'{target} = "SYNTHETIC_IDENTITY"\n'
                    + "binding.binding_key == DIAGNOSTIC_IDENTITY_KEY\n"
                )
                findings = _findings(audit)
                self.assertTrue(findings)
                for finding in findings:
                    self.assertEqual(
                        finding["classification"],
                        "allowed" if finding["line"] == 1 else "needs_classification",
                    )
                self.assertEqual(evaluate_config_authority_gate(audit)["status"], "fail")
