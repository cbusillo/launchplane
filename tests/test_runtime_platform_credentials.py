"""Platform credentials never reach an application runtime environment."""

from __future__ import annotations

import os
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
from unittest.mock import Mock, patch

import click

from control_plane.cli import _sync_artifact_image_reference_for_target
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.deployment_record import ResolvedTargetEvidence
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.dokploy import api as dokploy_api
from control_plane.dokploy import post_deploy as dokploy_post_deploy
from control_plane.dokploy.source import DokployTargetDefinition
from control_plane.live_target_runtime import (
    LiveTargetRuntimeError,
    apply_live_target_runtime_environment,
    report_lane_provider_env_platform_credentials,
)
from control_plane.runtime_platform_credentials import PlatformCredentialRefusedError
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.generic_web_preview import _render_preview_env_text
from tests.support.profiles import _generic_site_profile_payload
from tests.support.stores import _seed_tracked_target_records, _sqlite_database_url

# Shaped like a classic personal access token; not a real credential.
_FAKE_GITHUB_TOKEN = "ghp_" + "A1b2C3d4E5" * 4


def _profile(*keys: str) -> LaunchplaneProductProfileRecord:
    payload = _generic_site_profile_payload()
    payload["expected_config"] = {
        "runtime_environment_keys": [
            {"key": key, "context": "example-site", "instance": "testing"}
            for key in ("APP_MODE", *keys)
        ]
    }
    return LaunchplaneProductProfileRecord.model_validate(payload)


def _instance_record(
    env: dict[str, str], *, retired: tuple[str, ...] = ()
) -> RuntimeEnvironmentRecord:
    return RuntimeEnvironmentRecord(
        schema_version=2 if retired else 1,
        scope="instance",
        context="example-site",
        instance="testing",
        env=cast(dict[str, str | int | float | bool], env),
        retired_provider_keys=retired,
        updated_at="2026-09-28T00:00:00Z",
        source_label="test",
    )


class _LaneFixture:
    """A sqlite-backed lane with a tracked Dokploy application target."""

    def __init__(self, test: unittest.TestCase, *records: RuntimeEnvironmentRecord) -> None:
        temporary_directory = TemporaryDirectory()
        test.addCleanup(temporary_directory.cleanup)
        self.root = Path(temporary_directory.name)
        self.database_url = _sqlite_database_url(self.root / "records.sqlite3")
        with closing(PostgresRecordStore(database_url=self.database_url)) as store:
            store.ensure_schema()
            store.write_product_profile_record(_profile("APP_WEBHOOK_URL", "GITHUB_TOKEN"))
            for record in records:
                store.write_runtime_environment_record(record)
        _seed_tracked_target_records(
            database_url=self.database_url,
            context="example-site",
            instance="testing",
            target_id="app-testing",
            target_type="application",
            target_name="example-site-testing",
        )

    def sync(
        self, *, provider_env: str, apply_changes: bool = True
    ) -> tuple[dict[str, object], list[dict[str, object]]]:
        updates: list[dict[str, object]] = []

        def fetch(**_kwargs: object) -> dict[str, object]:
            env = provider_env
            if updates:
                env = str(updates[-1]["env_text"])
            return {"name": "example-site-testing", "env": env}

        with (
            patch(
                "control_plane.live_target_runtime.dokploy_source.read_dokploy_config",
                return_value=("https://dokploy.example", "provider-token"),
            ),
            patch(
                "control_plane.live_target_runtime.dokploy_api.fetch_dokploy_target_payload",
                side_effect=fetch,
            ),
            patch(
                "control_plane.dokploy.api.dokploy_request",
                side_effect=lambda **kwargs: updates.append({"env_text": kwargs["payload"]["env"]}),
            ),
        ):
            result = apply_live_target_runtime_environment(
                control_plane_root=self.root,
                database_url=self.database_url,
                product_name="example-site",
                context_name="example-site",
                instance_name="testing",
                apply_changes=apply_changes,
                deploy=False,
                no_cache=False,
                deploy_timeout_seconds=None,
                deploy_trigger=Mock(),
            )
        return result, updates


class StableLaneSyncRefusalTests(unittest.TestCase):
    def test_instance_record_platform_credential_refuses_the_sync(self) -> None:
        lane = _LaneFixture(
            self, _instance_record({"APP_MODE": "on", "GITHUB_TOKEN": "record-token-value"})
        )
        with self.assertRaises(LiveTargetRuntimeError) as refusal:
            lane.sync(provider_env="APP_MODE=off")

        message = str(refusal.exception)
        self.assertEqual(refusal.exception.code, "runtime_platform_credential_refused")
        self.assertIn("GITHUB_TOKEN", message)
        self.assertIn("example-site/testing instance runtime-environment record", message)
        self.assertNotIn("record-token-value", message)

    def test_github_token_value_under_another_key_refuses_the_sync(self) -> None:
        lane = _LaneFixture(
            self,
            _instance_record(
                {"APP_MODE": "on", "APP_WEBHOOK_URL": f"https://{_FAKE_GITHUB_TOKEN}@example"}
            ),
        )
        with self.assertRaises(LiveTargetRuntimeError) as refusal:
            lane.sync(provider_env="APP_MODE=off")

        self.assertIn("APP_WEBHOOK_URL", str(refusal.exception))
        self.assertIn("GitHub token value", str(refusal.exception))
        self.assertNotIn(_FAKE_GITHUB_TOKEN, str(refusal.exception))

    def test_context_scope_launchplane_credential_is_withheld_from_the_site_sync(self) -> None:
        context_record = RuntimeEnvironmentRecord(
            scope="context",
            context="example-site",
            instance="",
            env={"GITHUB_TOKEN": "launchplane-comment-token"},
            updated_at="2026-09-28T00:00:00Z",
            source_label="test",
        )
        lane = _LaneFixture(self, context_record, _instance_record({"APP_MODE": "on"}))

        result, updates = lane.sync(provider_env="APP_MODE=off")

        runtime_environment = cast(dict[str, object], result["runtime_environment"])
        self.assertNotIn("GITHUB_TOKEN", cast(list[str], runtime_environment["changed_keys"]))
        self.assertEqual(len(updates), 1)
        self.assertNotIn("GITHUB_TOKEN", str(updates[0]["env_text"]))
        self.assertNotIn("launchplane-comment-token", str(result))


class ProviderEnvReportTests(unittest.TestCase):
    def test_sync_reports_legacy_provider_credentials_without_deleting_them(self) -> None:
        lane = _LaneFixture(self, _instance_record({"APP_MODE": "on"}))
        result, updates = lane.sync(
            provider_env=f"APP_MODE=off\nDOKPLOY_HOST=https://provider\nOLD_HOOK={_FAKE_GITHUB_TOKEN}"
        )

        report = cast(dict[str, object], result["provider_env_platform_credentials"])
        self.assertEqual(report["status"], "found")
        self.assertEqual(report["unretired_keys"], ["DOKPLOY_HOST", "OLD_HOOK"])
        self.assertEqual(len(updates), 1)
        self.assertIn("DOKPLOY_HOST=https://provider", str(updates[0]["env_text"]))
        self.assertNotIn(_FAKE_GITHUB_TOKEN, str(report))

    def test_retired_legacy_credential_is_removed_by_the_sync(self) -> None:
        lane = _LaneFixture(self, _instance_record({"APP_MODE": "on"}, retired=("DOKPLOY_TOKEN",)))
        result, updates = lane.sync(provider_env="APP_MODE=on\nDOKPLOY_TOKEN=legacy-deploy")

        report = cast(dict[str, object], result["provider_env_platform_credentials"])
        self.assertEqual(report["retiring_keys"], ["DOKPLOY_TOKEN"])
        self.assertNotIn("DOKPLOY_TOKEN", str(updates[-1]["env_text"]))

    def test_lane_report_lists_every_flagged_lane(self) -> None:
        lane = _LaneFixture(self, _instance_record({"APP_MODE": "on"}))
        with (
            patch(
                "control_plane.live_target_runtime.dokploy_source.read_dokploy_config",
                return_value=("https://dokploy.example", "provider-token"),
            ),
            patch(
                "control_plane.live_target_runtime.dokploy_api.fetch_dokploy_target_payload",
                return_value={"env": "APP_MODE=on\nGITHUB_TOKEN=legacy-token-value"},
            ),
            patch.dict(os.environ, {"LAUNCHPLANE_DATABASE_URL": lane.database_url}),
        ):
            report = report_lane_provider_env_platform_credentials(
                control_plane_root=lane.root, database_url=lane.database_url
            )

        self.assertEqual(report["flagged_lane_count"], 1)
        lanes = cast(list[dict[str, object]], report["lanes"])
        self.assertEqual(lanes[0]["context"], "example-site")
        self.assertEqual(lanes[0]["unretired_keys"], ["GITHUB_TOKEN"])
        self.assertNotIn("legacy-token-value", str(report))


class ShipPathTests(unittest.TestCase):
    def _ship(self, lane: _LaneFixture, *, provider_env: str) -> dict[str, object]:
        captured: dict[str, object] = {}
        with (
            patch(
                "control_plane.dokploy.source.read_dokploy_config",
                return_value=("https://dokploy.example", "provider-token"),
            ),
            patch(
                "control_plane.dokploy.api.fetch_dokploy_target_payload",
                side_effect=lambda **_kwargs: {"env": str(captured.get("env_text", provider_env))},
            ),
            patch(
                "control_plane.dokploy.api.dokploy_request",
                side_effect=lambda **kwargs: captured.update(env_text=kwargs["payload"]["env"]),
            ),
            patch.dict(os.environ, {"LAUNCHPLANE_DATABASE_URL": lane.database_url}, clear=True),
        ):
            _sync_artifact_image_reference_for_target(
                context_name="example-site",
                instance_name="testing",
                artifact_manifest=None,
                resolved_target=ResolvedTargetEvidence(
                    target_type="application",
                    target_id="app-testing",
                    target_name="example-site-testing",
                ),
            )
        return captured

    def test_retired_key_is_absent_after_ship(self) -> None:
        lane = _LaneFixture(
            self, _instance_record({"APP_MODE": "on"}, retired=("GITHUB_TOKEN", "DOKPLOY_TOKEN"))
        )
        captured = self._ship(
            lane, provider_env="APP_MODE=on\nGITHUB_TOKEN=legacy\nDOKPLOY_TOKEN=legacy-deploy"
        )

        env_map = dokploy_api.parse_dokploy_env_text(str(captured["env_text"]))
        self.assertEqual(env_map, {"APP_MODE": "on"})

    def test_refused_record_key_fails_the_ship(self) -> None:
        lane = _LaneFixture(
            self, _instance_record({"APP_MODE": "on", "DOKPLOY_TOKEN": "record-deploy-token"})
        )
        with self.assertRaises(PlatformCredentialRefusedError) as refusal:
            self._ship(lane, provider_env="APP_MODE=on")

        self.assertIn("DOKPLOY_TOKEN", str(refusal.exception.message))
        self.assertNotIn("record-deploy-token", str(refusal.exception.message))


class ProviderEnvWriteTests(unittest.TestCase):
    def test_stable_bootstrap_refuses_a_platform_credential_it_would_add(self) -> None:
        # Bootstrap overlays only allowlisted keys onto the live env, so the
        # write guard is what stops a token value arriving through one of them.
        with TemporaryDirectory() as temporary_directory:
            env_file = Path(temporary_directory) / "bootstrap.env"
            env_file.write_text(
                f"ODOO_FILESTORE_PATH=/data/{_FAKE_GITHUB_TOKEN}\n", encoding="utf-8"
            )
            request = Mock()
            with (
                patch(
                    "control_plane.dokploy.post_deploy.api.fetch_dokploy_target_payload",
                    return_value={
                        "name": "example-site-testing",
                        "env": "ODOO_INSTALL_MODULES=base",
                    },
                ),
                patch("control_plane.dokploy.api.dokploy_request", request),
                self.assertRaises(PlatformCredentialRefusedError) as refusal,
            ):
                dokploy_post_deploy.run_compose_odoo_stable_bootstrap(
                    host="https://dokploy.example",
                    token="provider-token",
                    target_definition=DokployTargetDefinition(
                        context="example-site",
                        instance="testing",
                        target_type="compose",
                        target_id="compose-testing",
                        target_name="example-site-testing",
                    ),
                    env_file=env_file,
                )

        self.assertIn("ODOO_FILESTORE_PATH", str(refusal.exception.message))
        self.assertNotIn(_FAKE_GITHUB_TOKEN, str(refusal.exception.message))
        request.assert_not_called()

    def test_unchanged_legacy_key_is_preserved_and_launchplane_service_is_exempt(self) -> None:
        request = Mock()
        with patch("control_plane.dokploy.api.dokploy_request", request):
            dokploy_api.update_dokploy_target_env(
                host="https://dokploy.example",
                token="provider-token",
                target_type="compose",
                target_id="compose-testing",
                target_payload={"env": "GITHUB_TOKEN=legacy"},
                env_text="GITHUB_TOKEN=legacy\nAPP_MODE=on",
            )
            dokploy_api.update_dokploy_target_env(
                host="https://dokploy.example",
                token="provider-token",
                target_type="compose",
                target_id="launchplane-service",
                target_payload={"env": ""},
                env_text="LAUNCHPLANE_MASTER_ENCRYPTION_KEY=service-key",
                launchplane_service_target=True,
            )
            with self.assertRaises(PlatformCredentialRefusedError):
                dokploy_api.update_dokploy_target_env(
                    host="https://dokploy.example",
                    token="provider-token",
                    target_type="compose",
                    target_id="compose-testing",
                    target_payload={"env": "GITHUB_TOKEN=legacy"},
                    env_text="GITHUB_TOKEN=rotated",
                )

        self.assertEqual(request.call_count, 2)


class GenericWebPreviewTests(unittest.TestCase):
    def test_copied_template_credential_refuses_the_preview(self) -> None:
        payload = _generic_site_profile_payload()
        preview = cast(dict[str, object], payload["preview"])
        preview["copied_env_keys"] = ["APP_MODE", "GITHUB_TOKEN"]
        profile = LaunchplaneProductProfileRecord.model_validate(payload)

        with self.assertRaises(click.ClickException) as refusal:
            _render_preview_env_text(
                profile=profile,
                template_application={"env": "APP_MODE=on\nGITHUB_TOKEN=template-token"},
                preview_url="https://pr-1.preview.example",
            )

        self.assertIn("GITHUB_TOKEN", refusal.exception.message)
        self.assertIn("template application env", refusal.exception.message)
        self.assertNotIn("template-token", refusal.exception.message)


if __name__ == "__main__":
    unittest.main()
