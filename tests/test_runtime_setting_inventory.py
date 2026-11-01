import unittest

from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.secret_record import SecretBinding
from control_plane.contracts.odoo_instance_override_record import OdooInstanceOverrideRecord
from control_plane.runtime_setting_inventory import (
    SettingAuthorityConsumer,
    SettingConsumer,
    setting_consumer_inventory,
    product_setting_source_inventory,
)
from tests.support.profiles import _odoo_profile_payload_with_prod_lane
from tests.test_reviewed_runtime_setting_removal import runtime


class SettingConsumerInventoryTests(unittest.TestCase):
    def test_preview_sources_and_override_references_preserve_their_roles(self) -> None:
        profile = LaunchplaneProductProfileRecord.model_validate(
            _odoo_profile_payload_with_prod_lane()
        )
        context = profile.preview.context
        record = OdooInstanceOverrideRecord.model_validate(
            {
                "context": context,
                "instance": "testing",
                "updated_at": "fixture-time",
                "config_parameters": [
                    {
                        "key": "cm_data.db.user",
                        "value": {"source": "runtime_environment"},
                    }
                ],
                "addon_settings": [
                    {
                        "addon": "shopify",
                        "setting": "api_version",
                        "value": {"source": "literal", "value": "private-addon-value"},
                    }
                ],
            }
        )
        result = product_setting_source_inventory(
            profile=profile,
            records=(
                runtime(scope="context", context=context, env={"CONTEXT_ONLY": "private-context"}),
                runtime(
                    scope="instance",
                    context=context,
                    instance="testing",
                    env={"ENV_OVERRIDE_CONFIG_PARAM__CM_DATA__DB__USER": "private-runtime"},
                ),
                runtime(
                    scope="instance",
                    context=context,
                    instance="preview",
                    env={"PREVIEW_ONLY": "private-preview"},
                ),
            ),
            targets=(),
            bindings=(),
            overrides=(record,),
            previews=(),
        )
        testing = next(lane for lane in result.lanes if lane.instance == "testing")
        self.assertFalse(testing.overlapping_keys)
        preview = next(lane for lane in result.lanes if lane.instance == "preview")
        template = next(source for source in preview.sources if source.kind == "preview_template")
        self.assertIn("CONTEXT_ONLY", template.selected_keys or ())
        self.assertFalse(
            any(
                source.kind == "runtime_record" and source.scope == "context"
                for source in preview.sources
            )
        )
        self.assertIn(
            "PREVIEW_ONLY",
            {key for source in preview.sources for key in source.selected_keys or ()},
        )
        self.assertNotIn("private-", result.model_dump_json())

    def test_key_bounds_do_not_claim_complete_sources(self) -> None:
        profile = LaunchplaneProductProfileRecord.model_validate(
            _odoo_profile_payload_with_prod_lane()
        )
        report = product_setting_source_inventory(
            profile=profile,
            records=(
                runtime(
                    scope="context",
                    context=profile.lanes[0].context,
                    env={"A": "private-a", "B": "private-b"},
                ),
            ),
            targets=(),
            bindings=(),
            overrides=(),
            previews=(),
            limit=1,
        )
        self.assertTrue(report.sources_truncated)
        self.assertTrue(
            all(
                len(source.selected_keys or ()) <= 1
                for lane in report.lanes
                for source in lane.sources
            )
        )

    def test_product_sources_report_overlapping_layers_without_values(self) -> None:
        profile = LaunchplaneProductProfileRecord.model_validate(
            _odoo_profile_payload_with_prod_lane("example-site")
        )
        context = profile.lanes[0].context
        report = product_setting_source_inventory(
            profile=profile,
            records=(
                runtime(scope="context", context=context, env={"SETTING": "private-context"}),
                runtime(
                    scope="instance",
                    context=context,
                    instance="prod",
                    env={"SETTING": "private-lane"},
                ),
                runtime(scope="context", context="other-site", env={"HIDDEN": "private-other"}),
            ),
            targets=(
                DokployTargetRecord(
                    context=context,
                    instance="prod",
                    env={"SETTING": "private-target"},
                    updated_at="fixture-time",
                ),
            ),
            bindings=(),
            overrides=(),
            previews=(),
        )
        lane = next(item for item in report.lanes if item.instance == "prod")
        self.assertEqual(lane.overlapping_keys, ("SETTING",))
        self.assertEqual(
            {item.kind for item in lane.sources if item.selected_keys},
            {"runtime_record", "target_overlay"},
        )
        wire = report.model_dump_json()
        for private in ("private-context", "private-lane", "private-target", "HIDDEN"):
            self.assertNotIn(private, wire)
        self.assertFalse(report.runtime_consumption_verified)

    def test_orphan_records_retired_profiles_and_global_secret_metadata_are_included(self) -> None:
        payload = _odoo_profile_payload_with_prod_lane("retired-site")
        payload["expected_config"] = {
            "runtime_environment_keys": [{"key": "REMOVE"}],
            "managed_secret_bindings": [
                {"binding_key": "REMOVE", "context": "old-site", "instance": "prod"}
            ],
        }
        payload["lifecycle_state"] = "retired"
        payload["preview"] = {
            "enabled": False,
            "context": "",
            "override_env": {"REMOVE": "private-preview"},
            "copied_env_keys": ["REMOVE"],
            "omitted_env_keys": ["OMITTED"],
        }
        profile = LaunchplaneProductProfileRecord.model_validate(payload)
        result = setting_consumer_inventory(
            keys=("REMOVE", "OMITTED"),
            records=(
                runtime(),
                runtime(scope="instance", context="orphan", instance="historical"),
                runtime(scope="context", context="empty-match", env={"OTHER": "private-other"}),
            ),
            profiles=(profile,),
            targets=(
                DokployTargetRecord(
                    context="orphan",
                    instance="historical",
                    env={"REMOVE": "private-target"},
                    updated_at="fixture-time",
                ),
            ),
            bindings=(
                SecretBinding(
                    binding_id="private-binding",
                    secret_id="private-secret",
                    integration="runtime_environment",
                    binding_key="REMOVE",
                    created_at="fixture-time",
                    updated_at="fixture-time",
                ),
            ),
            authority_records=(
                SettingAuthorityConsumer(
                    kind="production_recipient",
                    product="retained-site",
                    context="old-site",
                    instance="prod",
                    state="prepared",
                ),
            ),
        )
        by_kind: dict[str, list[SettingConsumer]] = {}
        for consumer in result.consumers:
            by_kind.setdefault(consumer.kind, []).append(consumer)
        self.assertTrue(any(c.context == "orphan" for c in result.consumers))
        self.assertTrue(any(c.product == profile.product for c in result.consumers))
        self.assertEqual(by_kind["secret_binding"][0].selected_keys, ("REMOVE",))
        self.assertEqual(by_kind["runtime_requirement"][0].selected_keys, ("REMOVE",))
        self.assertEqual(by_kind["secret_requirement"][0].context, "old-site")
        self.assertEqual(
            {c.scope for c in result.consumers if c.kind == "preview_policy"},
            {"copied_env_keys", "omitted_env_keys"},
        )
        self.assertEqual(result.runtime_record_count, 3)
        self.assertFalse(result.truncated)
        wire = result.model_dump_json()
        for private in (
            "private-value",
            "private-preview",
            "private-target",
            "private-secret",
            "private-binding",
            "private-other",
        ):
            self.assertNotIn(private, wire)

    def test_truncation_preserves_full_population_counts_and_authority_limits(self) -> None:
        authorities = tuple(
            SettingAuthorityConsumer(
                kind="reconcile_request", product=f"site-{n}", instance="testing", state="failed"
            )
            for n in range(3)
        )
        records = (
            runtime(env={"OTHER": "unmatched"}),
            runtime(scope="context", context="undeclared"),
        )
        result = setting_consumer_inventory(
            keys=("REMOVE",),
            records=iter(records),
            profiles=(),
            targets=(),
            bindings=(),
            authority_records=iter(authorities),
            limit=1,
        )
        self.assertTrue(result.truncated)
        self.assertTrue(result.authority_records_truncated)
        self.assertEqual(result.total_consumers, len(records))
        self.assertEqual(result.authority_record_count, len(authorities))
        self.assertEqual(len(result.consumers), 1)
        self.assertEqual(result.consumers[0].context, "undeclared")
        self.assertEqual(len(result.authority_records), 1)
