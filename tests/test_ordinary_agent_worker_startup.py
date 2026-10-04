from __future__ import annotations

import os
from pathlib import Path
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


if __name__ == "__main__":
    unittest.main()
