from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from control_plane import release_tuples as control_plane_release_tuples


class ReleaseTupleTests(unittest.TestCase):
    def test_resolve_release_tuple_reads_context_channel_repo_refs(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            tuples_file = control_plane_root / "config" / "release-tuples.toml"
            tuples_file.parent.mkdir(parents=True, exist_ok=True)
            tuples_file.write_text(
                """
schema_version = 1

[contexts.opw.channels.testing]
tuple_id = "opw-testing-2026-04-13"

[contexts.opw.channels.testing.repo_shas]
tenant-opw = "1111111111111111111111111111111111111111"
shared-addons = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
""".strip()
                + "\n",
                encoding="utf-8",
            )

            release_tuple = control_plane_release_tuples.resolve_release_tuple(
                control_plane_root=control_plane_root,
                context_name="opw",
                channel_name="testing",
            )

        self.assertEqual(release_tuple.tuple_id, "opw-testing-2026-04-13")
        self.assertEqual(
            release_tuple.repo_shas["tenant-opw"],
            "1111111111111111111111111111111111111111",
        )
        self.assertEqual(
            release_tuple.repo_shas["shared-addons"],
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        )

    def test_resolve_release_tuple_uses_env_override_file(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            custom_file = control_plane_root / "custom-tuples.toml"
            custom_file.write_text(
                """
schema_version = 1

[contexts.cm.channels.prod]
tuple_id = "cm-prod-2026-04-13"

[contexts.cm.channels.prod.repo_shas]
tenant-cm = "4444444444444444444444444444444444444444"
shared-addons = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
""".strip()
                + "\n",
                encoding="utf-8",
            )

            with patch.dict(
                os.environ,
                {control_plane_release_tuples.RELEASE_TUPLES_FILE_ENV_VAR: str(custom_file)},
                clear=True,
            ):
                release_tuple = control_plane_release_tuples.resolve_release_tuple(
                    control_plane_root=control_plane_root,
                    context_name="cm",
                    channel_name="prod",
                )

        self.assertEqual(release_tuple.tuple_id, "cm-prod-2026-04-13")

    def test_resolve_release_tuple_fails_closed_when_channel_missing(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            tuples_file = control_plane_root / "config" / "release-tuples.toml"
            tuples_file.parent.mkdir(parents=True, exist_ok=True)
            tuples_file.write_text(
                """
schema_version = 1

[contexts.opw.channels.testing]
tuple_id = "opw-testing-2026-04-13"

[contexts.opw.channels.testing.repo_shas]
tenant-opw = "1111111111111111111111111111111111111111"
shared-addons = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
""".strip()
                + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(Exception, "opw/prod"):
                control_plane_release_tuples.resolve_release_tuple(
                    control_plane_root=control_plane_root,
                    context_name="opw",
                    channel_name="prod",
                )

    def test_load_release_tuple_catalog_rejects_duplicate_tuple_ids(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            tuples_file = control_plane_root / "config" / "release-tuples.toml"
            tuples_file.parent.mkdir(parents=True, exist_ok=True)
            tuples_file.write_text(
                """
schema_version = 1

[contexts.opw.channels.testing]
tuple_id = "shared-tuple"

[contexts.opw.channels.testing.repo_shas]
tenant-opw = "1111111111111111111111111111111111111111"
shared-addons = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

[contexts.cm.channels.testing]
tuple_id = "shared-tuple"

[contexts.cm.channels.testing.repo_shas]
tenant-cm = "3333333333333333333333333333333333333333"
shared-addons = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
""".strip()
                + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(Exception, "Duplicate release tuple id"):
                control_plane_release_tuples.load_release_tuple_catalog(
                    control_plane_root=control_plane_root,
                )

    def test_load_release_tuple_catalog_rejects_non_sha_repo_values(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            tuples_file = control_plane_root / "config" / "release-tuples.toml"
            tuples_file.parent.mkdir(parents=True, exist_ok=True)
            tuples_file.write_text(
                """
schema_version = 1

[contexts.opw.channels.testing]
tuple_id = "opw-testing-2026-04-13"

[contexts.opw.channels.testing.repo_shas]
tenant-opw = "origin/opw-testing"
shared-addons = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
""".strip()
                + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(Exception, "hexadecimal git sha"):
                control_plane_release_tuples.load_release_tuple_catalog(
                    control_plane_root=control_plane_root,
                )


if __name__ == "__main__":
    unittest.main()
