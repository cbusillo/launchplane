"""A production recipient is additive, private and recoverable after every failure."""

from collections.abc import Callable
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
import unittest
from unittest.mock import patch

from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.production_recipient import (
    ProductionRecipientRequest,
    RecipientServiceLimits,
)
from control_plane.production_recipient import (
    ROUTE,
    ProductionRecipientAdapter,
    custody,
    plan,
    qualified_render,
)
from control_plane.provider_operations import run_durable_provider_operation
from control_plane.runtime_environments import SiteRuntimeEnvironment
from control_plane.storage.postgres import PostgresRecordStore
from tests.support.profiles import _generic_site_profile_payload


class ProductionRecipientTests(unittest.TestCase):
    database_url = ""

    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = PostgresRecordStore(
            database_url=self.database_url or f"sqlite+pysqlite:///{self.root / 'records.db'}"
        )
        self.store.ensure_schema()
        self.addCleanup(self.store.close)
        profile: dict[str, Any] = _generic_site_profile_payload()
        profile["public_website"] = "none"
        for lane in profile["lanes"]:
            lane.update(base_url="", health_url="")
        self.profile = LaunchplaneProductProfileRecord.model_validate(profile)
        self.store.write_product_profile_record(self.profile)
        self.product = self.profile.product
        self.context = next(lane.context for lane in self.profile.lanes if lane.instance == "prod")
        self.old = DokployTargetRecord(
            context=self.context,
            instance="prod",
            target_type="compose",
            target_name="old-production",
            compose_path="compose.yml",
            updated_at="2026-10-15T10:00:00Z",
        )
        self.old_id = DokployTargetIdRecord(
            context=self.context,
            instance="prod",
            target_id="old-recipient",
            updated_at=self.old.updated_at,
        )
        self.old_provider = ProviderTargetRecord.from_dokploy_records(
            target_record=self.old, target_id_record=self.old_id
        )
        self.store.write_dokploy_target_record(self.old)
        self.store.write_dokploy_target_id_record(self.old_id)
        self.store.write_provider_target_record(self.old_provider)
        self.provider: dict[str, dict[str, Any]] = {
            "old-recipient": {
                "composeId": "old-recipient",
                "appName": "old-production",
                "serverId": "old-server",
                "composeFile": "needed old production source",
                "env": "OLD_VALUE=preserve",
            }
        }
        self.effects: list[str] = []
        self.projects: list[dict[str, Any]] = []
        self.values = {
            "DB_VOLUME": "old-database",
            "CONFIG_VOLUME": "old-config",
            "DB_PORT": "3307",
            "HTTP_PORT": "8090",
            "DB_PASSWORD": "managed-fixture-secret",
        }
        self.body = ProductionRecipientRequest(
            context=self.context,
            source_commit="a" * 40,
            destination_server_id="destination-server",
            volume_keys={"database": "DB_VOLUME", "config": "CONFIG_VOLUME"},
            ports={"DB_PORT": 19219, "HTTP_PORT": 19220},
            application_service="web",
            database_service="database",
            service_limits={
                "web": RecipientServiceLimits(cpus=1, memory_bytes=2 * 1024**3),
                "database": RecipientServiceLimits(
                    cpus=1,
                    memory_bytes=2 * 1024**3,
                    command_memory_limits={"--innodb-buffer-pool-size": 512 * 1024**2},
                ),
            },
            reason="Prepare a fresh state recipient while keeping production recoverable.",
        )
        self.document: dict[str, Any] = {
            "services": {
                "web": {
                    "image": "${DOCKER_IMAGE_REFERENCE}",
                    "environment": {"DB_HOST": "database", "DB_PASSWORD": "${DB_PASSWORD}"},
                    "ports": ["${HTTP_PORT}:8000"],
                    "volumes": ["config:/config"],
                    "deploy": {"resources": {"limits": {"cpus": "1", "memory": "2G"}}},
                },
                "database": {
                    "image": "mariadb@sha256:" + "b" * 64,
                    "environment": {"MYSQL_PASSWORD": "${DB_PASSWORD}"},
                    "ports": ["${DB_PORT}:3306"],
                    "volumes": ["database:/var/lib/mysql"],
                    "command": ["--innodb-buffer-pool-size=512M"],
                    "deploy": {"resources": {"limits": {"cpus": "1", "memory": "2G"}}},
                },
            },
            "volumes": {
                "database": {"name": "${DB_VOLUME}"},
                "config": {"name": "${CONFIG_VOLUME}"},
            },
        }
        patches: list[Any] = [
            patch(
                "control_plane.production_recipient.source.read_dokploy_config",
                return_value=("https://provider.invalid", "fixture-token"),
            ),
            patch(
                "control_plane.production_recipient.read_product_compose",
                side_effect=lambda *args: json.dumps(self.document),
            ),
            patch(
                "control_plane.production_recipient.verified_product_image",
                return_value="example/site@sha256:" + "a" * 64,
            ),
            patch(
                "control_plane.production_recipient.runtime_environments.resolve_site_runtime_environment",
                side_effect=lambda **kwargs: SiteRuntimeEnvironment(
                    values=dict(self.values), secret_keys=frozenset({"DB_PASSWORD"})
                ),
            ),
            patch(
                "control_plane.production_recipient.api.fetch_dokploy_target_payload",
                side_effect=lambda **kwargs: deepcopy(self.provider[kwargs["target_id"]]),
            ),
            patch(
                "control_plane.production_recipient.api.fetch_dokploy_projects",
                side_effect=lambda **kwargs: deepcopy(self.projects),
            ),
            patch(
                "control_plane.production_recipient.api.list_deployments_for_target",
                return_value=[],
            ),
            patch(
                "control_plane.production_recipient.api.dokploy_request",
                side_effect=self.provider_write,
            ),
            patch(
                "control_plane.production_recipient.compose.fetch_dokploy_converted_compose_file",
                side_effect=lambda **kwargs: self.provider[kwargs["compose_id"]]["composeFile"],
            ),
            patch(
                "control_plane.production_recipient.inspect_resources",
                side_effect=self.inspect_resources,
            ),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.adapter = ProductionRecipientAdapter(
            self.root,
            self.store,
            self.product,
            self.body,
            "admin:fixture",
            "fixture-scope",
            "launchplane_req_fixture",
            "original-key",
        )
        record = self.adapter.current()
        self.body = self.body.model_copy(
            update={"mode": "apply", "reviewed_plan_sha256": record.plan_sha256}
        )
        self.adapter.request = self.body

    def provider_write(self, **kwargs: Any) -> Any:
        path, body = kwargs["path"], kwargs["payload"]
        self.effects.append(path)
        if path == "/api/project.create":
            self.projects.append(
                {"name": body["name"], "projectId": "new-project", "environments": []}
            )
            return {"project": {"projectId": "new-project"}}
        if path == "/api/environment.create":
            self.projects[0]["environments"].append(
                {"name": body["name"], "environmentId": "new-environment", "compose": []}
            )
            return {"environmentId": "new-environment"}
        if path == "/api/compose.create":
            self.projects[0]["environments"][0]["compose"].append(
                {"name": body["name"], "composeId": "new-recipient"}
            )
            self.provider["new-recipient"] = {
                "composeId": "new-recipient",
                "environmentId": body["environmentId"],
                "appName": body["appName"],
                "serverId": body["serverId"],
            }
            return {"composeId": "new-recipient"}
        if path == "/api/compose.update":
            self.assertEqual(body["composeId"], "new-recipient")
            self.provider["new-recipient"].update(body)
            return self.provider["new-recipient"]
        self.fail(f"Unexpected provider effect {path}")

    def inspect_resources(self, root: Path, record: Any, *, prepare: bool, checkpoint: Any) -> None:
        checkpoint("prepare_resources" if prepare else "inspect_resources")
        self.assertNotIn("old-database", record.volumes.values())
        self.assertNotIn("old-config", record.volumes.values())

    def run_operation(self, key: str = "original-key") -> Any:
        self.adapter.original_key = key
        return run_durable_provider_operation(
            store=self.store,
            scope="fixture-scope",
            route_path=ROUTE.format(product=self.product),
            idempotency_key=key,
            request_fingerprint="b" * 64,
            lease_owner="fixture-worker",
            response_trace_id="launchplane_req_fixture",
            adapter=self.adapter,
        )

    def assert_old_preserved(self) -> None:
        self.assertEqual(
            self.store.read_dokploy_target_record(context_name=self.context, instance_name="prod"),
            self.old,
        )
        self.assertEqual(
            self.store.read_dokploy_target_id_record(
                context_name=self.context, instance_name="prod"
            ),
            self.old_id,
        )
        self.assertEqual(
            self.store.read_provider_target_record(context_name=self.context, instance_name="prod"),
            self.old_provider,
        )
        self.assertEqual(
            self.provider["old-recipient"]["composeFile"], "needed old production source"
        )
        self.assertEqual(self.provider["old-recipient"]["env"], "OLD_VALUE=preserve")
        self.assertEqual(self.store.list_product_reconcile_requests(), ())

    def test_preparation_replay_readback_preserve_old_and_never_deploy_or_delete(self) -> None:
        first = self.run_operation()
        self.assertEqual(first.response_status_code, 202, first)
        effects = list(self.effects)
        self.assertEqual(self.run_operation().response_status_code, 202)
        self.assertEqual(self.effects, effects)
        saved = custody(self.store, self.product, self.context)
        assert saved is not None
        self.adapter.verify(saved)
        self.assertTrue(saved.qualified_at)
        output = json.dumps(plan(saved))
        self.assertNotIn(self.values["DB_PASSWORD"], output)
        self.assert_old_preserved()

    def test_unknown_create_retains_custody_and_refuses_new_keys_without_redispatch(self) -> None:
        real_write = self.provider_write

        def lose_reply(**kwargs: Any) -> Any:
            result = real_write(**kwargs)
            if kwargs["path"] == "/api/compose.create":
                raise OSError("reply lost after creating recipient")
            return result

        with patch(
            "control_plane.production_recipient.api.dokploy_request", side_effect=lose_reply
        ):
            self.assertEqual(self.run_operation().response_status_code, 409)
        effects = list(self.effects)
        with patch(
            "control_plane.production_recipient.api.fetch_dokploy_projects",
            side_effect=OSError("inventory unavailable"),
        ):
            self.assertEqual(self.run_operation().response_status_code, 409)
            self.assertEqual(self.run_operation("another-key").response_status_code, 409)
        self.assertEqual(self.effects, effects)
        self.adapter.request = self.body.model_copy(
            update={"reason": "Changed intent after unknown effect"}
        )
        with self.assertRaises(ValueError):
            self.adapter.current()
        self.assertIsNotNone(custody(self.store, self.product, self.context))
        self.assertIn("new-recipient", self.provider)
        self.assert_old_preserved()

    def test_lost_configuration_reply_adopts_matching_actual_readback_only(self) -> None:
        def lose_reply(**kwargs: Any) -> Any:
            result = self.provider_write(**kwargs)
            if kwargs["path"] == "/api/compose.update":
                raise OSError("reply lost after configuration")
            return result

        with patch(
            "control_plane.production_recipient.api.dokploy_request", side_effect=lose_reply
        ):
            self.assertEqual(self.run_operation().response_status_code, 409)
        effects = list(self.effects)
        before = custody(self.store, self.product, self.context)
        with (
            patch("control_plane.production_recipient.inspect_resources") as host_check,
            patch.object(self.store, "write_production_recipient") as custody_write,
        ):
            for _ in range(2):
                observation = self.adapter.observe(
                    "unused", "configure_recipient", self.body.reviewed_plan_sha256
                )
                self.assertEqual(observation.outcome, "absent")
                self.assertTrue(observation.retry_safe)
            host_check.assert_not_called()
            custody_write.assert_not_called()
        self.assertEqual(custody(self.store, self.product, self.context), before)
        self.assertEqual(self.run_operation().response_status_code, 202)
        self.assertEqual(self.effects, effects)
        self.assert_old_preserved()

    def test_changed_authority_or_review_never_creates_recipient(self) -> None:
        changed = self.profile.model_copy(update={"updated_at": "2026-10-15T11:00:00Z"})
        self.store.write_product_profile_record(changed)
        with self.assertRaises(ValueError):
            self.run_operation()
        self.assertEqual(self.effects, [])
        self.assert_old_preserved()

    def test_provider_render_drift_never_qualifies_and_old_binding_survives(self) -> None:
        def wrong_render(**kwargs: Any) -> str:
            document = json.loads(self.provider[kwargs["compose_id"]]["composeFile"])
            document["services"]["database"]["deploy"]["resources"]["limits"]["cpus"] = "8"
            return json.dumps(document)

        with patch(
            "control_plane.production_recipient.compose.fetch_dokploy_converted_compose_file",
            side_effect=wrong_render,
        ):
            self.assertEqual(self.run_operation().response_status_code, 409)
        saved = custody(self.store, self.product, self.context)
        assert saved is not None
        self.assertFalse(saved.qualified_at)
        self.assert_old_preserved()

    def test_storage_alias_ports_missing_limits_buffer_pool_or_mutable_app_image_refuse(
        self,
    ) -> None:
        mutations: list[Callable[[Any], None]] = [
            lambda doc: doc["volumes"]["database"].update(name="old-database"),
            lambda doc: doc["services"]["web"].update(ports=["3307:8000"]),
            lambda doc: doc["services"]["web"].pop("deploy"),
            lambda doc: doc["services"]["database"].update(
                command=["--innodb-buffer-pool-size=2G"]
            ),
            lambda doc: doc["services"]["web"].update(image="example/site:latest"),
            lambda doc: doc["services"]["web"].update(privileged=True),
            lambda doc: doc.update(networks={"shared": {"external": True}}),
        ]
        record = self.adapter.current()
        values = {
            **self.values,
            "DB_VOLUME": record.volumes["database"],
            "CONFIG_VOLUME": record.volumes["config"],
            "DB_PORT": "19219",
            "HTTP_PORT": "19220",
            "DOCKER_IMAGE_REFERENCE": record.image_reference,
        }
        for mutate in mutations:
            document = deepcopy(self.document)
            mutate(document)
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                qualified_render(
                    record.model_copy(update={"compose_source": json.dumps(document)}), values
                )
        self.assert_old_preserved()

    def test_custody_cas_and_recovery_identities_are_immutable(self) -> None:
        self.assertEqual(self.run_operation().response_status_code, 202)
        record = custody(self.store, self.product, self.context)
        assert record is not None
        for replacement in (
            record.model_copy(update={"target_id": "another-target"}),
            record.model_copy(update={"original_key": "another-key"}),
            record.model_copy(update={"authority_snapshot": {}}),
        ):
            with self.assertRaises(ValueError):
                self.store.write_production_recipient(
                    expected=record.model_dump(mode="json"),
                    replacement=replacement.model_dump(mode="json"),
                )
        with self.assertRaises(ValueError):
            self.store.write_production_recipient(
                expected=None, replacement=record.model_dump(mode="json")
            )
        self.assert_old_preserved()

    def test_failure_before_first_provider_checkpoint_can_retry_original_key(self) -> None:
        original = self.adapter.require_unchanged
        failed = False

        def refuse_once(record: Any) -> None:
            nonlocal failed
            original(record)
            if not failed and record.phase == "create_project":
                failed = True
                raise OSError("transient pre-effect read failure")

        with patch.object(self.adapter, "require_unchanged", side_effect=refuse_once):
            with self.assertRaises(ValueError):
                self.run_operation()
        self.assertTrue(failed)
        self.assertEqual(self.effects, [])
        self.assertEqual(self.run_operation().response_status_code, 202)
        self.assertEqual(self.effects.count("/api/compose.create"), 1)
        self.assert_old_preserved()

    def test_observed_create_is_adopted_under_original_key_without_recreation(self) -> None:
        def lose_reply(**kwargs: Any) -> Any:
            result = self.provider_write(**kwargs)
            if kwargs["path"] == "/api/compose.create":
                raise OSError("reply lost after actual creation")
            return result

        with patch(
            "control_plane.production_recipient.api.dokploy_request", side_effect=lose_reply
        ):
            self.assertEqual(self.run_operation().response_status_code, 409)
        self.assertEqual(self.run_operation().response_status_code, 202)
        self.assertEqual(self.effects.count("/api/compose.create"), 1)
        self.assert_old_preserved()

    def test_transient_read_on_reacquired_attempt_retains_the_original_create_fence(self) -> None:
        def lose_reply(**kwargs: Any) -> Any:
            result = self.provider_write(**kwargs)
            if kwargs["path"] == "/api/compose.create":
                raise OSError("lost create reply")
            return result

        with patch(
            "control_plane.production_recipient.api.dokploy_request", side_effect=lose_reply
        ):
            self.assertEqual(self.run_operation().response_status_code, 409)
        with patch.object(self.adapter, "current", side_effect=OSError("transient retry read")):
            self.assertEqual(self.run_operation().response_status_code, 409)
        retained = self.store.read_idempotency_record(
            scope=self.adapter.scope,
            route_path=ROUTE.format(product=self.product),
            idempotency_key=self.adapter.original_key,
        )
        assert retained is not None
        self.assertEqual(retained.state, "reconcile_required")
        self.assertEqual(self.run_operation().response_status_code, 202)
        self.assertEqual(self.effects.count("/api/compose.create"), 1)
        self.assert_old_preserved()

    def test_zero_effect_drift_can_replan_with_retained_history_and_a_new_key(self) -> None:
        original = self.adapter.require_unchanged

        def drift_before_effect(record: Any) -> None:
            if record.phase == "create_project":
                self.store.write_product_profile_record(
                    self.profile.model_copy(
                        update={"updated_at": self.profile.updated_at + "-changed"}
                    )
                )
            original(record)

        with patch.object(self.adapter, "require_unchanged", side_effect=drift_before_effect):
            with self.assertRaises(ValueError):
                self.run_operation()
        prior = custody(self.store, self.product, self.context)
        assert prior is not None
        self.assertFalse(self.effects)
        with self.assertRaises(ValueError):
            self.adapter.current()
        self.adapter.original_key = "reviewed-correction-key"
        self.adapter.request = self.body.model_copy(update={"reason": "Corrected reviewed intent"})
        replacement = self.adapter.current()
        self.adapter.request = self.adapter.request.model_copy(
            update={"reviewed_plan_sha256": replacement.plan_sha256}
        )
        self.body = self.adapter.request
        self.assertEqual(self.run_operation("reviewed-correction-key").response_status_code, 202)
        current = custody(self.store, self.product, self.context)
        assert current is not None
        self.assertEqual(
            current.prior_attempts, [prior.model_dump(mode="json", exclude={"prior_attempts"})]
        )
        self.assertEqual(current.supersedes_plan_sha256, prior.plan_sha256)
        self.assertEqual(self.effects.count("/api/compose.create"), 1)
        self.assert_old_preserved()

    def test_command_memory_aliases_and_separate_values_cannot_override_the_bound(self) -> None:
        record = self.adapter.current()
        from control_plane.production_recipient import runtime_values

        values = runtime_values(self.store, self.root, record)
        for arguments in (
            ["--innodb_buffer_pool_size", "512M"],
            ["--innodb-buffer-pool-size=512M", "--innodb_buffer_pool_size=8G"],
            ["--innodb-buffer-pool-size=512M", "--innodb_buffer_pool_size", "8G"],
            ["--innodb_buffer_pool_size=8G"],
            ["--innodb-buffer-pool-size=512M", "--loose-innodb_buffer_pool_size=8G"],
            ["--innodb-buffer-pool-size=512M", "--maximum-innodb_buffer_pool_size=8G"],
            ["--innodb-buffer-pool-size=512M", "--defaults-extra-file=/config/my.cnf"],
            [
                "sh",
                "-c",
                "exec docker-entrypoint.sh mariadbd --innodb-buffer-pool-size=8G",
                "--innodb-buffer-pool-size=512M",
            ],
        ):
            with self.subTest(arguments=arguments):
                document = deepcopy(self.document)
                document["services"]["database"]["command"] = arguments
                changed = record.model_copy(update={"compose_source": json.dumps(document)})
                if arguments == ["--innodb_buffer_pool_size", "512M"]:
                    qualified_render(changed, values)
                else:
                    with self.assertRaises(ValueError):
                        qualified_render(changed, values)
        document = deepcopy(self.document)
        document["services"]["database"]["entrypoint"] = [
            "mariadbd",
            "--innodb-buffer-pool-size=8G",
        ]
        with self.assertRaises(ValueError):
            qualified_render(
                record.model_copy(update={"compose_source": json.dumps(document)}), values
            )

    def test_provider_status_observations_do_not_change_binding_authority(self) -> None:
        original = self.provider_write

        def change_status(**kwargs: Any) -> Any:
            result = original(**kwargs)
            self.provider["old-recipient"].update(
                composeStatus="running", deployments=[{"status": "done"}]
            )
            return result

        with patch(
            "control_plane.production_recipient.api.dokploy_request", side_effect=change_status
        ):
            self.assertEqual(self.run_operation().response_status_code, 202)
        record = custody(self.store, self.product, self.context)
        assert record is not None
        self.adapter.verify(record)
        self.assert_old_preserved()

    def test_authority_drift_after_create_does_not_lose_provider_identity(self) -> None:
        original = self.provider_write

        def change_authority(**kwargs: Any) -> Any:
            result = original(**kwargs)
            if kwargs["path"] == "/api/compose.create":
                self.store.write_product_profile_record(
                    self.profile.model_copy(update={"updated_at": "2026-10-15T12:00:00Z"})
                )
            return result

        with patch(
            "control_plane.production_recipient.api.dokploy_request", side_effect=change_authority
        ):
            self.assertEqual(self.run_operation().response_status_code, 409)
        record = custody(self.store, self.product, self.context)
        assert record is not None
        self.assertEqual(record.target_id, self.provider["new-recipient"]["composeId"])
        self.assertFalse(record.qualified_at)
        self.assert_old_preserved()

    def test_managed_old_database_endpoint_is_refused_and_local_url_is_supported(self) -> None:
        record = self.adapter.current()
        values = {
            **self.values,
            "DB_VOLUME": record.volumes["database"],
            "CONFIG_VOLUME": record.volumes["config"],
            "DB_PORT": "19219",
            "HTTP_PORT": "19220",
            "DOCKER_IMAGE_REFERENCE": record.image_reference,
            "DB_HOST": "old-production.internal",
        }
        document = deepcopy(self.document)
        document["services"]["web"]["environment"]["DB_HOST"] = "${DB_HOST}"
        with self.assertRaises(ValueError):
            qualified_render(
                record.model_copy(update={"compose_source": json.dumps(document)}), values
            )
        document["services"]["web"]["environment"]["DB_HOST"] = "database"
        document["services"]["web"]["environment"]["DATABASE_URL"] = "mysql://database:3306/example"
        qualified_render(record.model_copy(update={"compose_source": json.dumps(document)}), values)
