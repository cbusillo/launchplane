import subprocess
import unittest

from control_plane.workflows.verireel_preview_driver import _build_preview_database_command


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
        self.assertIn('node "$temp_runner" "$temp_script"', command)
        self.assertIn('base64 -d > "$temp_script"', command)
        self.assertIn('base64 -d > "$temp_runner"', command)
        self.assertIn('rm -f "$temp_script" "$temp_runner" || true', command)

        parse_result = subprocess.run(["sh", "-n", "-c", command], check=False)
        self.assertEqual(parse_result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
