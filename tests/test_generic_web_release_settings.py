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
    def test_rejected_setting_values_stay_out_of_direct_and_nested_errors(self) -> None:
        from control_plane.contracts.generic_web_release_settings import GenericWebReleaseBaseline

        canary = "host=db password=canary"
        settings = {"values": {"APP_VALUE": canary}}
        for model, payload in (
            (GenericWebReleaseSettings, settings),
            (GenericWebReleaseBaseline, {"deployment_record_id": "previous", "settings": settings}),
        ):
            with self.subTest(model=model.__name__):
                with self.assertRaises(ValueError) as raised:
                    model.model_validate(payload)
                self.assertNotIn("canary", str(raised.exception))
                self.assertNotIn("canary", repr(raised.exception))

    def test_existing_malformed_production_snapshot_is_unavailable(self) -> None:
        from control_plane.generic_web_release_settings import RELEASE_SETTINGS_KEY
        from control_plane.release_review import (
            ReleaseReviewStore,
            ReleaseEvidenceUnavailable,
            release_version,
        )
        from tests.test_generic_web_promotion import _store_with_production

        store = _store_with_production()
        previous = next(
            record for record in store.deployments.values() if record.instance == "prod"
        )
        store.write_deployment_record(
            previous.model_copy(
                update={
                    "runtime_source": {
                        **previous.runtime_source,
                        RELEASE_SETTINGS_KEY: "not a settings snapshot",
                    }
                }
            )
        )
        with self.assertRaises(ReleaseEvidenceUnavailable) as raised:
            release_version(
                store=cast(ReleaseReviewStore, store), profile=store.profile, instance="prod"
            )
        self.assertEqual(raised.exception.code, "production_settings_unavailable")

    def test_settings_delivery_requires_the_exact_predecessor_record(self) -> None:
        from control_plane.generic_web_release_settings import prepare_release_settings
        from tests.test_generic_web_promotion import _store_with_production

        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(Path(directory))
            seed = _store_with_production()
            lane = seed.profile.lanes[1]
            store.write_environment_inventory(
                seed.read_environment_inventory(context_name=lane.context, instance_name="prod")
            )
            provider = _ProductionProvider()
            target = provider.resolve_deploy_target(
                lane=lane,
                normalized_artifact_id="next-artifact",
                request_source_git_ref="a" * 40,
                request_timeout_seconds=30,
            )
            with patch.object(
                provider, "read_release_settings", wraps=provider.read_release_settings
            ) as readback:
                with self.assertRaises(FileNotFoundError):
                    prepare_release_settings(
                        store=store,
                        provider=provider,
                        control_plane_root=Path(directory),
                        target=target,
                        context=lane.context,
                        settings=GenericWebReleaseSettings(values={"APP_CHANNEL": "new"}),
                        evidence={},
                    )
                readback.assert_not_called()
            self.assertEqual(provider.deployed_artifacts, [])

    def test_invalid_settings_are_bounded_release_evidence_errors(self) -> None:
        from control_plane.release_review import (
            ReleaseReviewStore,
            ReleaseEvidenceUnavailable,
            release_version,
        )
        from tests.test_generic_web_promotion import _store_with_production

        for ambiguous in (False, True):
            with self.subTest(ambiguous=ambiguous):
                store = _store_with_production()
                record = RuntimeEnvironmentRecord(
                    scope="instance",
                    context=store.profile.lanes[1].context,
                    instance="prod",
                    env={"APP_CHANNEL": "safe"} if ambiguous else {"GITHUB_TOKEN": "synthetic"},
                    updated_at=store.profile.updated_at,
                )
                store.runtime_records = (record, record) if ambiguous else (record,)
                with self.assertRaises(ReleaseEvidenceUnavailable) as raised:
                    release_version(
                        store=cast(ReleaseReviewStore, store),
                        profile=store.profile,
                        instance="testing",
                    )
                self.assertEqual(raised.exception.code, "production_settings_unavailable")

    def test_secret_ownership_is_preserved_across_release_rollback_and_recovery(self) -> None:
        from control_plane.generic_web_release_settings import (
            RELEASE_SETTINGS_KEY,
            prepare_release_settings,
            verify_recorded_release_settings,
        )
        from tests.test_generic_web_promotion import _store_with_production

        for final_plain_key in (False, True):
            with self.subTest(final_plain_key=final_plain_key), TemporaryDirectory() as directory:
                store = FilesystemRecordStore(Path(directory))
                seeded = _store_with_production()
                product = seeded.profile
                lane = product.lanes[1]
                inventory = seeded.read_environment_inventory(
                    context_name=lane.context, instance_name="prod"
                )
                previous = seeded.read_deployment_record(inventory.deployment_record_id)
                previous_settings = GenericWebReleaseSettings(
                    values={
                        "APP_BOUND": "previous plain value",
                        **({} if final_plain_key else {"APP_CHANNEL": "old"}),
                    },
                    absent_keys=("OLD_BOUND",),
                )
                previous = previous.model_copy(
                    update={
                        "runtime_source": {
                            **previous.runtime_source,
                            RELEASE_SETTINGS_KEY: previous_settings.model_dump_json(),
                        }
                    }
                )
                store.write_environment_inventory(inventory)
                store.write_deployment_record(previous)
                for key in ("APP_BOUND", "OLD_BOUND"):
                    secret = SecretRecord(
                        secret_id=key.lower(),
                        scope="context_instance",
                        integration=RUNTIME_ENVIRONMENT_SECRET_INTEGRATION,
                        name="Synthetic managed setting",
                        context=lane.context,
                        instance="prod",
                        current_version_id="must-not-read",
                        created_at=product.updated_at,
                        updated_at=product.updated_at,
                    )
                    store.write_secret_record(secret)
                    store.write_secret_binding(
                        SecretBinding(
                            binding_id=key.lower(),
                            secret_id=secret.secret_id,
                            integration=secret.integration,
                            binding_key=key,
                            context=lane.context,
                            instance="prod",
                            created_at=product.updated_at,
                            updated_at=product.updated_at,
                        )
                    )
                if not final_plain_key:
                    store.write_runtime_environment_record(
                        RuntimeEnvironmentRecord(
                            scope="instance",
                            context=lane.context,
                            instance="prod",
                            env={} if final_plain_key else {"APP_CHANNEL": "new"},
                            updated_at=product.updated_at,
                        )
                    )
                desired = desired_release_settings(store, product)
                self.assertEqual(
                    desired,
                    GenericWebReleaseSettings(
                        values={} if final_plain_key else {"APP_CHANNEL": "new"}
                    ),
                )
                provider = _ProductionProvider()
                provider.settings = {
                    "APP_BOUND": "synthetic secret",
                    "OLD_BOUND": "synthetic secret",
                    "APP_CHANNEL": "old",
                }
                assert previous.runtime_identity is not None
                target = provider.resolve_deploy_target(
                    lane=lane,
                    normalized_artifact_id=previous.runtime_identity.artifact_id,
                    request_source_git_ref=previous.source_git_ref,
                    request_timeout_seconds=30,
                )
                evidence: dict[str, str] = {}
                with patch.object(
                    provider, "read_release_settings", wraps=provider.read_release_settings
                ) as reader:
                    prepare_release_settings(
                        store=store,
                        provider=provider,
                        control_plane_root=Path(directory),
                        target=target,
                        context=lane.context,
                        settings=desired,
                        evidence=evidence,
                    )
                    forward = previous.model_copy(
                        update={"record_id": "forward", "runtime_source": evidence}
                    )
                    store.write_deployment_record(forward)
                    assert previous.runtime_identity is not None
                    provider.execute_artifact_deploy(
                        runtime_identity=previous.runtime_identity,
                        resolved_deploy_target=target,
                        before_provider_mutation=lambda _phase: None,
                        effect_started=lambda: None,
                    )
                    self.assertTrue(
                        verify_recorded_release_settings(
                            store=store,
                            provider=provider,
                            control_plane_root=Path(directory),
                            target=target,
                            record=forward,
                        )
                    )
                    rollback_evidence: dict[str, str] = {}
                    restored = prepare_release_settings(
                        store=store,
                        provider=provider,
                        control_plane_root=Path(directory),
                        target=target,
                        context=lane.context,
                        settings=None,
                        evidence=rollback_evidence,
                        rollback_target=previous.record_id,
                        rollback_source=forward.record_id,
                    )
                    self.assertEqual(
                        restored,
                        GenericWebReleaseSettings(
                            values={} if final_plain_key else {"APP_CHANNEL": "old"}
                        ),
                    )
                    provider.execute_artifact_deploy(
                        runtime_identity=previous.runtime_identity,
                        resolved_deploy_target=target,
                        before_provider_mutation=lambda _phase: None,
                        effect_started=lambda: None,
                    )
                    from control_plane.contracts.deployment_record import (
                        DEPLOYMENT_OPERATION_EVIDENCE_KEY,
                    )

                    rollback = previous.model_copy(
                        update={
                            "runtime_source": {
                                **rollback_evidence,
                                DEPLOYMENT_OPERATION_EVIDENCE_KEY: "rollback",
                            }
                        }
                    )
                    self.assertTrue(
                        verify_recorded_release_settings(
                            store=store,
                            provider=provider,
                            control_plane_root=Path(directory),
                            target=target,
                            record=rollback,
                        )
                    )
                    for call in reader.call_args_list:
                        self.assertFalse({"APP_BOUND", "OLD_BOUND"} & call.kwargs["keys"])
                self.assertEqual(provider.settings["APP_BOUND"], "synthetic secret")
                self.assertEqual(provider.settings["OLD_BOUND"], "synthetic secret")
                self.assertEqual(store.read_deployment_record(previous.record_id), previous)

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
                    payload["env"] = "APP_VALUE=host=db password=canary@@\n"
                    with self.assertRaises(ValueError) as refused:
                        provider.read_release_settings(
                            control_plane_root=Path("."),
                            target=target,
                            keys=frozenset({"APP_VALUE"}),
                        )
                    self.assertNotIn("@@", str(refused.exception))

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
