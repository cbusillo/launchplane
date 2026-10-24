"""Exercise the existing bounded-client contract for record sanitization."""

from tests.support import bounded_testing_client as support


class TestingOverrideSanitizationClientTests(support.BoundedTestingClientContract):
    script_filename = "sanitize-testing-overrides.py"

    def setUp(self) -> None:
        super().setUp()
        self.receipt["result"].update(generation_preserved=True, copy_preserved=True)

    def test_receipt_without_preservation_proof_is_not_saved(self) -> None:
        self.receipt["result"]["generation_preserved"] = False
        self.assertEqual(self.call("dry-run")[0], 1)
        self.assertFalse(self.evidence.exists())
