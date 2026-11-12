import os
import json
import base64
import inspect
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
import unittest
from unittest.mock import patch

import click

from control_plane import secrets
from control_plane.contracts.canonical_json import nonvalidating_compact_sorted_json_sha256
from control_plane.dokploy import api, native_copy_stream, source
from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import (
    DokployTargetRecord,
    DokployTargetStaffTestingHold,
)
from control_plane.contracts.product_profile_record import (
    FRESH_TESTING_LOCAL_CREDENTIAL_KEYS,
    LaunchplaneProductProfileRecord,
)
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.disposable_lane import DisposableLaneSafety
from control_plane.contracts.runtime_key_safety_policy import (
    RuntimeKeySafetyPolicyRecord,
    RuntimeKeySafetyTarget,
    RuntimeSecretSafetyRule,
)
from control_plane.product_config import (
    ProductConfigError,
    apply_product_config_bundle,
    plan_product_config_authority_bundle,
)
from control_plane.runtime_key_safety import evaluate_runtime_key_safety_from_store
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.storage.product_authority_bundle import SecretCopySourceConflictError
from control_plane.odoo_first_empty_inputs import (
    first_empty_storage_snapshot,
    first_empty_storage_program,
    resolve_first_empty_authority,
)
from tests.http_app_test_support import _local_operator_bearer_config, _RejectingVerifier
from tests.support.http import request
from tests.support.profiles import _odoo_profile_payload_with_prod_lane
from tests.support.stores import _write_runtime_key_safety_policy
from tests.support.auth import _local_operator_policy


class FirstEmptyInputsFixture(unittest.TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        database_url = f"sqlite+pysqlite:///{Path(temporary.name) / 'state.db'}"
        _write_runtime_key_safety_policy(database_url=database_url)
        self.store = PostgresRecordStore(database_url=database_url)
        self.addCleanup(self.store.close)
        self.enterContext(
            patch.dict(
                os.environ,
                {secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "fictional-key"},
                clear=True,
            )
        )
        self.profile = LaunchplaneProductProfileRecord.model_validate(
            _odoo_profile_payload_with_prod_lane("example-site")
        )
        self.store.write_product_profile_record(self.profile)
        self.context = next(lane.context for lane in self.profile.lanes if lane.instance == "prod")
        self.target = DokployTargetRecord(
            context=self.context,
            instance="prod",
            target_name="example-prod",
            updated_at="2026-01-01T00:00:00Z",
        )
        self.target_id = DokployTargetIdRecord(
            context=self.context,
            instance="prod",
            target_id="compose-original",
            updated_at=self.target.updated_at,
        )
        self.provider = ProviderTargetRecord(
            context=self.context,
            instance="prod",
            provider_id="dokploy",
            target_category="compose",
            provider_target_type="compose",
            target_id=self.target_id.target_id,
            display_name=self.target.target_name,
            updated_at=self.target.updated_at,
        )
        self.store.write_dokploy_target_record(self.target)
        self.store.write_dokploy_target_id_record(self.target_id)
        self.store.write_provider_target_record(self.provider)
        self.runtime = RuntimeEnvironmentRecord(
            scope="instance",
            context=self.context,
            instance="prod",
            env={
                "ODOO_DB_VOLUME": "example-db",
                "ODOO_DATA_VOLUME": "example-data",
                "ODOO_LOG_VOLUME": "example-logs",
                "ODOO_DB_NAME": "example",
                "ODOO_LIST_DB": "false",
                "ODOO_WORKERS": "2",
                "ODOO_MAX_CRON_THREADS": "1",
                "ODOO_PROXY_MODE": "true",
                "ODOO_LOG_LEVEL": "info",
            },
            updated_at=self.target.updated_at,
        )
        self.store.write_runtime_environment_record(self.runtime)
        self.provider_payload = {
            "composeId": self.target_id.target_id,
            "appName": "example-prod",
            "serverId": "server-original",
            "env": "",
            "composeFile": "",
            "mounts": [],
            "autoDeploy": False,
        }
        self.enterContext(
            patch.object(
                source,
                "read_dokploy_config",
                return_value=("https://provider.example.test", "fictional"),
            )
        )
        self.enterContext(
            patch.object(
                api,
                "fetch_dokploy_target_payload",
                side_effect=lambda **_: dict(self.provider_payload),
            )
        )
        self.enterContext(patch.object(api, "list_deployments_for_target", return_value=[]))
        self.storage_identity = "a" * 64
        self.probe = self.enterContext(
            patch.object(native_copy_stream, "run_managed_server_program", side_effect=self.observe)
        )
        self.app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=_local_operator_policy(
                actions=("product_config.plan", "product_config.apply")
            ),
            bearer_identity_config=_local_operator_bearer_config(token_label="local-owner-write"),
            record_store_factory=lambda: self.store,
        )

    def observe(
        self, *, payload: dict[str, object], receive: Callable[[dict[str, object]], None], **_: Any
    ) -> None:
        receive(
            {
                "type": "complete",
                "binding": nonvalidating_compact_sorted_json_sha256(payload),
                "storage": self.storage_identity,
            }
        )


class FirstEmptyInputsHttpTests(FirstEmptyInputsFixture, unittest.IsolatedAsyncioTestCase):
    async def post(self, intent: str, *, key: str = "", **extra: Any) -> Any:
        return await request(
            self.app,
            "POST",
            "/v1/product-config/apply",
            headers={"Authorization": "Bearer local-operator-token", "Idempotency-Key": key},
            payload={
                "product": self.profile.product,
                "context": self.context,
                "instance": "prod",
                "mode": "dry-run",
                "reason": "Prepare empty original",
                "odoo_first_empty": intent,
                **(
                    {
                        "secrets": [
                            {
                                "binding_key": "ODOO_DB_PASSWORD",
                                "generate": "odoo_first_empty",
                                "secret_class": "prod_only",
                            }
                        ]
                    }
                    if intent == "runtime_credentials"
                    else {}
                ),
                **extra,
            },
        )

    async def test_missing_profile_or_observation_authority_refuses_before_remote_read(
        self,
    ) -> None:
        from control_plane.http_app import LaunchplaneAuthzPolicyRuntime

        original = LaunchplaneAuthzPolicyRuntime.allows
        for denied in ("product_profile.read", "operations.read"):

            def permits(policy: Any, **kwargs: Any) -> bool:
                return kwargs["action"] != denied and original(policy, **kwargs)

            with (
                self.subTest(action=denied),
                patch.object(LaunchplaneAuthzPolicyRuntime, "allows", permits),
            ):
                response = await self.post("runtime_credentials")
                self.assertEqual(response.status_code, 403, response.text)
        self.probe.assert_not_called()
        self.assertFalse(self.store.list_secret_records())

    async def test_data_policy_intent_cannot_leave_a_durable_empty_classification(self) -> None:
        for mode in ("dry-run", "apply"):
            response = await self.post(
                "data_policy",
                mode=mode,
                key="obsolete-policy",
                **({"reviewed_plan_sha256": "a" * 64} if mode == "apply" else {}),
            )
            self.assertEqual(response.status_code, 400, response.text)
        self.probe.assert_not_called()
        self.assertEqual(self.store.read_product_profile_record(self.profile.product), self.profile)
        self.assertFalse(self.store.list_secret_records())

    async def test_runtime_generation_is_apply_only_encrypted_and_replay_preserves_versions(
        self,
    ) -> None:
        entries = [
            {
                "binding_key": "ODOO_DB_PASSWORD",
                "generate": "odoo_first_empty",
                "secret_class": "prod_only",
            }
        ]
        with patch(
            "control_plane.product_config.random_secrets.token_urlsafe",
            side_effect=AssertionError("dry run generated"),
        ):
            response = await self.post("runtime_credentials", secrets=entries)
        self.assertEqual(response.status_code, 202, response.text)
        self.assertFalse(self.store.list_secret_records())
        review = response.json()["result"]["odoo_first_empty"]["plan_sha256"]
        with patch(
            "control_plane.product_config.random_secrets.token_urlsafe",
            return_value="fictional-fresh-password",
        ) as generate:
            applied = await self.post(
                "runtime_credentials",
                secrets=entries,
                mode="apply",
                reviewed_plan_sha256=review,
                key="allocation-original",
            )
        self.assertEqual(applied.status_code, 202, applied.text)
        generate.assert_called_once()
        records = self.store.list_secret_records()
        self.assertEqual(len(records), 1)
        version = self.store.read_secret_version(records[0].current_version_id)
        self.assertEqual(
            secrets._decrypt_secret_value(version.ciphertext, version.key_id),
            "fictional-fresh-password",
        )
        self.assertNotIn("fictional-fresh-password", applied.text)
        self.assertNotIn(version.ciphertext, applied.text)
        self.assertEqual(self.store.read_product_profile_record(self.profile.product), self.profile)
        self.assertEqual(self.store.list_runtime_environment_records(), (self.runtime,))
        with patch(
            "control_plane.product_config.random_secrets.token_urlsafe",
            side_effect=AssertionError("replay generated"),
        ):
            replay = await self.post(
                "runtime_credentials",
                secrets=entries,
                mode="apply",
                reviewed_plan_sha256=review,
                key="allocation-original",
            )
        self.assertEqual(replay.status_code, 202, replay.text)
        self.assertTrue(replay.json()["replayed"])
        self.assertEqual(self.store.list_secret_records(), records)
        refused = await self.post("runtime_credentials", secrets=entries)
        self.assertEqual(refused.status_code, 400, refused.text)

    async def test_storage_or_profile_change_invalidates_review_without_publishing(self) -> None:
        dry = await self.post("runtime_credentials")
        self.assertEqual(dry.status_code, 202, dry.text)
        review = dry.json()["result"]["odoo_first_empty"]["plan_sha256"]
        self.storage_identity = "b" * 64
        refused = await self.post(
            "runtime_credentials", mode="apply", reviewed_plan_sha256=review, key="stale-original"
        )
        self.assertEqual(refused.status_code, 409, refused.text)
        self.assertEqual(self.store.read_product_profile_record(self.profile.product), self.profile)

    async def test_existing_sibling_credentials_survive_new_absent_purpose(self) -> None:
        apply_product_config_bundle(
            record_store=self.store,
            payload={
                "product": self.profile.product,
                "context": self.context,
                "instance": "prod",
                "secrets": [
                    {
                        "binding_key": "ODOO_MASTER_PASSWORD",
                        "value": "invented-existing",
                        "secret_class": "prod_only",
                    }
                ],
            },
            mode="apply",
            actor="fixture",
            source_label="fixture",
        )
        existing = self.store.list_secret_records()
        versions = tuple(
            self.store.read_secret_version(record.current_version_id) for record in existing
        )
        requests: tuple[tuple[str, dict[str, Any]], ...] = (
            (
                "runtime_credentials",
                {
                    "secrets": [
                        {
                            "binding_key": "ODOO_DB_PASSWORD",
                            "generate": "odoo_first_empty",
                            "secret_class": "prod_only",
                        }
                    ]
                },
            ),
        )
        for intent, extra in requests:
            dry = await self.post(intent, **extra)
            self.assertEqual(dry.status_code, 202, dry.text)
            applied = await self.post(
                intent,
                mode="apply",
                key=intent,
                reviewed_plan_sha256=dry.json()["result"]["odoo_first_empty"]["plan_sha256"],
                **extra,
            )
            self.assertEqual(applied.status_code, 202, applied.text)
        self.assertTrue(
            set(record.secret_id for record in existing)
            <= set(record.secret_id for record in self.store.list_secret_records())
        )
        for before, version in zip(existing, versions, strict=True):
            self.assertEqual(self.store.read_secret_record(before.secret_id), before)
            self.assertEqual(self.store.read_secret_version(version.version_id), version)

    async def test_profile_cas_and_transaction_failure_never_publish_partial_allocation(
        self,
    ) -> None:
        entries = [
            {
                "binding_key": "ODOO_DB_PASSWORD",
                "generate": "odoo_first_empty",
                "secret_class": "prod_only",
            }
        ]
        dry = await self.post("runtime_credentials", secrets=entries)
        review = dry.json()["result"]["odoo_first_empty"]["plan_sha256"]
        original_write = self.store.write_product_authority_bundle

        def changed_before_commit(bundle: Any, **kwargs: Any) -> None:
            self.store.write_product_profile_record(
                self.profile.model_copy(update={"production_use": "live"})
            )
            original_write(bundle, **kwargs)

        with patch.object(
            self.store, "write_product_authority_bundle", side_effect=changed_before_commit
        ):
            refused = await self.post(
                "runtime_credentials",
                secrets=entries,
                mode="apply",
                key="cas-original",
                reviewed_plan_sha256=review,
            )
        self.assertEqual(refused.status_code, 409, refused.text)
        self.assertFalse(self.store.list_secret_records())
        self.assertFalse(self.store.list_secret_bindings())
        self.store.write_product_profile_record(self.profile)

        def fail_after_write(step: str) -> None:
            if step == "write_secret_binding":
                raise RuntimeError("injected transaction failure")

        with patch.object(
            self.store, "_after_product_authority_bundle_step", side_effect=fail_after_write
        ):
            with self.assertRaises(RuntimeError):
                await self.post(
                    "runtime_credentials",
                    secrets=entries,
                    mode="apply",
                    key="rollback-original",
                    reviewed_plan_sha256=review,
                )
        self.assertFalse(self.store.list_secret_records())
        self.assertFalse(self.store.list_secret_bindings())
        self.assertEqual(self.store.list_runtime_environment_records(), (self.runtime,))

    async def test_unknown_or_denied_observation_and_unreviewed_apply_fail_closed(self) -> None:
        self.probe.side_effect = lambda **kwargs: kwargs["receive"](
            {"type": "complete", "status": "unknown"}
        )
        self.assertEqual((await self.post("runtime_credentials")).status_code, 409)
        self.probe.side_effect = lambda **kwargs: kwargs["receive"]({"type": "denied"})
        denied = await self.post("runtime_credentials")
        self.assertEqual(denied.status_code, 403, denied.text)
        self.probe.side_effect = self.observe
        refused = await self.post(
            "runtime_credentials", mode="apply", key="unreviewed", reviewed_plan_sha256="a" * 64
        )
        self.assertEqual(refused.status_code, 409, refused.text)
        self.assertEqual(self.store.read_product_profile_record(self.profile.product), self.profile)

    async def refused_with_cause(self, status_code: int) -> tuple[dict[str, Any], dict[str, Any]]:
        with self.assertLogs("control_plane.http_error_evidence", "WARNING") as logged:
            response = await self.post("runtime_credentials")
        self.assertEqual(response.status_code, status_code, response.text)
        body = response.json()
        (event,) = (json.loads(record.getMessage()) for record in logged.records)
        self.assertEqual(event["trace_id"], body["trace_id"])
        for site in event["sites"]:
            self.assertNotIn(str(site["module"]), response.text)
        return {key: value for key, value in body.items() if key != "trace_id"}, event

    async def test_each_refused_qualification_check_logs_its_own_site_behind_one_response(
        self,
    ) -> None:
        held = self.target.model_copy(
            update={
                "policies": self.target.policies.model_copy(
                    update={"staff_testing_hold": DokployTargetStaffTestingHold(reason="fixture")}
                )
            }
        )
        alias = self.target_id.model_copy(update={"context": "another", "instance": "testing"})
        program_failure = native_copy_stream.NativeProgramFailed(
            "fixture", program="first\nsecond\nthird", program_sites=[2]
        )
        refusals: dict[str, Callable[[], None]] = {
            "live": lambda: self.store.write_product_profile_record(
                self.profile.model_copy(update={"production_use": "live"})
            ),
            "historical": lambda: self.store.write_product_profile_record(
                self.profile.model_copy(update={"historical_contexts": ("earlier-context",)})
            ),
            "staff_hold": lambda: self.store.write_dokploy_target_record(held),
            "storage_program": lambda: setattr(self.probe, "side_effect", program_failure),
            "target_alias": lambda: self.store.write_dokploy_target_id_record(alias),
        }
        bodies: list[dict[str, Any]] = []
        first_empty_sites: list[tuple[object, ...]] = []
        for name, refuse in refusals.items():
            with self.subTest(refusal=name):
                refuse()
                body, event = await self.refused_with_cause(409)
                bodies.append(body)
                first_empty_sites.append(
                    tuple(
                        site["line"]
                        for site in event["sites"]
                        if site["module"] == "control_plane.odoo_first_empty_inputs"
                    )
                )
                if name == "storage_program":
                    self.assertEqual(event["native_programs"][0]["lines"], [2])
                self.store.write_product_profile_record(self.profile)
                self.store.write_dokploy_target_record(self.target)
                self.store.write_dokploy_target_id_record(self.target_id)
        self.assertEqual(len(set(map(json.dumps, bodies))), 1)
        self.assertTrue(all(first_empty_sites))
        self.assertEqual(len(set(first_empty_sites)), len(refusals))

    async def test_refused_storage_read_logs_its_cause_behind_the_same_denial(self) -> None:
        self.probe.side_effect = lambda **kwargs: kwargs["receive"]({"type": "denied"})
        _, event = await self.refused_with_cause(403)
        self.assertIn(
            "control_plane.odoo_first_empty_inputs", {site["module"] for site in event["sites"]}
        )

    async def test_provider_refusals_identify_each_missing_or_conflicting_fact_without_values(
        self,
    ) -> None:
        self.provider_payload["name"] = "Example display name"
        original = dict(self.provider_payload)
        refusals = {
            f"missing_{field}": {key: value for key, value in original.items() if key != field}
            for field in (
                "composeId",
                "appName",
                "serverId",
                "env",
                "composeFile",
                "mounts",
                "autoDeploy",
            )
        }
        sentinel = "private-provider-sentinel"
        conflicts: dict[str, tuple[str, Any]] = {
            "identity": ("composeId", sentinel),
            "project_type": ("appName", [sentinel]),
            "project_name": ("appName", f"/{sentinel}"),
            "environment": ("env", f"PASSWORD={sentinel}"),
            "compose": ("composeFile", sentinel),
            "mounts": ("mounts", [{"source": sentinel}]),
            "auto_deploy_enabled": ("autoDeploy", True),
            "auto_deploy_unknown": ("autoDeploy", None),
            "server": ("serverId", f"/{sentinel}"),
        }
        refusals.update(
            {name: {**original, field: value} for name, (field, value) in conflicts.items()}
        )
        bodies: list[dict[str, Any]] = []
        sites: list[str] = []
        for name, payload in refusals.items():
            with self.subTest(refusal=name):
                self.provider_payload = payload
                body, event = await self.refused_with_cause(409)
                bodies.append(body)
                self.assertTrue(event["sites"])
                sites.append(json.dumps(event["sites"], sort_keys=True))
                self.assertNotIn(sentinel, json.dumps([body, event]))
        self.assertEqual(len(set(map(json.dumps, bodies))), 1)
        self.assertEqual(len(set(sites)), len(refusals))
        self.probe.assert_not_called()
        self.assertFalse(self.store.list_secret_records())

    async def test_empty_provider_values_keep_their_qualification(self) -> None:
        for empty in (None, ""):
            with self.subTest(empty=empty):
                self.provider_payload.update(env=empty, composeFile=empty, mounts=None)
                response = await self.post("runtime_credentials")
                self.assertEqual(response.status_code, 202, response.text)
        self.assertFalse(self.store.list_secret_records())

    async def test_inherited_global_credentials_yield_to_exact_instance_generation(
        self,
    ) -> None:
        inherited = ("ODOO_DB_PASSWORD", "ODOO_MASTER_PASSWORD")
        for key in inherited:
            secrets.write_secret_value(
                record_store=self.store,
                scope="global",
                integration="runtime_environment",
                name=key,
                plaintext_value=f"invented-global-{key}",
                binding_key=key,
                actor="fixture",
            )
        global_records = self.store.list_secret_records()
        global_versions = tuple(
            self.store.read_secret_version(record.current_version_id) for record in global_records
        )
        entries = [
            {"binding_key": key, "generate": "odoo_first_empty", "secret_class": "prod_only"}
            for key in inherited
        ]
        dry = await self.post("runtime_credentials", secrets=entries)
        self.assertEqual(dry.status_code, 202, dry.text)
        review = dry.json()["result"]["odoo_first_empty"]["plan_sha256"]
        applied = await self.post(
            "runtime_credentials",
            secrets=entries,
            mode="apply",
            reviewed_plan_sha256=review,
            key="inherited-original",
        )
        self.assertEqual(applied.status_code, 202, applied.text)
        for record, version in zip(global_records, global_versions, strict=True):
            self.assertEqual(self.store.read_secret_record(record.secret_id), record)
            self.assertEqual(self.store.read_secret_version(version.version_id), version)
        effective = secrets.resolve_effective_secret_bindings_from_store(
            record_store=self.store,
            integration="runtime_environment",
            context_name=self.context,
            instance_name="prod",
        )
        self.assertEqual(
            {key: effective[key][1].scope for key in inherited},
            dict.fromkeys(inherited, "context_instance"),
        )

    async def test_lane_or_context_credential_still_refuses_and_global_change_voids_review(
        self,
    ) -> None:
        secrets.write_secret_value(
            record_store=self.store,
            scope="context",
            integration="runtime_environment",
            name="ODOO_ADMIN_PASSWORD",
            plaintext_value="invented-context",
            binding_key="ODOO_ADMIN_PASSWORD",
            context_name=self.context,
            actor="fixture",
        )
        refused = await self.post(
            "runtime_credentials",
            secrets=[
                {
                    "binding_key": "ODOO_ADMIN_PASSWORD",
                    "generate": "odoo_first_empty",
                    "secret_class": "prod_only",
                }
            ],
        )
        self.assertEqual(refused.status_code, 400, refused.text)
        dry = await self.post("runtime_credentials")
        self.assertEqual(dry.status_code, 202, dry.text)
        before = self.store.list_secret_records()
        secrets.write_secret_value(
            record_store=self.store,
            scope="global",
            integration="runtime_environment",
            name="ODOO_DB_PASSWORD",
            plaintext_value="invented-global-after-review",
            binding_key="ODOO_DB_PASSWORD",
            actor="fixture",
        )
        stale = await self.post(
            "runtime_credentials",
            mode="apply",
            reviewed_plan_sha256=dry.json()["result"]["odoo_first_empty"]["plan_sha256"],
            key="stale-global",
        )
        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertEqual(len(self.store.list_secret_records()), len(before) + 1)

    SHARED_RULE_KEYS = tuple(sorted(FRESH_TESTING_LOCAL_CREDENTIAL_KEYS))

    def write_shared_rule_naming_other_sites(self) -> None:
        self.store.write_runtime_key_safety_policy_record(
            RuntimeKeySafetyPolicyRecord(
                record_id="runtime-key-safety-policy-shared-password",
                source="test",
                updated_at="2026-06-01T00:00:00Z",
                rules=tuple(
                    RuntimeSecretSafetyRule(
                        binding_key=rule_key,
                        secret_class="shared_safe",
                        allowed_contexts=("unrelated-site",),
                        allowed_instances=("prod",),
                    )
                    for rule_key in self.SHARED_RULE_KEYS
                ),
            )
        )

    async def generate_shared_rule_keys(self) -> None:
        self.write_shared_rule_naming_other_sites()
        entries = [
            {"binding_key": key, "generate": "odoo_first_empty", "secret_class": "prod_only"}
            for key in self.SHARED_RULE_KEYS
        ]
        dry = await self.post("runtime_credentials", secrets=entries)
        self.assertEqual(dry.status_code, 202, dry.text)
        applied = await self.post(
            "runtime_credentials",
            secrets=entries,
            mode="apply",
            reviewed_plan_sha256=dry.json()["result"]["odoo_first_empty"]["plan_sha256"],
            key="shared-rule-original",
        )
        self.assertEqual(applied.status_code, 202, applied.text)

    def stored_findings(self, binding_key: str) -> list[str]:
        evaluation = evaluate_runtime_key_safety_from_store(
            record_store=self.store,
            target=RuntimeKeySafetyTarget(
                context=self.context, instance="prod", environment_class="prod"
            ),
            required_binding_keys=(binding_key,),
        )
        return [finding.code for finding in evaluation.findings]

    def supplied_prod_secret(self, **secret: str) -> dict[str, object]:
        return {
            "product": self.profile.product,
            "context": self.context,
            "instance": "prod",
            "secrets": [
                {
                    "binding_key": "ODOO_DB_PASSWORD",
                    "value": "invented",
                    "secret_class": "prod_only",
                    **secret,
                }
            ],
        }

    def secret_state(self) -> tuple[object, ...]:
        return self.store.list_secret_records(), self.store.list_secret_bindings()

    async def test_only_qualified_generation_passes_a_shared_rule_naming_other_sites(
        self,
    ) -> None:
        self.write_shared_rule_naming_other_sites()
        for payload in (
            self.supplied_prod_secret(),
            self.supplied_prod_secret(value_source="generated"),
        ):
            with self.subTest(payload=payload), self.assertRaises(ProductConfigError) as refused:
                apply_product_config_bundle(
                    record_store=self.store,
                    payload=payload,
                    mode="dry-run",
                    actor="fixture",
                    source_label="test",
                )
            self.assertEqual(refused.exception.code, "runtime_key_safety_failed")
        unqualified = {
            "binding_key": "ODOO_DB_PASSWORD",
            "generate": "odoo_first_empty",
            "secret_class": "prod_only",
        }
        for intent in ({}, {"odoo_first_empty": "runtime_credentials"}):
            with self.subTest(intent=intent), self.assertRaises(ProductConfigError):
                apply_product_config_bundle(
                    record_store=self.store,
                    payload={**self.supplied_prod_secret(), **intent, "secrets": [unqualified]},
                    mode="dry-run",
                    actor="fixture",
                    source_label="test",
                )
        self.assertFalse(self.store.list_secret_records())
        await self.generate_shared_rule_keys()
        for key in self.SHARED_RULE_KEYS:
            self.assertEqual(self.stored_findings(key), [])
        with self.assertRaises(ProductConfigError) as rotated:
            apply_product_config_bundle(
                record_store=self.store,
                payload=self.supplied_prod_secret(),
                mode="dry-run",
                actor="fixture",
                source_label="test",
            )
        self.assertEqual(rotated.exception.code, "runtime_key_safety_failed")

    async def test_value_under_another_binding_key_never_hides_behind_a_generated_binding(
        self,
    ) -> None:
        await self.generate_shared_rule_keys()
        before = self.secret_state()
        for mode in ("dry-run", "apply"):
            with self.subTest(mode=mode), self.assertRaises(ProductConfigError) as aliased:
                apply_product_config_bundle(
                    record_store=self.store,
                    payload=self.supplied_prod_secret(name="ODOO_DB_PASSWORD", binding_key="ALIAS"),
                    mode=mode,
                    actor="fixture",
                    source_label="test",
                )
            self.assertEqual(aliased.exception.code, "secret_origin_alias_refused")
        with self.assertRaises(click.ClickException):
            secrets.write_secret_value(
                record_store=self.store,
                scope="context_instance",
                integration="runtime_environment",
                name="ODOO_MASTER_PASSWORD",
                plaintext_value="invented-alias",
                binding_key="ALIAS",
                context_name=self.context,
                instance_name="prod",
                actor="fixture",
            )
        self.assertEqual(self.secret_state(), before)
        for key in self.SHARED_RULE_KEYS:
            self.assertEqual(self.stored_findings(key), [])

    async def test_interrupted_plain_rotation_never_publishes_behind_a_generated_binding(
        self,
    ) -> None:
        await self.generate_shared_rule_keys()
        before = self.secret_state()
        rotation: dict[str, Any] = {
            "record_store": self.store,
            "scope": "context_instance",
            "integration": "runtime_environment",
            "name": "ODOO_MASTER_PASSWORD",
            "plaintext_value": "invented-rotation",
            "binding_key": "ODOO_MASTER_PASSWORD",
            "context_name": self.context,
            "instance_name": "prod",
            "actor": "fixture",
        }

        def stop_after_the_new_value_is_staged(step_name: str) -> None:
            if step_name == "write_secret_record":
                raise RuntimeError("stop")

        with (
            patch.object(
                self.store,
                "_after_product_authority_bundle_step",
                side_effect=stop_after_the_new_value_is_staged,
            ),
            self.assertRaises(RuntimeError),
        ):
            secrets.write_secret_value(**rotation)
        self.assertEqual(self.secret_state(), before)
        secrets.write_secret_value(**rotation)
        self.assertEqual(self.stored_findings("ODOO_MASTER_PASSWORD"), ["context_not_allowed"])

    async def test_value_planned_before_generation_never_commits_behind_the_generated_binding(
        self,
    ) -> None:
        planned = [
            plan_product_config_authority_bundle(
                record_store=self.store,
                payload=self.supplied_prod_secret(name=purpose, binding_key=key),
                mode="apply",
                actor="fixture",
                source_label="test",
            )[1]
            for purpose in self.SHARED_RULE_KEYS
            for key in (purpose, "ALIAS")
        ]
        await self.generate_shared_rule_keys()
        before = self.secret_state()
        for bundle in planned:
            with (
                self.subTest(bindings=bundle.secret_bindings),
                self.assertRaises(SecretCopySourceConflictError),
            ):
                self.store.write_product_authority_bundle(bundle)
        self.assertEqual(self.secret_state(), before)
        for purpose in self.SHARED_RULE_KEYS:
            self.assertEqual(self.stored_findings(purpose), [])

    async def test_plain_write_read_before_generation_never_commits_behind_the_generated_binding(
        self,
    ) -> None:
        bindings_read_before_generation = self.store.list_secret_bindings()
        await self.generate_shared_rule_keys()
        before = self.secret_state()
        for key in ("ODOO_DB_PASSWORD", "ALIAS"):
            with (
                self.subTest(binding_key=key),
                patch.object(self.store, "find_secret_record", return_value=None),
                patch.object(
                    self.store,
                    "list_secret_bindings",
                    return_value=bindings_read_before_generation,
                ),
                self.assertRaises(click.ClickException),
            ):
                secrets.write_secret_value(
                    record_store=self.store,
                    scope="context_instance",
                    integration="runtime_environment",
                    name="ODOO_DB_PASSWORD",
                    plaintext_value="invented-plain",
                    binding_key=key,
                    context_name=self.context,
                    instance_name="prod",
                    actor="fixture",
                )
            self.assertEqual(self.secret_state(), before)
        self.assertEqual(self.stored_findings("ODOO_DB_PASSWORD"), [])

    async def test_caller_values_testing_namespace_and_mixed_writes_cannot_enter_first_empty(
        self,
    ) -> None:
        for extra in (
            {"runtime_env": {"env": {"ODOO_WORKERS": "4"}}},
            {"instance": "testing"},
            {
                "secrets": [
                    {
                        "binding_key": "ODOO_DB_PASSWORD",
                        "generate": "testing_local",
                        "secret_class": "testing",
                    }
                ]
            },
            {"secrets": [{"binding_key": "ODOO_DB_PASSWORD", "value": "borrowed"}]},
        ):
            with self.subTest(extra=extra):
                response = await self.post("runtime_credentials", **extra)
                self.assertEqual(response.status_code, 400, response.text)
        self.probe.assert_not_called()
        self.assertFalse(self.store.list_secret_records())

    async def test_live_non_odoo_or_provider_workload_refuses_before_probe(
        self,
    ) -> None:
        for change in ({"production_use": "live"}, {"driver_id": "generic-web"}):
            with self.subTest(change=change):
                self.store.write_product_profile_record(self.profile.model_copy(update=change))
                refused = await self.post("runtime_credentials")
                self.assertEqual(refused.status_code, 409, refused.text)
                self.probe.assert_not_called()
        self.store.write_product_profile_record(self.profile)
        self.provider_payload["composeFile"] = "services: {existing: {image: existing}}"
        self.assertEqual((await self.post("runtime_credentials")).status_code, 409)
        self.probe.assert_not_called()

    async def test_authoritative_data_recorded_use_custody_and_target_alias_refuse(self) -> None:
        protected = self.profile.model_copy(
            update={
                "lanes": tuple(
                    lane.model_copy(
                        update={
                            "odoo_data_policy": lane.odoo_data_policy.model_copy(
                                update={"data_authority": "authoritative"}
                            )
                        }
                    )
                    if lane.instance == "prod"
                    else lane
                    for lane in self.profile.lanes
                )
            }
        )
        self.store.write_product_profile_record(protected)
        self.assertEqual((await self.post("runtime_credentials")).status_code, 409)
        self.store.write_product_profile_record(self.profile)
        now = datetime.now(UTC)
        for fields in (
            {"usage": "in_use", "usage_evidence": "fixture"},
            {"data": "only_copy", "data_evidence": "fixture"},
            {"production_resources": "shared", "isolation_evidence": "fixture"},
        ):
            self.store.write_disposable_lane_safety(
                DisposableLaneSafety.model_validate(
                    {
                        "product": self.profile.product,
                        "context": self.context,
                        "instance": "prod",
                        "observed_by": "fixture",
                        "observer_kind": "fixture",
                        "binding_sha256": "a" * 64,
                        "observed_at": now,
                        "expires_at": now + timedelta(seconds=60),
                        **fields,
                    }
                )
            )
            self.assertEqual((await self.post("runtime_credentials")).status_code, 409)
        self.probe.assert_not_called()

    async def test_same_physical_target_cannot_be_claimed_by_another_lane(self) -> None:
        self.store.write_dokploy_target_id_record(
            self.target_id.model_copy(update={"context": "another", "instance": "testing"})
        )
        self.assertEqual((await self.post("runtime_credentials")).status_code, 409)
        self.probe.assert_not_called()

    async def test_plain_runtime_credential_cannot_be_hashed_into_a_review_or_overridden(
        self,
    ) -> None:
        self.store.write_runtime_environment_record(
            self.runtime.model_copy(
                update={"env": {**self.runtime.env, "ODOO_DB_PASSWORD": "borrowed-runtime-value"}}
            )
        )
        refused = await self.post("runtime_credentials")
        self.assertEqual(refused.status_code, 409, refused.text)
        self.assertNotIn("borrowed-runtime-value", refused.text)
        self.probe.assert_not_called()

    async def test_foreign_context_owner_and_missing_prod_lane_never_observe_storage(self) -> None:
        self.store.write_product_profile_record(
            self.profile.model_copy(update={"product": "foreign-product"})
        )
        response = await self.post("runtime_credentials")
        self.assertIn(response.status_code, {403, 409})
        self.probe.assert_not_called()
        self.store.write_product_profile_record(
            self.profile.model_copy(
                update={
                    "lanes": tuple(lane for lane in self.profile.lanes if lane.instance != "prod")
                }
            )
        )
        with self.assertRaises(ValueError):
            resolve_first_empty_authority(self.store, self.profile.product, self.context)


class FirstEmptyStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.volume_root = self.root / "docker" / "volumes"
        self.volume_root.mkdir(parents=True)
        self.spec = {"project": "example-prod", "volumes": ["db", "data", "logs"]}
        self.names: list[str] = []
        self.volume_metadata: dict[str, dict[str, Any]] = {}
        self.containers: dict[str, dict[str, Any]] = {}

    def docker(self, *arguments: str) -> str:
        if arguments[0] == "info":
            return json.dumps(
                {
                    "DockerRootDir": str(self.volume_root.parent),
                    "Swarm": {"LocalNodeState": "inactive"},
                }
            )
        if arguments[:2] == ("volume", "ls"):
            return "\n".join(self.names)
        if arguments[0] == "ps":
            return "\n".join(self.containers)
        if arguments[0] == "inspect":
            return "\n".join(
                json.dumps(self.containers[identifier]) for identifier in arguments[5:]
            )
        if arguments[:2] == ("volume", "inspect"):
            return "\n".join(
                json.dumps(
                    {
                        "Name": name,
                        "Driver": "local",
                        "Mountpoint": str(self.volume_root / name / "_data"),
                        **self.volume_metadata.get(name, {}),
                    }
                )
                for name in arguments[4:]
            )
        raise AssertionError(arguments)

    def snapshot(self) -> dict[str, object]:
        return first_empty_storage_snapshot(
            self.docker, self.spec, compose_root=self.root / "compose", read_swarm=lambda _: []
        )

    def test_absent_and_actually_empty_original_storage_qualify_but_nonempty_does_not(self) -> None:
        self.snapshot()
        backing = self.volume_root / "db" / "_data"
        backing.mkdir(parents=True)
        with self.assertRaises(ValueError):
            self.snapshot()
        self.names.append("db")
        self.snapshot()
        (backing / "PG_VERSION").write_text("fictional database")
        with self.assertRaises(ValueError):
            self.snapshot()

    def test_orphaned_project_volume_and_bind_alias_cannot_hide_behind_new_intent(self) -> None:
        self.names.append("old-data")
        self.volume_metadata["old-data"] = {
            "Labels": {"com.docker.compose.project": "example-prod"}
        }
        with self.assertRaises(ValueError):
            self.snapshot()
        self.volume_metadata["old-data"] = {
            "Options": {
                "type": "none",
                "o": "bind",
                "device": str(self.volume_root / "db" / "_data"),
            }
        }
        with self.assertRaises(ValueError):
            self.snapshot()

    def test_original_project_files_or_another_container_mount_protect_custody(self) -> None:
        project = self.root / "compose" / "example-prod"
        project.mkdir(parents=True)
        (project / "backup").write_text("only copy")
        with self.assertRaises(ValueError):
            self.snapshot()

        (project / "backup").unlink()
        self.containers["container-other"] = {
            "Id": "container-other",
            "Name": "other",
            "Labels": {},
            "Mounts": [
                {"Type": "volume", "Name": "db", "Source": str(self.volume_root / "db" / "_data")}
            ],
        }
        with self.assertRaises(ValueError):
            self.snapshot()

    def test_physical_inode_aliases_protect_storage_even_when_paths_differ(self) -> None:
        backing = self.volume_root / "db" / "_data"
        backing.mkdir(parents=True)
        self.names.append("db")
        alias = self.root / "different-path"
        alias.mkdir()
        self.containers["container-other"] = {
            "Id": "container-other",
            "Name": "other",
            "Labels": {},
            "Mounts": [{"Type": "bind", "Source": str(alias)}],
        }
        self.snapshot()

        def same_backing(path: Path, **kwargs: Any) -> os.stat_result:
            return os.stat(backing if path == alias else path, **kwargs)

        with patch.object(Path, "stat", same_backing), self.assertRaises(ValueError):
            self.snapshot()


class FirstEmptyProgramTests(unittest.TestCase):
    def test_generated_program_batches_inventory_and_reports_denied_or_its_failed_check(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            volume_root = root / "engine" / "volumes"
            volume_root.mkdir(parents=True)
            state_path = root / "state.json"
            executable = root / "docker"
            executable.write_text(
                f"#!{sys.executable}\n"
                + r"""
import json,os,sys
from pathlib import Path
p=Path(os.environ['FAKE_DOCKER_STATE'])
s=json.loads(p.read_text())
s['calls']=s.get('calls',0)+1
p.write_text(json.dumps(s))
if sys.argv[1:3] != ['--host','unix:///var/run/docker.sock']:
    raise SystemExit(97)
a=sys.argv[3:]
services=[{'ID':'k3x9p2m7q4w8e1r5t6y0u2i3o','Name':'unrelated'}]
if s.get('renamed'):
    services.append({'ID':'z8c4v6b2n1m5a7s3d9f0g4h2j','Name':services[0]['ID'][:12]})
if s.get('denied'):
    print('permission denied while trying to connect to the docker daemon',file=sys.stderr)
    raise SystemExit(1)
if a[0]=='info':
    print('[]' if s.get('unknown') else json.dumps({'DockerRootDir':s['root'],'Swarm':{'LocalNodeState':'active'}}))
elif a[:2]==['node','ls']:
    print(json.dumps({'ID':'node-original','Self':True}))
elif a[:2]==['service','ls']:
    if '--no-trunc' in a:
        print('unknown flag: --no-trunc',file=sys.stderr)
        raise SystemExit(125)
    print('\n'.join(service['ID'][:12] for service in services))
elif a[:2]==['service','inspect']:
    for reference in a[4:]:
        found=([service for service in services if service['ID']==reference]
            or [service for service in services if service['Name']==reference]
            or [service for service in services if service['ID'].startswith(reference)])
        if len(found)!=1:
            print('Error: no such service or ambiguous: '+reference,file=sys.stderr)
            raise SystemExit(1)
        print(json.dumps({**found[0],'Labels':{},'Mounts':[]}))
elif a[:2]==['volume','ls']:
    print('\n'.join('unrelated-'+str(i) for i in range(s['volumes'])))
elif a[:2]==['volume','inspect']:
    for name in a[4:]:
        print(json.dumps({'Name':name,'Driver':'local','Options':{},'Labels':{},'Mountpoint':s['root']+'/volumes/'+name+'/_data'}))
elif a[0]=='ps':
    print('foreign-container')
elif a[0]=='inspect':
    for identifier in a[5:]:
        print(json.dumps({'Id':identifier,'Name':'unrelated','Labels':{},'Mounts':[]}))
else:
    raise SystemExit(98)
"""
            )
            executable.chmod(0o755)
            spec = {"project": "fictional-prod", "volumes": ["db", "data", "logs"]}
            program = first_empty_storage_program(compose_root=root / "compose")
            for scenario, expected in (
                ("empty", "complete"),
                ("denied", "denied"),
                ("unknown", "failed"),
                ("renamed", "failed"),
            ):
                with self.subTest(scenario=scenario):
                    state_path.write_text(
                        json.dumps(
                            {"root": str(volume_root.parent), "volumes": 400, scenario: True}
                        )
                    )
                    result = subprocess.run(
                        [
                            sys.executable,
                            "-I",
                            "-c",
                            program,
                            base64.b64encode(json.dumps(spec).encode()).decode(),
                            "FRAME:",
                        ],
                        env={"PATH": str(root), "FAKE_DOCKER_STATE": str(state_path)},
                        capture_output=True,
                        text=True,
                        timeout=20,
                        check=True,
                    )
                    frame = json.loads(result.stdout.removeprefix("FRAME:"))
                    self.assertEqual(frame["type"], expected, result.stdout)
                    if scenario == "empty":
                        self.assertEqual(
                            frame["binding"], nonvalidating_compact_sorted_json_sha256(spec)
                        )
                        self.assertEqual(len(frame["storage"]), 64)
                        self.assertLess(json.loads(state_path.read_text())["calls"], 40)
                    elif expected == "failed":
                        failure = native_copy_stream.NativeProgramFailed(
                            "fixture", program=program, program_sites=frame["sites"]
                        )
                        self.assertEqual(failure.program_sites, tuple(frame["sites"]))
                        failed_line = program.splitlines()[failure.program_sites[-1] - 1]
                        self.assertIn(
                            failed_line,
                            inspect.getsource(first_empty_storage_snapshot).splitlines(),
                        )
