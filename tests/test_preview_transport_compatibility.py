import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import click
from pydantic import ValidationError

from control_plane.contracts.product_onboarding_manifest import ProductOnboardingPreviewManifest
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductPreviewProfile,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import LaunchplaneProductProfileRow, PostgresRecordStore
from control_plane.drivers.generic_web_preview_extensions import (
    apply_generic_web_preview_driver_refresh,
)
from control_plane.workflows.generic_web_preview import (
    GenericWebPreviewReadinessRequest,
    GenericWebPreviewRefreshRequest,
    evaluate_generic_web_preview_readiness,
    execute_generic_web_preview_refresh,
)
from tests.test_generic_web_preview import _GenericWebPreviewStore, _profile


class PreviewTransportCompatibilityTests(unittest.TestCase):
    def test_admin_and_onboarding_reject_unimplemented_intent(self) -> None:
        for model in (ProductPreviewProfile, ProductOnboardingPreviewManifest):
            for mode in ("clone", "bootstrap", "migrate_seed"):
                with self.subTest(model=model.__name__, mode=mode):
                    with self.assertRaises(ValidationError):
                        model.model_validate({"data_transport_mode": mode})
            for mode in ("none", "driver"):
                for command in ("migration_command", "seed_command"):
                    with self.subTest(model=model.__name__, mode=mode, command=command):
                        with self.assertRaises(ValidationError):
                            model.model_validate({"data_transport_mode": mode, command: "/app/run"})

    def test_stored_unsupported_profiles_load_and_block_provider_work(self) -> None:
        for mode in ("clone", "bootstrap", "migrate_seed", "driver"):
            with self.subTest(mode=mode), TemporaryDirectory() as directory:
                root = Path(directory)
                profile = _profile()
                payload = profile.model_dump(mode="json")
                intent = {
                    "data_transport_mode": mode,
                    "migration_command": "/app/migrate",
                    "seed_command": "/app/seed",
                }
                payload["preview"].update(intent)
                filesystem = FilesystemRecordStore(root / "state")
                path = filesystem.write_product_profile_record(profile)
                path.write_text(json.dumps(payload))
                database = PostgresRecordStore(database_url=f"sqlite+pysqlite:///{root / 'db'}")
                database.ensure_schema()
                database.write_product_profile_record(profile)
                with database._session_factory() as session:
                    row = session.get(LaunchplaneProductProfileRow, profile.product)
                    assert row is not None
                    row.payload = payload
                    session.commit()
                for storage in (filesystem, database):
                    saved = storage.read_product_profile_record(profile.product)
                    self.assertEqual(saved.preview.historical_transport, intent)
                    self.assertEqual(storage.list_product_profile_records()[0], saved)
                    storage.write_product_profile_record(saved)
                    self.assertEqual(storage.read_product_profile_record(profile.product), saved)
                    with self.assertRaises(ValidationError):
                        LaunchplaneProductProfileRecord.model_validate(
                            saved.model_dump(mode="json")
                        )
                    if mode == "driver":
                        with (
                            patch(
                                "control_plane.drivers.generic_web_preview_extensions."
                                "apply_verireel_preview_refresh_result"
                            ) as driver_refresh,
                            self.assertRaises(click.ClickException),
                        ):
                            apply_generic_web_preview_driver_refresh(
                                control_plane_root=root,
                                record_store=storage,
                                profile=saved,
                                request=GenericWebPreviewRefreshRequest(
                                    product=profile.product,
                                    anchor_pr_number=42,
                                    image_reference="ghcr.io/example/app:preview",
                                ),
                            )
                        driver_refresh.assert_not_called()
                    store = _GenericWebPreviewStore(saved)
                    with (
                        patch(
                            "control_plane.workflows.generic_web_preview._read_template_payload"
                        ) as read_template,
                        patch(
                            "control_plane.workflows.generic_web_preview._ensure_application"
                        ) as create_application,
                    ):
                        readiness = evaluate_generic_web_preview_readiness(
                            control_plane_root=root,
                            record_store=store,
                            request=GenericWebPreviewReadinessRequest(product=profile.product),
                            checked_at=profile.updated_at,
                        )
                        self.assertEqual(readiness.readiness_status, "blocked")
                        self.assertEqual(
                            readiness.transport.compatibility_status, "unsupported_saved_intent"
                        )
                        result = execute_generic_web_preview_refresh(
                            control_plane_root=root,
                            record_store=store,
                            request=GenericWebPreviewRefreshRequest(
                                product=profile.product,
                                preview_slug="preview-42-site",
                                anchor_pr_number=42,
                                anchor_head_sha="a" * 40,
                                preview_url="https://preview.example.test",
                                image_reference="ghcr.io/example/app:preview",
                            ),
                            profile=saved,
                        )
                        self.assertEqual(result.refresh_status, "blocked")
                        read_template.assert_not_called()
                        create_application.assert_not_called()
                    replacement = saved.model_dump(mode="json")
                    replacement["preview"] = profile.preview.model_dump(mode="json")
                    repaired = LaunchplaneProductProfileRecord.model_validate(replacement)
                    storage.write_product_profile_record(repaired)
                    self.assertFalse(
                        storage.read_product_profile_record(
                            profile.product
                        ).preview.historical_transport
                    )

    def test_empty_retired_commands_do_not_block_plain_stored_previews(self) -> None:
        payload = _profile().model_dump(mode="json")
        payload["preview"].update(migration_command="", seed_command="")
        saved = LaunchplaneProductProfileRecord.model_validate(
            payload, context={"stored_product_profile": True}
        )
        self.assertFalse(saved.preview.historical_transport)
        self.assertEqual(saved.preview.data_transport_mode, "none")

    def test_generic_readiness_does_not_claim_driver_preparation_executed(self) -> None:
        profile = _profile()
        profile.preview.data_transport_mode = "driver"
        with patch("control_plane.workflows.generic_web_preview._read_template_payload") as read:
            result = evaluate_generic_web_preview_readiness(
                control_plane_root=Path("."),
                record_store=_GenericWebPreviewStore(profile),
                request=GenericWebPreviewReadinessRequest(product=profile.product),
                checked_at=profile.updated_at,
            )
        self.assertEqual(result.readiness_status, "blocked")
        read.assert_not_called()
