from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from control_plane import release_tuples as control_plane_release_tuples
from control_plane.contracts.artifact_identity import ArtifactIdentityManifest
from control_plane.contracts.release_tuple_record import ReleaseTupleRecord
from control_plane.storage.postgres import PostgresRecordStore


def _sqlite_database_url(database_path: Path) -> str:
    return f"sqlite+pysqlite:///{database_path}"


def _write_release_tuple_records(
    *,
    control_plane_root: Path,
    records: tuple[ReleaseTupleRecord, ...],
) -> str:
    database_url = _sqlite_database_url(control_plane_root / "launchplane.sqlite3")
    store = PostgresRecordStore(database_url=database_url)
    store.ensure_schema()
    try:
        for record in records:
            store.write_release_tuple_record(record)
    finally:
        store.close()
    return database_url


class ReleaseTupleTests(unittest.TestCase):
    def test_should_mint_release_tuple_only_for_stable_remote_channels(self) -> None:
        self.assertTrue(control_plane_release_tuples.should_mint_release_tuple_for_channel("testing"))
        self.assertTrue(control_plane_release_tuples.should_mint_release_tuple_for_channel("prod"))
        self.assertFalse(control_plane_release_tuples.should_mint_release_tuple_for_channel("dev"))
        self.assertFalse(control_plane_release_tuples.should_mint_release_tuple_for_channel("preview"))

    def test_resolve_release_tuple_reads_context_channel_repo_refs_from_database(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            database_url = _write_release_tuple_records(
                control_plane_root=control_plane_root,
                records=(
                    ReleaseTupleRecord(
                        tuple_id="opw-testing-2026-04-13",
                        context="opw",
                        channel="testing",
                        artifact_id="artifact-testing",
                        repo_shas={
                            "tenant-opw": "1111111111111111111111111111111111111111",
                            "shared-addons": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                        },
                        provenance="ship",
                        minted_at="2026-04-21T18:00:00Z",
                    ),
                ),
            )

            with patch.dict(os.environ, {"LAUNCHPLANE_DATABASE_URL": database_url}, clear=True):
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

    def test_load_release_tuple_catalog_requires_database_url(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)

            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(Exception, "LAUNCHPLANE_DATABASE_URL"):
                    control_plane_release_tuples.load_release_tuple_catalog(
                        control_plane_root=control_plane_root,
                    )

    def test_load_release_tuple_catalog_requires_stored_records(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(control_plane_root / "launchplane.sqlite3")
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            store.close()

            with patch.dict(os.environ, {"LAUNCHPLANE_DATABASE_URL": database_url}, clear=True):
                with self.assertRaisesRegex(Exception, "No Launchplane release tuple records"):
                    control_plane_release_tuples.load_release_tuple_catalog(
                        control_plane_root=control_plane_root,
                    )

    def test_load_release_tuple_catalog_reads_database_records_only(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            database_url = _write_release_tuple_records(
                control_plane_root=control_plane_root,
                records=(
                    ReleaseTupleRecord(
                        tuple_id="opw-testing-db",
                        context="opw",
                        channel="testing",
                        artifact_id="artifact-testing",
                        repo_shas={"tenant-opw": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
                        provenance="ship",
                        minted_at="2026-04-21T18:00:00Z",
                    ),
                    ReleaseTupleRecord(
                        tuple_id="opw-prod-db",
                        context="opw",
                        channel="prod",
                        artifact_id="artifact-prod",
                        repo_shas={"tenant-opw": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"},
                        provenance="ship",
                        minted_at="2026-04-21T19:00:00Z",
                    ),
                ),
            )

            with patch.dict(os.environ, {"LAUNCHPLANE_DATABASE_URL": database_url}, clear=True):
                catalog = control_plane_release_tuples.load_release_tuple_catalog(
                    control_plane_root=control_plane_root,
                )

        self.assertEqual(catalog.contexts["opw"].channels["testing"].tuple_id, "opw-testing-db")
        self.assertEqual(catalog.contexts["opw"].channels["prod"].tuple_id, "opw-prod-db")

    def test_resolve_release_tuple_fails_closed_when_channel_missing(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            database_url = _write_release_tuple_records(
                control_plane_root=control_plane_root,
                records=(
                    ReleaseTupleRecord(
                        tuple_id="opw-testing-2026-04-13",
                        context="opw",
                        channel="testing",
                        artifact_id="artifact-testing",
                        repo_shas={
                            "tenant-opw": "1111111111111111111111111111111111111111",
                            "shared-addons": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                        },
                        provenance="ship",
                        minted_at="2026-04-21T18:00:00Z",
                    ),
                ),
            )

            with patch.dict(os.environ, {"LAUNCHPLANE_DATABASE_URL": database_url}, clear=True):
                with self.assertRaisesRegex(Exception, "opw/prod"):
                    control_plane_release_tuples.resolve_release_tuple(
                        control_plane_root=control_plane_root,
                        context_name="opw",
                        channel_name="prod",
                    )

    def test_build_release_tuple_catalog_from_records_rejects_duplicate_tuple_ids(self) -> None:
        with self.assertRaisesRegex(Exception, "Duplicate release tuple id"):
            control_plane_release_tuples.build_release_tuple_catalog_from_records(
                (
                    ReleaseTupleRecord(
                        tuple_id="shared-tuple",
                        context="opw",
                        channel="testing",
                        artifact_id="artifact-opw",
                        repo_shas={"tenant-opw": "1111111111111111111111111111111111111111"},
                        provenance="ship",
                        minted_at="2026-04-21T18:00:00Z",
                    ),
                    ReleaseTupleRecord(
                        tuple_id="shared-tuple",
                        context="cm",
                        channel="testing",
                        artifact_id="artifact-cm",
                        repo_shas={"tenant-cm": "3333333333333333333333333333333333333333"},
                        provenance="ship",
                        minted_at="2026-04-21T18:10:00Z",
                    ),
                )
            )

    def test_build_release_tuple_catalog_from_records_rejects_non_stable_remote_channels(self) -> None:
        with self.assertRaisesRegex(Exception, "stable remote channels"):
            control_plane_release_tuples.build_release_tuple_catalog_from_records(
                (
                    ReleaseTupleRecord(
                        tuple_id="opw-dev-2026-04-13",
                        context="opw",
                        channel="dev",
                        artifact_id="artifact-dev",
                        repo_shas={"tenant-opw": "1111111111111111111111111111111111111111"},
                        provenance="ship",
                        minted_at="2026-04-21T18:00:00Z",
                    ),
                )
            )

    def test_build_release_tuple_record_from_artifact_manifest_uses_split_repo_shas(self) -> None:
        manifest = ArtifactIdentityManifest(
            artifact_id="artifact-sha256-image456",
            source_commit="abc1234",
            enterprise_base_digest="sha256:enterprise123",
            addon_sources=(
                {
                    "repository": "cbusillo/odoo-shared-addons",
                    "ref": "def5678",
                },
            ),
            image={
                "repository": "ghcr.io/cbusillo/odoo-private",
                "digest": "sha256:image456",
            },
        )

        release_tuple = control_plane_release_tuples.build_release_tuple_record_from_artifact_manifest(
            context_name="opw",
            channel_name="testing",
            artifact_manifest=manifest,
            deployment_record_id="deployment-1",
            minted_at="2026-04-10T18:24:00Z",
        )

        self.assertEqual(release_tuple.tuple_id, "opw-testing-artifact-sha256-image456")
        self.assertEqual(release_tuple.repo_shas["tenant-opw"], "abc1234")
        self.assertEqual(release_tuple.repo_shas["shared-addons"], "def5678")
        self.assertEqual(release_tuple.provenance, "ship")

    def test_build_release_tuple_record_ignores_addon_selector_metadata(self) -> None:
        manifest = ArtifactIdentityManifest(
            artifact_id="artifact-sha256-image456",
            source_commit="abc1234",
            enterprise_base_digest="sha256:enterprise123",
            addon_sources=(
                {
                    "repository": "cbusillo/disable_odoo_online",
                    "ref": "def5678",
                },
            ),
            addon_selectors=(
                {
                    "repository": "cbusillo/disable_odoo_online",
                    "selector": "main",
                    "resolved_ref": "def5678",
                },
            ),
            image={
                "repository": "ghcr.io/cbusillo/odoo-private",
                "digest": "sha256:image456",
            },
        )

        release_tuple = control_plane_release_tuples.build_release_tuple_record_from_artifact_manifest(
            context_name="opw",
            channel_name="testing",
            artifact_manifest=manifest,
            deployment_record_id="deployment-1",
            minted_at="2026-04-10T18:24:00Z",
        )

        self.assertEqual(release_tuple.repo_shas, {"tenant-opw": "abc1234", "disable_odoo_online": "def5678"})

    def test_build_release_tuple_record_rejects_branch_refs(self) -> None:
        manifest = ArtifactIdentityManifest(
            artifact_id="artifact-sha256-image456",
            source_commit="abc1234",
            enterprise_base_digest="sha256:enterprise123",
            addon_sources=({"repository": "cbusillo/odoo-shared-addons", "ref": "main"},),
            image={
                "repository": "ghcr.io/cbusillo/odoo-private",
                "digest": "sha256:image456",
            },
        )

        with self.assertRaisesRegex(Exception, "must be a 7-40 character hexadecimal git sha"):
            control_plane_release_tuples.repo_shas_from_artifact_manifest(
                context_name="opw",
                artifact_manifest=manifest,
            )

    def test_build_release_tuple_record_rejects_dev_channel(self) -> None:
        manifest = ArtifactIdentityManifest(
            artifact_id="artifact-sha256-image456",
            source_commit="abc1234",
            enterprise_base_digest="sha256:enterprise123",
            addon_sources=(
                {
                    "repository": "cbusillo/odoo-shared-addons",
                    "ref": "def5678",
                },
            ),
            image={
                "repository": "ghcr.io/cbusillo/odoo-private",
                "digest": "sha256:image456",
            },
        )

        with self.assertRaisesRegex(Exception, "stable remote channels"):
            control_plane_release_tuples.build_release_tuple_record_from_artifact_manifest(
                context_name="opw",
                channel_name="dev",
                artifact_manifest=manifest,
                deployment_record_id="deployment-1",
                minted_at="2026-04-10T18:24:00Z",
            )

    def test_render_release_tuple_catalog_toml_renders_database_backed_records(self) -> None:
        rendered_catalog = control_plane_release_tuples.render_release_tuple_catalog_toml(
            (
                ReleaseTupleRecord(
                    tuple_id="opw-testing-artifact-sha256-image456",
                    context="opw",
                    channel="testing",
                    artifact_id="artifact-sha256-image456",
                    repo_shas={"tenant-opw": "abc1234", "shared-addons": "def5678"},
                    provenance="ship",
                    minted_at="2026-04-10T18:24:00Z",
                ),
            )
        )

        self.assertIn('tuple_id = "opw-testing-artifact-sha256-image456"', rendered_catalog)
        self.assertIn('tenant-opw = "abc1234"', rendered_catalog)
        self.assertIn('shared-addons = "def5678"', rendered_catalog)

    def test_render_release_tuple_catalog_toml_rejects_preview_channel(self) -> None:
        with self.assertRaisesRegex(Exception, "stable remote channels"):
            control_plane_release_tuples.render_release_tuple_catalog_toml(
                (
                    ReleaseTupleRecord(
                        tuple_id="opw-preview-artifact-sha256-image456",
                        context="opw",
                        channel="preview",
                        artifact_id="artifact-sha256-image456",
                        repo_shas={"tenant-opw": "abc1234", "shared-addons": "def5678"},
                        provenance="ship",
                        minted_at="2026-04-10T18:24:00Z",
                    ),
                )
            )

    def test_build_promoted_release_tuple_record_rejects_preview_channel(self) -> None:
        source_tuple = ReleaseTupleRecord(
            tuple_id="opw-testing-artifact-sha256-image456",
            context="opw",
            channel="testing",
            artifact_id="artifact-sha256-image456",
            repo_shas={"tenant-opw": "abc1234", "shared-addons": "def5678"},
            provenance="ship",
            minted_at="2026-04-10T18:24:00Z",
        )

        with self.assertRaisesRegex(Exception, "stable remote channels"):
            control_plane_release_tuples.build_promoted_release_tuple_record(
                source_tuple=source_tuple,
                to_channel_name="preview",
                deployment_record_id="deployment-2",
                promotion_record_id="promotion-1",
                minted_at="2026-04-10T18:25:00Z",
            )


if __name__ == "__main__":
    unittest.main()
