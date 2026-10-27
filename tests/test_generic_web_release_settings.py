from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from typing import cast
from control_plane.dokploy import api
from control_plane.workflows.generic_web_deploy_provider import (
    build_generic_web_provider_reconciliation_key,
)

from control_plane.contracts.generic_web_release_settings import GenericWebReleaseSettings
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.secret_record import SecretRecord, SecretBinding
from control_plane.secrets import RUNTIME_ENVIRONMENT_SECRET_INTEGRATION
from control_plane.generic_web_release_settings import desired_release_settings
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.workflows.generic_web_deploy_provider import DokployGenericWebDeployProvider
from tests.test_generic_web_promotion import _ProductionProvider, _profile


class GenericWebReleaseSettingsTests(unittest.TestCase):
    def test_dokploy_delivers_and_restores_settings_for_application_and_compose(self) -> None:
        for kind in ("application", "compose"):
            with self.subTest(kind=kind):
                fixture = _ProductionProvider()
                target = fixture.resolve_deploy_target(
                    lane=_profile().lanes[1],
                    normalized_artifact_id="example/app:sha-next",
                    request_source_git_ref="a" * 40,
                    request_timeout_seconds=30,
                )
                target.resolved_target = target.resolved_target.model_copy(
                    update={"target_type": kind}
                )
                target.ship_request = target.ship_request.model_copy(
                    update={
                        "target_type": kind,
                        "provider_target_type": kind,
                        "target_category": kind,
                    }
                )
                original_key = build_generic_web_provider_reconciliation_key(target)
                provider = DokployGenericWebDeployProvider()
                payload: api.JsonObject = {
                    "env": "APP_CHANNEL=${{project.CHANNEL}}\nOLD_SETTING=retained\nUNRELATED_TOKEN=private\nSHARED_VALUE=${{project.SHARED}}\n",
                    "composeType": "docker-compose",
                }

                persist = True

                def request(**kwargs: object) -> bool:
                    path = str(kwargs["path"])
                    body = cast(api.JsonObject, kwargs["payload"])
                    if persist and path in {
                        "/api/compose.update",
                        "/api/application.saveEnvironment",
                    }:
                        payload["env"] = body["env"]
                    if path == "/api/application.saveDockerProvider":
                        payload["dockerImage"] = body["dockerImage"]
                    return True

                from tests.test_generic_web_promotion import _deployment_record

                identity = _deployment_record().runtime_identity
                assert identity is not None
                with (
                    patch.object(
                        provider,
                        "_read_provider_config",
                        return_value=("https://provider.invalid", "synthetic"),
                    ),
                    patch.object(
                        api,
                        "fetch_dokploy_target_payload",
                        side_effect=lambda **_kwargs: dict(payload),
                    ),
                    patch.object(api, "dokploy_request", side_effect=request),
                    patch.object(api, "latest_deployment_for_target", return_value=None),
                    patch.object(api, "trigger_deployment") as trigger,
                    patch.object(api, "wait_for_target_deployment"),
                ):
                    captured = provider.read_release_settings(
                        control_plane_root=Path("."),
                        target=target,
                        keys=frozenset({"APP_CHANNEL", "OLD_SETTING", "ADDED_SETTING"}),
                    )
                    for settings, artifact in (
                        (
                            GenericWebReleaseSettings(
                                values={"APP_CHANNEL": "new", "ADDED_SETTING": "enabled"},
                                absent_keys=("OLD_SETTING",),
                            ),
                            "example/app:sha-next",
                        ),
                        (captured, "example/app:sha-previous"),
                    ):
                        target.release_settings = settings
                        self.assertEqual(
                            build_generic_web_provider_reconciliation_key(target), original_key
                        )
                        target.ship_request = target.ship_request.model_copy(
                            update={"artifact_id": artifact}
                        )
                        provider.execute_artifact_deploy(
                            control_plane_root=Path("."),
                            resolved_deploy_target=target,
                            runtime_identity=identity,
                            deployment_title="",
                            before_provider_mutation=lambda _phase: None,
                            effect_started=lambda: None,
                        )
                        values = api.parse_dokploy_env_text(str(payload["env"]))
                        self.assertEqual(
                            {key: values.get(key) for key in settings.values}, settings.values
                        )
                        self.assertFalse(set(settings.absent_keys) & values.keys())
                        self.assertEqual(values["UNRELATED_TOKEN"], "private")
                        self.assertEqual(values["SHARED_VALUE"], "${{project.SHARED}}")
                    self.assertEqual(values["APP_CHANNEL"], captured.values["APP_CHANNEL"])
                    self.assertEqual(values["OLD_SETTING"], "retained")
                    self.assertNotIn("ADDED_SETTING", values)
                    from control_plane.generic_web_testing_inputs import TestingInputSafetyRefused

                    persist = False
                    before = trigger.call_count
                    target.release_settings = GenericWebReleaseSettings(
                        values={"APP_CHANNEL": "not saved"}
                    )
                    with self.assertRaises(TestingInputSafetyRefused):
                        provider.execute_artifact_deploy(
                            control_plane_root=Path("."),
                            resolved_deploy_target=target,
                            runtime_identity=identity,
                            deployment_title="",
                            before_provider_mutation=lambda _phase: None,
                            effect_started=lambda: None,
                        )
                    self.assertEqual(trigger.call_count, before)

    def test_production_snapshot_excludes_global_and_other_lane_settings(self) -> None:
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(Path(directory))
            for scope, context, instance, env in (
                ("global", "", "", {"PLATFORM_ONLY": "private"}),
                (
                    "context",
                    _profile().lanes[1].context,
                    "",
                    {"APP_THEME": "dark", "APP_BOUND": "masked by a binding"},
                ),
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
            timestamp = "2026-10-27T00:00:00Z"
            secret = SecretRecord(
                secret_id="bound-setting",
                scope="context_instance",
                integration=RUNTIME_ENVIRONMENT_SECRET_INTEGRATION,
                name="Bound application value",
                context=_profile().lanes[1].context,
                instance="prod",
                current_version_id="not-read",
                created_at=timestamp,
                updated_at=timestamp,
            )
            store.write_secret_record(secret)
            store.write_secret_binding(
                SecretBinding(
                    binding_id="app-bound",
                    secret_id=secret.secret_id,
                    integration=secret.integration,
                    binding_key="APP_BOUND",
                    context=secret.context,
                    instance=secret.instance,
                    created_at=timestamp,
                    updated_at=timestamp,
                )
            )
            self.assertEqual(
                desired_release_settings(store, _profile()),
                GenericWebReleaseSettings(
                    values={"APP_THEME": "dark", "APP_CHANNEL": "production"},
                ),
            )

    def test_cutover_lanes_keep_their_existing_settings_authority(self) -> None:
        from tests.test_generic_web_cutover import plan

        product = _profile()
        product = product.model_copy(
            update={
                "lanes": (
                    product.lanes[0],
                    product.lanes[1].model_copy(update={"generic_web_cutover": plan().lane}),
                )
            }
        )
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(Path(directory))
            store.write_runtime_environment_record(
                RuntimeEnvironmentRecord(
                    scope="instance",
                    context=product.lanes[1].context,
                    instance="prod",
                    env={"APP_CHANNEL": "staged"},
                    updated_at="2026-10-27T00:00:00Z",
                )
            )
            self.assertIsNone(desired_release_settings(store, product))

    def test_snapshot_refuses_credentials_in_values(self) -> None:
        plain = {"TOKENIZER_THREADS": "4", "KEYCLOAK_URL": "https://identity.example.invalid"}
        self.assertEqual(GenericWebReleaseSettings(values=plain).values, plain)
        with self.assertRaises(ValueError):
            GenericWebReleaseSettings(values={"APP_PASSWORD": "private"})
        with self.assertRaises(ValueError):
            GenericWebReleaseSettings(values={"APP_DSN": "host=db password=private"})
