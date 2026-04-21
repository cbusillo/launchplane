import base64
import os
import stat
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


class StartHarborServiceScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.script_path = Path(__file__).resolve().parents[1] / "scripts" / "start-harbor-service.sh"

    def _write_fake_uv(self, bin_dir: Path) -> None:
        uv_path = bin_dir / "uv"
        uv_path.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' \"$@\" >\"$UV_CAPTURE_FILE\"\n",
            encoding="utf-8",
        )
        uv_path.chmod(uv_path.stat().st_mode | stat.S_IXUSR)

    def test_requires_explicit_policy_input(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_directory = Path(temporary_directory_name)
            app_root = temporary_directory / "app"
            app_root.mkdir()

            result = subprocess.run(
                [str(self.script_path)],
                capture_output=True,
                text=True,
                env={
                    **os.environ,
                    "HARBOR_APP_ROOT": str(app_root),
                    "HARBOR_STATE_DIR": str(temporary_directory / "state"),
                },
                check=False,
            )

        self.assertEqual(result.returncode, 1, msg=result.stderr)
        self.assertIn("requires an explicit policy input", result.stderr)

    def test_rejects_example_policy_file_path(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_directory = Path(temporary_directory_name)
            app_root = temporary_directory / "app"
            example_policy = app_root / "config" / "harbor-authz.toml.example"
            example_policy.parent.mkdir(parents=True)
            example_policy.write_text("schema_version = 1\n", encoding="utf-8")

            result = subprocess.run(
                [str(self.script_path)],
                capture_output=True,
                text=True,
                env={
                    **os.environ,
                    "HARBOR_APP_ROOT": str(app_root),
                    "HARBOR_STATE_DIR": str(temporary_directory / "state"),
                    "HARBOR_POLICY_FILE": str(example_policy),
                },
                check=False,
            )

        self.assertEqual(result.returncode, 1, msg=result.stderr)
        self.assertIn("Refusing to start Harbor with example policy file", result.stderr)

    def test_accepts_explicit_base64_policy_input(self) -> None:
        policy_path = Path("/tmp/harbor-authz.toml")
        policy_path.unlink(missing_ok=True)

        try:
            with TemporaryDirectory() as temporary_directory_name:
                temporary_directory = Path(temporary_directory_name)
                app_root = temporary_directory / "app"
                bin_dir = temporary_directory / "bin"
                capture_file = temporary_directory / "uv-args.txt"
                app_root.mkdir()
                bin_dir.mkdir()
                self._write_fake_uv(bin_dir)

                result = subprocess.run(
                    [str(self.script_path)],
                    capture_output=True,
                    text=True,
                    env={
                        **os.environ,
                        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
                        "UV_CAPTURE_FILE": str(capture_file),
                        "HARBOR_APP_ROOT": str(app_root),
                        "HARBOR_STATE_DIR": str(temporary_directory / "state"),
                        "HARBOR_POLICY_B64": base64.b64encode(b"schema_version = 1\n").decode("ascii"),
                    },
                    check=False,
                )

                captured_args = capture_file.read_text(encoding="utf-8").splitlines()

            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertIn("--policy-file", captured_args)
            self.assertIn(str(policy_path), captured_args)
            self.assertEqual(policy_path.read_text(encoding="utf-8"), "schema_version = 1\n")
        finally:
            policy_path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
