"""Exercise the existing bounded-client contract for record sanitization."""

from pathlib import Path
import runpy

from tests import test_testing_public_ingress_client as client_tests


class TestingOverrideSanitizationClientTests(client_tests.TestingPublicIngressClientTests):
    def setUp(self) -> None:
        super().setUp()
        self.client = runpy.run_path(
            str(Path(__file__).resolve().parents[1] / "scripts/sanitize-testing-overrides.py")
        )
        self.receipt["result"].update(generation_preserved=True, copy_preserved=True)

    def test_receipt_without_preservation_proof_is_not_saved(self) -> None:
        self.receipt["result"]["generation_preserved"] = False
        self.assertEqual(self.call("dry-run")[0], 1)
        self.assertFalse(self.evidence.exists())
