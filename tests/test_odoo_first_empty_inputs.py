import os
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
import unittest
from unittest.mock import patch

from control_plane import secrets
from control_plane.contracts.canonical_json import nonvalidating_compact_sorted_json_sha256
from control_plane.dokploy import api, native_copy_stream, source
from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.disposable_lane import DisposableLaneSafety
from control_plane.product_config import apply_product_config_bundle
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.odoo_first_empty_inputs import first_empty_storage_snapshot
from tests.http_app_test_support import _local_operator_bearer_config, _RejectingVerifier
from tests.support.http import request
from tests.support.profiles import _odoo_profile_payload_with_prod_lane
from tests.support.stores import _write_runtime_key_safety_policy
from tests.support.auth import _local_operator_policy


class FirstEmptyInputsHttpTests(unittest.IsolatedAsyncioTestCase):
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
                **extra,
            },
        )

    async def test_reviewed_data_policy_changes_only_original_lane_and_completed_replay_skips_probe(
        self,
    ) -> None:
        response = await self.post("data_policy")
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(self.store.read_product_profile_record(self.profile.product), self.profile)
        review = response.json()["result"]["odoo_first_empty"]["plan_sha256"]
        applied = await self.post(
            "data_policy", mode="apply", reviewed_plan_sha256=review, key="policy-original"
        )
        self.assertEqual(applied.status_code, 202, applied.text)
        after = self.store.read_product_profile_record(self.profile.product)
        expected = self.profile.model_dump(mode="json")
        for lane in expected["lanes"]:
            if lane["instance"] == "prod":
                lane["odoo_data_policy"].update(
                    data_authority="resettable", allowed_rebuild_sources=["empty"]
                )
        expected.update(source=after.source, updated_at=after.updated_at)
        self.assertEqual(after.model_dump(mode="json"), expected)
        self.assertEqual(self.store.list_runtime_environment_records(), (self.runtime,))
        self.assertFalse(self.store.list_secret_records())
        self.probe.reset_mock()
        replay = await self.post(
            "data_policy", mode="apply", reviewed_plan_sha256=review, key="policy-original"
        )
        self.assertEqual(replay.status_code, 202, replay.text)
        self.assertTrue(replay.json()["replayed"])
        self.probe.assert_not_called()

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
        dry = await self.post("data_policy")
        self.assertEqual(dry.status_code, 202, dry.text)
        review = dry.json()["result"]["odoo_first_empty"]["plan_sha256"]
        self.storage_identity = "b" * 64
        refused = await self.post(
            "data_policy", mode="apply", reviewed_plan_sha256=review, key="stale-original"
        )
        self.assertEqual(refused.status_code, 409, refused.text)
        self.assertEqual(self.store.read_product_profile_record(self.profile.product), self.profile)

    async def test_existing_sibling_credentials_survive_policy_and_new_absent_purpose(self) -> None:
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
            ("data_policy", {}),
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
        self.assertEqual((await self.post("data_policy")).status_code, 409)
        self.probe.side_effect = lambda **kwargs: kwargs["receive"]({"type": "denied"})
        denied = await self.post("data_policy")
        self.assertEqual(denied.status_code, 403, denied.text)
        self.probe.side_effect = self.observe
        refused = await self.post(
            "data_policy", mode="apply", key="unreviewed", reviewed_plan_sha256="a" * 64
        )
        self.assertEqual(refused.status_code, 409, refused.text)
        self.assertEqual(self.store.read_product_profile_record(self.profile.product), self.profile)

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

    async def test_live_authoritative_foreign_or_provider_workload_refuses_before_probe(
        self,
    ) -> None:
        for change in ({"production_use": "live"}, {"driver_id": "generic-web"}):
            with self.subTest(change=change):
                self.store.write_product_profile_record(self.profile.model_copy(update=change))
                refused = await self.post("data_policy")
                self.assertEqual(refused.status_code, 409, refused.text)
                self.probe.assert_not_called()
        self.store.write_product_profile_record(self.profile)
        self.provider_payload["composeFile"] = "services: {existing: {image: existing}}"
        self.assertEqual((await self.post("data_policy")).status_code, 409)
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
        self.assertEqual((await self.post("data_policy")).status_code, 409)
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
            self.assertEqual((await self.post("data_policy")).status_code, 409)
        self.probe.assert_not_called()

    async def test_same_physical_target_cannot_be_claimed_by_another_lane(self) -> None:
        self.store.write_dokploy_target_id_record(
            self.target_id.model_copy(update={"context": "another", "instance": "testing"})
        )
        self.assertEqual((await self.post("data_policy")).status_code, 409)
        self.probe.assert_not_called()

    async def test_plain_runtime_credential_cannot_be_hashed_into_a_review_or_overridden(
        self,
    ) -> None:
        self.store.write_runtime_environment_record(
            self.runtime.model_copy(
                update={"env": {**self.runtime.env, "ODOO_DB_PASSWORD": "borrowed-runtime-value"}}
            )
        )
        refused = await self.post("data_policy")
        self.assertEqual(refused.status_code, 409, refused.text)
        self.assertNotIn("borrowed-runtime-value", refused.text)
        self.probe.assert_not_called()


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
            return json.dumps([self.containers[arguments[-1]]])
        if arguments[:2] == ("volume", "inspect"):
            return json.dumps(
                [
                    {
                        "Name": arguments[-1],
                        "Driver": "local",
                        "Mountpoint": str(self.volume_root / arguments[-1] / "_data"),
                        **self.volume_metadata.get(arguments[-1], {}),
                    }
                ]
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
