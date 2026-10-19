from collections.abc import Callable
from pathlib import Path

import click
import unittest
from unittest.mock import patch

from control_plane import secrets
from control_plane.generic_web_testing_inputs import (
    DELIVERY_RECEIPT_KEY,
    TestingInputSafetyRefused,
    testing_input_authority,
)
from control_plane.workflows.generic_web_deploy import record_observed_generic_web_deploy
from control_plane.workflows.ship import build_deployment_record
from control_plane.workflows.generic_web_deploy_provider import (
    GenericWebProviderDeploymentObservation,
)
from control_plane.contracts.product_profile_record import (
    ProductExpectedConfigProfile,
    ProductSecretConfigRequirement,
)
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.runtime_identity import RuntimeIdentity, runtime_identity_env
from control_plane.contracts.runtime_key_safety_policy import (
    RuntimeKeySafetyPolicyRecord,
    RuntimeSecretSafetyRule,
)
from control_plane.dokploy import api as dokploy_api
from control_plane.workflows.generic_web_deploy_provider import (
    DokployGenericWebDeployProvider,
    GenericWebResolvedDeployTarget,
    build_generic_web_provider_reconciliation_key,
    decode_generic_web_provider_reconciliation_target,
)
from tests.test_generic_web_deploy import (
    _DokployGenericWebDeployStore,
    _profile,
    _runtime_secret_binding,
    _runtime_secret_record,
    _runtime_secret_version,
)


class _InputStore(_DokployGenericWebDeployStore):
    policy_available: bool = True

    def list_runtime_key_safety_policy_records(
        self, **_: object
    ) -> tuple[RuntimeKeySafetyPolicyRecord, ...]:
        if not self.policy_available:
            return ()
        return (
            RuntimeKeySafetyPolicyRecord(
                record_id="input-policy",
                source="test",
                updated_at="2026-10-19T00:00:00Z",
                rules=(RuntimeSecretSafetyRule(binding_key="APP_TOKEN", secret_class="testing"),),
            ),
        )


class GenericWebTestingInputsTests(unittest.TestCase):
    def setUp(self) -> None:
        key = patch.dict(
            "os.environ", {secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key"}
        )
        key.start()
        self.addCleanup(key.stop)

    def store(self, *, include_secret: bool = True) -> _InputStore:
        profile = _profile()
        lane = profile.lanes[0]
        return _InputStore(
            profile,
            runtime_environment_records=(
                RuntimeEnvironmentRecord(
                    scope="context",
                    context=lane.context,
                    env={"APP_SETTING": "site value"},
                    updated_at="2026-10-19T00:00:00Z",
                    source_label="test",
                ),
            ),
            secret_records=(
                _runtime_secret_record(
                    secret_id="site-secret",
                    scope="context",
                    name="app-token",
                    version_id="site-version",
                    context=lane.context,
                ),
            )
            if include_secret
            else (),
            secret_versions=(
                _runtime_secret_version(
                    secret_id="site-secret",
                    version_id="site-version",
                    plaintext_value="fixture token",
                ),
            )
            if include_secret
            else (),
            secret_bindings=(
                _runtime_secret_binding(
                    secret_id="site-secret",
                    binding_key="APP_TOKEN",
                    context=lane.context,
                ),
            )
            if include_secret
            else (),
        )

    def resolve(self, store: _InputStore) -> GenericWebResolvedDeployTarget:
        return DokployGenericWebDeployProvider().resolve_deploy_target(
            control_plane_root=Path("."),
            request_artifact_id="example/app:sha-test",
            request_source_git_ref="test",
            request_timeout_seconds=10,
            request_no_cache=False,
            record_store=store,
            profile=store.profile,
            lane=store.profile.lanes[0],
            normalized_artifact_id="example/app:sha-test",
            fallback_target_name="fixture",
        )

    def execute(
        self,
        target: GenericWebResolvedDeployTarget,
        *,
        provider_env: str = "PROVIDER_ONLY=keep\n",
        persist: bool = True,
        checkpoint: Callable[[str], None] | None = None,
        registry_id: str = "",
    ) -> tuple[dict[str, str], list[str]]:
        identity = RuntimeIdentity(
            product="fixture",
            context=target.ship_request.context,
            instance=target.ship_request.instance,
            artifact_id=target.ship_request.artifact_id,
            source_git_ref="test",
            deployment_record_id="fixture-deploy",
            deployed_at="2026-10-19T00:00:00Z",
        )
        payload: dict[str, object] = {"env": provider_env}
        if registry_id:
            payload["registryId"] = registry_id
        events: list[str] = []
        self.events = events

        def update(**kwargs: object) -> None:
            if persist:
                payload["env"] = kwargs["env_text"]
            events.append("env")

        def checked(phase: str) -> None:
            events.append(phase)
            if checkpoint:
                checkpoint(phase)

        provider = DokployGenericWebDeployProvider()
        with (
            patch.object(provider, "_read_provider_config", return_value=("fixture", "unused")),
            patch(
                "control_plane.dokploy.api.fetch_dokploy_target_payload",
                side_effect=lambda **_: dict(payload),
            ),
            patch("control_plane.dokploy.api.latest_deployment_for_target", return_value=None),
            patch("control_plane.dokploy.api.update_dokploy_target_env", side_effect=update),
            patch(
                "control_plane.dokploy.api.dokploy_request",
                side_effect=lambda **_: events.append("image"),
            ),
            patch(
                "control_plane.dokploy.api.trigger_deployment",
                side_effect=lambda **_: events.append("deploy"),
            ),
            patch(
                "control_plane.dokploy.api.wait_for_titled_or_sole_new_target_deployment",
                return_value=None,
            ),
            patch("control_plane.dokploy.api.wait_for_target_deployment", return_value=None),
        ):
            provider.execute_artifact_deploy(
                control_plane_root=Path("."),
                resolved_deploy_target=target,
                runtime_identity=identity,
                deployment_title="",
                before_provider_mutation=checked,
                effect_started=lambda: None,
            )
        return dokploy_api.parse_dokploy_env_text(str(payload["env"])), events

    def test_actual_application_provider_delivers_site_setting_and_secret_before_deploy(
        self,
    ) -> None:
        target = self.resolve(self.store())
        delivered, events = self.execute(target)
        self.assertEqual(delivered["APP_SETTING"], "site value")
        self.assertEqual(delivered["APP_TOKEN"], "fixture token")
        self.assertEqual(delivered["PROVIDER_ONLY"], "keep")
        self.assertTrue(
            set(
                runtime_identity_env(
                    RuntimeIdentity(
                        product="fixture",
                        context=target.ship_request.context,
                        instance="testing",
                        artifact_id="fixture",
                        source_git_ref="test",
                        deployment_record_id="fixture",
                        deployed_at="2026-10-19T00:00:00Z",
                    )
                )
            )
            <= delivered.keys()
        )
        self.assertLess(events.index("provider_dispatch_prepared"), events.index("env"))
        self.assertLess(events.index("env"), events.index("runtime_inputs_verified"))
        self.assertLess(events.index("runtime_inputs_verified"), events.index("image"))
        self.assertLess(events.index("image"), events.index("deploy"))
        self.assertEqual(target.testing_runtime_verified_digest, target.testing_runtime_digest)
        self.assertNotIn("fixture token", repr(target))
        self.assertNotIn("fixture token", target.model_dump_json())

    def test_failed_readback_cannot_trigger_or_credit_delivery(self) -> None:
        target = self.resolve(self.store())
        with self.assertRaisesRegex(ValueError, "readback failed"):
            self.execute(target, persist=False)
        self.assertNotIn("deploy", self.events)
        self.assertNotIn("image", self.events)
        self.assertFalse(target.testing_runtime_verified_digest)

    def test_policy_revocation_after_prepare_stops_provider_effects(self) -> None:
        store = self.store()
        target = self.resolve(store)

        def revoke(phase: str) -> None:
            if phase == "provider_dispatch_prepared":
                store.policy_available = False

        with self.assertRaisesRegex(TestingInputSafetyRefused, "policy is unavailable"):
            self.execute(target, checkpoint=revoke)
        self.assertNotIn("env", self.events)
        self.assertNotIn("deploy", self.events)
        self.assertFalse(target.testing_runtime_verified_digest)

    def test_policy_guards_recheck_metadata_without_decrypting_again(self) -> None:
        store = self.store()
        target = self.resolve(store)
        with patch.object(store, "read_secret_version", wraps=store.read_secret_version) as read:
            self.execute(target)
        read.assert_called_once()

    def test_policy_revocation_after_readback_stops_image_and_deploy(self) -> None:
        store = self.store()
        target = self.resolve(store)

        def revoke(phase: str) -> None:
            if phase == "runtime_inputs_verified":
                store.policy_available = False

        with self.assertRaisesRegex(TestingInputSafetyRefused, "policy is unavailable"):
            self.execute(target, checkpoint=revoke)
        self.assertIn("env", self.events)
        self.assertNotIn("image", self.events)
        self.assertNotIn("deploy", self.events)

    def test_global_and_other_lane_inputs_are_not_delivered_or_fingerprinted(self) -> None:
        store = self.store()
        before = testing_input_authority(store, store.profile, store.profile.lanes[0])
        store.runtime_environment_records += (
            RuntimeEnvironmentRecord(
                scope="global", env={"GLOBAL_ONLY": "global"}, updated_at="2026-10-19T00:00:00Z"
            ),
            RuntimeEnvironmentRecord(
                scope="instance",
                context=store.profile.lanes[0].context,
                instance="prod",
                env={"PROD_ONLY": "prod"},
                updated_at="2026-10-19T00:00:00Z",
            ),
        )
        self.assertEqual(
            before, testing_input_authority(store, store.profile, store.profile.lanes[0])
        )
        delivered, _ = self.execute(self.resolve(store))
        self.assertNotIn("GLOBAL_ONLY", delivered)
        self.assertNotIn("PROD_ONLY", delivered)

    def test_retirement_only_removes_exact_key_and_preserves_provider_only(self) -> None:
        store = self.store(include_secret=False)
        store.runtime_environment_records = (
            RuntimeEnvironmentRecord(
                schema_version=2,
                env={},
                scope="instance",
                context=store.profile.lanes[0].context,
                instance="testing",
                retired_provider_keys=("OLD_TOKEN",),
                updated_at="2026-10-19T00:00:00Z",
            ),
        )
        delivered, _ = self.execute(
            self.resolve(store), provider_env="OLD_TOKEN=old\nPROVIDER_ONLY=keep"
        )
        self.assertNotIn("OLD_TOKEN", delivered)
        self.assertEqual(delivered["PROVIDER_ONLY"], "keep")

    def test_platform_credential_in_provider_env_requires_explicit_retirement(self) -> None:
        with self.assertRaisesRegex(ValueError, "platform credentials"):
            self.execute(
                self.resolve(self.store()), provider_env="GITHUB_TOKEN=ghp_" + "A1b2C3d4E5" * 4
            )

    def test_input_refusal_precedes_saved_registry_login(self) -> None:
        with self.assertRaisesRegex(ValueError, "platform credentials"):
            self.execute(
                self.resolve(self.store()),
                provider_env="GITHUB_TOKEN=ghp_" + "A1b2C3d4E5" * 4,
                registry_id="fixture-registry",
            )
        self.assertNotIn("image", self.events)

    def test_new_required_secret_provenance_changes_authority_and_refuses_plain_value(self) -> None:
        store = self.store(include_secret=False)
        before = testing_input_authority(store, store.profile, store.profile.lanes[0])
        store.profile = store.profile.model_copy(
            update={
                "expected_config": ProductExpectedConfigProfile(
                    managed_secret_bindings=(
                        ProductSecretConfigRequirement(binding_key="APP_SETTING"),
                    )
                )
            }
        )
        self.assertNotEqual(
            before, testing_input_authority(store, store.profile, store.profile.lanes[0])
        )
        with self.assertRaisesRegex(ValueError, "Declared managed secret"):
            self.execute(self.resolve(store))

    def test_undeclared_delivered_secret_still_requires_testing_key_safety(self) -> None:
        store = self.store()
        with patch.object(store, "list_runtime_key_safety_policy_records", return_value=()):
            with self.assertRaisesRegex(TestingInputSafetyRefused, "policy is unavailable"):
                self.execute(self.resolve(store))

    def test_authority_change_after_resolution_refuses_before_effects(self) -> None:
        store = self.store()
        target = self.resolve(store)
        store.runtime_environment_records = (
            store.runtime_environment_records[0].model_copy(
                update={"env": {"APP_SETTING": "changed"}}
            ),
        )
        with self.assertRaisesRegex(ValueError, "authority changed"):
            self.execute(target)

    def test_profile_provenance_change_after_capture_refuses_before_mutation(self) -> None:
        store = self.store(include_secret=False)
        target = self.resolve(store)
        store.profile = store.profile.model_copy(
            update={
                "expected_config": ProductExpectedConfigProfile(
                    managed_secret_bindings=(
                        ProductSecretConfigRequirement(binding_key="APP_SETTING"),
                    )
                )
            }
        )
        with self.assertRaisesRegex(ValueError, "authority changed"):
            self.execute(target)

    def test_secret_rotation_changes_authority_without_decrypting_values(self) -> None:
        store = self.store()
        lane = store.profile.lanes[0]
        with patch.object(store, "read_secret_version", side_effect=AssertionError("value read")):
            before = testing_input_authority(store, store.profile, lane)
            store.secret_records = (
                store.secret_records[0].model_copy(update={"current_version_id": "next-version"}),
            )
            self.assertNotEqual(before, testing_input_authority(store, store.profile, lane))

    def test_missing_declared_setting_refuses_before_provider_effect(self) -> None:
        from control_plane.contracts.product_profile_record import ProductRuntimeConfigRequirement

        store = self.store()
        store.profile = store.profile.model_copy(
            update={
                "expected_config": ProductExpectedConfigProfile(
                    runtime_environment_keys=(
                        ProductRuntimeConfigRequirement(key="MISSING_SETTING"),
                    )
                )
            }
        )
        with self.assertRaisesRegex(ValueError, "MISSING_SETTING"):
            self.execute(self.resolve(store))

    def test_stale_caller_profile_cannot_skip_current_required_plain_key(self) -> None:
        from control_plane.contracts.product_profile_record import ProductRuntimeConfigRequirement
        from control_plane.generic_web_testing_inputs import prepare_testing_inputs

        store = self.store(include_secret=False)
        stale = store.profile
        store.profile = stale.model_copy(
            update={
                "expected_config": ProductExpectedConfigProfile(
                    runtime_environment_keys=(
                        ProductRuntimeConfigRequirement(key="MISSING_SETTING"),
                    )
                )
            }
        )
        digest = testing_input_authority(store, stale, stale.lanes[0])
        with self.assertRaisesRegex(click.ClickException, "MISSING_SETTING"):
            prepare_testing_inputs(store, stale, stale.lanes[0], digest)

    def test_platform_credential_under_application_name_refuses(self) -> None:
        store = self.store(include_secret=False)
        store.runtime_environment_records = (
            store.runtime_environment_records[0].model_copy(
                update={"env": {"APP_SETTING": "ghp_" + "A1b2C3d4E5" * 4}}
            ),
        )
        with self.assertRaisesRegex(ValueError, "APP_SETTING"):
            self.execute(self.resolve(store))

    def test_recovery_snapshot_carries_no_values_and_legacy_gets_no_receipt(self) -> None:
        store = self.store()
        target = self.resolve(store)
        key = build_generic_web_provider_reconciliation_key(target)
        snapshot = decode_generic_web_provider_reconciliation_target(key)
        self.assertEqual(snapshot.testing_runtime_digest, target.testing_runtime_digest)
        self.assertNotIn("fixture token", snapshot.model_dump_json())
        for verified in (False, True):
            with self.subTest(verified=verified):
                record = build_deployment_record(
                    request=target.ship_request,
                    record_id="original",
                    deployment_id="original-provider",
                    deployment_status="pending",
                    started_at="2026-10-19T00:00:00Z",
                    finished_at="",
                    resolved_target=target.resolved_target,
                    runtime_source={DELIVERY_RECEIPT_KEY: target.testing_runtime_digest}
                    if verified
                    else {},
                )
                store.write_deployment_record(record)
                record_observed_generic_web_deploy(
                    record_store=store,
                    profile=store.profile,
                    lane=store.profile.lanes[0],
                    resolved_deploy_target=target,
                    observation=GenericWebProviderDeploymentObservation(
                        outcome="present",
                        deployment_status="success",
                        deployment_id="original-provider",
                        started_at="2026-10-19T00:00:00Z",
                        finished_at="2026-10-19T00:01:00Z",
                    ),
                    deployment_record_id="original",
                    post_deploy_unobserved=False,
                )
                recovered = store.read_deployment_record("original")
                self.assertEqual(
                    recovered.runtime_source.get(DELIVERY_RECEIPT_KEY, ""),
                    target.testing_runtime_digest if verified else "",
                )

    def test_no_managed_inputs_needs_no_crypto_or_new_delivery(self) -> None:
        store = self.store(include_secret=False)
        store.runtime_environment_records = ()
        with patch.object(
            secrets,
            "keyed_secret_payload_fingerprint",
            side_effect=AssertionError("unneeded crypto"),
        ):
            self.assertFalse(self.resolve(store).testing_runtime_digest)

    def test_production_and_odoo_do_not_activate_testing_delivery(self) -> None:
        store = self.store()
        for profile in (
            store.profile.model_copy(update={"driver_id": "odoo"}),
            store.profile.model_copy(
                update={"lanes": (store.profile.lanes[0].model_copy(update={"instance": "prod"}),)}
            ),
        ):
            self.assertFalse(testing_input_authority(store, profile, profile.lanes[0]))
