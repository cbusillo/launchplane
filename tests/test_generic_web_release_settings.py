from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from control_plane.contracts.generic_web_release_settings import GenericWebReleaseSettings
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.generic_web_release_settings import desired_release_settings
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.workflows.generic_web_deploy_provider import DokployGenericWebDeployProvider
from tests.test_generic_web_promotion import _ProductionProvider, _profile


class GenericWebReleaseSettingsTests(unittest.TestCase):
    def test_dokploy_delivers_recorded_settings_and_captures_only_selected_prior_values(
        self,
    ) -> None:
        fixture = _ProductionProvider()
        target = fixture.resolve_deploy_target(
            lane=_profile().lanes[1],
            normalized_artifact_id="example/app:sha-fixture",
            request_source_git_ref="a" * 40,
            request_timeout_seconds=30,
        )
        settings = GenericWebReleaseSettings(
            values={"APP_CHANNEL": "new"}, absent_keys=("OLD_SETTING",)
        )
        target.release_settings = settings
        provider = DokployGenericWebDeployProvider()
        with (
            patch.object(
                provider,
                "_read_provider_config",
                return_value=("https://provider.invalid", "synthetic"),
            ),
            patch(
                "control_plane.dokploy.api.fetch_dokploy_target_payload",
                return_value={
                    "env": "APP_CHANNEL=old\nUNRELATED_TOKEN=private\n",
                },
            ),
            patch(
                "control_plane.workflows.generic_web_deploy_provider.execute_dokploy_artifact_deploy",
                return_value=None,
            ) as deploy,
        ):
            captured = provider.read_release_settings(
                control_plane_root=Path("."),
                target=target,
                keys=frozenset({"APP_CHANNEL", "OLD_SETTING"}),
            )
            from tests.test_generic_web_promotion import _deployment_record

            identity = _deployment_record().runtime_identity
            assert identity is not None
            provider.execute_artifact_deploy(
                control_plane_root=Path("."),
                resolved_deploy_target=target,
                runtime_identity=identity,
                deployment_title="release",
                before_provider_mutation=lambda _phase: None,
                effect_started=lambda: None,
            )
        self.assertEqual(
            captured,
            GenericWebReleaseSettings(
                values={"APP_CHANNEL": "old"},
                absent_keys=("OLD_SETTING",),
            ),
        )
        self.assertEqual(deploy.call_args.kwargs["runtime_environment"], settings.values)
        self.assertEqual(
            deploy.call_args.kwargs["retired_runtime_keys"],
            frozenset(settings.absent_keys),
        )

    def test_production_snapshot_excludes_global_and_other_lane_settings(self) -> None:
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(Path(directory))
            for scope, context, instance, env in (
                ("global", "", "", {"PLATFORM_ONLY": "private"}),
                ("context", _profile().lanes[1].context, "", {"APP_THEME": "dark"}),
                ("instance", _profile().lanes[1].context, "testing", {"APP_CHANNEL": "testing"}),
                ("instance", _profile().lanes[1].context, "prod", {"APP_CHANNEL": "production"}),
            ):
                store.write_runtime_environment_record(
                    RuntimeEnvironmentRecord.model_validate(
                        {
                            "scope": scope,
                            "context": context,
                            "instance": instance,
                            "env": env,
                            "updated_at": "2026-10-27T00:00:00Z",
                        }
                    )
                )
            self.assertEqual(
                desired_release_settings(store, _profile()),
                GenericWebReleaseSettings(
                    values={"APP_THEME": "dark", "APP_CHANNEL": "production"},
                ),
            )

    def test_snapshot_refuses_credentials_in_values(self) -> None:
        with self.assertRaises(ValueError):
            GenericWebReleaseSettings(values={"APP_PASSWORD": "private"})
