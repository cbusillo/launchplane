import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from control_plane.workflows.verireel_preview_driver import _build_preview_database_command
from control_plane.workflows.verireel_preview_driver import _preview_database_admin_module_source


class VeriReelPreviewDriverTests(unittest.TestCase):
    def test_build_preview_database_command_uses_bundled_temp_files(self) -> None:
        command = _build_preview_database_command(
            action="ensure",
            admin_database_url="postgresql://user:pass@host:5432/postgres",
            database_name="verireel_preview_pr_71",
            role_name="verireel_preview_pr_71",
            password="secret",
        )

        self.assertIn("PREVIEW_DB_ARGS_BASE64=", command)
        self.assertIn('/tmp/.preview-db-admin-', command)
        self.assertIn('/tmp/.preview-db-admin-runner-', command)
        self.assertIn('node "$temp_runner" "$temp_script"', command)
        self.assertIn('base64 -d > "$temp_script"', command)
        self.assertIn('base64 -d > "$temp_runner"', command)
        self.assertIn('rm -f "$temp_script" "$temp_runner" || true', command)

        parse_result = subprocess.run(["sh", "-n", "-c", command], check=False)
        self.assertEqual(parse_result.returncode, 0)

    def test_preview_database_admin_module_source_is_valid_javascript(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            module_path = Path(temporary_directory_name) / "preview-db-admin.mjs"
            module_path.write_text(_preview_database_admin_module_source(), encoding="utf-8")

            parse_result = subprocess.run(["node", "--check", str(module_path)], check=False)

        self.assertEqual(parse_result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
