import json
import unittest
from contextlib import closing
from unittest.mock import patch

from control_plane.contracts.runtime_key_safety_policy import RuntimeSecretSafetyRule
from control_plane.dokploy import api as dokploy_api
from control_plane.live_target_runtime import LiveTargetRuntimeApplyEnvelope, LiveTargetRuntimeError
from control_plane import secrets
from control_plane.service_auth import LaunchplaneAuthzPolicy
from control_plane.storage.postgres import PostgresRecordStore
from tests.support.stores import _seed_tracked_target_records, _write_runtime_key_safety_policy
from tests.support.auth import _StubVerifier, _identity
from tests.test_runtime_platform_credentials import _LaneFixture, _instance_record
from tests.test_service import (
    _invoke_app,
    create_launchplane_fastapi_test_app,
)


def _delta(result: dict[str, object]) -> dict[str, object]:
    delta = result["runtime_environment"]
    assert isinstance(delta, dict)
    return delta


class SelectedRuntimeSyncTests(unittest.TestCase):
    def test_compose_selection_preserves_unselected_legacy_transport_and_retained_password(
        self,
    ) -> None:
        lane = _LaneFixture(
            self,
            _instance_record({"APP_MODE": "new $VALUE", "ODOO_DB_PASSWORD": "different-password"}),
        )
        _seed_tracked_target_records(
            database_url=lane.database_url,
            context="example-site",
            instance="testing",
            target_id="app-testing",
            target_type="compose",
            target_name="example-site-testing",
        )
        provider = "APP_MODE=old\nODOO_DB_PASSWORD=legacy-$PASSWORD\nPROVIDER_ONLY=$REFERENCE"
        result, updates = lane.sync(provider_env=provider, selected_keys=("APP_MODE",))
        written = str(updates[0]["env_text"])
        self.assertEqual(
            dokploy_api.parse_dokploy_env_text(written),
            {
                "APP_MODE": "new $VALUE",
                "ODOO_DB_PASSWORD": "legacy-$PASSWORD",
                "PROVIDER_ONLY": "$REFERENCE",
            },
        )
        self.assertEqual(dokploy_api.compose_literal_keys(written), {"APP_MODE"})
        self.assertEqual(_delta(result)["changed_keys"], ["APP_MODE"])
        with self.assertRaises(LiveTargetRuntimeError) as refusal:
            lane.sync(provider_env=written, selected_keys=("ODOO_DB_PASSWORD",))
        self.assertEqual(refusal.exception.code, "retained_database_credential_changed")

    def test_http_selection_keeps_authorization_and_idempotency_scoped(self) -> None:
        lane = _LaneFixture(
            self, _instance_record({"APP_MODE": "managed", "APP_WEBHOOK_URL": "new"})
        )
        policy = LaunchplaneAuthzPolicy.model_validate(
            {
                "github_actions": [
                    {
                        "repository": "example/control",
                        "products": ["example-site"],
                        "contexts": ["example-site"],
                        "actions": ["live_target_runtime.plan", "live_target_runtime.apply"],
                    }
                ]
            }
        )
        app = create_launchplane_fastapi_test_app(
            state_dir=lane.root / "state",
            database_url=lane.database_url,
            control_plane_root_path=lane.root,
            authz_policy=policy,
            verifier=_StubVerifier(
                _identity(repository="example/control", event_name="workflow_dispatch")
            ),
        )
        payload = dict(
            mode="apply",
            product="example-site",
            context="example-site",
            instance="testing",
            selected_keys=["APP_WEBHOOK_URL"],
        )
        provider = {"env": "APP_MODE=drifted\nAPP_WEBHOOK_URL=old"}

        def update(**kwargs: object) -> None:
            provider["env"] = str(kwargs["env_text"])

        with (
            patch(
                "control_plane.live_target_runtime.dokploy_source.read_dokploy_config",
                return_value=("host", "token"),
            ),
            patch(
                "control_plane.live_target_runtime.dokploy_api.fetch_dokploy_target_payload",
                side_effect=lambda **_kwargs: dict(provider),
            ),
            patch(
                "control_plane.live_target_runtime.dokploy_api.update_dokploy_target_env",
                side_effect=update,
            ) as write,
        ):
            status, result = _invoke_app(
                app,
                method="POST",
                path="/v1/live-target-runtime/apply",
                payload=payload,
                headers={"Idempotency-Key": "selected-sync"},
            )
            self.assertEqual(status, 202, result)
            self.assertEqual(result["result"]["selected_keys"], payload["selected_keys"])
            self.assertEqual(provider["env"], "APP_MODE=drifted\nAPP_WEBHOOK_URL=new")
            status, replay = _invoke_app(
                app,
                method="POST",
                path="/v1/live-target-runtime/apply",
                payload=payload,
                headers={"Idempotency-Key": "selected-sync"},
            )
            self.assertEqual(status, 202, replay)
            write.assert_called_once()
            status, refusal = _invoke_app(
                app,
                method="POST",
                path="/v1/live-target-runtime/apply",
                payload={**payload, "selected_keys": ["APP_MODE"]},
                headers={"Idempotency-Key": "selected-sync"},
            )
            self.assertEqual(status, 409, refusal)
            self.assertEqual(refusal["error"]["code"], "idempotency_key_reused")
            policy.github_actions[0].actions = ("live_target_runtime.plan",)
            status, refusal = _invoke_app(
                app,
                method="POST",
                path="/v1/live-target-runtime/apply",
                payload=payload,
                headers={"Idempotency-Key": "unauthorized-selected-sync"},
            )
            self.assertEqual(status, 403, refusal)
            self.assertEqual(refusal["error"]["code"], "authorization_denied")
            write.assert_called_once()

    def test_selected_delivery_preserves_drift_retirement_intent_and_provider_values(self) -> None:
        lane = _LaneFixture(
            self,
            _instance_record(
                {"APP_MODE": "managed", "APP_WEBHOOK_URL": "new"}, retired=("OLD_TOKEN",)
            ),
        )
        provider = (
            "APP_MODE=drifted\nAPP_WEBHOOK_URL=old\nOLD_TOKEN=retained\nPROVIDER_ONLY=untouched"
        )
        plan, updates = lane.sync(
            provider_env=provider, apply_changes=False, selected_keys=("APP_WEBHOOK_URL",)
        )
        self.assertEqual(updates, [])
        self.assertEqual(_delta(plan)["changed_keys"], ["APP_WEBHOOK_URL"])
        result, updates = lane.sync(provider_env=provider, selected_keys=("APP_WEBHOOK_URL",))
        self.assertEqual(result["selected_keys"], ["APP_WEBHOOK_URL"])
        applied = result["apply"]
        assert isinstance(applied, dict)
        self.assertEqual(applied["verification"]["status"], "pass")
        self.assertEqual(
            dokploy_api.parse_dokploy_env_text(str(updates[0]["env_text"])),
            {
                "APP_MODE": "drifted",
                "APP_WEBHOOK_URL": "new",
                "OLD_TOKEN": "retained",
                "PROVIDER_ONLY": "untouched",
            },
        )
        readback, updates = lane.sync(
            provider_env=str(updates[0]["env_text"]),
            apply_changes=False,
            selected_keys=("APP_WEBHOOK_URL",),
        )
        self.assertEqual(_delta(readback)["changed_keys"], [])
        self.assertEqual(updates, [])
        with closing(PostgresRecordStore(database_url=lane.database_url)) as store:
            self.assertEqual(
                store.list_runtime_environment_records()[0].retired_provider_keys, ("OLD_TOKEN",)
            )

    def test_unknown_retired_or_empty_selection_refuses_before_provider_mutation(self) -> None:
        lane = _LaneFixture(self, _instance_record({"APP_MODE": "managed"}, retired=("OLD_TOKEN",)))
        for selection in (("UNKNOWN",), ("OLD_TOKEN",), ()):
            with (
                self.subTest(selection=selection),
                patch(
                    "control_plane.live_target_runtime.dokploy_api.update_dokploy_target_env"
                ) as update,
            ):
                with self.assertRaises(LiveTargetRuntimeError):
                    lane.sync(
                        provider_env="APP_MODE=old\nOLD_TOKEN=retained", selected_keys=selection
                    )
                update.assert_not_called()

    def test_selection_requires_nonempty_unique_names_and_cannot_stage_separation(self) -> None:
        payload = dict(
            mode="dry-run", product="example-site", context="example-site", instance="testing"
        )
        selected = LiveTargetRuntimeApplyEnvelope.model_validate(
            {**payload, "selected_keys": ["B", "A"]}
        )
        self.assertEqual(selected.selected_keys, ("A", "B"))
        invalid_requests: tuple[dict[str, object], ...] = (
            {"selected_keys": []},
            {"selected_keys": ["A", "A"]},
            {"selected_keys": ["A=value"]},
            {"selected_keys": ["A"], "separation_only": True},
            {"selected_keys": ["A"], "requalify_literal_keys": ["B"]},
        )
        for changes in invalid_requests:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                LiveTargetRuntimeApplyEnvelope.model_validate({**payload, **changes})

    def test_managed_secret_delivery_returns_no_plaintext(self) -> None:
        lane = _LaneFixture(self, _instance_record({"APP_MODE": "managed"}))
        key = "APP_TOKEN"
        secret_value = "selected-secret-sentinel"
        with patch.dict(
            "os.environ", {secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key"}
        ):
            with closing(PostgresRecordStore(database_url=lane.database_url)) as store:
                secrets.write_secret_value(
                    record_store=store,
                    scope="context_instance",
                    integration=secrets.RUNTIME_ENVIRONMENT_SECRET_INTEGRATION,
                    name="app-token",
                    plaintext_value=secret_value,
                    binding_key=key,
                    context_name="example-site",
                    instance_name="testing",
                    actor="test",
                    source_label="test",
                )
            _write_runtime_key_safety_policy(
                database_url=lane.database_url,
                rules=(
                    RuntimeSecretSafetyRule(
                        binding_key=key,
                        secret_class="testing",
                        allowed_contexts=("example-site",),
                        allowed_instances=("testing",),
                    ),
                ),
            )
            plan, _ = lane.sync(
                provider_env="APP_MODE=drifted", apply_changes=False, selected_keys=(key,)
            )
            result, updates = lane.sync(provider_env="APP_MODE=drifted", selected_keys=(key,))
        self.assertEqual(
            dokploy_api.parse_dokploy_env_text(str(updates[0]["env_text"]))[key], secret_value
        )
        for response in (plan, result):
            self.assertNotIn(secret_value, json.dumps(response))
            self.assertEqual(_delta(response)["changed_keys"], [key])

    def test_selected_readback_failure_is_not_hidden_by_unrelated_drift(self) -> None:
        lane = _LaneFixture(
            self, _instance_record({"APP_MODE": "managed", "APP_WEBHOOK_URL": "new"})
        )
        with self.assertRaises(LiveTargetRuntimeError) as refusal:
            lane.sync(
                provider_env="APP_MODE=drifted\nAPP_WEBHOOK_URL=old",
                readback_env="APP_MODE=drifted\nAPP_WEBHOOK_URL=old",
                selected_keys=("APP_WEBHOOK_URL",),
            )
        self.assertEqual(refusal.exception.code, "dokploy_target_verification_failed")
