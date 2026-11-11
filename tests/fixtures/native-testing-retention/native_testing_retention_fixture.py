import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import types
import unittest
from typing import Any


def main() -> None:
    workspace = Path(__file__).resolve().parent
    exports = json.loads((workspace / "programs.json").read_text())
    package = types.ModuleType("control_plane")
    setattr(package, "__path__", [])
    sys.modules[package.__name__] = package
    for name, contents in exports["modules"].items():
        module = types.ModuleType(name)
        exec(contents, module.__dict__)
        sys.modules[name] = module
    from tests import test_retained_volume_sweep_program as fixture
    from tests.test_retained_volume_sweep_program import RetainedVolumeSweepProgramTests

    class PhysicalReadbackTests(RetainedVolumeSweepProgramTests):
        def physical_run(
            self, mode: str, identity: str, operation: str = "physical-original", **changes: Any
        ) -> dict[str, Any]:
            from control_plane.retained_volume_sweep_program import (
                RETAINED_VOLUME_SWEEP_HOST_PROGRAM,
            )

            spec = {
                "mode": mode,
                "volume": self.state["name"],
                "identity": identity,
                "operation": operation,
                "physical_readback": True,
                "retirement": self.permit,
                **changes,
            }
            program = (
                "from pathlib import Path\nfrom unittest.mock import patch\n"
                "original=Path.home\n"
                f"with patch.object(Path,'home',return_value=Path({str(self.home)!r})):\n"
                f"    exec({RETAINED_VOLUME_SWEEP_HOST_PROGRAM!r})\n"
            )
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    program,
                    base64.b64encode(json.dumps(spec).encode()).decode(),
                    "FRAME:",
                ],
                env=os.environ
                | {
                    "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
                    "FAKE_DOCKER_STATE": str(self.state_path),
                },
                capture_output=True,
                text=True,
                timeout=90,
                check=True,
            )
            self.assertNotIn(str(self.data), result.stdout)
            value: dict[str, Any] = json.loads(result.stdout.removeprefix("FRAME:"))["result"]
            return value

        def test_physical_bytes_and_original_readback_without_replay(self) -> None:
            from control_plane.retained_volume_capture import retained_volume_identity

            docker = self.bin / "docker"
            docker.write_text(
                fixture.DOCKER_FIXTURE.replace(
                    "state['removed']=True;",
                    "import shutil;shutil.rmtree(root+'/volumes/'+name);state['removed']=True;",
                )
            )
            self.permit: dict[str, Any] = {
                "binding": hashlib.sha256(b"fabricated-original").hexdigest(),
                "fresh_until": time.time() + 300,
                "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                "monotonic_deadline": time.monotonic() + 300,
            }
            (self.data / "fabricated-data").write_bytes(os.urandom(2 * 1024 * 1024))
            metadata = self.data.stat()
            identity = retained_volume_identity(
                self.volume(),
                "engine-one",
                metadata.st_dev,
                metadata.st_ino,
                backing_filesystem_id=os.statvfs(self.data).f_fsid,
            )
            self.assertEqual(self.physical_run("dry-run", identity)["status"], "eligible")
            removed = self.physical_run("apply", identity)
            self.assertEqual(removed["status"], "removed")
            self.assertFalse(self.data.exists())
            physical = removed["physical"]
            self.assertEqual(
                physical["observed_change_bytes"],
                physical["after_bytes"] - physical["before_bytes"],
            )
            for mode in ("readback", "apply"):
                observed = self.physical_run(mode, identity)
                self.assertEqual(observed["status"], "absent")
                self.assertEqual(observed["physical"]["before_bytes"], physical["before_bytes"])
            state = json.loads(self.state_path.read_text())
            self.assertEqual(sum(call[:2] == ["volume", "rm"] for call in state["calls"]), 1)

        def test_native_inspection_keeps_production_boundaries_without_source_holds(self) -> None:
            resources = exports["resources"]
            namespace = resources["namespace"]
            new_parent = self.data.parent.with_name(namespace + "-db")
            self.data.parent.rename(new_parent)
            self.data = new_parent / "_data"
            self.state.update(
                name=namespace + "-db",
                volume_labels={
                    "io.launchplane.testing-resources": hashlib.sha256(
                        json.dumps(resources, sort_keys=True, separators=(",", ":")).encode()
                    ).hexdigest(),
                    "io.launchplane.testing-resource-role": "ODOO_DB_VOLUME",
                },
            )

            def inspect_native() -> dict[str, Any]:
                self.write_state()
                payload = {"resources": resources, "project": namespace, "generation": None}
                result = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        exports["inspection"],
                        base64.b64encode(json.dumps(payload).encode()).decode(),
                        "FRAME:",
                    ],
                    env=os.environ
                    | {
                        "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
                        "FAKE_DOCKER_STATE": str(self.state_path),
                    },
                    text=True,
                    capture_output=True,
                    check=True,
                    timeout=90,
                )
                frame = json.loads(result.stdout.removeprefix("FRAME:"))
                row: dict[str, Any] = next(
                    (row for row in frame["volumes"] if row["role"] == "ODOO_DB_VOLUME"), frame
                )
                return row

            observed = inspect_native()
            self.assertEqual(observed["status"], "observed")
            self.assertEqual(observed["reason"], "")
            self.assertGreater(observed["free_bytes"], 0)
            self.state.update(containers=["a" * 64], container_ref=True)
            self.assertEqual(inspect_native()["reason_code"], "production_boundary")
            self.state.update(containers=[], container_ref=False)
            (self.data / ".lp-native-fabricated-input").mkdir()
            self.assertEqual(inspect_native()["status"], "observed")
            self.state["volume_labels"] = {}
            self.assertEqual(inspect_native()["reason_code"], "production_boundary")

    results = workspace / "results"
    results.mkdir(exist_ok=True)
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(
        fixture.RetainedVolumeSweepProgramTests
    )
    suite.addTest(PhysicalReadbackTests("test_physical_bytes_and_original_readback_without_replay"))
    suite.addTest(
        PhysicalReadbackTests(
            "test_native_inspection_keeps_production_boundaries_without_source_holds"
        )
    )
    with (results / "tests.log").open("w") as output:
        result = unittest.TextTestRunner(stream=output, verbosity=2).run(suite)
    (results / "result.json").write_text(
        json.dumps(
            {
                "tests": result.testsRun,
                "failures": len(result.failures),
                "errors": len(result.errors),
                "successful": result.wasSuccessful(),
                "scope": "fabricated Docker protocol with native filesystem and subprocesses; no real Docker volume lifecycle",
            }
        )
    )
    raise SystemExit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
