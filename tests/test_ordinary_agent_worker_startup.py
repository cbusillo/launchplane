from __future__ import annotations

import os
from pathlib import Path
import stat
import subprocess
import unittest


class OrdinaryAgentWorkerStartupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parents[1]
        self.script = self.root / "scripts" / "start-launchplane-ordinary-agent-workers.sh"

    def test_startup_requires_database_url(self) -> None:
        result = subprocess.run(
            [str(self.script)],
            cwd=self.root,
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "LAUNCHPLANE_DATABASE_URL": "",
                "LAUNCHPLANE_APP_ROOT": "/app",
            },
            check=False,
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("refuse startup without LAUNCHPLANE_DATABASE_URL", result.stderr)

    def test_compose_definition_is_separate_from_deployed_default(self) -> None:
        default_compose = (self.root / "docker-compose.yml").read_text(encoding="utf-8")
        dormant_compose_path = self.root / "docker-compose.ordinary-agent-workers.yml"
        dormant_compose = dormant_compose_path.read_text(encoding="utf-8")

        self.assertNotIn("launchplane-ordinary-agent-workers", default_compose)
        self.assertIn("launchplane-ordinary-agent-workers", dormant_compose)
        self.assertIn("ordinary-agent-workers", dormant_compose)
        self.assertTrue(self.script.stat().st_mode & stat.S_IXUSR)


if __name__ == "__main__":
    unittest.main()
