import unittest

from control_plane.operation_status_read import safe_operation_error_code


class SafeOperationErrorCodeTests(unittest.TestCase):
    def test_keeps_codes_launchplane_writes(self) -> None:
        for code in ("deploy_failed", "plan_not_ready.runtime_keys_undeclared", ""):
            with self.subTest(code=code):
                self.assertEqual(safe_operation_error_code(code), code)

    def test_replaces_code_shaped_provider_values(self) -> None:
        for value in (
            "10.9.8.7",
            "db-internal-01",
            "provider:target",
            "Provider refused",
            "host.10",
            "a" * 97,
        ):
            with self.subTest(value=value):
                self.assertEqual(safe_operation_error_code(value), "unrecognized_code")


if __name__ == "__main__":
    unittest.main()
