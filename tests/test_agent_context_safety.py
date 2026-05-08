from __future__ import annotations

import unittest

from control_plane.contracts.data_provenance import (
    agent_safe_host_label,
    safe_agent_context_text,
)


class AgentContextSafetyTests(unittest.TestCase):
    def test_safe_agent_context_text_redacts_paths_and_secret_shaped_values(self) -> None:
        detail = safe_agent_context_text(
            "Failed in /Users/chris/private/checkout with token=ghp_123456789012345678901234567890abcd "
            "and Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456",
            fallback="No detail recorded.",
        )

        self.assertIn("[redacted-path]", detail)
        self.assertIn("token=[redacted]", detail)
        self.assertIn("Bearer [redacted]", detail)
        self.assertNotIn("/Users/chris", detail)
        self.assertNotIn("ghp_", detail)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", detail)

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
