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
                    "records = [r for r in store.list_secret_records(\n"
                    "    integration=DIAGNOSTIC_SECRET_INTEGRATION)\n"
                    "    if r.integration == DIAGNOSTIC_SECRET_INTEGRATION]\n"
                    "bindings = [b for b in store.list_secret_bindings(\n"
                    "    integration=DIAGNOSTIC_SECRET_INTEGRATION)\n"
                    "    if DIAGNOSTIC_SECRET_INTEGRATION == b.integration]\n"
                    "secrets = payload.get('secrets')\n"
                    "for secret in secrets:\n"
                    "    secret.get('integration') != DIAGNOSTIC_SECRET_INTEGRATION\n",
                ),
                (
                    "DIAGNOSTIC_IDENTITY_KEY",
                    "SYNTHETIC_DIAGNOSTIC_IDENTITY",
                    "bindings = [b for b in store.list_secret_bindings()\n"
                    "    if b.binding_key == DIAGNOSTIC_IDENTITY_KEY]\n"
                    "secrets = payload.get('secrets')\n"
                    "for secret in secrets:\n"
                    "    secret.get('binding_key') != DIAGNOSTIC_IDENTITY_KEY\n",
                ),
                (
                    "ODOO_READ_REJECTED",
                    "synthetic_read_rejected",
                    ERROR_CLASS + "\ntry:\n"
                    "    raise DiagnosticError(ODOO_READ_REJECTED, 'Rejected')\n"
                    "except DiagnosticError as error:\n"
                    "    error.code != ODOO_READ_REJECTED\n",
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
        comparison = (
            "for record in store.list_secret_records():\n"
            "    record.integration == DIAGNOSTIC_SECRET_INTEGRATION\n"
        )
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
            "from settings import *\n",
            "match payload:\n    case {'x': DIAGNOSTIC_SECRET_INTEGRATION}:\n        pass\n",
        ):
            with self.subTest(extra=extra):
                audit = self.audit(declaration + comparison + extra)
                self.assertEqual(evaluate_config_authority_gate(audit)["status"], "fail")
                self.assertTrue(
                    all(f["classification"] == "needs_classification" for f in _findings(audit))
                )

    def test_unrelated_field_receivers_are_not_protocol_proof(self) -> None:
        for name, value, comparison in (
            ("DEFAULT_TENANT", "synthetic_tenant", "tenant.code"),
            ("ODOO_DATABASE", "production", "context.get('code')"),
            ("PROVIDER_TARGET", "dokploy_prod", "target.code"),
            ("AUTHZ_SUBJECT", "synthetic_agent", "subject.integration"),
            ("DIAGNOSTIC_IDENTITY_KEY", "real_database", "database.binding_key"),
        ):
            with self.subTest(name=name):
                audit = self.audit(f'{name} = "{value}"\n{comparison} == {name}\n')
                self.assertEqual(evaluate_config_authority_gate(audit)["status"], "fail")

    def test_selectors_without_comparison_are_not_proof(self) -> None:
        for source in (
            'DIAGNOSTIC_SECRET_INTEGRATION = "synthetic_diagnostics"\n'
            "store.list_secret_records(integration=DIAGNOSTIC_SECRET_INTEGRATION)\n",
            'ODOO_READ_REJECTED = "synthetic_rejected"\n'
            + ERROR_CLASS
            + "raise DiagnosticError(ODOO_READ_REJECTED, 'Rejected')\n",
        ):
            with self.subTest(source=source):
                audit = self.audit(source)
                self.assertEqual(evaluate_config_authority_gate(audit)["status"], "fail")

    def test_ambiguous_error_constructors_remain_rejected(self) -> None:
        for error_class in (
            ERROR_CLASS.replace("(ValueError)", ""),
            ERROR_CLASS.replace("self.code = code", "self.message = code"),
            ERROR_CLASS.replace("code, message", "subject, message").replace("= code", "= subject"),
            ERROR_CLASS.replace(
                "self.code = code", "self.code = code\n        self.subject = code"
            ),
            ERROR_CLASS + "\nDiagnosticError = configure\n",
        ):
            with self.subTest(error_class=error_class):
                audit = self.audit(
                    'ODOO_READ_REJECTED = "synthetic_rejected"\n'
                    + error_class
                    + "\ntry:\n    raise DiagnosticError(ODOO_READ_REJECTED, 'Rejected')\n"
                    + "except DiagnosticError as error:\n    error.code == ODOO_READ_REJECTED\n"
                )
                self.assertEqual(evaluate_config_authority_gate(audit)["status"], "fail")

    def test_rebound_secret_receiver_does_not_supply_proof(self) -> None:
        audit = self.audit(
            'DIAGNOSTIC_IDENTITY_KEY = "synthetic_database"\n'
            "for binding in store.list_secret_bindings():\n"
            "    binding = runtime_database\n"
            "    binding.binding_key == DIAGNOSTIC_IDENTITY_KEY\n"
        )
        self.assertEqual(evaluate_config_authority_gate(audit)["status"], "fail")

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
                    + "for binding in store.list_secret_bindings():\n"
                    + "    binding.binding_key == DIAGNOSTIC_IDENTITY_KEY\n"
                )
                findings = _findings(audit)
                self.assertTrue(findings)
                for finding in findings:
                    self.assertEqual(
                        finding["classification"],
                        "allowed" if finding["line"] == 1 else "needs_classification",
                    )
                self.assertEqual(evaluate_config_authority_gate(audit)["status"], "fail")
