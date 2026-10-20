from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

from tests.support.workflows import load_workflow

ROOT = Path(__file__).resolve().parents[1]

# Model the package manager's store selection and copy into node_modules. The
# fixture needs no installed Node runtime, network, or host package cache.
PNPM = """import os
import shutil
import sys
from pathlib import Path

if os.environ.get("PNPM_FAIL"):
    sys.exit(42)
store = Path(os.environ.get("npm_config_store_dir", str(Path.home() / "store"))) / "v-fixture"
if "store" in sys.argv:
    print(store)
else:
    try:
        shutil.copyfile(store / "package-object", Path.cwd() / "installed-object")
    except FileNotFoundError:
        print("ERR_PNPM_ENOENT: missing store file", file=sys.stderr)
        sys.exit(1)
"""


class PnpmStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        commands = self.root / "commands"
        commands.mkdir()
        pnpm = commands / "pnpm"
        pnpm.write_text(f"#!{sys.executable}\n{PNPM}")
        pnpm.chmod(0o755)
        self.home = self.root / "home"
        self.home.mkdir()
        self.environment = {
            key: value
            for key, value in os.environ.items()
            if not key.lower().startswith("npm_config_")
        } | {"HOME": str(self.home), "PATH": f"{commands}{os.pathsep}{os.environ['PATH']}"}
        self.workflow = load_workflow(ROOT / ".github/workflows/ci.yml")

    def prepare(self, job: str, name: str) -> tuple[Path, Path, dict[str, str]]:
        workspace = self.root / name
        workspace.mkdir()
        (workspace / "frontend").mkdir()
        (workspace / "scripts").mkdir()
        shutil.copyfile(
            ROOT / "scripts/setup-ci-pnpm-store.sh", workspace / "scripts/setup-ci-pnpm-store.sh"
        )
        environment = self.environment | {
            "GITHUB_WORKSPACE": str(workspace),
            "GITHUB_ENV": str(workspace / "environment"),
            "GITHUB_OUTPUT": str(workspace / "outputs"),
        }
        step = self.workflow.step_named(job, "Resolve pnpm store path")
        assert step is not None
        result = subprocess.run(
            ["bash", "-euo", "pipefail", "-c", step.run],
            cwd=workspace,
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        env_file = Path(environment["GITHUB_ENV"])
        if env_file.exists():
            environment |= dict(line.split("=", 1) for line in env_file.read_text().splitlines())
        outputs = dict(
            line.split("=", 1)
            for line in Path(environment["GITHUB_OUTPUT"]).read_text().splitlines()
        )
        return workspace, Path(outputs["path"]), environment

    def install(self, job: str, workspace: Path, environment: dict[str, str]) -> None:
        step = self.workflow.step_named(job, "Install frontend dependencies")
        assert step is not None
        result = subprocess.run(
            ["bash", "-euo", "pipefail", "-c", step.run],
            cwd=workspace,
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((workspace / "installed-object").read_bytes(), b"package payload")

    def test_sibling_cache_replacement_cannot_remove_an_install_source(self) -> None:
        jobs = (
            "frontend_validate",
            "frontend_validate_fork",
            "frontend_browser_smoke",
            "frontend_browser_smoke_evidence",
        )
        for job in jobs:
            with self.subTest(job=job):
                workspace, store, environment = self.prepare(job, job)
                _, sibling_store, _ = self.prepare(job, f"{job}-sibling")
                for target in (store, sibling_store):
                    target.mkdir(parents=True, exist_ok=True)
                    (target / "package-object").write_bytes(b"package payload")
                # Reproduce the unlink window of a sibling archive replacing a
                # cached file while the first job is about to import it.
                (sibling_store / "package-object").unlink()
                self.install(job, workspace, environment)
                self.assertTrue(store.is_relative_to(workspace))
                self.assertFalse(store.is_relative_to(self.home))

    def test_store_resolution_failure_does_not_publish_a_cache_target(self) -> None:
        env_file = self.root / "environment"
        output_file = self.root / "outputs"
        result = subprocess.run(
            ["bash", "scripts/setup-ci-pnpm-store.sh"],
            cwd=ROOT,
            env=self.environment
            | {
                "GITHUB_WORKSPACE": str(self.root),
                "GITHUB_ENV": str(env_file),
                "GITHUB_OUTPUT": str(output_file),
                "PNPM_FAIL": "1",
            },
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(env_file.exists())
        self.assertFalse(output_file.exists())

    def test_cached_store_relocates_to_the_restoring_checkout(self) -> None:
        job = "frontend_browser_smoke"
        first, store, environment = self.prepare(job, "first")
        store.mkdir(parents=True)
        (store / "package-object").write_bytes(b"package payload")
        archive = self.root / "store.tar"
        with tarfile.open(archive, "w") as handle:
            handle.add(store, arcname=store.relative_to(first))
        second, _, second_environment = self.prepare(job, "second")
        with tarfile.open(archive) as handle:
            handle.extractall(second, filter="data")
        self.install(job, second, second_environment)
        self.install(job, first, environment)


if __name__ == "__main__":
    unittest.main()
