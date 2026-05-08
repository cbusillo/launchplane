from __future__ import annotations

import unittest

from control_plane.contracts.data_provenance import (
    agent_safe_host_label,
    safe_agent_context_text,
)


class AgentContextSafetyTests(unittest.TestCase):
    def test_safe_agent_context_text_redacts_paths_and_secret_shaped_values(self) -> None:
        token_assignment = "token=" + ("x" * 36)
        bearer_header = "Authorization: Bearer " + ("a" * 36)
        detail = safe_agent_context_text(
            f"Failed in /Users/chris/private/checkout with {token_assignment} and {bearer_header}",
            fallback="No detail recorded.",
        )

        self.assertIn("[redacted-path]", detail)
        self.assertIn("token=[redacted]", detail)
        self.assertIn("Bearer [redacted]", detail)
        self.assertNotIn("/Users/chris", detail)
        self.assertNotIn("x" * 36, detail)
        self.assertNotIn("a" * 36, detail)

    def test_safe_agent_context_text_uses_fallback_for_blank_values(self) -> None:
        self.assertEqual(
            safe_agent_context_text("   ", fallback="No detail recorded."),
            "No detail recorded.",
        )

    def test_agent_safe_host_label_hides_local_topology(self) -> None:
        self.assertEqual(agent_safe_host_label("Chris-Studio.local"), "claimed_local_worker")
        self.assertEqual(agent_safe_host_label(""), "")


if __name__ == "__main__":
    unittest.main()
