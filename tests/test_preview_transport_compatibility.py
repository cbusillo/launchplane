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
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.product_owner_setting import ProductOwnerIdentity, updated_product_owner_profile
from tests.http_app_test_support import (
    _asgi_request,
    _product_profile_read_policy,
    _product_profile_write_policy,
)
from tests.support.auth import StubVerifier, identity
from control_plane.dokploy import DokployTargetDefinition
from control_plane.drivers.generic_web_preview_extensions import (
    apply_generic_web_preview_driver_refresh,
)
from control_plane.workflows.generic_web_preview import (
    GenericWebPreviewReadinessRequest,
    GenericWebPreviewTransportSummary,
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
                    self.assertEqual(
                        LaunchplaneProductProfileRecord.model_validate(
                            saved.model_dump(mode="json")
                        ),
                        saved,
                    )
                    with self.assertRaises(ValidationError):
                        LaunchplaneProductProfileRecord.model_validate(
                            saved.model_dump(mode="json"), context={"product_profile_input": True}
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

    def test_old_readiness_transport_evidence_stays_readable_and_truthful(self) -> None:
        for mode, commands in (("bootstrap", False), ("driver", True), ("none", False)):
            with self.subTest(mode=mode):
                summary = GenericWebPreviewTransportSummary.model_validate(
                    {
                        "data_transport_mode": mode,
                        "copied_env_keys": [],
                        "omitted_env_keys": [],
                        "override_env_keys": [],
                        "preview_url_env_keys": [],
                        "preview_domain_env_keys": [],
                        "migration_command_configured": commands,
                        "seed_command_configured": commands,
                    }
                )
                self.assertEqual(
                    summary.compatibility_status,
                    "supported" if mode == "none" else "unsupported_saved_intent",
                )

    def test_supported_driver_and_plain_prerequisites_remain_usable(self) -> None:
        for mode in ("none", "driver"):
            profile = _profile()
            profile.preview.data_transport_mode = mode
            with patch(
                "control_plane.workflows.generic_web_preview._read_template_payload",
                return_value=(
                    DokployTargetDefinition(
                        context=profile.lanes[0].context,
                        instance="testing",
                        target_type="application",
                        target_id="template",
                    ),
                    {
                        "env": "SMTP_HOST=smtp.example\nSMTP_FROM=hello@example.test",
                        "dockerImage": "image",
                        "username": "registry-user",
                    },
                    "",
                ),
            ):
                readiness = evaluate_generic_web_preview_readiness(
                    control_plane_root=Path("."),
                    record_store=_GenericWebPreviewStore(profile),
                    request=GenericWebPreviewReadinessRequest(product=profile.product),
                    checked_at=profile.updated_at,
                    preview_url_override="https://preview.example.test",
                )
            self.assertEqual(readiness.readiness_status, "pass")


class PreviewTransportHttpCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_historical_intent_survives_reads_and_settings_but_is_not_write_input(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            original = _profile()
            payload = original.model_dump(mode="json")
            payload["preview"]["data_transport_mode"] = "bootstrap"
            historical = LaunchplaneProductProfileRecord.model_validate(
                payload, context={"stored_product_profile": True}
            )
            store = PostgresRecordStore(database_url=f"sqlite+pysqlite:///{Path(directory) / 'db'}")
            self.addCleanup(store.close)
            store.ensure_schema()
            store.write_product_profile_record(historical)
            list_policy = _product_profile_read_policy(product="launchplane")
            policy = list_policy.model_copy(
                update={
                    "github_actions": (
                        *list_policy.github_actions,
                        *_product_profile_read_policy(product=original.product).github_actions,
                        *_product_profile_write_policy(product=original.product).github_actions,
                    )
                }
            )
            app = create_launchplane_fastapi_app(
                verifier=StubVerifier(identity()),
                authz_policy=policy,
                record_store_factory=lambda: store,
            )
            headers = {"Authorization": "Bearer valid-token", "Idempotency-Key": "repair-preview"}
            for route in ("/v1/product-profiles", f"/v1/product-profiles/{original.product}"):
                response = await _asgi_request(app, "GET", route, headers=headers)
                self.assertEqual(response.status_code, 200, response.text)
                response_profile = response.json().get("profile") or response.json()["profiles"][0]
                self.assertEqual(
                    response_profile["preview"]["historical_transport"],
                    historical.preview.historical_transport,
                )
            updated = updated_product_owner_profile(
                profile=historical,
                resolved_owner=ProductOwnerIdentity(github_login="example-user", github_id="42"),
                updated_at=historical.updated_at,
            )
            self.assertEqual(updated.preview, historical.preview)
            store.write_product_profile_record(updated)
            rejected = await _asgi_request(
                app,
                "POST",
                "/v1/product-profiles",
                headers=headers,
                payload=updated.model_dump(mode="json"),
            )
            self.assertEqual(rejected.status_code, 400, rejected.text)
            replacement = updated.model_dump(mode="json")
            replacement["preview"] = original.preview.model_dump(mode="json")
            accepted = await _asgi_request(
                app,
                "POST",
                "/v1/product-profiles",
                headers=headers,
                payload=replacement,
            )
            self.assertEqual(accepted.status_code, 202, accepted.text)
            self.assertFalse(
                store.read_product_profile_record(original.product).preview.historical_transport
            )
