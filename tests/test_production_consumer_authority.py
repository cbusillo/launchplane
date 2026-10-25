import copy
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

from control_plane import production_consumer_reads as reader


def serialized_record(value: dict[str, Any]) -> MagicMock:
    record = MagicMock()
    record.model_dump.side_effect = lambda **kwargs: copy.deepcopy(value)
    return record


class ConsumerAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = MagicMock()
        self.selection = MagicMock()
        self.selection.identity = SimpleNamespace(context="synthetic", instance="prod")
        self.override = serialized_record({"context": "synthetic"})
        self.store.read_odoo_instance_override_record.return_value = self.override
        self.binding = SimpleNamespace(
            secret_id="source-secret",
            binding_key="SOURCE_PASSWORD",
            declared_secret_class="shared_safe",
            sharing_reason=SimpleNamespace(kind="read_only_source"),
        )
        self.store.read_secret_record.return_value = SimpleNamespace(
            secret_id="source-secret", current_version_id="current-version"
        )
        version = serialized_record({"secret_id": "source-secret", "version_id": "current-version"})
        version.secret_id = "source-secret"
        version.version_id = "current-version"
        version.ciphertext = "synthetic-ciphertext"
        version.key_id = "synthetic-key"
        self.store.read_secret_version.return_value = version
        self.environment = SimpleNamespace(
            values={"SOURCE_HOST": "synthetic-host", "SOURCE_PASSWORD": "synthetic-password"},
            secret_keys={"SOURCE_PASSWORD"},
            retired_keys=set(),
        )
        self.parameters = [
            {"key": "repairshopr.sync_db.host", "value": {"environment_variable": "SOURCE_HOST"}},
            {
                "key": "repairshopr.sync_db.password",
                "value": {"environment_variable": "SOURCE_PASSWORD"},
            },
            {"key": "repairshopr.sync_db.name", "value": {"literal": "synthetic_database"}},
        ]
        authority = SimpleNamespace(metadata=serialized_record({}), runtime=serialized_record({}))
        patches = (
            patch.object(reader, "read_secret_reference_authority", return_value=authority),
            patch.object(
                reader,
                "selected_config_parameter_bindings",
                return_value={"repairshopr.sync_db.password": self.binding},
            ),
            patch.object(reader, "resolve_site_runtime_environment", return_value=self.environment),
            patch.object(reader, "_decrypt_secret_value", return_value="synthetic-password"),
            patch.object(
                reader,
                "render_post_deploy_payload",
                return_value=serialized_record(
                    {
                        "config_parameters": self.parameters
                        + [
                            {
                                "key": "unrelated.setting",
                                "value": {"environment_variable": "UNAVAILABLE"},
                            }
                        ]
                    }
                ),
            ),
        )
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def authority(self) -> tuple[dict[str, Any], str]:
        return reader.consumer_authority(self.store, Path("/synthetic"), self.selection)

    def test_complete_current_values_supply_all_desired_references(self) -> None:
        inputs, snapshot = self.authority()
        self.assertEqual(inputs["desired_env"], self.environment.values)
        self.assertEqual(inputs["desired_payload"]["config_parameters"], self.parameters)
        self.assertEqual(json.loads(snapshot)["inputs"], inputs)

    def test_missing_referenced_nonsecret_value_refuses_authority(self) -> None:
        del self.environment.values["SOURCE_HOST"]
        with self.assertRaises(ValueError):
            self.authority()

    def test_retired_references_refuse_even_when_values_remain_present(self) -> None:
        for key in self.environment.values:
            with self.subTest(key=key):
                self.environment.retired_keys = {key}
                with self.assertRaises(ValueError):
                    self.authority()


class StableSourceAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.current: dict[str, Any] = {
            "bindings": [
                {
                    "binding_id": "binding",
                    "secret_id": "secret",
                    "created_at": "first",
                    "updated_at": "first",
                }
            ],
            "secrets": [
                {
                    "secret_id": "secret",
                    "current_version_id": "version",
                    "last_validated_at": "first",
                }
            ],
            "inventory": {"updated_at": "first", "runtime_identity": {"artifact_id": "artifact"}},
            "runtime": [{"revision": "runtime-revision", "updated_at": "first"}],
        }

    def test_bookkeeping_renewal_preserves_identity_without_mutating_input(self) -> None:
        original = copy.deepcopy(self.current)
        renewed = copy.deepcopy(self.current)
        renewed["bindings"][0].update(created_at="later", updated_at="later")
        renewed["secrets"][0]["last_validated_at"] = "later"
        renewed["inventory"]["updated_at"] = "later"
        self.assertEqual(
            reader.stable_source_authority(self.current), reader.stable_source_authority(renewed)
        )
        self.assertEqual(self.current, original)

    def test_binding_secret_version_and_runtime_changes_preserve_drift(self) -> None:
        changes = (
            ("bindings", "secret_id", "another-secret"),
            ("secrets", "current_version_id", "another-version"),
            ("runtime", "revision", "another-revision"),
            ("runtime", "updated_at", "later"),
        )
        expected = reader.stable_source_authority(self.current)
        for collection, key, value in changes:
            with self.subTest(collection=collection, key=key):
                changed = copy.deepcopy(self.current)
                changed[collection][0][key] = value
                self.assertNotEqual(reader.stable_source_authority(changed), expected)
        changed = copy.deepcopy(self.current)
        changed["inventory"]["runtime_identity"]["artifact_id"] = "another-artifact"
        self.assertNotEqual(reader.stable_source_authority(changed), expected)

    def test_missing_inventory_remains_supported(self) -> None:
        self.current["inventory"] = None
        self.assertIsNone(reader.stable_source_authority(self.current)["inventory"])
