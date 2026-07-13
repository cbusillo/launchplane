from __future__ import annotations

import unittest

from control_plane.child_process_errors import (
    normalize_child_process_failure,
    redact_untrusted_text,
)


class ChildProcessErrorTests(unittest.TestCase):
    def test_normalize_redacts_subprocess_sentinels_with_deterministic_correlation(self) -> None:
        stderr = "\n".join(
            (
                "read /Users/operator/.config/launchplane/credentials",
                "connect ECONNREFUSED 10.42.0.19:4444",
                "Authorization: Bearer example-bearer-value",
                "Authorization: Basic dXNlcjpzaG91bGQtbm90LXN1cnZpdmU=",
                "Cookie: launchplane_session=short-cookie-secret",
                "Set-Cookie: launchplane_session=short-response-secret; Secure",
                "LAUNCHPLANE_EVERY_CODE_GITHUB_TOKEN=example-token-value",
                "request https://octocat:password@build.internal/api/private failed",
                "retry this safe diagnostic",
            )
        )

        with self.assertLogs("control_plane.child_process_errors", level="INFO") as logs:
            first = normalize_child_process_failure(
                operation="Sync Every Code webhook",
                tool="github_cli",
                returncode=1,
                stderr=stderr,
            )
            second = normalize_child_process_failure(
                operation="Sync Every Code webhook",
                tool="github_cli",
                returncode=1,
                stderr=stderr,
            )

        self.assertEqual(first.code, "github_cli_failed")
        self.assertEqual(first.correlation_id, second.correlation_id)
        self.assertTrue(first.correlation_id.startswith("cpf-"))
        self.assertLessEqual(len(first.detail), 240)
        self.assertNotIn("\n", first.detail)
        for value in (
            "example-bearer-value",
            "dXNlcjpzaG91bGQtbm90LXN1cnZpdmU=",
            "short-cookie-secret",
            "short-response-secret",
            "example-token-value",
            "octocat:password",
            "build.internal",
            "10.42.0.19",
            "/Users/operator/.config/launchplane/credentials",
        ):
            self.assertNotIn(value, first.detail)
            self.assertNotIn(value, first.operator_message())
        log_output = "\n".join(logs.output)
        self.assertIn(first.code, log_output)
        self.assertIn(first.correlation_id, log_output)
        self.assertNotIn("example-token-value", log_output)

    def test_redact_untrusted_text_covers_process_output_sentinels(self) -> None:
        detail = redact_untrusted_text(
            "\n".join(
                (
                    "read /Users/operator/.config/launchplane/credentials",
                    "connect ECONNREFUSED 10.42.0.19:4444",
                    "Authorization: Bearer example-bearer-value",
                    "Authorization: Basic dXNlcjpzaG91bGQtbm90LXN1cnZpdmU=",
                    "Cookie: launchplane_session=short-cookie-secret",
                    "Set-Cookie: launchplane_session=short-response-secret; Secure",
                    "LAUNCHPLANE_EVERY_CODE_GITHUB_TOKEN=example-token-value",
                    "request https://octocat:password@build.internal/api/private failed",
                    "retry this safe diagnostic",
                )
            ),
            fallback="Unavailable.",
            maximum_length=500,
        )

        self.assertNotIn("\n", detail)
        self.assertIn("[redacted-path]", detail)
        self.assertIn("[redacted-host]", detail)
        self.assertIn("Authorization: Bearer [redacted]", detail)
        self.assertIn("Authorization: Basic [redacted]", detail)
        self.assertIn("Cookie: [redacted]", detail)
        self.assertIn("Set-Cookie: [redacted]", detail)
        self.assertIn("LAUNCHPLANE_EVERY_CODE_GITHUB_TOKEN=[redacted]", detail)
        self.assertIn("[redacted-url]", detail)
        self.assertIn("retry this safe diagnostic", detail)

    def test_redact_untrusted_text_preserves_ordinary_safe_messages(self) -> None:
        detail = "GitHub API returned 403: Resource not accessible by integration."

        self.assertEqual(redact_untrusted_text(detail, fallback="Unavailable."), detail)


if __name__ == "__main__":
    unittest.main()
