from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane import dokploy as control_plane_dokploy
from control_plane.dokploy_target_inspect import (
    DokployTargetInspectRequest,
    DokployTargetInspectStore,
    inspect_dokploy_target,
    summarize_dokploy_target_payload,
)
from control_plane.dokploy.application_runtime import (
    ApplicationContainerMetadata,
    ApplicationEnvironmentKeys,
    ApplicationRuntimeObservation,
    ApplicationTaskObservation,
    MAX_APPLICATION_DIAGNOSTIC_ITEMS,
)
from control_plane.dokploy.runtime_evidence import DokployEvidenceProviderError
from control_plane.storage.postgres import PostgresRecordStore


def _sqlite_database_url(path: Path) -> str:
    return f"sqlite:///{path}"


def _require_mapping(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return value


def _fake_target_payload(
    *, host: str, token: str, target_type: str, target_id: str
) -> control_plane_dokploy.JsonObject:
    del host, token, target_type, target_id
    return {
        "id": "compose-cm-prod",
        "name": "cm-prod",
        "serverId": "server-123",
        "environment": {
            "id": "env-prod",
            "name": "prod",
            "project": {"id": "project-odoo", "name": "odoo"},
        },
        "env": {"ODOO_DB_PASSWORD": "secret"},
    }


class DokployTargetInspectTests(unittest.TestCase):
    def test_summarize_payload_redacts_env_values_and_keeps_identity_fields(self) -> None:
        summary = summarize_dokploy_target_payload(
            target_type="compose",
            target_id="compose-123",
            payload={
                "id": "compose-123",
                "name": "odoo-tenant-cm-website-prod",
                "appName": "odoo-tenant-cm-website-prod",
                "serverId": "server-123",
                "sourceType": "raw",
                "composePath": "docker-compose.yml",
                "environment": {
                    "id": "env-prod",
                    "name": "prod",
                    "project": {"id": "project-odoo", "name": "odoo"},
                },
                "env": "ODOO_DB_PASSWORD=secret\nDISABLE_ODOO_ONLINE=true\n",
                "domains": [
                    {"id": "domain-1", "host": "cm-website-prod.shinycomputers.com", "port": 8069}
                ],
            },
        )

        self.assertEqual(summary["id"], "compose-123")
        self.assertEqual(summary["server_id"], "server-123")
        environment = _require_mapping(summary["environment"])
        env = _require_mapping(summary["env"])
        self.assertEqual(environment["id"], "env-prod")
        self.assertEqual(environment["project_id"], "project-odoo")
        self.assertEqual(env["key_count"], 2)
        self.assertEqual(env["keys"], ["DISABLE_ODOO_ONLINE", "ODOO_DB_PASSWORD"])
        self.assertTrue(env["values_redacted"])
        self.assertNotIn("secret", str(summary))

    def test_inspect_tracked_target_includes_records_and_redacted_provider_summary(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(temporary_directory_name) / "db.sqlite3")
            )
            try:
                store.ensure_schema()
                store.write_dokploy_target_record(
                    DokployTargetRecord(
                        context="cm_website",
                        instance="prod",
                        target_type="compose",
                        target_name="cm-prod",
                        project_name="odoo",
                        domains=("cm-website-prod.shinycomputers.com",),
                        public_hosts=("example.com", "www.example.com"),
                        updated_at="2026-06-14T00:00:00Z",
                        source_label="test",
                    )
                )
                store.write_dokploy_target_id_record(
                    DokployTargetIdRecord(
                        context="cm_website",
                        instance="prod",
                        target_id="compose-cm-prod",
                        updated_at="2026-06-14T00:00:00Z",
                        source_label="test",
                    )
                )
                store.write_provider_target_record(
                    ProviderTargetRecord(
                        context="cm_website",
                        instance="prod",
                        provider_id="dokploy",
                        target_category="compose",
                        target_id="compose-cm-prod",
                        display_name="cm-prod",
                        provider_target_type="compose",
                        provider_evidence={"project_name": "odoo"},
                        updated_at="2026-06-14T00:00:00Z",
                        source_label="test",
                    )
                )

                result = inspect_dokploy_target(
                    record_store=store,
                    host="https://dokploy.example.invalid",
                    token="token",
                    request=DokployTargetInspectRequest(
                        context="cm_website",
                        instance="prod",
                    ),
                    fetch_target_payload=_fake_target_payload,
                )
                stored_evidence = store.read_provider_target_record(
                    context_name="cm_website", instance_name="prod"
                ).provider_evidence
                stored_target = store.read_dokploy_target_record(
                    context_name="cm_website", instance_name="prod"
                )
            finally:
                store.close()

        self.assertEqual(result["target_id"], "compose-cm-prod")
        tracked_target = _require_mapping(result["tracked_target"])
        provider_target_record = _require_mapping(result["provider_target_record"])
        provider = _require_mapping(result["provider"])
        provider_environment = _require_mapping(provider["environment"])
        provider_env = _require_mapping(provider["env"])
        self.assertEqual(tracked_target["target_name"], "cm-prod")
        self.assertEqual(tracked_target["public_hosts"], list(stored_target.public_hosts))
        self.assertEqual(tracked_target["domains"], list(stored_target.domains))
        self.assertEqual(provider_target_record["display_name"], "cm-prod")
        self.assertEqual(
            provider_target_record["provider_evidence"],
            stored_evidence,
        )
        self.assertEqual(provider_environment["id"], "env-prod")
        self.assertEqual(provider_env["keys"], ["ODOO_DB_PASSWORD"])
        self.assertTrue(result["provider_payload_redacted"])
        self.assertNotIn("secret", str(result))


class ApplicationRuntimeInspectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.expected = "ghcr.io/example/app@sha256:" + "a" * 64
        self.actual = "ghcr.io/example/app@sha256:" + "b" * 64
        self.target = DokployTargetRecord(
            context="fixture",
            instance="testing",
            target_type="application",
            target_name="fixture-app",
            updated_at="2026-10-21T12:00:00Z",
        )
        self.target_id = DokployTargetIdRecord(
            context="fixture",
            instance="testing",
            target_id="application-1",
            updated_at=self.target.updated_at,
        )
        self.provider = ProviderTargetRecord.from_dokploy_records(
            target_record=self.target,
            target_id_record=self.target_id,
        )
        self.store = Mock(spec=DokployTargetInspectStore)
        self.store.read_dokploy_target_record.return_value = self.target
        self.store.read_dokploy_target_id_record.return_value = self.target_id
        self.store.read_provider_target_record.return_value = self.provider
        self.payload: dict[str, object] = {
            "applicationId": "application-1",
            "appName": "fixture-app",
            "serverId": "server-1",
            "dockerImage": self.expected,
            "env": "APP_TOKEN=private-value",
        }
        self.observed = ApplicationRuntimeObservation(
            application_id="application-1",
            app_name="fixture-app",
            service_id="service-1",
            service_image="ghcr.io/example/app:old",
            service_deployment_record_id="deployment-old",
            desired_replicas=1,
            rollout_state="completed",
            rollout_started_at="2026-10-21T12:00:00Z",
            running_container_images=((self.actual,),),
            running_container_deployment_record_ids=("deployment-old",),
            running_container_health=("healthy",),
            pending_task_count=0,
            tasks=(
                ApplicationTaskObservation(
                    "ghcr.io/example/app:old", "deployment-old", "running", "task-1", "running"
                ),
            ),
            server_id="server-1",
            service_environment_keys=ApplicationEnvironmentKeys(
                ("APP_TOKEN", "FEATURE_CONFIG"), True
            ),
            running_containers=(
                ApplicationContainerMetadata(
                    "c" * 64, "task-1", ApplicationEnvironmentKeys(("APP_TOKEN",), True)
                ),
            ),
        )
        self.fetch = Mock(return_value=self.observed)
        self.fetch_payload = Mock(return_value=self.payload)

    def inspect(self, **selection: str) -> dict[str, object]:
        return inspect_dokploy_target(
            record_store=self.store,
            host="https://provider.invalid",
            token="fixture-token",
            request=DokployTargetInspectRequest.model_validate(
                {"context": "fixture", "instance": "testing"} | selection
            ),
            fetch_target_payload=self.fetch_payload,
            fetch_application=self.fetch,
            now=lambda: datetime(2026, 10, 21, 12, tzinfo=timezone.utc),
        )

    def test_saved_image_is_not_actual_image_and_environment_sets_remain_separate(self) -> None:
        result = self.inspect(expected_image=self.expected)
        runtime = _require_mapping(result["runtime_evidence"])
        self.assertTrue(runtime["process_ready"])
        self.assertFalse(runtime["image_matches_expected"])
        self.assertTrue(runtime["diagnostic_complete"])
        self.assertEqual(runtime["service_image"], self.observed.service_image)
        containers = runtime["containers"]
        assert isinstance(containers, list)
        container = _require_mapping(containers[0])
        self.assertEqual(container["immutable_images"], [self.actual])
        self.assertEqual(
            container["container_id"], self.observed.running_containers[0].container_id
        )
        self.assertEqual(_require_mapping(container["environment_keys"])["names"], ["APP_TOKEN"])
        self.assertEqual(
            _require_mapping(runtime["service_environment_keys"])["names"],
            ["APP_TOKEN", "FEATURE_CONFIG"],
        )
        self.assertNotIn("private-value", str(result))
        self.fetch.return_value = replace(
            self.observed, running_container_images=((self.expected,),)
        )
        self.assertTrue(
            _require_mapping(self.inspect(expected_image=self.expected)["runtime_evidence"])[
                "image_matches_expected"
            ]
        )

    def test_metadata_only_makes_no_runtime_read_and_compose_image_only_is_refused(self) -> None:
        self.assertNotIn("runtime_evidence", self.inspect())
        self.fetch.assert_not_called()
        self.store.read_dokploy_target_record.return_value = self.target.model_copy(
            update={"target_type": "compose"}
        )
        self.fetch_payload.reset_mock()
        with self.assertRaises(ValueError):
            self.inspect(expected_image=self.expected)
        self.fetch_payload.assert_not_called()
        self.fetch.assert_not_called()
        with self.assertRaises(ValueError):
            DokployTargetInspectRequest(context="fixture", instance="testing", service="api")
        with self.assertRaises(ValueError):
            DokployTargetInspectRequest(context="fixture", instance="testing", event="event")

    def test_incomplete_empty_or_over_bound_reads_cannot_credit_actual_image(self) -> None:
        cases = (
            replace(self.observed, task_evidence_complete=False),
            replace(
                self.observed,
                running_container_images=(),
                running_container_deployment_record_ids=(),
                running_container_health=(),
                running_containers=(),
            ),
            replace(self.observed, running_containers=()),
            replace(
                self.observed, tasks=self.observed.tasks * (MAX_APPLICATION_DIAGNOSTIC_ITEMS + 1)
            ),
        )
        for observed in cases:
            with self.subTest(observed=observed):
                self.fetch.return_value = observed
                runtime = _require_mapping(
                    self.inspect(expected_image=self.actual)["runtime_evidence"]
                )
                self.assertIsNone(runtime["image_matches_expected"])
                tasks = runtime["tasks"]
                assert isinstance(tasks, list)
                self.assertLessEqual(len(tasks), MAX_APPLICATION_DIAGNOSTIC_ITEMS)
        self.fetch.return_value = replace(self.observed, running_container_health=("unhealthy",))
        runtime = _require_mapping(self.inspect(expected_image=self.actual)["runtime_evidence"])
        self.assertTrue(runtime["image_matches_expected"])
        self.assertFalse(runtime["process_ready"])

    def test_scope_or_provider_placement_changes_discard_observation(self) -> None:
        for observed in (
            replace(self.observed, application_id="foreign"),
            replace(self.observed, app_name="other"),
            replace(self.observed, server_id="other"),
        ):
            with self.subTest(observed=observed):
                self.fetch.return_value = observed
                with self.assertRaises(DokployEvidenceProviderError) as provider_error:
                    self.inspect(expected_image=self.expected)
        self.fetch.return_value = self.observed
        self.store.read_dokploy_target_id_record.side_effect = [
            self.target_id,
            self.target_id.model_copy(update={"target_id": "replacement"}),
        ]
        with self.assertRaises(DokployEvidenceProviderError) as binding_error:
            self.inspect(expected_image=self.expected)
        self.assertNotEqual(binding_error.exception.operation, provider_error.exception.operation)

    def test_local_server_normalization_and_non_authority_refresh_are_supported(self) -> None:
        self.payload["serverId"] = None
        self.fetch.return_value = replace(self.observed, server_id="")
        self.store.read_dokploy_target_record.side_effect = [
            self.target,
            self.target.model_copy(update={"updated_at": "2026-10-21T13:00:00Z"}),
        ]
        self.assertTrue(
            _require_mapping(self.inspect(expected_image=self.expected)["runtime_evidence"])[
                "diagnostic_complete"
            ]
        )


if __name__ == "__main__":
    unittest.main()
