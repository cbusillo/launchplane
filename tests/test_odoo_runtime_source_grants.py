import base64
import json
import subprocess
from typing import Any, Literal
from unittest.mock import patch

from control_plane import odoo_runtime_source_prerequisites as reader
from control_plane.odoo_import_diagnostics import SOURCE_PREFIXES
from control_plane.odoo_instance_overrides import (
    ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY,
    config_parameter_secret_env_key,
)
from tests.test_odoo_import_diagnostics import PRIVATE
from tests.test_odoo_runtime_source_prerequisites import PrerequisiteFixture


class RuntimeSourceTests(PrerequisiteFixture):
    def setUp(self) -> None:
        super().setUp()
        self.probes: list[dict[str, str]] = []
        self.probe_failure = ""
        self.probe_unknown = False
        for name, prefix in SOURCE_PREFIXES.items():
            self.values[prefix + ".host"] = name + ".invalid"
        self.set_overrides({"fishbowl.db": "inventory"})
        self.values["fishbowl.db"] = "inventory"

    def subprocess_run(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if command[:3] == ["python3", "-I", "-c"] or command[1:3] == ["-I", "-c"]:
            settings = json.loads(kwargs["input"])
            self.probes.append(settings)
            if self.probe_failure == settings["host"]:
                if self.probe_unknown:
                    return subprocess.CompletedProcess(
                        command, 0, stdout=json.dumps({"grants": "unknown"})
                    )
                raise subprocess.TimeoutExpired(command, 10)
            return subprocess.CompletedProcess(
                command, 0, stdout=json.dumps({"grants": "read_only"})
            )
        return super().subprocess_run(command, **kwargs)

    def consume(self, mode: Literal["grants", "preview"] = "grants") -> reader.SourceConsumerResult:
        from control_plane import odoo_runtime_reads as reads

        with (
            patch.object(reader, "_read_remote", side_effect=self.remote),
            patch.object(
                reads, "select_stable_runtime", return_value=self.fixture.connection.selection
            ),
            patch.object(reads, "connect_runtime", return_value=self.fixture.connection),
            patch.object(
                reads, "inspect_runtime_container", side_effect=lambda *args: self.env.copy()
            ),
            patch.object(reads, "assert_selection_current"),
            patch.object(reader, "_decrypt_secret_value", return_value=PRIVATE),
        ):
            return reader.read_source_consumers(
                store=self.fixture.store,
                product=self.fixture.profile.product,
                control_plane_root=self.root,
                database_url=None,
                mode=mode,
                sources=tuple(SOURCE_PREFIXES) if mode == "grants" else ("fishbowl",),
            )

    def test_capacity_and_reader_failures_keep_bounded_stages(self) -> None:
        with patch.object(reader._READ_SLOTS, "acquire", return_value=False):
            result = self.consume()
        self.assertEqual(result.failure_stage, "capacity")
        self.assertEqual(self.probes, [])
        for failure, expected in (
            (RuntimeError(PRIVATE), "transport"),
            (None, "result_validation"),
        ):
            with self.subTest(expected=expected), patch.object(self, "remote", side_effect=failure):
                result = self.consume()
                self.assertEqual(result.failure_stage, expected)
                self.assertTrue(all(item.grants == "unknown" for item in result.sources))
                self.assertNotIn(PRIVATE, result.model_dump_json())

    def test_invalid_failure_stages_are_redacted_validation_failures(self) -> None:
        minimal = {
            "runtime": "unknown",
            "sources": [{"source": name} for name in reader.SOURCE_ADDONS],
        }
        for field in ("global", "source"):
            with self.subTest(field=field):
                value = json.loads(json.dumps(minimal))
                target = value if field == "global" else value["sources"][0]
                target["failure_stage"] = PRIVATE

                def remote(**kwargs: Any) -> None:
                    kwargs["receive"]({"type": "complete", "result": value})

                with patch.object(self, "remote", side_effect=remote):
                    result = self.consume()
                self.assertEqual(result.failure_stage, "result_validation")
                self.assertNotIn(PRIVATE, result.model_dump_json())
                self.assertEqual(self.probes, [])

    def test_uninstalled_source_keeps_its_stage_and_other_grants(self) -> None:
        self.installed[reader.SOURCE_ADDONS["cm_data"][0]] = "uninstalled"
        result = self.consume()
        self.assertEqual(result.runtime, "verified")
        failed = next(item for item in result.sources if item.source == "cm_data")
        self.assertEqual(failed.failure_stage, "addon_state")
        self.assertEqual(failed.grants, "unknown")
        self.assertTrue(
            all(
                item.grants == "read_only"
                for item in result.sources
                if item.source != failed.source
            )
        )

    def test_typed_payload_and_installed_defaults_supply_runtime_origin_grants(self) -> None:
        result = self.consume()
        self.assertEqual(result.runtime, "verified")
        self.assertTrue(all(source.grants == "read_only" for source in result.sources), result)
        self.assertEqual(
            {item["host"] for item in self.probes},
            {self.values[prefix + ".host"] for prefix in SOURCE_PREFIXES.values()},
        )
        self.assertNotIn(PRIVATE, result.model_dump_json())
        self.assertTrue(all(not source.parameters for source in result.sources))
        self.pg.rollback.assert_called()
        self.pg.close.assert_called()

    def test_omitted_options_keep_persisted_values_and_preview_freezes_effective_source(
        self,
    ) -> None:
        self.values["fishbowl.port"] = "3499"
        result = self.consume("preview")
        source = next(item for item in result.sources if item.source == "fishbowl")
        self.assertEqual(source.parameters["port"], self.values["fishbowl.port"])
        self.assertEqual(source.parameters["db"], self.values["fishbowl.db"])
        self.assertNotIn(PRIVATE, result.model_dump_json())
        self.assertEqual(self.probes, [])

    def test_explicit_option_must_agree_with_applied_and_delivered_payload(self) -> None:
        self.set_overrides({"fishbowl.db": "inventory", "fishbowl.port": 3401})
        result = self.consume()
        self.assertEqual(
            next(item for item in result.sources if item.source == "fishbowl").grants, "unknown"
        )
        self.assertEqual(len(self.probes), 2)
        self.values["fishbowl.port"] = "3401"
        self.assertEqual(
            next(item for item in self.consume().sources if item.source == "fishbowl").grants,
            "read_only",
        )

    def test_missing_payload_legacy_conflict_and_uninstalled_addon_never_probe(self) -> None:
        self.env.pop(ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY)
        self.assertTrue(all(item.grants == "unknown" for item in self.consume().sources))
        self.assertEqual(self.probes, [])
        self.deliver_overrides()
        self.fixture.runtime = self.fixture.runtime.model_copy(
            update={"env": {"ENV_OVERRIDE_CONFIG_PARAM__FISHBOWL__PORT": "3306"}}
        )
        result = self.consume("preview")
        self.assertFalse(
            next(item for item in result.sources if item.source == "fishbowl").parameters
        )
        self.fixture.runtime = self.fixture.runtime.model_copy(update={"env": {}})
        self.installed = {key: "uninstalled" for key in self.installed}
        self.assertTrue(all(item.grants == "unknown" for item in self.consume().sources))
        self.assertEqual(self.probes, [])

    def test_managed_password_must_match_agreeing_applied_and_delivered_values(self) -> None:
        for prefix in SOURCE_PREFIXES.values():
            key = prefix + ".password"
            self.values[key] = "different-value"
            self.env[config_parameter_secret_env_key(key)] = "different-value"
        modes: tuple[Literal["grants", "preview"], ...] = ("grants", "preview")
        for mode in modes:
            with self.subTest(mode=mode):
                self.probes.clear()
                result = self.consume(mode)
                self.assertEqual(result.runtime, "verified")
                self.assertTrue(all(item.grants == "unknown" for item in result.sources))
                self.assertTrue(all(not item.parameters for item in result.sources))
                self.assertEqual(self.probes, [])

    def test_applied_password_tls_and_payload_disagreement_remain_unknown(self) -> None:
        for kind in ("delivered_password", "disabled_tls", "payload_mismatch"):
            with self.subTest(kind=kind):
                self.probes.clear()
                self.set_overrides({"fishbowl.db": "inventory"})
                self.values.pop("fishbowl.ssl_verify", None)
                self.env[config_parameter_secret_env_key("fishbowl.password")] = PRIVATE
                if kind == "delivered_password":
                    self.values["fishbowl.password"] = "different-value"
                elif kind == "disabled_tls":
                    self.values["fishbowl.ssl_verify"] = "false"
                elif kind == "payload_mismatch":
                    payload = json.loads(
                        base64.b64decode(self.env[ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY])
                    )
                    next(
                        item
                        for item in payload["config_parameters"]
                        if item["key"] == "fishbowl.host"
                    )["value"]["value"] = "other.invalid"
                    self.env[ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY] = base64.b64encode(
                        json.dumps(payload).encode()
                    ).decode()
                result = self.consume("preview")
                source = next(item for item in result.sources if item.source == "fishbowl")
                self.assertFalse(source.parameters)
                self.assertEqual(source.failure_stage, "source_resolution")
                self.values["fishbowl.password"] = PRIVATE

    def test_parameter_and_container_drift_discards_the_observation(self) -> None:
        for kind in ("parameter", "container", "image"):
            with self.subTest(kind=kind):
                self.parameter_reads = 0
                self.inspections = 0
                self.drift = kind
                result = self.consume("preview")
                self.assertEqual(result.runtime, "unknown")
                self.assertEqual(
                    result.failure_stage, "host_inspection" if kind == "image" else "drift"
                )
                self.assertTrue(all(not item.parameters for item in result.sources))

    def test_probe_timeout_stage_and_unexplained_unknown_preserve_other_sources(self) -> None:
        self.probe_failure = self.values["cm_data.db.host"]
        for self.probe_unknown in (False, True):
            with self.subTest(unknown=self.probe_unknown):
                result = self.consume()
                failed = next(item for item in result.sources if item.source == "cm_data")
                self.assertEqual(failed.grants, "unknown")
                self.assertEqual(
                    failed.failure_stage, None if self.probe_unknown else "source_resolution"
                )
                self.assertTrue(
                    all(
                        item.grants == "read_only"
                        for item in result.sources
                        if item.source != failed.source
                    )
                )

    def test_typed_only_authority_reaches_grant_route_and_preview_consumer(self) -> None:
        from control_plane import odoo_import_diagnostics as diagnostic, odoo_runtime_reads as reads
        from control_plane.odoo_preview_import_source import resolve_fishbowl_preview_source
        from control_plane.product_secret_copy import ProductSecretCopyFrom

        fishbowl = next(
            index
            for index, item in enumerate(self.fixture.bindings)
            if item.binding_id == "fishbowl"
        )
        from control_plane.contracts.secret_record import SecretBinding

        payload = self.fixture.bindings[fishbowl].model_dump(mode="json")
        payload.update(
            declared_secret_class="shared_safe",
            sharing_reason={
                "kind": "read_only_source",
                "reason": "Fixture source",
                "evidence": "Fixture SELECT-only grant",
            },
        )
        self.fixture.bindings[fishbowl] = SecretBinding.model_validate(payload)
        self.values["fishbowl.port"] = "3499"
        with (
            patch.object(reader, "_read_remote", side_effect=self.remote),
            patch.object(
                reads, "select_stable_runtime", return_value=self.fixture.connection.selection
            ),
            patch.object(reads, "connect_runtime", return_value=self.fixture.connection),
            patch.object(
                reads, "inspect_runtime_container", side_effect=lambda *args: self.env.copy()
            ),
            patch.object(reads, "assert_selection_current"),
            patch.object(reader, "_decrypt_secret_value", return_value=PRIVATE) as compare_decrypt,
            patch(
                "control_plane.odoo_preview_import_source.copy_source_value",
                side_effect=AssertionError("Planning must not copy source passwords"),
            ),
            patch.object(diagnostic, "_decrypt_secret_value", return_value=PRIVATE),
            patch.dict(diagnostic._FAILED_PROBES, {}, clear=True),
        ):
            result = diagnostic.diagnose_runtime_source_grants(
                store=self.fixture.store,
                product=self.fixture.profile.product,
                control_plane_root=self.root,
                database_url=None,
            )
            self.assertTrue(
                all(item.applied_connection_grants == "read_only" for item in result.sources),
                result,
            )
            preview = resolve_fishbowl_preview_source(
                store=self.fixture.store,
                profile=self.fixture.profile,
                preview_slug="pr-42",
                copy_from=ProductSecretCopyFrom(
                    context=self.fixture.context, instance="testing", version_id="fishbowl-v1"
                ),
                control_plane_root=self.root,
                database_url=None,
            )
        fields = {
            item.key: item.value.value
            for item in preview.parameters
            if item.value.source == "literal"
        }
        self.assertEqual(fields["fishbowl.port"], self.values["fishbowl.port"])
        self.assertEqual(preview.environment, {})
        compare_decrypt.assert_called()

    def test_grant_consumer_cannot_substitute_a_new_runtime_origin(self) -> None:
        from dataclasses import replace
        from control_plane import odoo_runtime_reads as reads, odoo_runtime_source_grants as grants

        previous = replace(
            self.fixture.connection,
            evidence=self.fixture.connection.evidence.model_copy(update={"container_id": "d" * 64}),
        )
        with (
            patch.object(reader, "_read_remote", side_effect=self.remote),
            patch.object(
                reads, "select_stable_runtime", return_value=self.fixture.connection.selection
            ),
            patch.object(reads, "connect_runtime", return_value=self.fixture.connection),
            patch.object(
                reads, "inspect_runtime_container", side_effect=lambda *args: self.env.copy()
            ),
            patch.object(reads, "assert_selection_current"),
            patch.object(reader, "_decrypt_secret_value", return_value=PRIVATE),
        ):
            result = grants.runtime_grants(
                previous,
                tuple(SOURCE_PREFIXES),
                store=self.fixture.store,
                product=self.fixture.profile.product,
                control_plane_root=self.root,
                database_url=None,
            )
        self.assertTrue(
            all(
                value.grants == "unknown" and value.failure_stage == "drift"
                for value in result.values()
            )
        )
        self.assertEqual(self.container_calls, 0)
