import base64
import hashlib
import json
import os
import subprocess
import sys
import tarfile
import tomllib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
from unittest.mock import patch

import click
from click.testing import CliRunner, Result
from pydantic import ValidationError

from control_plane import dokploy as control_plane_dokploy
from control_plane.dokploy import JsonValue
from control_plane.dokploy import api as dokploy_api
from control_plane.odoo_instance_overrides import LAUNCHPLANE_INSTANCE_OVERRIDES_REQUIRED_ENV_KEY
from control_plane.odoo_instance_overrides import LAUNCHPLANE_WEBSITE_BOOTSTRAP_REQUIRED_ENV_KEY
from control_plane.odoo_instance_overrides import ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY
from control_plane import secrets as control_plane_secrets
from control_plane.cli import main
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.release_tuple_record import ReleaseTupleRecord
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductImageProfile,
    ProductLaneProfile,
    ProductPreviewProfile,
)
from control_plane.storage.postgres import PostgresRecordStore
from tests.support.cli import _allow_direct_db_mutation_argument


def _sqlite_database_url(database_path: Path) -> str:
    return f"sqlite+pysqlite:///{database_path}"


def _assert_direct_db_mutation_rejected(test_case: unittest.TestCase, result: Result) -> None:
    test_case.assertNotEqual(result.exit_code, 0)
    test_case.assertIn("Direct local DB mutation is restricted", result.output)
    test_case.assertIn("--allow-direct-db-mutation", result.output)


def _write_dokploy_managed_secrets(*, store: PostgresRecordStore, host: str, token: str) -> None:
    control_plane_secrets.write_secret_value(
        record_store=store,
        scope="global",
        integration=control_plane_secrets.DOKPLOY_SECRET_INTEGRATION,
        name="host",
        plaintext_value=host,
        binding_key="DOKPLOY_HOST",
        actor="test",
    )
    control_plane_secrets.write_secret_value(
        record_store=store,
        scope="global",
        integration=control_plane_secrets.DOKPLOY_SECRET_INTEGRATION,
        name="token",
        plaintext_value=token,
        binding_key="DOKPLOY_TOKEN",
        actor="test",
    )


def _seed_dokploy_target_records(
    *,
    store: PostgresRecordStore,
    payload: str,
    updated_at: str = "2026-04-22T00:00:00Z",
) -> None:
    source_of_truth = control_plane_dokploy.DokploySourceOfTruth.model_validate(
        tomllib.loads(payload.strip())
    )
    for target in source_of_truth.targets:
        store.write_dokploy_target_record(
            control_plane_dokploy.build_dokploy_target_record_from_definition(
                target,
                updated_at=updated_at,
                source_label="test",
            )
        )
        store.write_dokploy_target_id_record(
            DokployTargetIdRecord(
                context=target.context,
                instance=target.instance,
                target_id=target.target_id,
                updated_at=updated_at,
                source_label="test",
            )
        )


def _write_odoo_product_profile_record(*, store: PostgresRecordStore) -> None:
    store.write_product_profile_record(
        LaunchplaneProductProfileRecord(
            product="odoo-tenant-cm",
            display_name="Odoo CM",
            repository="cbusillo/odoo-tenant-cm",
            driver_id="odoo",
            image=ProductImageProfile(repository="ghcr.io/cbusillo/odoo-tenant-cm"),
            runtime_port=8069,
            health_path="/web/health",
            lanes=(
                ProductLaneProfile(
                    instance="testing",
                    context="cm",
                    base_url="https://cm-testing.shinycomputers.com",
                ),
            ),
            preview=ProductPreviewProfile(enabled=True, context="cm"),
            updated_at="2026-05-09T00:00:00Z",
            source="test",
        )
    )


class _FakeDokployTargetStore:
    def __init__(
        self,
        *,
        target_records: tuple[DokployTargetRecord, ...],
        target_id_records: tuple[DokployTargetIdRecord, ...],
    ) -> None:
        self.target_records = target_records
        self.target_id_records = target_id_records

    def list_dokploy_target_records(self) -> tuple[DokployTargetRecord, ...]:
        return self.target_records

    def list_dokploy_target_id_records(self) -> tuple[DokployTargetIdRecord, ...]:
        return self.target_id_records


class DokployConfigTests(unittest.TestCase):
    def test_wait_for_schedule_deployment_exposes_exact_terminal_failure(self) -> None:
        with (
            patch(
                "control_plane.dokploy.api.latest_deployment_for_schedule",
                return_value={
                    "deploymentId": "deployment-failed",
                    "status": "failed",
                },
            ),
            patch("control_plane.dokploy.api.time.sleep"),
        ):
            with self.assertRaises(dokploy_api.DokployDeploymentFailed) as raised:
                control_plane_dokploy.wait_for_dokploy_schedule_deployment(
                    host="https://dokploy.example",
                    token="token",
                    schedule_id="schedule-one",
                    before_key="deployment-before",
                    timeout_seconds=30,
                )

        self.assertEqual(raised.exception.deployment_id, "deployment-failed")
        self.assertEqual(raised.exception.deployment_status, "failed")
        self.assertEqual(
            raised.exception.format_message(),
            "Dokploy schedule deployment failed: deployment=deployment-failed status=failed",
        )

    def test_wait_for_target_deployment_tracks_exact_operation_title(self) -> None:
        exact_deployment = {
            "deploymentId": "deployment-exact",
            "title": "Launchplane operation exact",
            "status": "success",
        }
        with (
            patch(
                "control_plane.dokploy.api.deployment_for_target_by_title",
                side_effect=(None, exact_deployment),
            ) as exact_lookup,
            patch(
                "control_plane.dokploy.api.latest_deployment_for_target",
                return_value={
                    "deploymentId": "deployment-unrelated",
                    "status": "success",
                },
            ) as latest_lookup,
            patch("control_plane.dokploy.api.time.sleep"),
        ):
            result = control_plane_dokploy.wait_for_target_deployment(
                host="https://dokploy.example",
                token="token",
                target_type="application",
                target_id="application-one",
                before_key="deployment-before",
                timeout_seconds=30,
                deployment_title="Launchplane operation exact",
            )

        self.assertEqual(result, "deployment=deployment-exact status=success")
        self.assertEqual(exact_lookup.call_count, 2)
        latest_lookup.assert_not_called()

    def test_odoo_target_replacement_plan_cli_requires_product(self) -> None:
        result = CliRunner().invoke(
            main,
            [
                "odoo-targets",
                "replacement-plan",
                "--database-url",
                "postgresql://launchplane.example/launchplane",
                "--instance",
                "testing",
            ],
        )

        self.assertEqual(result.exit_code, 2)
        self.assertIn("Missing option '--product'", result.output)

    def test_load_optional_source_of_truth_uses_structural_store_boundary(self) -> None:
        target = control_plane_dokploy.DokployTargetDefinition(
            context="verireel",
            instance="prod",
            target_id="placeholder",
            target_type="application",
            target_name="ver-prod-app",
        )
        source_of_truth = control_plane_dokploy.load_optional_dokploy_source_of_truth_from_store(
            record_store=_FakeDokployTargetStore(
                target_records=(
                    control_plane_dokploy.build_dokploy_target_record_from_definition(
                        target,
                        updated_at="2026-05-01T00:00:00Z",
                        source_label="fake-store",
                    ),
                ),
                target_id_records=(
                    DokployTargetIdRecord(
                        context="verireel",
                        instance="prod",
                        target_id="target-ver-prod",
                        updated_at="2026-05-01T00:00:00Z",
                        source_label="fake-store",
                    ),
                ),
            )
        )

        self.assertIsNotNone(source_of_truth)
        assert source_of_truth is not None
        self.assertEqual(len(source_of_truth.targets), 1)
        self.assertEqual(source_of_truth.targets[0].target_id, "target-ver-prod")
        self.assertEqual(source_of_truth.targets[0].target_name, "ver-prod-app")

    def test_load_optional_source_of_truth_returns_none_for_empty_store(self) -> None:
        source_of_truth = control_plane_dokploy.load_optional_dokploy_source_of_truth_from_store(
            record_store=_FakeDokployTargetStore(
                target_records=(),
                target_id_records=(),
            )
        )

        self.assertIsNone(source_of_truth)

    def test_application_log_payload_normalization_redacts_likely_secrets(self) -> None:
        lines = control_plane_dokploy.normalize_dokploy_log_payload(
            {
                "logs": [
                    "started",
                    (
                        "RESEND_API_KEY=re_123 Bearer abc.def "
                        "SMTP_PASSWORD=smtp-secret LAUNCHPLANE_DATABASE_URL=postgresql://secret "
                        "postgresql://odoo:db-secret@database/cm"
                    ),
                ]
            }
        )

        self.assertEqual(lines[0], "started")
        self.assertIn("RESEND_API_KEY=[redacted]", lines[1])
        self.assertIn("Bearer [redacted]", lines[1])
        self.assertIn("SMTP_PASSWORD=[redacted]", lines[1])
        self.assertIn("LAUNCHPLANE_DATABASE_URL=[redacted]", lines[1])
        self.assertIn("postgresql://odoo:[redacted]@database/cm", lines[1])
        self.assertNotIn("re_123", lines[1])
        self.assertNotIn("smtp-secret", lines[1])
        self.assertNotIn("postgresql://secret", lines[1])
        self.assertNotIn("db-secret", lines[1])

    def test_redact_dokploy_log_line_redacts_quoted_secret_fields(self) -> None:
        redacted_line = control_plane_dokploy.redact_dokploy_log_line(
            '{"API_KEY":"super-secret","nested":{"SERVICE_TOKEN":"inner-secret"},"note":"safe"}'
        )

        self.assertEqual(
            redacted_line,
            '{"API_KEY":"[redacted]","nested":{"SERVICE_TOKEN":"[redacted]"},"note":"safe"}',
        )

    def test_normalize_dokploy_log_payload_reads_message_objects_in_dict_lists(self) -> None:
        lines = control_plane_dokploy.normalize_dokploy_log_payload(
            {
                "logs": [
                    {"message": "started"},
                    {"line": "SERVICE_TOKEN=inner-secret"},
                ]
            }
        )

        self.assertEqual(lines, ("started", "SERVICE_TOKEN=[redacted]"))

    def test_fetch_application_logs_calls_dokploy_read_logs_endpoint(self) -> None:
        requests: list[dict[str, object]] = []

        def capture_request(**kwargs: object) -> dict[str, str]:
            requests.append(kwargs)
            return {"logs": "one\ntwo\nTHREE_TOKEN=secret"}

        with patch(
            "control_plane.dokploy.api.dokploy_request",
            side_effect=capture_request,
        ):
            lines = control_plane_dokploy.fetch_dokploy_application_logs(
                host="https://dokploy.example.com",
                token="secret-token",
                application_id="app-123",
                line_count=2,
            )

        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["path"], "/api/application.readLogs")
        self.assertEqual(
            requests[0]["query"], {"applicationId": "app-123", "tail": 2, "since": "all"}
        )
        self.assertEqual(lines, ("two", "THREE_TOKEN=[redacted]"))

    def test_fetch_compose_logs_calls_dokploy_read_logs_endpoint(self) -> None:
        requests: list[dict[str, object]] = []

        def capture_request(**kwargs: object) -> object:
            requests.append(kwargs)
            if kwargs["path"] == "/api/docker.getContainersByAppNameMatch":
                return [
                    {"containerId": "database-container", "name": "cm-database-1"},
                    {"containerId": "web-container", "name": "cm-web-1"},
                ]
            return {"logs": "one\ntwo\nTHREE_TOKEN=secret"}

        with patch(
            "control_plane.dokploy.api.dokploy_request",
            side_effect=capture_request,
        ):
            lines = control_plane_dokploy.fetch_dokploy_compose_logs(
                host="https://dokploy.example.com",
                token="secret-token",
                compose_id="compose-123",
                app_name="cm-testing-iul0ql",
                server_id="server-1",
                line_count=2,
                since="5m",
                search="odoo",
            )

        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0]["path"], "/api/docker.getContainersByAppNameMatch")
        self.assertEqual(
            requests[0]["query"],
            {
                "appName": "cm-testing-iul0ql",
                "appType": "docker-compose",
                "serverId": "server-1",
            },
        )
        self.assertEqual(requests[1]["path"], "/api/compose.readLogs")
        self.assertEqual(
            requests[1]["query"],
            {
                "composeId": "compose-123",
                "containerId": "web-container",
                "tail": 2,
                "since": "5m",
                "search": "odoo",
            },
        )
        self.assertEqual(lines, ("two", "THREE_TOKEN=[redacted]"))

    def test_fetch_compose_logs_selects_exact_service(self) -> None:
        requests: list[dict[str, object]] = []

        def capture_request(**kwargs: object) -> object:
            requests.append(kwargs)
            if kwargs["path"] == "/api/docker.getContainersByAppNameMatch":
                return [
                    {"containerId": "database", "serviceName": "database"},
                    {"containerId": "web", "serviceName": "web"},
                ]
            return {"logs": "database ready"}

        with patch(
            "control_plane.dokploy.api.dokploy_request",
            side_effect=capture_request,
        ):
            lines = control_plane_dokploy.fetch_dokploy_compose_logs(
                host="https://dokploy.example.com",
                token="secret-token",
                compose_id="compose-123",
                app_name="tenant",
                service_name="database",
                line_count=10,
            )

        self.assertEqual(lines, ("database ready",))
        query = cast(dict[str, object], requests[1]["query"])
        self.assertEqual(query["containerId"], "database")

    def test_fetch_compose_logs_selects_exact_service_from_container_name(self) -> None:
        requests: list[dict[str, object]] = []

        def capture_request(**kwargs: object) -> object:
            requests.append(kwargs)
            if kwargs["path"] == "/api/docker.getContainersByAppNameMatch":
                return [
                    {"containerId": "database", "Name": "/tenant_database_1"},
                    {"containerId": "web", "Name": "/tenant_web_1"},
                ]
            return {"logs": "database ready"}

        with patch(
            "control_plane.dokploy.api.dokploy_request",
            side_effect=capture_request,
        ):
            lines = control_plane_dokploy.fetch_dokploy_compose_logs(
                host="https://dokploy.example.com",
                token="secret-token",
                compose_id="compose-123",
                app_name="tenant",
                service_name="database",
                line_count=10,
            )

        self.assertEqual(lines, ("database ready",))
        query = cast(dict[str, object], requests[1]["query"])
        self.assertEqual(query["containerId"], "database")

    def test_fetch_compose_logs_rejects_missing_exact_service(self) -> None:
        with (
            patch(
                "control_plane.dokploy.api.dokploy_request",
                return_value=[{"containerId": "web", "serviceName": "web"}],
            ),
            self.assertRaises(click.ClickException) as error_context,
        ):
            control_plane_dokploy.fetch_dokploy_compose_logs(
                host="https://dokploy.example.com",
                token="secret-token",
                compose_id="compose-123",
                app_name="tenant",
                service_name="database",
            )

        self.assertIn("not found", str(error_context.exception))

    def test_fetch_compose_logs_does_not_match_longer_service_name_suffix(self) -> None:
        with (
            patch(
                "control_plane.dokploy.api.dokploy_request",
                return_value=[
                    {
                        "containerId": "primary-database",
                        "name": "tenant_primary-database_1",
                    }
                ],
            ),
            self.assertRaises(click.ClickException) as error_context,
        ):
            control_plane_dokploy.fetch_dokploy_compose_logs(
                host="https://dokploy.example.com",
                token="secret-token",
                compose_id="compose-123",
                app_name="tenant",
                service_name="database",
            )

        self.assertIn("not found", str(error_context.exception))

    def test_fetch_compose_logs_rejects_ambiguous_exact_service(self) -> None:
        with (
            patch(
                "control_plane.dokploy.api.dokploy_request",
                return_value=[
                    {"containerId": "database-1", "serviceName": "database"},
                    {"containerId": "database-2", "serviceName": "database"},
                ],
            ),
            self.assertRaises(click.ClickException) as error_context,
        ):
            control_plane_dokploy.fetch_dokploy_compose_logs(
                host="https://dokploy.example.com",
                token="secret-token",
                compose_id="compose-123",
                app_name="tenant",
                service_name="database",
            )

        self.assertIn("ambiguous", str(error_context.exception))

    def test_fetch_deployment_logs_calls_dokploy_read_logs_endpoint(self) -> None:
        requests: list[dict[str, object]] = []

        def capture_request(**kwargs: object) -> dict[str, str]:
            requests.append(kwargs)
            return {"logs": "one\ntwo\nTHREE_TOKEN=secret"}

        with patch(
            "control_plane.dokploy.api.dokploy_request",
            side_effect=capture_request,
        ):
            lines = control_plane_dokploy.fetch_dokploy_deployment_logs(
                host="https://dokploy.example.com",
                token="secret-token",
                deployment_id="deploy-123",
                line_count=2,
            )

        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["path"], "/api/deployment.readLogs")
        self.assertEqual(requests[0]["query"], {"deploymentId": "deploy-123", "tail": 2})
        self.assertEqual(lines, ("two", "THREE_TOKEN=[redacted]"))

    def test_fetch_deployment_logs_requires_deployment_id(self) -> None:
        with self.assertRaises(click.ClickException) as error_context:
            control_plane_dokploy.fetch_dokploy_deployment_logs(
                host="https://dokploy.example.com",
                token="secret-token",
                deployment_id=" ",
            )

        self.assertIn("deployment id", str(error_context.exception))

    def test_deployment_key_from_wait_result_reads_deployment_token(self) -> None:
        self.assertEqual(
            control_plane_dokploy.deployment_key_from_wait_result(
                "deployment=schedule-after status=done"
            ),
            "schedule-after",
        )

    def test_fetch_compose_logs_prefers_web_container_name_variants(self) -> None:
        requests: list[dict[str, object]] = []

        def capture_request(**kwargs: object) -> object:
            requests.append(kwargs)
            if kwargs["path"] == "/api/docker.getContainersByAppNameMatch":
                return [
                    {"containerId": "db", "name": "/tenant_database_1"},
                    {"containerId": "script", "name": "tenant.script-runner.1"},
                    {"containerId": "web", "name": "tenant_web_1"},
                ]
            return {"logs": "web log"}

        with patch(
            "control_plane.dokploy.api.dokploy_request",
            side_effect=capture_request,
        ):
            lines = control_plane_dokploy.fetch_dokploy_compose_logs(
                host="https://dokploy.example.com",
                token="secret-token",
                compose_id="compose-123",
                app_name="tenant",
                line_count=10,
            )

        self.assertEqual(lines, ("web log",))
        self.assertEqual(requests[1]["path"], "/api/compose.readLogs")
        self.assertEqual(
            requests[1]["query"],
            {"composeId": "compose-123", "containerId": "web", "tail": 10, "since": "all"},
        )

    def test_fetch_compose_logs_ignores_web_named_sidecar_containers(self) -> None:
        requests: list[dict[str, object]] = []

        def capture_request(**kwargs: object) -> object:
            requests.append(kwargs)
            if kwargs["path"] == "/api/docker.getContainersByAppNameMatch":
                return [
                    {"containerId": "worker", "name": "tenant_web_worker_1"},
                    {"containerId": "web", "name": "tenant_web_1"},
                ]
            return {"logs": "web log"}

        with patch(
            "control_plane.dokploy.api.dokploy_request",
            side_effect=capture_request,
        ):
            lines = control_plane_dokploy.fetch_dokploy_compose_logs(
                host="https://dokploy.example.com",
                token="secret-token",
                compose_id="compose-123",
                app_name="tenant",
                line_count=10,
            )

        self.assertEqual(lines, ("web log",))
        self.assertEqual(
            requests[1]["query"],
            {"composeId": "compose-123", "containerId": "web", "tail": 10, "since": "all"},
        )

    def test_fetch_compose_logs_prefers_web_container_service_name(self) -> None:
        requests: list[dict[str, object]] = []

        def capture_request(**kwargs: object) -> object:
            requests.append(kwargs)
            if kwargs["path"] == "/api/docker.getContainersByAppNameMatch":
                return [
                    {"containerId": "db", "serviceName": "database"},
                    {"containerId": "web", "serviceName": "web"},
                ]
            return {"logs": "web log"}

        with patch(
            "control_plane.dokploy.api.dokploy_request",
            side_effect=capture_request,
        ):
            lines = control_plane_dokploy.fetch_dokploy_compose_logs(
                host="https://dokploy.example.com",
                token="secret-token",
                compose_id="compose-123",
                app_name="tenant",
                line_count=10,
            )

        self.assertEqual(lines, ("web log",))
        self.assertEqual(
            requests[1]["query"],
            {"composeId": "compose-123", "containerId": "web", "tail": 10, "since": "all"},
        )

    def test_fetch_compose_logs_prefers_web_container_label(self) -> None:
        requests: list[dict[str, object]] = []

        def capture_request(**kwargs: object) -> object:
            requests.append(kwargs)
            if kwargs["path"] == "/api/docker.getContainersByAppNameMatch":
                return [
                    {"containerId": "db", "name": "tenant-db-1"},
                    {
                        "containerId": "web",
                        "name": "tenant-runtime-1",
                        "labels": {"com.docker.compose.service": "web"},
                    },
                ]
            return {"logs": "web log"}

        with patch(
            "control_plane.dokploy.api.dokploy_request",
            side_effect=capture_request,
        ):
            lines = control_plane_dokploy.fetch_dokploy_compose_logs(
                host="https://dokploy.example.com",
                token="secret-token",
                compose_id="compose-123",
                app_name="tenant",
                line_count=10,
            )

        self.assertEqual(lines, ("web log",))
        query = cast(dict[str, object], requests[1]["query"])
        self.assertEqual(query["containerId"], "web")

    def test_fetch_compose_logs_prefers_web_container_pascal_case_label(self) -> None:
        requests: list[dict[str, object]] = []

        def capture_request(**kwargs: object) -> object:
            requests.append(kwargs)
            if kwargs["path"] == "/api/docker.getContainersByAppNameMatch":
                return [
                    {"containerId": "db", "Name": "/tenant-database-1"},
                    {
                        "containerId": "web",
                        "Name": "/tenant-runtime-1",
                        "Labels": {"com.docker.compose.service": "web"},
                    },
                ]
            return {"logs": "web log"}

        with patch(
            "control_plane.dokploy.api.dokploy_request",
            side_effect=capture_request,
        ):
            lines = control_plane_dokploy.fetch_dokploy_compose_logs(
                host="https://dokploy.example.com",
                token="secret-token",
                compose_id="compose-123",
                app_name="tenant",
                line_count=10,
            )

        self.assertEqual(lines, ("web log",))
        query = cast(dict[str, object], requests[1]["query"])
        self.assertEqual(query["containerId"], "web")

    def test_fetch_compose_logs_prefers_service_match_over_name_fallback(self) -> None:
        requests: list[dict[str, object]] = []

        def capture_request(**kwargs: object) -> object:
            requests.append(kwargs)
            if kwargs["path"] == "/api/docker.getContainersByAppNameMatch":
                return [
                    {"containerId": "sidecar", "Name": "/tenant-cron-web-1"},
                    {"containerId": "web", "serviceName": "web"},
                ]
            return {"logs": "web log"}

        with patch(
            "control_plane.dokploy.api.dokploy_request",
            side_effect=capture_request,
        ):
            lines = control_plane_dokploy.fetch_dokploy_compose_logs(
                host="https://dokploy.example.com",
                token="secret-token",
                compose_id="compose-123",
                app_name="tenant",
                line_count=10,
            )

        self.assertEqual(lines, ("web log",))
        query = cast(dict[str, object], requests[1]["query"])
        self.assertEqual(query["containerId"], "web")

    def test_fetch_compose_logs_prefers_web_container_docker_name_variants(self) -> None:
        requests: list[dict[str, object]] = []

        def capture_request(**kwargs: object) -> object:
            requests.append(kwargs)
            if kwargs["path"] == "/api/docker.getContainersByAppNameMatch":
                return [
                    {"containerId": "db", "Name": "/tenant-database-1"},
                    {"containerId": "script", "containerName": "tenant_script-runner_1"},
                    {"containerId": "web", "Names": ["/tenant-web-1"]},
                ]
            return {"logs": "web log"}

        with patch(
            "control_plane.dokploy.api.dokploy_request",
            side_effect=capture_request,
        ):
            lines = control_plane_dokploy.fetch_dokploy_compose_logs(
                host="https://dokploy.example.com",
                token="secret-token",
                compose_id="compose-123",
                app_name="tenant",
                line_count=10,
            )

        self.assertEqual(lines, ("web log",))
        query = cast(dict[str, object], requests[1]["query"])
        self.assertEqual(query["containerId"], "web")

    def test_fetch_compose_logs_prefers_web_container_service_variants(self) -> None:
        requests: list[dict[str, object]] = []

        def capture_request(**kwargs: object) -> object:
            requests.append(kwargs)
            if kwargs["path"] == "/api/docker.getContainersByAppNameMatch":
                return [
                    {"containerId": "db", "Service": "database"},
                    {
                        "containerId": "web",
                        "composeServiceName": "web",
                        "Name": "/tenant-runtime-1",
                    },
                ]
            return {"logs": "web log"}

        with patch(
            "control_plane.dokploy.api.dokploy_request",
            side_effect=capture_request,
        ):
            lines = control_plane_dokploy.fetch_dokploy_compose_logs(
                host="https://dokploy.example.com",
                token="secret-token",
                compose_id="compose-123",
                app_name="tenant",
                line_count=10,
            )

        self.assertEqual(lines, ("web log",))
        query = cast(dict[str, object], requests[1]["query"])
        self.assertEqual(query["containerId"], "web")

    def test_environments_logs_resolves_tracked_application_and_redacts_output(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            _seed_dokploy_target_records(
                store=store,
                payload="""
schema_version = 2

[[targets]]
context = "sellyouroutboard-testing"
instance = "testing"
target_id = "app-123"
target_type = "application"
target_name = "syo-testing-app"
""",
            )
            store.close()

            with (
                patch(
                    "control_plane.tracked_target_logs.dokploy_source.read_dokploy_config",
                    return_value=("https://dokploy.example.com", "secret-token"),
                ),
                patch(
                    "control_plane.tracked_target_logs.dokploy_api.fetch_dokploy_target_payload",
                    return_value={"appName": "syo-testing-gfbiqh", "serverId": "server-1"},
                ),
                patch(
                    "control_plane.tracked_target_logs.dokploy_api.fetch_dokploy_application_logs",
                    return_value=("started", "SMTP_PASSWORD=[redacted]"),
                ),
            ):
                result = runner.invoke(
                    main,
                    [
                        "environments",
                        "logs",
                        "--database-url",
                        database_url,
                        "--context",
                        " SellyourOutboard-Testing ",
                        "--instance",
                        " Testing ",
                        "--lines",
                        "2",
                    ],
                )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["context"], "sellyouroutboard-testing")
        self.assertEqual(payload["instance"], "testing")
        self.assertEqual(payload["target"]["target_id"], "app-123")
        self.assertEqual(payload["target"]["target_name"], "syo-testing-app")
        self.assertEqual(payload["target"]["app_name"], "syo-testing-gfbiqh")
        self.assertEqual(payload["logs"]["lines"], ["started", "SMTP_PASSWORD=[redacted]"])
        self.assertNotIn("secret-token", result.output)

    def test_environments_logs_reports_missing_records_without_traceback(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            store.close()

            result = runner.invoke(
                main,
                [
                    "environments",
                    "logs",
                    "--database-url",
                    database_url,
                    "--context",
                    "missing",
                    "--instance",
                    "testing",
                ],
            )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Missing DB-backed tracked Dokploy target records", result.output)
        self.assertNotIn("Traceback", result.output)

    def test_environments_logs_resolves_tracked_compose_and_redacts_output(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            _seed_dokploy_target_records(
                store=store,
                payload="""
schema_version = 2

[[targets]]
context = "opw"
instance = "testing"
target_id = "compose-123"
target_type = "compose"
target_name = "opw-testing"
""",
            )
            store.close()

            with (
                patch(
                    "control_plane.tracked_target_logs.dokploy_source.read_dokploy_config",
                    return_value=("https://dokploy.example.com", "secret-token"),
                ),
                patch(
                    "control_plane.tracked_target_logs.dokploy_api.fetch_dokploy_target_payload",
                    return_value={"appName": "opw-testing-iul0ql", "serverId": "server-1"},
                ),
                patch(
                    "control_plane.tracked_target_logs.dokploy_api.fetch_dokploy_compose_logs",
                    return_value=("started", "ODOO_DB_PASSWORD=[redacted]"),
                ) as logs_mock,
            ):
                result = runner.invoke(
                    main,
                    [
                        "environments",
                        "logs",
                        "--database-url",
                        database_url,
                        "--context",
                        "opw",
                        "--instance",
                        "testing",
                        "--lines",
                        "2",
                        "--since",
                        "5m",
                        "--service",
                        "database",
                    ],
                )

        self.assertEqual(result.exit_code, 0, result.output)
        logs_mock.assert_called_once_with(
            host="https://dokploy.example.com",
            token="secret-token",
            compose_id="compose-123",
            app_name="opw-testing-iul0ql",
            server_id="server-1",
            service_name="database",
            line_count=2,
            since="5m",
            search="",
        )
        payload = json.loads(result.output)
        self.assertEqual(payload["context"], "opw")
        self.assertEqual(payload["target"]["target_id"], "compose-123")
        self.assertEqual(payload["target"]["target_type"], "compose")
        self.assertEqual(payload["target"]["app_name"], "opw-testing-iul0ql")
        self.assertEqual(payload["request"]["service"], "database")
        self.assertEqual(payload["logs"]["lines"], ["started", "ODOO_DB_PASSWORD=[redacted]"])
        self.assertNotIn("secret-token", result.output)

    def test_update_application_env_includes_empty_build_fields_when_missing(self) -> None:
        requests: list[dict[str, object]] = []

        def capture_request(**kwargs: object) -> dict[str, bool]:
            requests.append(kwargs)
            return {"ok": True}

        with patch(
            "control_plane.dokploy.api.dokploy_request",
            side_effect=capture_request,
        ):
            control_plane_dokploy.update_dokploy_target_env(
                host="https://dokploy.example.com",
                token="secret-token",
                target_type="application",
                target_id="app-123",
                target_payload={"createEnvFile": True, "buildArgs": None, "buildSecrets": None},
                env_text="APP_URL=https://example.com",
            )

        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["path"], "/api/application.saveEnvironment")
        payload = cast("dict[str, object]", requests[0]["payload"])
        self.assertIsInstance(payload, dict)
        self.assertEqual(payload["buildArgs"], "")
        self.assertEqual(payload["buildSecrets"], "")

    def test_dokploy_targets_list_and_show_include_shopify_policy_metadata(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            _seed_dokploy_target_records(
                store=store,
                payload="""
schema_version = 2

[[targets]]
context = "opw"
instance = "testing"
target_id = "compose-123"
target_type = "compose"
project_name = "opw-testing"
target_name = "opw-testing"

[targets.policies.shopify]
protected_store_keys = ["yps-your-part-supplier"]
""",
            )

            list_result = runner.invoke(
                main, ["dokploy-targets", "list", "--database-url", database_url]
            )
            show_result = runner.invoke(
                main,
                [
                    "dokploy-targets",
                    "show",
                    "--database-url",
                    database_url,
                    "--context",
                    "OPW",
                    "--instance",
                    "Testing",
                ],
            )
            store.close()

        self.assertEqual(list_result.exit_code, 0, msg=list_result.output)
        self.assertEqual(show_result.exit_code, 0, msg=show_result.output)
        list_payload = json.loads(list_result.output)
        show_payload = json.loads(show_result.output)
        self.assertEqual(list_payload["count"], 1)
        self.assertEqual(list_payload["records"][0]["target_id"], "compose-123")
        self.assertEqual(
            list_payload["records"][0]["shopify_protected_store_keys"], ["yps-your-part-supplier"]
        )
        self.assertEqual(show_payload["target_id"], "compose-123")
        self.assertEqual(show_payload["shopify_protected_store_keys"], ["yps-your-part-supplier"])

    def test_dokploy_targets_put_shopify_protected_store_key_updates_record(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            _seed_dokploy_target_records(
                store=store,
                payload="""
schema_version = 2

[[targets]]
context = "opw"
instance = "testing"
target_id = "compose-123"
target_type = "compose"
""",
            )

            result = runner.invoke(
                main,
                [
                    "dokploy-targets",
                    "put-shopify-protected-store-key",
                    "--database-url",
                    database_url,
                    "--context",
                    "OPW",
                    "--instance",
                    "Testing",
                    "--key",
                    " YPS-Your-Part-Supplier ",
                    "--key",
                    "yps-your-part-supplier",
                    "--source-label",
                    "policy:test",
                    *_allow_direct_db_mutation_argument(),
                ],
            )
            stored_record = store.read_dokploy_target_record(
                context_name="opw", instance_name="testing"
            )
            provider_target = store.read_provider_target_record(
                context_name="opw", instance_name="testing"
            )
            store.close()

        self.assertEqual(result.exit_code, 0, msg=result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["added_keys"], ["yps-your-part-supplier"])
        self.assertEqual(payload["already_present_keys"], ["yps-your-part-supplier"])
        self.assertEqual(
            payload["record"]["shopify_protected_store_keys"], ["yps-your-part-supplier"]
        )
        self.assertEqual(
            stored_record.policies.shopify.protected_store_keys, ("yps-your-part-supplier",)
        )
        self.assertEqual(stored_record.source_label, "policy:test")
        self.assertEqual(provider_target.target_id, "compose-123")
        self.assertEqual(provider_target.provider_target_type, "compose")
        self.assertEqual(provider_target.source_label, "policy:test")

    def test_dokploy_targets_put_shopify_protected_store_key_requires_direct_db_acknowledgement(
        self,
    ) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )

            result = runner.invoke(
                main,
                [
                    "dokploy-targets",
                    "put-shopify-protected-store-key",
                    "--database-url",
                    database_url,
                    "--context",
                    "opw",
                    "--instance",
                    "testing",
                    "--key",
                    "yps-your-part-supplier",
                ],
            )

        _assert_direct_db_mutation_rejected(self, result)

    def test_dokploy_targets_unset_shopify_protected_store_key_reports_missing_keys(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            _seed_dokploy_target_records(
                store=store,
                payload="""
schema_version = 2

[[targets]]
context = "opw"
instance = "testing"
target_id = "compose-123"
target_type = "compose"

[targets.policies.shopify]
protected_store_keys = ["yps-your-part-supplier", "spare-store"]
""",
            )

            result = runner.invoke(
                main,
                [
                    "dokploy-targets",
                    "unset-shopify-protected-store-key",
                    "--database-url",
                    database_url,
                    "--context",
                    "opw",
                    "--instance",
                    "testing",
                    "--key",
                    "yps-your-part-supplier",
                    "--key",
                    "missing-store",
                    *_allow_direct_db_mutation_argument(),
                ],
            )
            stored_record = store.read_dokploy_target_record(
                context_name="opw", instance_name="testing"
            )
            provider_target = store.read_provider_target_record(
                context_name="opw", instance_name="testing"
            )
            store.close()

        self.assertEqual(result.exit_code, 0, msg=result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["removed_keys"], ["yps-your-part-supplier"])
        self.assertEqual(payload["missing_keys"], ["missing-store"])
        self.assertEqual(payload["record"]["shopify_protected_store_keys"], ["spare-store"])
        self.assertEqual(stored_record.policies.shopify.protected_store_keys, ("spare-store",))
        self.assertEqual(provider_target.target_id, "compose-123")

    def test_dokploy_targets_unset_shopify_protected_store_key_requires_direct_db_acknowledgement(
        self,
    ) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )

            result = runner.invoke(
                main,
                [
                    "dokploy-targets",
                    "unset-shopify-protected-store-key",
                    "--database-url",
                    database_url,
                    "--context",
                    "opw",
                    "--instance",
                    "testing",
                    "--key",
                    "yps-your-part-supplier",
                ],
            )

        _assert_direct_db_mutation_rejected(self, result)

    def test_dokploy_targets_put_shopify_protected_store_key_requires_existing_record(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            store.close()

            result = runner.invoke(
                main,
                [
                    "dokploy-targets",
                    "put-shopify-protected-store-key",
                    "--database-url",
                    database_url,
                    "--context",
                    "opw",
                    "--instance",
                    "testing",
                    "--key",
                    "yps-your-part-supplier",
                    *_allow_direct_db_mutation_argument(),
                ],
            )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Missing DB-backed tracked Dokploy target record", result.output)

    def test_dokploy_targets_relabel_updates_source_metadata(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            _seed_dokploy_target_records(
                store=store,
                payload="""
schema_version = 2

[[targets]]
context = "opw"
instance = "testing"
target_id = "compose-123"
target_type = "compose"
""",
            )

            result = runner.invoke(
                main,
                [
                    "dokploy-targets",
                    "relabel",
                    "--database-url",
                    database_url,
                    "--context",
                    "OPW",
                    "--instance",
                    "Testing",
                    "--source-label",
                    "repair:operator",
                    *_allow_direct_db_mutation_argument(),
                ],
            )
            stored_record = store.read_dokploy_target_record(
                context_name="opw", instance_name="testing"
            )
            provider_target = store.read_provider_target_record(
                context_name="opw", instance_name="testing"
            )
            store.close()

        self.assertEqual(result.exit_code, 0, msg=result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["record"]["source_label"], "repair:operator")
        self.assertEqual(stored_record.source_label, "repair:operator")
        self.assertEqual(provider_target.source_label, "repair:operator")

    def test_dokploy_targets_relabel_requires_direct_db_acknowledgement(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )

            result = runner.invoke(
                main,
                [
                    "dokploy-targets",
                    "relabel",
                    "--database-url",
                    database_url,
                    "--context",
                    "opw",
                    "--instance",
                    "testing",
                    "--source-label",
                    "repair:operator",
                ],
            )

        _assert_direct_db_mutation_rejected(self, result)

    def test_service_inspect_config_boundary_reports_db_only_authority(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(control_plane_root / "launchplane.sqlite3")
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            with patch.dict(
                os.environ,
                {
                    "LAUNCHPLANE_DATABASE_URL": database_url,
                    control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key",
                },
                clear=True,
            ):
                _write_dokploy_managed_secrets(
                    store=store,
                    host="https://dokploy.db.example",
                    token="db-token",
                )
                store.write_runtime_environment_record(
                    RuntimeEnvironmentRecord(
                        scope="global",
                        context="",
                        instance="",
                        env={"LAUNCHPLANE_PREVIEW_BASE_URL": "https://launchplane.example.com"},
                        updated_at="2026-04-22T00:00:00Z",
                        source_label="test",
                    )
                )
                store.write_dokploy_target_id_record(
                    DokployTargetIdRecord(
                        context="cm",
                        instance="prod",
                        target_id="compose-123",
                        updated_at="2026-04-22T00:00:00Z",
                        source_label="test",
                    )
                )
                result = runner.invoke(
                    main,
                    [
                        "service",
                        "inspect-config-boundary",
                        "--control-plane-root",
                        str(control_plane_root),
                    ],
                )

            store.close()

        self.assertEqual(result.exit_code, 0, msg=result.output)
        payload = json.loads(result.output)
        self.assertTrue(payload["db"]["inspectable"])
        self.assertEqual(payload["authority"]["dokploy_credentials"], "db_only")
        self.assertEqual(payload["authority"]["runtime_environments"], "db_only")
        self.assertEqual(payload["authority"]["dokploy_target_ids"], "db_only")
        self.assertEqual(payload["authority"]["stable_targets"], "missing")
        self.assertEqual(payload["authority"]["release_tuples_catalog"], "missing")
        self.assertEqual(payload["transition_inputs"]["selector_env_keys_present"], [])
        self.assertEqual(payload["transition_inputs"]["payload_env_keys_present"], [])

    def test_service_inspect_config_boundary_reports_mixed_authority(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(control_plane_root / "launchplane.sqlite3")
            config_directory = control_plane_root / "config"
            config_directory.mkdir(parents=True, exist_ok=True)
            (control_plane_root / ".env").write_text(
                "DOKPLOY_HOST=https://dokploy.file.example\nDOKPLOY_TOKEN=file-token\n",
                encoding="utf-8",
            )
            (config_directory / "runtime-environments.toml").write_text(
                (
                    "schema_version = 1\n\n"
                    "[shared_env]\n"
                    'LAUNCHPLANE_PREVIEW_BASE_URL = "https://launchplane.file.example.com"\n'
                ),
                encoding="utf-8",
            )
            (config_directory / "dokploy.toml").write_text(
                (
                    "schema_version = 2\n\n"
                    "[[targets]]\n"
                    'context = "cm"\n'
                    'instance = "prod"\n'
                    'target_id = "compose-file"\n'
                ),
                encoding="utf-8",
            )
            (config_directory / "dokploy-targets.toml").write_text(
                (
                    "schema_version = 1\n\n"
                    "[[targets]]\n"
                    'context = "cm"\n'
                    'instance = "prod"\n'
                    'target_id = "compose-override"\n'
                ),
                encoding="utf-8",
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            with patch.dict(
                os.environ,
                {
                    "LAUNCHPLANE_DATABASE_URL": database_url,
                    control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key",
                },
                clear=True,
            ):
                _write_dokploy_managed_secrets(
                    store=store,
                    host="https://dokploy.db.example",
                    token="db-token",
                )
                store.write_runtime_environment_record(
                    RuntimeEnvironmentRecord(
                        scope="global",
                        context="",
                        instance="",
                        env={"LAUNCHPLANE_PREVIEW_BASE_URL": "https://launchplane.db.example.com"},
                        updated_at="2026-04-22T00:00:00Z",
                        source_label="test",
                    )
                )
                store.write_dokploy_target_id_record(
                    DokployTargetIdRecord(
                        context="cm",
                        instance="prod",
                        target_id="compose-123",
                        updated_at="2026-04-22T00:00:00Z",
                        source_label="test",
                    )
                )
                store.write_dokploy_target_record(
                    control_plane_dokploy.build_dokploy_target_record_from_definition(
                        control_plane_dokploy.DokployTargetDefinition(
                            context="cm",
                            instance="prod",
                            target_id="compose-123",
                            target_type="compose",
                        ),
                        updated_at="2026-04-22T00:00:00Z",
                        source_label="test",
                    )
                )
                store.write_release_tuple_record(
                    ReleaseTupleRecord(
                        tuple_id="cm-testing-2026-04-22",
                        context="cm",
                        channel="testing",
                        artifact_id="artifact-testing",
                        repo_shas={"tenant-cm": "3333333333333333333333333333333333333333"},
                        provenance="ship",
                        minted_at="2026-04-22T00:00:00Z",
                    )
                )

                result = runner.invoke(
                    main,
                    [
                        "service",
                        "inspect-config-boundary",
                        "--control-plane-root",
                        str(control_plane_root),
                    ],
                )

            store.close()

        self.assertEqual(result.exit_code, 0, msg=result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["authority"]["dokploy_credentials"], "db_only")
        self.assertEqual(payload["authority"]["runtime_environments"], "db_only")
        self.assertEqual(payload["authority"]["dokploy_target_ids"], "db_only")
        self.assertEqual(payload["authority"]["stable_targets"], "db_only")
        self.assertEqual(payload["authority"]["release_tuples_catalog"], "db_only")
        self.assertTrue(payload["legacy_paths"]["repo_env_file"]["exists"])
        self.assertTrue(payload["legacy_paths"]["repo_runtime_environments_file"]["exists"])
        self.assertTrue(payload["legacy_paths"]["repo_dokploy_source_file"]["exists"])
        self.assertTrue(payload["legacy_paths"]["repo_dokploy_target_ids_file"]["exists"])

    def test_environments_show_live_target_reports_legacy_runtime_contract_blockers(self) -> None:
        runner = CliRunner()
        source_of_truth = control_plane_dokploy.DokploySourceOfTruth.model_validate(
            {
                "schema_version": 2,
                "targets": [
                    {
                        "context": "opw",
                        "instance": "testing",
                        "target_id": "compose-123",
                        "target_type": "compose",
                        "target_name": "opw-testing",
                    }
                ],
            }
        )

        with (
            patch(
                "control_plane.dokploy.source.read_control_plane_dokploy_source_of_truth",
                return_value=source_of_truth,
            ),
            patch(
                "control_plane.dokploy.source.read_dokploy_config",
                return_value=("https://dokploy.example.com", "token-123"),
            ),
            patch(
                "control_plane.dokploy.api.fetch_dokploy_target_payload",
                return_value={
                    "name": "opw-testing",
                    "appName": "compose-opw-testing",
                    "sourceType": "git",
                    "customGitUrl": "git@github.com:cbusillo/odoo-ai.git",
                    "customGitBranch": "opw-testing",
                    "composePath": "./docker-compose.yml",
                    "env": (
                        "ODOO_BASE_RUNTIME_IMAGE=ghcr.io/cbusillo/odoo-enterprise-docker:19.0-runtime\n"
                        "ODOO_ADDON_REPOSITORIES=cbusillo/disable_odoo_online@main,OCA/OpenUpgrade@89e649728027a8ab656b3aa4be18f4bd364db417"
                    ),
                },
            ),
        ):
            result = runner.invoke(
                main,
                [
                    "environments",
                    "show-live-target",
                    "--context",
                    "opw",
                    "--instance",
                    "testing",
                ],
            )

        self.assertEqual(result.exit_code, 0, msg=result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["tracked_target"]["target_id"], "compose-123")
        self.assertEqual(
            payload["live_target"]["custom_git_url"], "git@github.com:cbusillo/odoo-ai.git"
        )
        self.assertFalse(payload["artifact_runtime_contract"]["artifact_ready"])
        self.assertIn(
            "git@github.com:cbusillo/odoo-ai.git",
            payload["artifact_runtime_contract"]["legacy_monorepo_sources"],
        )
        self.assertIn(
            "cbusillo/disable_odoo_online@main",
            payload["artifact_runtime_contract"]["mutable_addon_refs"],
        )

    def test_read_control_plane_dokploy_source_of_truth_prefers_postgres_target_ids_without_file_fallback(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(control_plane_root / "launchplane.sqlite3")
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            _seed_dokploy_target_records(
                store=store,
                payload="""
schema_version = 2

[[targets]]
context = "opw"
instance = "prod"
target_id = "compose-file"
target_type = "compose"

[[targets]]
context = "cm"
instance = "testing"
target_id = "compose-cm-file"
target_type = "compose"
""",
            )
            store.write_dokploy_target_id_record(
                DokployTargetIdRecord(
                    context="opw",
                    instance="prod",
                    target_id="compose-db",
                    updated_at="2026-04-21T19:00:00Z",
                    source_label="import:test",
                )
            )
            store.write_dokploy_target_id_record(
                DokployTargetIdRecord(
                    context="cm",
                    instance="testing",
                    target_id="compose-cm-db",
                    updated_at="2026-04-21T19:00:00Z",
                    source_label="import:test",
                )
            )

            with patch.dict(os.environ, {"LAUNCHPLANE_DATABASE_URL": database_url}, clear=True):
                source_of_truth = control_plane_dokploy.read_control_plane_dokploy_source_of_truth(
                    control_plane_root=control_plane_root
                )

            store.close()

        self.assertEqual(
            [
                (target.context, target.instance, target.target_id)
                for target in source_of_truth.targets
            ],
            [("cm", "testing", "compose-cm-db"), ("opw", "prod", "compose-db")],
        )

    def test_read_control_plane_dokploy_source_of_truth_requires_database_target_ids_without_file_fallback(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(control_plane_root / "launchplane.sqlite3")
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            store.write_dokploy_target_record(
                control_plane_dokploy.build_dokploy_target_record_from_definition(
                    control_plane_dokploy.DokployTargetDefinition(
                        context="opw",
                        instance="prod",
                        target_id="compose-placeholder",
                        target_type="compose",
                    ),
                    updated_at="2026-04-22T00:00:00Z",
                    source_label="test",
                )
            )

            with patch.dict(os.environ, {"LAUNCHPLANE_DATABASE_URL": database_url}, clear=True):
                with self.assertRaises(click.ClickException) as raised_error:
                    control_plane_dokploy.read_control_plane_dokploy_source_of_truth(
                        control_plane_root=control_plane_root
                    )

            store.close()

        self.assertIn(
            "Missing DB-backed Dokploy target-id record for opw/prod", str(raised_error.exception)
        )

    def test_read_control_plane_dokploy_source_of_truth_can_ignore_incomplete_unrelated_targets(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(control_plane_root / "launchplane.sqlite3")
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            store.write_dokploy_target_record(
                DokployTargetRecord(
                    context="sellyouroutboard",
                    instance="testing",
                    target_type="application",
                    target_name="syo-testing",
                    updated_at="2026-05-04T19:00:00Z",
                    source_label="test",
                )
            )
            store.write_dokploy_target_record(
                DokployTargetRecord(
                    context="discord-blue",
                    instance="prod",
                    target_type="application",
                    target_name="discord-blue",
                    updated_at="2026-05-04T19:00:00Z",
                    source_label="test",
                )
            )
            store.write_dokploy_target_id_record(
                DokployTargetIdRecord(
                    context="sellyouroutboard",
                    instance="testing",
                    target_id="app-syo-testing",
                    updated_at="2026-05-04T19:00:00Z",
                    source_label="test",
                )
            )

            with patch.dict(os.environ, {"LAUNCHPLANE_DATABASE_URL": database_url}, clear=True):
                source_of_truth = control_plane_dokploy.read_control_plane_dokploy_source_of_truth(
                    control_plane_root=control_plane_root,
                    allow_incomplete_target_ids=True,
                )

            store.close()

        self.assertEqual(
            [
                (target.context, target.instance, target.target_id)
                for target in source_of_truth.targets
            ],
            [("sellyouroutboard", "testing", "app-syo-testing")],
        )

    def test_read_control_plane_dokploy_source_of_truth_can_preserve_allowed_incomplete_target(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(control_plane_root / "launchplane.sqlite3")
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            store.write_dokploy_target_record(
                DokployTargetRecord(
                    context="sellyouroutboard-testing",
                    instance="testing",
                    target_type="application",
                    target_name="syo-testing",
                    updated_at="2026-05-04T19:00:00Z",
                    source_label="test",
                )
            )
            store.write_dokploy_target_record(
                DokployTargetRecord(
                    context="discord-blue",
                    instance="prod",
                    target_type="application",
                    target_name="discord-blue",
                    updated_at="2026-05-04T19:00:00Z",
                    source_label="test",
                )
            )

            with patch.dict(os.environ, {"LAUNCHPLANE_DATABASE_URL": database_url}, clear=True):
                source_of_truth = control_plane_dokploy.read_control_plane_dokploy_source_of_truth(
                    control_plane_root=control_plane_root,
                    allow_incomplete_target_ids=True,
                    allowed_incomplete_target_routes=(("sellyouroutboard-testing", "testing"),),
                )

            store.close()

        self.assertEqual(
            [
                (target.context, target.instance, target.target_id)
                for target in source_of_truth.targets
            ],
            [("sellyouroutboard-testing", "testing", "")],
        )

    def test_read_control_plane_dokploy_source_of_truth_reads_database_target_records(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(control_plane_root / "launchplane.sqlite3")
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            _seed_dokploy_target_records(
                store=store,
                payload="""
schema_version = 2

[[targets]]
context = "opw"
instance = "prod"
target_id = "compose-123"
target_type = "compose"
""",
            )

            with patch.dict(os.environ, {"LAUNCHPLANE_DATABASE_URL": database_url}, clear=True):
                source_of_truth = control_plane_dokploy.read_control_plane_dokploy_source_of_truth(
                    control_plane_root=control_plane_root
                )

            store.close()

        self.assertEqual(len(source_of_truth.targets), 1)
        self.assertEqual(source_of_truth.targets[0].target_id, "compose-123")

    def test_read_control_plane_dokploy_source_of_truth_preserves_target_policies(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(control_plane_root / "launchplane.sqlite3")
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            _seed_dokploy_target_records(
                store=store,
                payload="""
schema_version = 2

[[targets]]
context = "opw"
instance = "prod"
target_id = "compose-123"
target_type = "compose"

[targets.policies.shopify]
protected_store_keys = ["yps-your-part-supplier"]
""",
            )

            with patch.dict(os.environ, {"LAUNCHPLANE_DATABASE_URL": database_url}, clear=True):
                source_of_truth = control_plane_dokploy.read_control_plane_dokploy_source_of_truth(
                    control_plane_root=control_plane_root
                )

            store.close()

        self.assertEqual(
            source_of_truth.targets[0].policies.shopify.protected_store_keys,
            ("yps-your-part-supplier",),
        )

    def test_read_control_plane_dokploy_source_of_truth_fails_closed_when_target_id_missing(
        self,
    ) -> None:
        with self.assertRaises(click.ClickException) as raised_error:
            control_plane_dokploy.build_dokploy_source_of_truth_from_records(
                (
                    control_plane_dokploy.build_dokploy_target_record_from_definition(
                        control_plane_dokploy.DokployTargetDefinition(
                            context="opw",
                            instance="prod",
                            target_id="compose-placeholder",
                            target_type="compose",
                        ),
                        updated_at="2026-04-22T00:00:00Z",
                        source_label="test",
                    ),
                ),
                (),
            )

        self.assertIn(
            "Missing DB-backed Dokploy target-id record for opw/prod", str(raised_error.exception)
        )

        source_of_truth = control_plane_dokploy.build_dokploy_source_of_truth_from_records(
            (
                control_plane_dokploy.build_dokploy_target_record_from_definition(
                    control_plane_dokploy.DokployTargetDefinition(
                        context="opw",
                        instance="prod",
                        target_id="compose-placeholder",
                        target_type="compose",
                    ),
                    updated_at="2026-04-22T00:00:00Z",
                    source_label="test",
                ),
            ),
            (),
            allow_incomplete_target_ids=True,
        )
        self.assertEqual(source_of_truth.targets, ())

    def test_read_control_plane_dokploy_source_of_truth_rejects_duplicate_context_instance_targets(
        self,
    ) -> None:
        duplicate_records = (
            control_plane_dokploy.build_dokploy_target_record_from_definition(
                control_plane_dokploy.DokployTargetDefinition(
                    context="opw",
                    instance="prod",
                    target_id="compose-123",
                    target_type="compose",
                ),
                updated_at="2026-04-22T00:00:00Z",
                source_label="test",
            ),
            control_plane_dokploy.build_dokploy_target_record_from_definition(
                control_plane_dokploy.DokployTargetDefinition(
                    context="opw",
                    instance="prod",
                    target_id="compose-456",
                    target_type="compose",
                ),
                updated_at="2026-04-22T00:00:00Z",
                source_label="test",
            ),
        )
        target_id_records = (
            DokployTargetIdRecord(
                context="opw",
                instance="prod",
                target_id="compose-123",
                updated_at="2026-04-22T00:00:00Z",
                source_label="test",
            ),
        )

        with self.assertRaises(ValidationError) as raised_error:
            control_plane_dokploy.build_dokploy_source_of_truth_from_records(
                duplicate_records, target_id_records
            )

        self.assertIn(
            "Duplicate Dokploy target definition for opw/prod", str(raised_error.exception)
        )

    def test_read_control_plane_dokploy_source_of_truth_rejects_dev_lane_targets(self) -> None:
        with self.assertRaises(ValidationError) as raised_error:
            control_plane_dokploy.build_dokploy_source_of_truth_from_records(
                (
                    control_plane_dokploy.build_dokploy_target_record_from_definition(
                        control_plane_dokploy.DokployTargetDefinition(
                            context="opw",
                            instance="dev",
                            target_id="compose-123",
                            target_type="compose",
                        ),
                        updated_at="2026-04-22T00:00:00Z",
                        source_label="test",
                    ),
                ),
                (
                    DokployTargetIdRecord(
                        context="opw",
                        instance="dev",
                        target_id="compose-123",
                        updated_at="2026-04-22T00:00:00Z",
                        source_label="test",
                    ),
                ),
            )

        self.assertIn("stable remote instances prod, testing", str(raised_error.exception))
        self.assertIn("opw/dev", str(raised_error.exception))
        self.assertIn("Launchplane preview records", str(raised_error.exception))

    def test_read_dokploy_config_reads_managed_secrets(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(control_plane_root / "launchplane.sqlite3")
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()

            with patch.dict(
                os.environ,
                {
                    "LAUNCHPLANE_DATABASE_URL": database_url,
                    control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key",
                },
                clear=True,
            ):
                _write_dokploy_managed_secrets(
                    store=store,
                    host="https://dokploy.db.example",
                    token="db-token",
                )
                host, token = control_plane_dokploy.read_dokploy_config(
                    control_plane_root=control_plane_root
                )

            store.close()

        self.assertEqual(host, "https://dokploy.db.example")
        self.assertEqual(token, "db-token")

    def test_read_dokploy_config_uses_explicit_database_url(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            explicit_database_url = _sqlite_database_url(
                control_plane_root / "explicit-launchplane.sqlite3"
            )
            ignored_database_url = _sqlite_database_url(
                control_plane_root / "ignored-launchplane.sqlite3"
            )
            explicit_store = PostgresRecordStore(database_url=explicit_database_url)
            ignored_store = PostgresRecordStore(database_url=ignored_database_url)
            explicit_store.ensure_schema()
            ignored_store.ensure_schema()

            with patch.dict(
                os.environ,
                {
                    "LAUNCHPLANE_DATABASE_URL": ignored_database_url,
                    control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key",
                },
                clear=True,
            ):
                _write_dokploy_managed_secrets(
                    store=explicit_store,
                    host="https://dokploy.explicit.example",
                    token="explicit-token",
                )
                _write_dokploy_managed_secrets(
                    store=ignored_store,
                    host="https://dokploy.ignored.example",
                    token="ignored-token",
                )
                host, token = control_plane_dokploy.read_dokploy_config(
                    control_plane_root=control_plane_root,
                    database_url=explicit_database_url,
                )

            explicit_store.close()
            ignored_store.close()

        self.assertEqual(host, "https://dokploy.explicit.example")
        self.assertEqual(token, "explicit-token")

    def test_odoo_target_replacement_plan_cli_uses_database_bound_dokploy_secrets(
        self,
    ) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            explicit_database_url = _sqlite_database_url(
                control_plane_root / "explicit-launchplane.sqlite3"
            )
            ignored_database_url = _sqlite_database_url(
                control_plane_root / "ignored-launchplane.sqlite3"
            )
            explicit_store = PostgresRecordStore(database_url=explicit_database_url)
            ignored_store = PostgresRecordStore(database_url=ignored_database_url)
            explicit_store.ensure_schema()
            ignored_store.ensure_schema()
            _write_odoo_product_profile_record(store=explicit_store)
            _seed_dokploy_target_records(
                store=explicit_store,
                payload="""
schema_version = 2

[[targets]]
context = "cm"
instance = "testing"
target_id = "compose-cm-testing"
target_type = "compose"
target_name = "cm-testing"
domains = ["cm-testing.shinycomputers.com"]
""",
            )

            with patch.dict(
                os.environ,
                {
                    "LAUNCHPLANE_DATABASE_URL": ignored_database_url,
                    control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key",
                },
                clear=True,
            ):
                _write_dokploy_managed_secrets(
                    store=explicit_store,
                    host="https://dokploy.explicit.example",
                    token="explicit-token",
                )
                _write_dokploy_managed_secrets(
                    store=ignored_store,
                    host="https://dokploy.ignored.example",
                    token="ignored-token",
                )
                captured_requests: list[dict[str, object]] = []

                def fake_dokploy_request(**kwargs: object) -> JsonValue:
                    captured_requests.append(dict(kwargs))
                    if kwargs.get("path") == "/api/domain.byComposeId":
                        return [
                            {
                                "host": "cm-testing.shinycomputers.com",
                                "domainId": "domain-cm",
                            }
                        ]
                    return []

                with (
                    patch(
                        "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.dokploy_request",
                        side_effect=fake_dokploy_request,
                    ),
                    patch(
                        "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.fetch_dokploy_target_payload",
                        return_value={
                            "name": "cm-testing",
                            "sourceType": "raw",
                            "composePath": "docker-compose.yml",
                            "composeFile": "services: {}",
                            "env": "",
                        },
                    ),
                ):
                    result = runner.invoke(
                        main,
                        [
                            "odoo-targets",
                            "replacement-plan",
                            "--database-url",
                            explicit_database_url,
                            "--product",
                            "odoo-tenant-cm",
                            "--instance",
                            "testing",
                            "--control-plane-root",
                            str(control_plane_root),
                        ],
                        catch_exceptions=False,
                    )

            explicit_store.close()
            ignored_store.close()

        self.assertEqual(result.exit_code, 0)
        self.assertTrue(captured_requests)
        self.assertEqual(captured_requests[0]["host"], "https://dokploy.explicit.example")
        self.assertEqual(captured_requests[0]["token"], "explicit-token")
        self.assertNotIn("ignored-token", result.output)

    def test_read_dokploy_config_ignores_repo_env_file(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(control_plane_root / "launchplane.sqlite3")
            (control_plane_root / ".env").write_text(
                "DOKPLOY_HOST=https://dokploy.control-plane.example\nDOKPLOY_TOKEN=control-plane-token\n",
                encoding="utf-8",
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()

            with patch.dict(
                os.environ,
                {
                    "LAUNCHPLANE_DATABASE_URL": database_url,
                    control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key",
                },
                clear=True,
            ):
                _write_dokploy_managed_secrets(
                    store=store,
                    host="https://dokploy.db.example",
                    token="db-token",
                )
                host, token = control_plane_dokploy.read_dokploy_config(
                    control_plane_root=control_plane_root
                )

            store.close()

        self.assertEqual(host, "https://dokploy.db.example")
        self.assertEqual(token, "db-token")

    def test_read_dokploy_config_ignores_process_environment_bootstrap(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(control_plane_root / "launchplane.sqlite3")
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()

            with patch.dict(
                os.environ,
                {
                    "LAUNCHPLANE_DATABASE_URL": database_url,
                    control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key",
                    "DOKPLOY_HOST": "https://dokploy.process.example",
                    "DOKPLOY_TOKEN": "process-token",
                },
                clear=True,
            ):
                _write_dokploy_managed_secrets(
                    store=store,
                    host="https://dokploy.db.example",
                    token="db-token",
                )
                host, token = control_plane_dokploy.read_dokploy_config(
                    control_plane_root=control_plane_root
                )

            store.close()

        self.assertEqual(host, "https://dokploy.db.example")
        self.assertEqual(token, "db-token")

    def test_read_dokploy_config_fails_closed_with_only_repo_env_file(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            (control_plane_root / ".env").write_text(
                "DOKPLOY_HOST=https://dokploy.file.example\nDOKPLOY_TOKEN=file-token\n",
                encoding="utf-8",
            )

            with patch.dict(
                os.environ,
                {},
                clear=True,
            ):
                with self.assertRaises(click.ClickException) as raised_error:
                    control_plane_dokploy.read_dokploy_config(control_plane_root=control_plane_root)

        self.assertIn(
            "Configure Launchplane-managed Dokploy secrets in the shared store",
            str(raised_error.exception),
        )

    def test_read_dokploy_config_fails_closed_with_only_process_environment_bootstrap(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)

            with patch.dict(
                os.environ,
                {
                    "DOKPLOY_HOST": "https://dokploy.process.example",
                    "DOKPLOY_TOKEN": "process-token",
                },
                clear=True,
            ):
                with self.assertRaises(click.ClickException) as raised_error:
                    control_plane_dokploy.read_dokploy_config(control_plane_root=control_plane_root)

        self.assertIn(
            "Configure Launchplane-managed Dokploy secrets in the shared store",
            str(raised_error.exception),
        )

    def test_read_control_plane_environment_values_reads_managed_secrets_only(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(control_plane_root / "launchplane.sqlite3")
            (control_plane_root / ".env").write_text(
                "DOKPLOY_HOST=https://dokploy.file.example\nDOKPLOY_TOKEN=file-token\n",
                encoding="utf-8",
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()

            with patch.dict(
                os.environ,
                {
                    "LAUNCHPLANE_DATABASE_URL": database_url,
                    control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key",
                    "DOKPLOY_HOST": "https://dokploy.process.example",
                    "DOKPLOY_TOKEN": "process-token",
                },
                clear=True,
            ):
                _write_dokploy_managed_secrets(
                    store=store,
                    host="https://dokploy.db.example",
                    token="db-token",
                )
                environment_values = control_plane_dokploy.read_control_plane_environment_values(
                    control_plane_root=control_plane_root
                )

            store.close()

        self.assertEqual(environment_values["DOKPLOY_HOST"], "https://dokploy.db.example")
        self.assertEqual(environment_values["DOKPLOY_TOKEN"], "db-token")

    def test_read_dokploy_config_fails_closed_without_control_plane_secret_source(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)

            with patch.dict(
                os.environ,
                {},
                clear=True,
            ):
                with self.assertRaises(click.ClickException) as raised_error:
                    control_plane_dokploy.read_dokploy_config(control_plane_root=control_plane_root)

        self.assertIn("DOKPLOY_HOST or DOKPLOY_TOKEN", str(raised_error.exception))

    def test_require_odoo_module_update_readback_evidence_accepts_complete_proof(self) -> None:
        control_plane_dokploy.require_odoo_module_update_readback_evidence(
            {
                "log_available": "true",
                "odoo_module_update_completed": "true",
                "odoo_module_update_image_match": "true",
                "odoo_module_update_modules_configured": "true",
            }
        )

    def test_require_odoo_module_update_readback_evidence_rejects_missing_marker(self) -> None:
        with self.assertRaisesRegex(click.ClickException, "did not prove"):
            control_plane_dokploy.require_odoo_module_update_readback_evidence(
                {
                    "log_available": "true",
                    "odoo_module_update_image_match": "true",
                    "odoo_module_update_modules_configured": "true",
                }
            )

    def test_run_compose_post_deploy_update_applies_explicit_env_file_without_control_plane_secrets(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            env_file = Path(temporary_directory_name) / "post-deploy.env"
            env_file.write_text(
                "\n".join(
                    (
                        "ODOO_DB_NAME=opw_prod",
                        "ODOO_FILESTORE_PATH=/volumes/data/custom-filestore",
                        "DOKPLOY_TOKEN=should-not-sync",
                    )
                ),
                encoding="utf-8",
            )
            target_definition = control_plane_dokploy.DokployTargetDefinition(
                context="opw", instance="prod", target_id="compose-123", target_name="opw-prod"
            )
            updated_env_payloads: list[str] = []
            schedule_payloads: list[dict[str, object]] = []
            request_paths: list[str] = []
            effect_phases: list[str] = []
            override_payload_b64 = base64.b64encode(
                json.dumps(
                    {"schema_version": 1, "context": "opw", "instance": "prod"},
                    sort_keys=True,
                ).encode("utf-8")
            ).decode("ascii")

            def capture_schedule_payload(**kwargs: object) -> dict[str, str]:
                schedule_payloads.append(cast("dict[str, object]", kwargs["schedule_payload"]))
                return {"scheduleId": "schedule-123"}

            def capture_request_path(**kwargs: object) -> dict[str, bool]:
                request_paths.append(str(kwargs["path"]))
                return {"ok": True}

            def fetch_target_payload(**_: object) -> dict[str, str]:
                return {
                    "env": updated_env_payloads[-1]
                    if updated_env_payloads
                    else (
                        "ODOO_DB_NAME=old_db\n"
                        "ODOO_FILESTORE_PATH=/volumes/data/filestore\n"
                        "ODOO_ADDONS_PATH=/opt/project/addons,/opt/launchplane/addons,/odoo/addons\n"
                        "ODOO_INSTALL_MODULES=opw_custom\n"
                    ),
                    "appName": "opw-prod-app",
                    "serverId": "server-123",
                }

            with (
                patch(
                    "control_plane.dokploy.api.fetch_dokploy_target_payload",
                    side_effect=fetch_target_payload,
                ),
                patch(
                    "control_plane.dokploy.api.update_dokploy_target_env",
                    side_effect=lambda **kwargs: updated_env_payloads.append(
                        str(kwargs["env_text"])
                    ),
                ),
                patch(
                    "control_plane.dokploy.api.latest_deployment_for_target",
                    return_value={"deploymentId": "deployment-before"},
                ),
                patch(
                    "control_plane.dokploy.api.wait_for_target_deployment",
                    side_effect=lambda **_kwargs: None,
                ) as wait_target_deployment,
                patch(
                    "control_plane.dokploy.api.find_matching_dokploy_schedule",
                    return_value=None,
                ),
                patch(
                    "control_plane.dokploy.api.upsert_dokploy_schedule",
                    side_effect=capture_schedule_payload,
                ),
                patch(
                    "control_plane.dokploy.api.latest_deployment_for_schedule",
                    return_value={"deploymentId": "schedule-before"},
                ),
                patch(
                    "control_plane.dokploy.api.wait_for_dokploy_schedule_deployment",
                    return_value="deployment=schedule-after status=done",
                ),
                patch(
                    "control_plane.dokploy.api.fetch_dokploy_deployment_logs",
                    return_value=(),
                ),
                patch(
                    "control_plane.dokploy.api.dokploy_request",
                    side_effect=capture_request_path,
                ),
            ):
                control_plane_dokploy.run_compose_post_deploy_update(
                    host="https://dokploy.example.com",
                    token="secret-token",
                    target_definition=target_definition,
                    env_file=env_file,
                    workflow_environment_overrides={
                        ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY: override_payload_b64,
                        LAUNCHPLANE_INSTANCE_OVERRIDES_REQUIRED_ENV_KEY: "true",
                        LAUNCHPLANE_WEBSITE_BOOTSTRAP_REQUIRED_ENV_KEY: "true",
                        "ONE_OFF_WORKFLOW_ONLY": "do-not-persist",
                    },
                    before_provider_mutation=effect_phases.append,
                    deployment_title="Launchplane post-deploy exact",
                )

        self.assertEqual(len(updated_env_payloads), 1)
        self.assertIn("ODOO_DB_NAME=opw_prod", updated_env_payloads[0])
        self.assertIn("ODOO_FILESTORE_PATH=/volumes/data/custom-filestore", updated_env_payloads[0])
        self.assertIn(
            "ODOO_ADDONS_PATH=/opt/project/addons,/opt/launchplane/addons,/odoo/addons,/opt/enterprise",
            updated_env_payloads[0],
        )
        self.assertIn(
            f"{ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY}={override_payload_b64}",
            updated_env_payloads[0],
        )
        self.assertIn(
            f"{LAUNCHPLANE_INSTANCE_OVERRIDES_REQUIRED_ENV_KEY}=true", updated_env_payloads[0]
        )
        self.assertIn(
            f"{LAUNCHPLANE_WEBSITE_BOOTSTRAP_REQUIRED_ENV_KEY}=true", updated_env_payloads[0]
        )
        self.assertNotIn("ONE_OFF_WORKFLOW_ONLY=do-not-persist", updated_env_payloads[0])
        self.assertNotIn("DOKPLOY_TOKEN=should-not-sync", updated_env_payloads[0])
        self.assertEqual(len(schedule_payloads), 1)
        self.assertEqual(schedule_payloads[0]["command"], "control-plane post-deploy update")
        schedule_script = str(schedule_payloads[0]["script"])
        self.assertIn("--post-deploy-maintenance", schedule_script)
        self.assertNotIn("--update-only", schedule_script)
        self.assertIn("ONE_OFF_WORKFLOW_ONLY", schedule_script)
        self.assertIn(
            "workflow_environment+=(-e ODOO_FILESTORE_PATH=/volumes/data/custom-filestore)",
            schedule_script,
        )
        self.assertIn("workflow_environment+=(-e ODOO_UPDATE_MODULES=opw_custom)", schedule_script)
        self.assertIn("resolve_single_running_container", schedule_script)
        self.assertIn('echo "odoo_module_update_image_match=true"', schedule_script)
        self.assertIn('echo "odoo_module_update_modules_configured=true"', schedule_script)
        self.assertIn('echo "odoo_module_update_completed=true"', schedule_script)
        self.assertIn('workflow_pipeline_status=("${PIPESTATUS[@]}")', schedule_script)
        self.assertIn("workflow_exit_status=${workflow_pipeline_status[0]}", schedule_script)
        self.assertIn("workflow_output_status=${workflow_pipeline_status[1]}", schedule_script)
        self.assertIn("/api/compose.deploy", request_paths)
        self.assertIn("/api/schedule.runManually", request_paths)
        self.assertEqual(
            effect_phases,
            [
                "post_deploy_target_update",
                "post_deploy_deploy_trigger",
                "post_deploy_schedule_upsert",
                "post_deploy_schedule_trigger",
            ],
        )
        self.assertEqual(
            wait_target_deployment.call_args.kwargs["deployment_title"],
            "Launchplane post-deploy exact",
        )

    def test_run_compose_post_deploy_update_fails_when_runtime_override_env_does_not_persist(
        self,
    ) -> None:
        target_definition = control_plane_dokploy.DokployTargetDefinition(
            context="opw", instance="prod", target_id="compose-123", target_name="opw-prod"
        )
        override_payload_b64 = base64.b64encode(
            json.dumps(
                {"schema_version": 1, "context": "opw", "instance": "prod"},
                sort_keys=True,
            ).encode("utf-8")
        ).decode("ascii")

        with (
            patch(
                "control_plane.dokploy.api.fetch_dokploy_target_payload",
                return_value={
                    "env": "ODOO_DB_NAME=opw_prod\nODOO_FILESTORE_PATH=/volumes/data/filestore\n",
                    "appName": "opw-prod-app",
                    "serverId": "server-123",
                },
            ),
            patch("control_plane.dokploy.api.update_dokploy_target_env"),
            patch(
                "control_plane.dokploy.api.latest_deployment_for_target",
                return_value={"deploymentId": "deployment-before"},
            ),
            patch(
                "control_plane.dokploy.api.wait_for_target_deployment",
                side_effect=lambda **_kwargs: None,
            ),
            patch("control_plane.dokploy.api.trigger_deployment"),
            patch("control_plane.dokploy.api.upsert_dokploy_schedule") as upsert_schedule,
        ):
            with self.assertRaises(click.ClickException) as raised_error:
                control_plane_dokploy.run_compose_post_deploy_update(
                    host="https://dokploy.example.com",
                    token="secret-token",
                    target_definition=target_definition,
                    env_file=None,
                    workflow_environment_overrides={
                        ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY: override_payload_b64,
                        LAUNCHPLANE_INSTANCE_OVERRIDES_REQUIRED_ENV_KEY: "true",
                    },
                )

        self.assertIn(
            "Compose post-deploy update did not persist runtime override key(s)",
            str(raised_error.exception),
        )
        self.assertIn(ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY, str(raised_error.exception))
        self.assertNotIn(override_payload_b64, str(raised_error.exception))
        upsert_schedule.assert_not_called()

    def test_run_compose_post_deploy_update_removes_stale_runtime_override_target_env(
        self,
    ) -> None:
        target_definition = control_plane_dokploy.DokployTargetDefinition(
            context="opw", instance="prod", target_id="compose-123", target_name="opw-prod"
        )
        updated_env_payloads: list[str] = []
        schedule_payloads: list[dict[str, object]] = []
        stale_payload_b64 = base64.b64encode(b'{"stale":true}').decode("ascii")

        def capture_schedule_payload(**kwargs: object) -> dict[str, str]:
            schedule_payloads.append(cast("dict[str, object]", kwargs["schedule_payload"]))
            return {"scheduleId": "schedule-123"}

        def fetch_target_payload(**_: object) -> dict[str, str]:
            return {
                "env": updated_env_payloads[-1]
                if updated_env_payloads
                else "\n".join(
                    (
                        "ODOO_DB_NAME=opw_prod",
                        "ODOO_FILESTORE_PATH=/volumes/data/filestore",
                        f"{ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY}={stale_payload_b64}",
                        f"{LAUNCHPLANE_INSTANCE_OVERRIDES_REQUIRED_ENV_KEY}=true",
                        f"{LAUNCHPLANE_WEBSITE_BOOTSTRAP_REQUIRED_ENV_KEY}=true",
                    )
                ),
                "appName": "opw-prod-app",
                "serverId": "server-123",
            }

        with (
            patch(
                "control_plane.dokploy.api.fetch_dokploy_target_payload",
                side_effect=fetch_target_payload,
            ),
            patch(
                "control_plane.dokploy.api.update_dokploy_target_env",
                side_effect=lambda **kwargs: updated_env_payloads.append(str(kwargs["env_text"])),
            ),
            patch(
                "control_plane.dokploy.api.latest_deployment_for_target",
                return_value={"deploymentId": "deployment-before"},
            ),
            patch(
                "control_plane.dokploy.api.wait_for_target_deployment",
                side_effect=lambda **_kwargs: None,
            ),
            patch(
                "control_plane.dokploy.api.find_matching_dokploy_schedule",
                return_value=None,
            ),
            patch(
                "control_plane.dokploy.api.upsert_dokploy_schedule",
                side_effect=capture_schedule_payload,
            ),
            patch(
                "control_plane.dokploy.api.latest_deployment_for_schedule",
                return_value={"deploymentId": "schedule-before"},
            ),
            patch(
                "control_plane.dokploy.api.wait_for_dokploy_schedule_deployment",
                return_value="deployment=schedule-after status=done",
            ),
            patch("control_plane.dokploy.api.fetch_dokploy_deployment_logs", return_value=()),
            patch(
                "control_plane.dokploy.api.dokploy_request",
                side_effect=lambda **_kwargs: {"ok": True},
            ),
        ):
            control_plane_dokploy.run_compose_post_deploy_update(
                host="https://dokploy.example.com",
                token="secret-token",
                target_definition=target_definition,
                env_file=None,
            )

        self.assertEqual(len(updated_env_payloads), 1)
        self.assertNotIn(ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY, updated_env_payloads[0])
        self.assertNotIn(LAUNCHPLANE_INSTANCE_OVERRIDES_REQUIRED_ENV_KEY, updated_env_payloads[0])
        self.assertNotIn(LAUNCHPLANE_WEBSITE_BOOTSTRAP_REQUIRED_ENV_KEY, updated_env_payloads[0])
        self.assertEqual(len(schedule_payloads), 1)

    def test_run_compose_post_deploy_update_returns_readback_markers_from_schedule_logs(
        self,
    ) -> None:
        target_definition = control_plane_dokploy.DokployTargetDefinition(
            context="opw", instance="prod", target_id="compose-123", target_name="opw-prod"
        )

        def capture_schedule_payload(**_kwargs: object) -> dict[str, str]:
            return {"scheduleId": "schedule-123"}

        with (
            patch(
                "control_plane.dokploy.api.fetch_dokploy_target_payload",
                return_value={
                    "env": "ODOO_DB_NAME=opw_prod\nODOO_FILESTORE_PATH=/volumes/data/filestore\n",
                    "appName": "opw-prod-app",
                    "serverId": "server-123",
                },
            ),
            patch("control_plane.dokploy.api.update_dokploy_target_env"),
            patch("control_plane.dokploy.api.find_matching_dokploy_schedule", return_value=None),
            patch(
                "control_plane.dokploy.api.upsert_dokploy_schedule",
                side_effect=capture_schedule_payload,
            ),
            patch(
                "control_plane.dokploy.api.latest_deployment_for_schedule",
                side_effect=(
                    {"deploymentId": "schedule-before"},
                    {"id": "schedule-after", "status": "done"},
                ),
            ),
            patch(
                "control_plane.dokploy.api.wait_for_dokploy_schedule_deployment",
                return_value="deployment=schedule-after status=done",
            ),
            patch(
                "control_plane.dokploy.api.fetch_dokploy_deployment_logs",
                return_value=(
                    "website_bootstrap_domain_matches_canonical=true",
                    "website_bootstrap_website_id=7",
                    "website_bootstrap_secret=123456",
                ),
            ) as fetch_logs_mock,
            patch(
                "control_plane.dokploy.api.dokploy_request",
                side_effect=lambda **_kwargs: {"ok": True},
            ),
        ):
            markers = control_plane_dokploy.run_compose_post_deploy_update(
                host="https://dokploy.example.com",
                token="secret-token",
                target_definition=target_definition,
                env_file=None,
            )

        fetch_logs_mock.assert_called_once_with(
            host="https://dokploy.example.com",
            token="secret-token",
            deployment_id="schedule-after",
            line_count=control_plane_dokploy.MAX_DOKPLOY_LOG_LINE_COUNT,
        )
        self.assertEqual(
            markers,
            {
                "schedule_id": "schedule-123",
                "schedule_deployment_key": "schedule-after",
                "schedule_deployment_id": "schedule-after",
                "log_available": "true",
                "website_bootstrap_domain_matches_canonical": "true",
                "website_bootstrap_website_id": "7",
            },
        )

    def test_run_compose_post_deploy_update_reads_inline_schedule_log_markers(
        self,
    ) -> None:
        target_definition = control_plane_dokploy.DokployTargetDefinition(
            context="opw", instance="prod", target_id="compose-123", target_name="opw-prod"
        )

        with (
            patch(
                "control_plane.dokploy.api.fetch_dokploy_target_payload",
                return_value={
                    "env": "ODOO_DB_NAME=opw_prod\nODOO_FILESTORE_PATH=/volumes/data/filestore\n",
                    "appName": "opw-prod-app",
                    "serverId": "server-123",
                },
            ),
            patch("control_plane.dokploy.api.update_dokploy_target_env"),
            patch("control_plane.dokploy.api.find_matching_dokploy_schedule", return_value=None),
            patch(
                "control_plane.dokploy.api.upsert_dokploy_schedule",
                return_value={"scheduleId": "schedule-123"},
            ),
            patch(
                "control_plane.dokploy.api.latest_deployment_for_schedule",
                side_effect=(
                    {"deploymentId": "schedule-before"},
                    {
                        "deploymentId": "schedule-after",
                        "status": "done",
                        "logs": (
                            "website_bootstrap_domain_matches_canonical=true\n"
                            "website_bootstrap_website_id=9\n"
                        ),
                    },
                ),
            ),
            patch(
                "control_plane.dokploy.api.wait_for_dokploy_schedule_deployment",
                return_value="deployment=schedule-after status=done",
            ),
            patch("control_plane.dokploy.api.fetch_dokploy_deployment_logs") as fetch_logs_mock,
            patch(
                "control_plane.dokploy.api.dokploy_request",
                side_effect=lambda **_kwargs: {"ok": True},
            ),
        ):
            markers = control_plane_dokploy.run_compose_post_deploy_update(
                host="https://dokploy.example.com",
                token="secret-token",
                target_definition=target_definition,
                env_file=None,
            )

        fetch_logs_mock.assert_not_called()
        self.assertEqual(
            markers,
            {
                "schedule_id": "schedule-123",
                "schedule_deployment_key": "schedule-after",
                "schedule_deployment_id": "schedule-after",
                "log_available": "true",
                "website_bootstrap_domain_matches_canonical": "true",
                "website_bootstrap_website_id": "9",
            },
        )

    def test_run_compose_post_deploy_update_uses_waited_deployment_for_logs(
        self,
    ) -> None:
        target_definition = control_plane_dokploy.DokployTargetDefinition(
            context="opw", instance="prod", target_id="compose-123", target_name="opw-prod"
        )

        with (
            patch(
                "control_plane.dokploy.api.fetch_dokploy_target_payload",
                return_value={
                    "env": "ODOO_DB_NAME=opw_prod\nODOO_FILESTORE_PATH=/volumes/data/filestore\n",
                    "appName": "opw-prod-app",
                    "serverId": "server-123",
                },
            ),
            patch("control_plane.dokploy.api.update_dokploy_target_env"),
            patch("control_plane.dokploy.api.find_matching_dokploy_schedule", return_value=None),
            patch(
                "control_plane.dokploy.api.upsert_dokploy_schedule",
                return_value={"scheduleId": "schedule-123"},
            ),
            patch(
                "control_plane.dokploy.api.latest_deployment_for_schedule",
                side_effect=(
                    {"deploymentId": "schedule-before"},
                    {
                        "deploymentId": "schedule-after-newer",
                        "status": "running",
                        "logs": "website_bootstrap_website_id=99\n",
                    },
                ),
            ),
            patch(
                "control_plane.dokploy.api.wait_for_dokploy_schedule_deployment",
                return_value="deployment=schedule-after-waited status=done",
            ),
            patch(
                "control_plane.dokploy.api.fetch_dokploy_deployment_logs",
                return_value=(
                    "website_bootstrap_domain_matches_canonical=true",
                    "website_bootstrap_website_id=7",
                ),
            ) as fetch_logs_mock,
            patch(
                "control_plane.dokploy.api.dokploy_request",
                side_effect=lambda **_kwargs: {"ok": True},
            ),
        ):
            markers = control_plane_dokploy.run_compose_post_deploy_update(
                host="https://dokploy.example.com",
                token="secret-token",
                target_definition=target_definition,
                env_file=None,
            )

        fetch_logs_mock.assert_called_once_with(
            host="https://dokploy.example.com",
            token="secret-token",
            deployment_id="schedule-after-waited",
            line_count=control_plane_dokploy.MAX_DOKPLOY_LOG_LINE_COUNT,
        )
        self.assertEqual(
            markers,
            {
                "schedule_id": "schedule-123",
                "schedule_deployment_key": "schedule-after-waited",
                "schedule_deployment_id": "schedule-after-waited",
                "log_available": "true",
                "website_bootstrap_domain_matches_canonical": "true",
                "website_bootstrap_website_id": "7",
            },
        )

    def test_run_compose_post_deploy_update_does_not_fail_when_schedule_logs_are_unavailable(
        self,
    ) -> None:
        target_definition = control_plane_dokploy.DokployTargetDefinition(
            context="opw", instance="prod", target_id="compose-123", target_name="opw-prod"
        )

        with (
            patch(
                "control_plane.dokploy.api.fetch_dokploy_target_payload",
                return_value={
                    "env": "ODOO_DB_NAME=opw_prod\nODOO_FILESTORE_PATH=/volumes/data/filestore\n",
                    "appName": "opw-prod-app",
                    "serverId": "server-123",
                },
            ),
            patch("control_plane.dokploy.api.update_dokploy_target_env"),
            patch("control_plane.dokploy.api.find_matching_dokploy_schedule", return_value=None),
            patch(
                "control_plane.dokploy.api.upsert_dokploy_schedule",
                return_value={"scheduleId": "schedule-123"},
            ),
            patch(
                "control_plane.dokploy.api.latest_deployment_for_schedule",
                side_effect=(
                    {"deploymentId": "schedule-before"},
                    {"deploymentId": "schedule-after", "status": "done"},
                ),
            ),
            patch(
                "control_plane.dokploy.api.wait_for_dokploy_schedule_deployment",
                return_value="deployment=schedule-after status=done",
            ),
            patch(
                "control_plane.dokploy.api.fetch_dokploy_deployment_logs",
                side_effect=click.ClickException("not found"),
            ),
            patch(
                "control_plane.dokploy.api.dokploy_request",
                side_effect=lambda **_kwargs: {"ok": True},
            ),
        ):
            markers = control_plane_dokploy.run_compose_post_deploy_update(
                host="https://dokploy.example.com",
                token="secret-token",
                target_definition=target_definition,
                env_file=None,
            )

        self.assertEqual(
            markers,
            {
                "schedule_id": "schedule-123",
                "schedule_deployment_key": "schedule-after",
                "schedule_deployment_id": "schedule-after",
                "log_available": "false",
            },
        )

    def test_run_compose_post_deploy_update_uses_waited_id_when_refetch_has_none(
        self,
    ) -> None:
        target_definition = control_plane_dokploy.DokployTargetDefinition(
            context="opw", instance="prod", target_id="compose-123", target_name="opw-prod"
        )

        with (
            patch(
                "control_plane.dokploy.api.fetch_dokploy_target_payload",
                return_value={
                    "env": "ODOO_DB_NAME=opw_prod\nODOO_FILESTORE_PATH=/volumes/data/filestore\n",
                    "appName": "opw-prod-app",
                    "serverId": "server-123",
                },
            ),
            patch("control_plane.dokploy.api.update_dokploy_target_env"),
            patch("control_plane.dokploy.api.find_matching_dokploy_schedule", return_value=None),
            patch(
                "control_plane.dokploy.api.upsert_dokploy_schedule",
                return_value={"scheduleId": "schedule-123"},
            ),
            patch(
                "control_plane.dokploy.api.latest_deployment_for_schedule",
                side_effect=(
                    {"status": "done"},
                    {"status": "done"},
                ),
            ),
            patch(
                "control_plane.dokploy.api.wait_for_dokploy_schedule_deployment",
                return_value="deployment=schedule-row-after status=done",
            ),
            patch(
                "control_plane.dokploy.api.fetch_dokploy_deployment_logs",
                side_effect=click.ClickException("not found"),
            ) as fetch_logs_mock,
            patch(
                "control_plane.dokploy.api.dokploy_request",
                side_effect=lambda **_kwargs: {"ok": True},
            ),
        ):
            markers = control_plane_dokploy.run_compose_post_deploy_update(
                host="https://dokploy.example.com",
                token="secret-token",
                target_definition=target_definition,
                env_file=None,
            )

        fetch_logs_mock.assert_called_once_with(
            host="https://dokploy.example.com",
            token="secret-token",
            deployment_id="schedule-row-after",
            line_count=control_plane_dokploy.MAX_DOKPLOY_LOG_LINE_COUNT,
        )
        self.assertEqual(
            markers,
            {
                "schedule_id": "schedule-123",
                "schedule_deployment_key": "schedule-row-after",
                "schedule_deployment_id": "schedule-row-after",
                "log_available": "false",
            },
        )

    def test_run_compose_post_deploy_update_can_run_destructive_restore_workflow(
        self,
    ) -> None:
        target_definition = control_plane_dokploy.DokployTargetDefinition(
            context="opw", instance="testing", target_id="compose-123", target_name="opw-testing"
        )
        schedule_payloads: list[dict[str, object]] = []
        updated_env_payloads: list[str] = []

        def capture_schedule_payload(**kwargs: object) -> dict[str, str]:
            schedule_payloads.append(cast("dict[str, object]", kwargs["schedule_payload"]))
            return {"scheduleId": "schedule-123"}

        with (
            patch(
                "control_plane.dokploy.api.fetch_dokploy_target_payload",
                return_value={
                    "env": (
                        "ODOO_DB_NAME=opw_testing\n"
                        "ODOO_FILESTORE_PATH=/volumes/data/filestore\n"
                        "ODOO_UPSTREAM_HOST=source.example.com\n"
                        "ODOO_UPSTREAM_USER=root\n"
                        "ODOO_UPSTREAM_DB_NAME=upstream\n"
                        "ODOO_UPSTREAM_DB_USER=odoo\n"
                        "ODOO_UPSTREAM_FILESTORE_PATH=/volumes/data/filestore/upstream\n"
                    ),
                    "appName": "opw-testing-app",
                    "serverId": "server-123",
                },
            ),
            patch(
                "control_plane.dokploy.api.update_dokploy_target_env",
                side_effect=lambda **kwargs: updated_env_payloads.append(str(kwargs["env_text"])),
            ),
            patch("control_plane.dokploy.api.find_matching_dokploy_schedule", return_value=None),
            patch(
                "control_plane.dokploy.api.upsert_dokploy_schedule",
                side_effect=capture_schedule_payload,
            ),
            patch(
                "control_plane.dokploy.api.latest_deployment_for_schedule",
                return_value={"deploymentId": "schedule-before"},
            ),
            patch(
                "control_plane.dokploy.api.wait_for_dokploy_schedule_deployment",
                return_value="deployment=schedule-after status=done",
            ),
            patch(
                "control_plane.dokploy.api.fetch_dokploy_deployment_logs",
                return_value=(),
            ),
            patch(
                "control_plane.dokploy.api.dokploy_request",
                side_effect=lambda **_kwargs: {"ok": True},
            ),
        ):
            control_plane_dokploy.run_compose_post_deploy_update(
                host="https://dokploy.example.com",
                token="secret-token",
                target_definition=target_definition,
                env_file=None,
                run_destructive_restore=True,
            )

        self.assertEqual(len(schedule_payloads), 1)
        self.assertEqual(len(updated_env_payloads), 0)
        script = str(schedule_payloads[0]["script"])
        self.assertIn("workflow_arguments=()", script)
        self.assertIn("Running Odoo restore", script)
        self.assertNotIn("--post-deploy-maintenance", script)
        self.assertIn("workflow_environment+=(-e ODOO_UPSTREAM_HOST=source.example.com)", script)
        self.assertIn("workflow_environment+=(-e ODOO_UPSTREAM_DB_NAME=upstream)", script)
        self.assertIn(
            "required_workflow_environment_keys+=(ODOO_UPSTREAM_FILESTORE_PATH)",
            script,
        )

    def test_run_compose_post_deploy_update_rejects_restore_without_upstream_source(
        self,
    ) -> None:
        target_definition = control_plane_dokploy.DokployTargetDefinition(
            context="opw", instance="testing", target_id="compose-123", target_name="opw-testing"
        )

        with (
            patch(
                "control_plane.dokploy.api.fetch_dokploy_target_payload",
                return_value={
                    "env": "ODOO_DB_NAME=opw_testing\nODOO_FILESTORE_PATH=/volumes/data/filestore\n",
                    "appName": "opw-testing-app",
                    "serverId": "server-123",
                },
            ),
            self.assertRaises(click.ClickException) as raised_error,
        ):
            control_plane_dokploy.run_compose_post_deploy_update(
                host="https://dokploy.example.com",
                token="secret-token",
                target_definition=target_definition,
                env_file=None,
                run_destructive_restore=True,
            )

        self.assertIn("ODOO_UPSTREAM_HOST", str(raised_error.exception))

    def test_run_compose_post_deploy_update_requires_database_name(self) -> None:
        target_definition = control_plane_dokploy.DokployTargetDefinition(
            context="opw", instance="prod", target_id="compose-123", target_name="opw-prod"
        )

        with patch(
            "control_plane.dokploy.api.fetch_dokploy_target_payload",
            return_value={
                "env": "ODOO_FILESTORE_PATH=/volumes/data/filestore\n",
                "appName": "opw-prod-app",
                "serverId": "server-123",
            },
        ):
            with self.assertRaises(click.ClickException) as raised_error:
                control_plane_dokploy.run_compose_post_deploy_update(
                    host="https://dokploy.example.com",
                    token="secret-token",
                    target_definition=target_definition,
                    env_file=None,
                )

        self.assertIn("ODOO_DB_NAME", str(raised_error.exception))

    def test_run_compose_odoo_backup_gate_uses_manual_schedule_with_consistency_script(
        self,
    ) -> None:
        target_definition = control_plane_dokploy.DokployTargetDefinition(
            context="cm", instance="prod", target_id="compose-123", target_name="cm-prod"
        )
        backup_result: dict[str, object] = {
            "schema_version": 1,
            "backup_nonce": "c" * 64,
            "backup_record_id": "backup-gate-cm-prod-1",
            "database_name": "cm",
            "database_dump_sha256": "a" * 64,
            "filestore_archive_sha256": "b" * 64,
            "database_dump_size": 4096,
            "filestore_archive_size": 8192,
        }
        encoded_result = base64.b64encode(
            json.dumps(backup_result, sort_keys=True).encode()
        ).decode()
        schedule_payloads: list[dict[str, object]] = []
        request_paths: list[str] = []

        def capture_schedule_payload(**kwargs: object) -> dict[str, str]:
            schedule_payloads.append(cast("dict[str, object]", kwargs["schedule_payload"]))
            return {"scheduleId": "schedule-123"}

        def capture_request_path(**kwargs: object) -> dict[str, bool]:
            request_paths.append(str(kwargs["path"]))
            return {"ok": True}

        with (
            patch(
                "control_plane.dokploy.api.fetch_dokploy_target_payload",
                return_value={"appName": "cm-prod-app", "serverId": "server-123"},
            ),
            patch(
                "control_plane.dokploy.api.upsert_dokploy_schedule",
                side_effect=capture_schedule_payload,
            ),
            patch(
                "control_plane.dokploy.api.latest_deployment_for_schedule",
                return_value={"deploymentId": "schedule-before"},
            ),
            patch(
                "control_plane.dokploy.api.wait_for_dokploy_schedule_deployment",
                return_value="deployment=schedule-after status=done",
            ),
            patch(
                "control_plane.dokploy.api.dokploy_request",
                side_effect=capture_request_path,
            ),
            patch(
                "control_plane.dokploy.api.fetch_dokploy_deployment_logs",
                return_value=(
                    f"{control_plane_dokploy.ODOO_BACKUP_GATE_RESULT_MARKER}={encoded_result}",
                ),
            ) as fetch_logs_mock,
        ):
            result = control_plane_dokploy.run_compose_odoo_backup_gate(
                host="https://dokploy.example.com",
                token="secret-token",
                target_definition=target_definition,
                backup_nonce="c" * 64,
                backup_record_id="backup-gate-cm-prod-1",
                database_name="cm",
                filestore_path="/volumes/data/filestore",
                backup_root="/volumes/data/backups/launchplane",
            )

        self.assertEqual(result, backup_result)
        self.assertEqual(len(schedule_payloads), 1)
        self.assertEqual(
            schedule_payloads[0]["name"],
            control_plane_dokploy.DOKPLOY_ODOO_BACKUP_GATE_SCHEDULE_NAME,
        )
        self.assertEqual(schedule_payloads[0]["command"], "control-plane odoo backup gate")
        script = str(schedule_payloads[0]["script"])
        self.assertIn("docker stop", script)
        self.assertIn("trap exit_trap EXIT", script)
        self.assertIn('local exit_status="$?"', script)
        self.assertIn('if [ "${web_was_running}" != "1" ]; then', script)
        self.assertIn('docker start "${web_container_id}" >/dev/null || true', script)
        self.assertIn("start_web_container\ntrap - EXIT", script)
        self.assertNotIn("restart_web_on_exit", script)
        self.assertIn("pg_dump", script)
        self.assertIn("tar -C", script)
        self.assertIn("manifest.json", script)
        self.assertIn('"database_dump_sha256"', script)
        self.assertIn('"filestore_archive_sha256"', script)
        self.assertIn(
            'script_runner_uid=$(docker exec "${script_runner_container_id}" id -u)', script
        )
        self.assertIn('-o "$SCRIPT_RUNNER_UID" -g "$SCRIPT_RUNNER_GID"', script)
        self.assertIn("RESULT_MARKER", script)
        self.assertIn("BACKUP_NONCE", script)
        self.assertIn("docker exec -i", script)
        manifest_script = script.split("python3 - <<'PY'\n", 1)[1].split("\nPY", 1)[0]
        compile(manifest_script, "embedded-odoo-backup-manifest.py", "exec")
        self.assertIn("/api/schedule.runManually", request_paths)
        fetch_logs_mock.assert_called_once_with(
            host="https://dokploy.example.com",
            token="secret-token",
            deployment_id="schedule-after",
            line_count=control_plane_dokploy.MAX_DOKPLOY_LOG_LINE_COUNT,
        )

    def test_run_compose_odoo_backup_gate_rejects_done_schedule_without_completion_evidence(
        self,
    ) -> None:
        target_definition = control_plane_dokploy.DokployTargetDefinition(
            context="cm", instance="prod", target_id="compose-123", target_name="cm-prod"
        )

        with (
            patch(
                "control_plane.dokploy.api.fetch_dokploy_target_payload",
                return_value={"appName": "cm-prod-app", "serverId": "server-123"},
            ),
            patch(
                "control_plane.dokploy.api.upsert_dokploy_schedule",
                return_value={"scheduleId": "schedule-123"},
            ),
            patch(
                "control_plane.dokploy.api.latest_deployment_for_schedule",
                return_value={"deploymentId": "schedule-before"},
            ),
            patch(
                "control_plane.dokploy.api.dokploy_request",
                return_value={"ok": True},
            ),
            patch(
                "control_plane.dokploy.api.wait_for_dokploy_schedule_deployment",
                return_value="deployment=schedule-after status=done",
            ),
            patch(
                "control_plane.dokploy.api.fetch_dokploy_deployment_logs",
                return_value=("pg_dump: Permission denied",),
            ),
            self.assertRaisesRegex(click.ClickException, "no unique bounded result"),
        ):
            control_plane_dokploy.run_compose_odoo_backup_gate(
                host="https://dokploy.example.com",
                token="secret-token",
                target_definition=target_definition,
                backup_nonce="c" * 64,
                backup_record_id="backup-gate-cm-prod-1",
                database_name="cm",
                filestore_path="/volumes/data/filestore",
                backup_root="/volumes/data/backups/launchplane",
            )

    def test_run_compose_odoo_backup_verification_returns_only_bounded_evidence(
        self,
    ) -> None:
        target_definition = control_plane_dokploy.DokployTargetDefinition(
            context="cm", instance="prod", target_id="compose-123", target_name="cm-prod"
        )
        verification_result: dict[str, object] = {
            "schema_version": 1,
            "verification_nonce": "c" * 64,
            "backup_record_id": "backup-gate-cm-prod-1",
            "database_name": "cm",
            "verification_status": "pass",
            "manifest_status": "pass",
            "sha256_status": "pass",
            "pg_restore_status": "pass",
            "tar_status": "pass",
            "staging_space_status": "pass",
            "database_dump_sha256": "a" * 64,
            "filestore_archive_sha256": "b" * 64,
            "database_dump_size": 4096,
            "filestore_archive_size": 8192,
            "pg_restore_entry_count": 42,
            "filestore_member_count": 128,
            "filestore_unpacked_size": 16384,
            "data_volume_free_bytes": 32768,
            "staging_required_bytes": 16384,
            "failure_code": "",
        }
        encoded_result = base64.b64encode(
            json.dumps(verification_result, sort_keys=True).encode()
        ).decode()
        schedule_payloads: list[dict[str, object]] = []

        def capture_schedule_payload(**kwargs: object) -> dict[str, str]:
            schedule_payloads.append(cast("dict[str, object]", kwargs["schedule_payload"]))
            return {"scheduleId": "schedule-verify"}

        with (
            patch(
                "control_plane.dokploy.api.fetch_dokploy_target_payload",
                return_value={"appName": "cm-prod-app", "serverId": "server-123"},
            ),
            patch(
                "control_plane.dokploy.api.upsert_dokploy_schedule",
                side_effect=capture_schedule_payload,
            ),
            patch(
                "control_plane.dokploy.api.latest_deployment_for_schedule",
                return_value={"deploymentId": "schedule-before"},
            ),
            patch(
                "control_plane.dokploy.api.dokploy_request",
                return_value={"ok": True},
            ),
            patch(
                "control_plane.dokploy.api.wait_for_dokploy_schedule_deployment",
                return_value="deployment=schedule-after status=success",
            ),
            patch(
                "control_plane.dokploy.api.fetch_dokploy_deployment_logs",
                return_value=(
                    f"{control_plane_dokploy.ODOO_BACKUP_VERIFICATION_RESULT_MARKER}={encoded_result}",
                ),
            ) as fetch_logs_mock,
        ):
            result = control_plane_dokploy.run_compose_odoo_backup_verification(
                host="https://dokploy.example.com",
                token="secret-token",
                target_definition=target_definition,
                verification_nonce="c" * 64,
                backup_record_id="backup-gate-cm-prod-1",
                database_name="cm",
                filestore_path="/volumes/data/filestore",
                backup_dir="/volumes/data/backups/launchplane/cm/backup-gate-cm-prod-1",
                database_dump_path=(
                    "/volumes/data/backups/launchplane/cm/backup-gate-cm-prod-1/cm.dump"
                ),
                filestore_archive_path=(
                    "/volumes/data/backups/launchplane/cm/backup-gate-cm-prod-1/cm-filestore.tar.gz"
                ),
                manifest_path=(
                    "/volumes/data/backups/launchplane/cm/backup-gate-cm-prod-1/manifest.json"
                ),
            )

        self.assertEqual(result, verification_result)
        self.assertEqual(len(schedule_payloads), 1)
        self.assertEqual(
            schedule_payloads[0]["name"],
            control_plane_dokploy.DOKPLOY_ODOO_BACKUP_VERIFICATION_SCHEDULE_NAME,
        )
        self.assertEqual(schedule_payloads[0]["command"], "control-plane odoo backup verification")
        script = str(schedule_payloads[0]["script"])
        self.assertIn('subprocess.run(\n        ["pg_restore", "--list"', script)
        self.assertIn('"--file=/dev/null"', script)
        self.assertIn("tarfile.open", script)
        self.assertIn("shutil.disk_usage", script)
        for check_name in (
            "manifest_status",
            "sha256_status",
            "pg_restore_status",
            "tar_status",
            "staging_space_status",
        ):
            self.assertIn(f'active_check = "{check_name}"', script)
        self.assertIn('result[active_check] = "fail"', script)
        self.assertNotIn("docker start", script)
        self.assertNotIn("docker stop", script)
        self.assertNotIn("extractall", script)
        verification_script = script.split("python3 - <<'PY'\n", 1)[1].split("\nPY", 1)[0]
        compile(verification_script, "embedded-odoo-backup-verification.py", "exec")
        fetch_logs_mock.assert_called_once_with(
            host="https://dokploy.example.com",
            token="secret-token",
            deployment_id="schedule-after",
            line_count=control_plane_dokploy.MAX_DOKPLOY_LOG_LINE_COUNT,
        )

    def test_extract_odoo_backup_verification_result_rejects_unbounded_fields(self) -> None:
        payload: dict[str, object] = {
            key: 0 for key in control_plane_dokploy.ODOO_BACKUP_VERIFICATION_RESULT_FIELDS
        }
        payload["private_path"] = "/volumes/data/private"
        encoded_result = base64.b64encode(json.dumps(payload).encode()).decode()

        with self.assertRaisesRegex(click.ClickException, "unexpected bounded result shape"):
            control_plane_dokploy.extract_odoo_backup_verification_result(
                {
                    "logs": [
                        f"{control_plane_dokploy.ODOO_BACKUP_VERIFICATION_RESULT_MARKER}={encoded_result}"
                    ]
                }
            )

    def test_odoo_backup_verification_marks_unexpected_active_check_failure(self) -> None:
        script = control_plane_dokploy._build_dokploy_odoo_backup_verification_script(
            compose_app_name="cm-prod",
            verification_nonce="c" * 64,
            backup_record_id="backup-gate-cm-prod-1",
            database_name="cm_prod",
            filestore_path="/volumes/data/filestore",
            backup_dir="/volumes/data/backups/launchplane/cm_prod/backup-gate-cm-prod-1",
            database_dump_path=(
                "/volumes/data/backups/launchplane/cm_prod/backup-gate-cm-prod-1/cm_prod.dump"
            ),
            filestore_archive_path=(
                "/volumes/data/backups/launchplane/cm_prod/backup-gate-cm-prod-1/"
                "cm_prod-filestore.tar.gz"
            ),
            manifest_path=(
                "/volumes/data/backups/launchplane/cm_prod/backup-gate-cm-prod-1/manifest.json"
            ),
        )
        verification_script = script.split("python3 - <<'PY'\n", 1)[1].split("\nPY", 1)[0]
        injected_script = verification_script.replace(
            'try:\n    filestore_root = Path(os.environ["FILESTORE_ROOT"])',
            'try:\n    raise RuntimeError("private /volumes/data/path")\n'
            '    filestore_root = Path(os.environ["FILESTORE_ROOT"])',
            1,
        )
        self.assertNotEqual(injected_script, verification_script)
        environment = {
            **os.environ,
            "VERIFICATION_NONCE": "c" * 64,
            "BACKUP_RECORD_ID": "backup-gate-cm-prod-1",
            "DATABASE_NAME": "cm_prod",
            "FILESTORE_ROOT": "/volumes/data/filestore",
            "BACKUP_DIR": "/volumes/data/backups",
            "DATABASE_DUMP_PATH": "/volumes/data/backups/cm_prod.dump",
            "FILESTORE_ARCHIVE_PATH": "/volumes/data/backups/cm_prod-filestore.tar.gz",
            "MANIFEST_PATH": "/volumes/data/backups/manifest.json",
            "RESULT_MARKER": control_plane_dokploy.ODOO_BACKUP_VERIFICATION_RESULT_MARKER,
        }

        completed = subprocess.run(
            [sys.executable, "-c", injected_script],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )

        self.assertNotIn("private /volumes/data/path", completed.stdout)
        marker_prefix = f"{control_plane_dokploy.ODOO_BACKUP_VERIFICATION_RESULT_MARKER}="
        encoded_result = completed.stdout.strip().removeprefix(marker_prefix)
        result = json.loads(base64.b64decode(encoded_result).decode("utf-8"))
        self.assertEqual(result["verification_status"], "fail")
        self.assertEqual(result["manifest_status"], "fail")
        self.assertEqual(result["failure_code"], "manifest_path_verification_error")

    def test_odoo_backup_verification_maps_manifest_stat_error(self) -> None:
        script = control_plane_dokploy._build_dokploy_odoo_backup_verification_script(
            compose_app_name="cm-prod",
            verification_nonce="c" * 64,
            backup_record_id="backup-gate-cm-prod-1",
            database_name="cm_prod",
            filestore_path="/volumes/data/filestore",
            backup_dir="/volumes/data/backups/launchplane/cm_prod/backup-gate-cm-prod-1",
            database_dump_path=(
                "/volumes/data/backups/launchplane/cm_prod/backup-gate-cm-prod-1/cm_prod.dump"
            ),
            filestore_archive_path=(
                "/volumes/data/backups/launchplane/cm_prod/backup-gate-cm-prod-1/"
                "cm_prod-filestore.tar.gz"
            ),
            manifest_path=(
                "/volumes/data/backups/launchplane/cm_prod/backup-gate-cm-prod-1/manifest.json"
            ),
        )
        verification_script = script.split("python3 - <<'PY'\n", 1)[1].split("\nPY", 1)[0]
        injected_script = verification_script.replace(
            '    manifest_path = Path(os.environ["MANIFEST_PATH"])',
            '    manifest_path = Path(os.environ["MANIFEST_PATH"])\n'
            "    class UnreadableManifestPath:\n"
            "        def is_symlink(self):\n"
            "            return False\n"
            "        def is_file(self):\n"
            "            return True\n"
            "        def stat(self):\n"
            '            raise PermissionError("private /volumes/data/path")\n'
            "    manifest_path = UnreadableManifestPath()",
            1,
        )
        self.assertNotEqual(injected_script, verification_script)
        environment = {
            **os.environ,
            "VERIFICATION_NONCE": "c" * 64,
            "BACKUP_RECORD_ID": "backup-gate-cm-prod-1",
            "DATABASE_NAME": "cm_prod",
            "FILESTORE_ROOT": "/volumes/data/filestore",
            "BACKUP_DIR": "/volumes/data/backups",
            "DATABASE_DUMP_PATH": "/volumes/data/backups/cm_prod.dump",
            "FILESTORE_ARCHIVE_PATH": "/volumes/data/backups/cm_prod-filestore.tar.gz",
            "MANIFEST_PATH": "/volumes/data/backups/manifest.json",
            "RESULT_MARKER": control_plane_dokploy.ODOO_BACKUP_VERIFICATION_RESULT_MARKER,
        }

        completed = subprocess.run(
            [sys.executable, "-c", injected_script],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )

        self.assertNotIn("private /volumes/data/path", completed.stdout)
        marker_prefix = f"{control_plane_dokploy.ODOO_BACKUP_VERIFICATION_RESULT_MARKER}="
        encoded_result = completed.stdout.strip().removeprefix(marker_prefix)
        result = json.loads(base64.b64decode(encoded_result).decode("utf-8"))
        self.assertEqual(result["verification_status"], "fail")
        self.assertEqual(result["manifest_status"], "fail")
        self.assertEqual(result["failure_code"], "manifest_metadata_unreadable")

    def test_odoo_backup_verification_accepts_legacy_manifest_and_computes_hashes(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_name = "cm"
            backup_record_id = "backup-gate-cm-prod-legacy"
            backup_dir = root / "backups" / database_name / backup_record_id
            backup_dir.mkdir(parents=True)
            filestore_root = root / "filestore"
            filestore_database_path = filestore_root / database_name
            filestore_database_path.mkdir(parents=True)
            filestore_file = filestore_database_path / "ab" / "asset"
            filestore_file.parent.mkdir()
            filestore_file.write_bytes(b"filestore")
            database_dump_path = backup_dir / f"{database_name}.dump"
            database_dump_path.write_bytes(b"legacy-custom-dump")
            filestore_archive_path = backup_dir / f"{database_name}-filestore.tar.gz"
            with tarfile.open(filestore_archive_path, "w:gz") as archive:
                archive.add(filestore_database_path, arcname=database_name)
            manifest_path = backup_dir / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "backup_record_id": backup_record_id,
                        "database_name": database_name,
                        "backup_dir": str(backup_dir),
                        "database_dump_path": str(database_dump_path),
                        "filestore_archive_path": str(filestore_archive_path),
                        "database_dump_size": str(database_dump_path.stat().st_size),
                        "filestore_archive_size": str(filestore_archive_path.stat().st_size),
                        "captured_at": "2026-06-14T00:00:00Z",
                    }
                ),
                encoding="utf-8",
            )
            fake_bin = root / "bin"
            fake_bin.mkdir()
            fake_pg_restore = fake_bin / "pg_restore"
            fake_pg_restore.write_text(
                "#!/bin/sh\nprintf '1; 1259 1 TABLE public example owner\\n'\n",
                encoding="utf-8",
            )
            fake_pg_restore.chmod(0o755)
            script = control_plane_dokploy._build_dokploy_odoo_backup_verification_script(
                compose_app_name="cm-prod",
                verification_nonce="c" * 64,
                backup_record_id=backup_record_id,
                database_name=database_name,
                filestore_path=str(filestore_root),
                backup_dir=str(backup_dir),
                database_dump_path=str(database_dump_path),
                filestore_archive_path=str(filestore_archive_path),
                manifest_path=str(manifest_path),
            )
            verification_script = script.split("python3 - <<'PY'\n", 1)[1].split("\nPY", 1)[0]
            environment = {
                **os.environ,
                "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
                "VERIFICATION_NONCE": "c" * 64,
                "BACKUP_RECORD_ID": backup_record_id,
                "DATABASE_NAME": database_name,
                "FILESTORE_ROOT": str(filestore_root),
                "BACKUP_DIR": str(backup_dir),
                "DATABASE_DUMP_PATH": str(database_dump_path),
                "FILESTORE_ARCHIVE_PATH": str(filestore_archive_path),
                "MANIFEST_PATH": str(manifest_path),
                "RESULT_MARKER": (control_plane_dokploy.ODOO_BACKUP_VERIFICATION_RESULT_MARKER),
            }

            completed = subprocess.run(
                [sys.executable, "-c", verification_script],
                check=True,
                capture_output=True,
                text=True,
                env=environment,
            )

        marker_prefix = f"{control_plane_dokploy.ODOO_BACKUP_VERIFICATION_RESULT_MARKER}="
        encoded_result = completed.stdout.strip().removeprefix(marker_prefix)
        result = json.loads(base64.b64decode(encoded_result).decode("utf-8"))
        self.assertEqual(result["verification_status"], "pass")
        self.assertEqual(result["verification_nonce"], "c" * 64)
        self.assertEqual(result["backup_record_id"], backup_record_id)
        self.assertEqual(result["database_name"], database_name)
        self.assertEqual(result["manifest_status"], "pass")
        self.assertEqual(result["sha256_status"], "pass")
        self.assertEqual(
            result["database_dump_sha256"],
            hashlib.sha256(b"legacy-custom-dump").hexdigest(),
        )

    def test_run_compose_odoo_stable_bootstrap_uses_dedicated_manual_schedule(self) -> None:
        target_definition = control_plane_dokploy.DokployTargetDefinition(
            context="cm",
            instance="testing",
            target_id="compose-123",
            target_name="cm-testing",
        )
        schedule_payloads: list[dict[str, object]] = []
        updated_env_payloads: list[str] = []
        request_paths: list[str] = []

        def capture_schedule_payload(**kwargs: object) -> dict[str, str]:
            schedule_payloads.append(cast("dict[str, object]", kwargs["schedule_payload"]))
            return {"scheduleId": "schedule-123"}

        def capture_request_path(**kwargs: object) -> dict[str, bool]:
            request_paths.append(str(kwargs["path"]))
            return {"ok": True}

        with (
            patch(
                "control_plane.dokploy.api.fetch_dokploy_target_payload",
                return_value={
                    "name": "cm-testing",
                    "env": (
                        "ODOO_DB_NAME=cm_testing\n"
                        "ODOO_FILESTORE_PATH=/volumes/data/filestore\n"
                        "ODOO_ADDONS_PATH=/opt/project/addons,/opt/launchplane/addons,/odoo/addons\n"
                        "ODOO_INSTALL_MODULES=launchplane_settings,disable_odoo_online,cm_website\n"
                    ),
                    "appName": "cm-testing-app",
                    "serverId": "server-123",
                },
            ),
            patch(
                "control_plane.dokploy.api.find_matching_dokploy_schedule",
                return_value=None,
            ),
            patch(
                "control_plane.dokploy.api.update_dokploy_target_env",
                side_effect=lambda **kwargs: updated_env_payloads.append(str(kwargs["env_text"])),
            ),
            patch(
                "control_plane.dokploy.api.latest_deployment_for_target",
                return_value={"deploymentId": "deployment-before"},
            ),
            patch("control_plane.dokploy.api.trigger_deployment"),
            patch("control_plane.dokploy.api.wait_for_target_deployment"),
            patch(
                "control_plane.dokploy.api.upsert_dokploy_schedule",
                side_effect=capture_schedule_payload,
            ),
            patch(
                "control_plane.dokploy.api.latest_deployment_for_schedule",
                side_effect=(
                    {"deploymentId": "schedule-before"},
                    {
                        "deploymentId": "schedule-after",
                        "logs": [
                            "odoo_module_update_image_match=true",
                            "odoo_module_update_modules_configured=true",
                            "odoo_module_update_completed=true",
                        ],
                    },
                ),
            ),
            patch(
                "control_plane.dokploy.api.wait_for_dokploy_schedule_deployment",
                return_value="deployment=schedule-after status=done",
            ),
            patch(
                "control_plane.dokploy.api.dokploy_request",
                side_effect=capture_request_path,
            ),
        ):
            control_plane_dokploy.run_compose_odoo_stable_bootstrap(
                host="https://dokploy.example.com",
                token="secret-token",
                target_definition=target_definition,
                env_file=None,
            )

        self.assertEqual(len(updated_env_payloads), 1)
        self.assertIn(
            "ODOO_ADDONS_PATH=/opt/project/addons,/opt/launchplane/addons,/odoo/addons,/opt/enterprise",
            updated_env_payloads[0],
        )
        self.assertEqual(len(schedule_payloads), 1)
        self.assertEqual(
            schedule_payloads[0]["name"],
            control_plane_dokploy.DOKPLOY_ODOO_BOOTSTRAP_SCHEDULE_NAME,
        )
        self.assertEqual(schedule_payloads[0]["command"], "control-plane odoo stable bootstrap")
        script = str(schedule_payloads[0]["script"])
        self.assertIn("workflow_arguments=(--bootstrap)", script)
        self.assertNotIn("workflow_arguments=(--update-only)", script)
        self.assertIn(
            "workflow_environment+=(-e ODOO_UPDATE_MODULES=launchplane_settings,disable_odoo_online,cm_website)",
            script,
        )
        self.assertIn(
            "workflow_environment+=(-e ODOO_FILESTORE_PATH=/volumes/data/filestore)",
            script,
        )
        self.assertIn("/api/schedule.runManually", request_paths)

    def test_run_compose_odoo_stable_bootstrap_requires_artifact_module_list(self) -> None:
        target_definition = control_plane_dokploy.DokployTargetDefinition(
            context="cm",
            instance="testing",
            target_id="compose-123",
            target_name="cm-testing",
        )

        with (
            patch(
                "control_plane.dokploy.api.fetch_dokploy_target_payload",
                return_value={
                    "name": "cm-testing",
                    "env": "ODOO_DB_NAME=cm_testing\n",
                    "appName": "cm-testing-app",
                    "serverId": "server-123",
                },
            ),
            patch("control_plane.dokploy.api.upsert_dokploy_schedule") as upsert_schedule,
            self.assertRaisesRegex(click.ClickException, "ODOO_INSTALL_MODULES"),
        ):
            control_plane_dokploy.run_compose_odoo_stable_bootstrap(
                host="https://dokploy.example.com",
                token="secret-token",
                target_definition=target_definition,
                env_file=None,
            )

        upsert_schedule.assert_not_called()

    def test_run_compose_odoo_stable_bootstrap_refuses_live_name_mismatch(self) -> None:
        target_definition = control_plane_dokploy.DokployTargetDefinition(
            context="cm",
            instance="testing",
            target_id="compose-123",
            target_name="cm-testing",
        )

        with patch(
            "control_plane.dokploy.api.fetch_dokploy_target_payload",
            return_value={
                "name": "cm-prod",
                "env": "ODOO_DB_NAME=cm_testing\n",
                "appName": "cm-testing-app",
                "serverId": "server-123",
            },
        ):
            with self.assertRaises(click.ClickException) as raised_error:
                control_plane_dokploy.run_compose_odoo_stable_bootstrap(
                    host="https://dokploy.example.com",
                    token="secret-token",
                    target_definition=target_definition,
                    env_file=None,
                )

        self.assertIn("target proof failed", str(raised_error.exception))

    def test_run_compose_post_deploy_update_rejects_unsupported_env_overlay_keys(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            env_file = Path(temporary_directory_name) / "post-deploy.env"
            env_file.write_text(
                "\n".join(
                    (
                        "ODOO_DB_NAME=opw_prod",
                        "UNRELATED_RUNTIME_KEY=not-allowed",
                    )
                ),
                encoding="utf-8",
            )
            target_definition = control_plane_dokploy.DokployTargetDefinition(
                context="opw", instance="prod", target_id="compose-123", target_name="opw-prod"
            )

            with patch(
                "control_plane.dokploy.api.fetch_dokploy_target_payload",
                return_value={
                    "env": "ODOO_DB_NAME=old_db\n",
                    "appName": "opw-prod-app",
                    "serverId": "server-123",
                },
            ):
                with self.assertRaises(click.ClickException) as raised_error:
                    control_plane_dokploy.run_compose_post_deploy_update(
                        host="https://dokploy.example.com",
                        token="secret-token",
                        target_definition=target_definition,
                        env_file=env_file,
                    )

        self.assertIn("only supports", str(raised_error.exception))
        self.assertIn("UNRELATED_RUNTIME_KEY", str(raised_error.exception))


class LaunchplaneServiceDeployTests(unittest.TestCase):
    @staticmethod
    def _target_payload(
        *, env_text: str, custom_git_ssh_key_id: str = "ssh-key-123"
    ) -> dict[str, object]:
        return {
            "name": "launchplane",
            "appName": "compose-launchplane",
            "sourceType": "git",
            "customGitUrl": "git@github.com:example/launchplane.git",
            "customGitBranch": "main",
            "customGitSSHKeyId": custom_git_ssh_key_id,
            "composePath": "./docker-compose.yml",
            "composeStatus": "done",
            "env": env_text,
        }

    def test_render_dokploy_env_text_with_overrides_updates_and_removes_keys(self) -> None:
        rendered = control_plane_dokploy.render_dokploy_env_text_with_overrides(
            "KEEP=1\nREMOVE=old\n",
            updates={"ADD": "2"},
            removals=("REMOVE",),
        )

        self.assertEqual(rendered, "KEEP=1\nADD=2")

    def test_launchplane_compose_exports_runtime_image_reference(self) -> None:
        compose_text = Path("docker-compose.yml").read_text()

        self.assertIn("image: ${DOCKER_IMAGE_REFERENCE:-launchplane:local}", compose_text)
        self.assertIn(
            "DOCKER_IMAGE_REFERENCE: ${DOCKER_IMAGE_REFERENCE:-launchplane:local}",
            compose_text,
        )

    def test_render_odoo_raw_compose_file_pins_artifact_image_and_services(self) -> None:
        compose_file = control_plane_dokploy.render_odoo_raw_compose_file(
            image_reference="ghcr.io/cbusillo/odoo-tenant-cm@sha256:abc123",
            domain_hosts=("cm-testing.shinycomputers.com",),
            runtime_port=8069,
        )

        self.assertIn('image: "ghcr.io/cbusillo/odoo-tenant-cm@sha256:abc123"', compose_file)
        self.assertIn("\n  web:", compose_file)
        self.assertIn(
            "${ODOO_WEB_COMMAND:-python3 /volumes/scripts/run_odoo_startup.py -c /tmp/platform.odoo.conf}",
            compose_file,
        )
        self.assertIn("PLATFORM_CONTEXT: ${PLATFORM_CONTEXT:-}", compose_file)
        self.assertIn("PLATFORM_INSTANCE: ${PLATFORM_INSTANCE:-}", compose_file)
        self.assertIn(
            "ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64: ${ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64:-}",
            compose_file,
        )
        self.assertIn(
            "LAUNCHPLANE_INSTANCE_OVERRIDES_REQUIRED: ${LAUNCHPLANE_INSTANCE_OVERRIDES_REQUIRED:-}",
            compose_file,
        )
        self.assertIn(
            "LAUNCHPLANE_WEBSITE_BOOTSTRAP_REQUIRED: ${LAUNCHPLANE_WEBSITE_BOOTSTRAP_REQUIRED:-}",
            compose_file,
        )
        self.assertIn('- "${ODOO_WEB_HOST_PORT:-8069}:8069"', compose_file)
        self.assertIn('- "${ODOO_LONGPOLL_HOST_PORT:-8072}:8072"', compose_file)
        self.assertIn("\n  database:", compose_file)
        self.assertIn("\n  script-runner:", compose_file)
        self.assertIn(
            "ODOO_ADDONS_PATH: ${ODOO_ADDONS_PATH:-/opt/project/addons,/opt/extra_addons,/opt/launchplane/addons,/opt/enterprise,/odoo/addons}",
            compose_file,
        )
        self.assertIn(
            "ODOO_SERVER_WIDE_MODULES: ${ODOO_SERVER_WIDE_MODULES:-base,web,launchplane_runtime_health}",
            compose_file,
        )
        self.assertIn("name: ${ODOO_PROJECT_NAME:-odoo}", compose_file)
        self.assertIn("dokploy-network:", compose_file)
        self.assertIn("traefik.enable=true", compose_file)
        self.assertIn("traefik.docker.network=dokploy-network", compose_file)
        self.assertIn(
            "traefik.http.routers.launchplane-odoo-web-cm-testing-shinycomputers-com-c93dcbe8-web.rule=Host(`cm-testing.shinycomputers.com`)",
            compose_file,
        )
        self.assertIn(
            "traefik.http.routers.launchplane-odoo-web-cm-testing-shinycomputers-com-c93dcbe8-web.entrypoints=web",
            compose_file,
        )
        self.assertIn(
            "traefik.http.services.launchplane-odoo-web-cm-testing-shinycomputers-com-c93dcbe8-web.loadbalancer.server.port=8069",
            compose_file,
        )
        self.assertIn(
            "traefik.http.routers.launchplane-odoo-web-cm-testing-shinycomputers-com-c93dcbe8-web.middlewares=redirect-to-https@file",
            compose_file,
        )
        self.assertIn(
            "traefik.http.routers.launchplane-odoo-web-cm-testing-shinycomputers-com-c93dcbe8-websecure.rule=Host(`cm-testing.shinycomputers.com`)",
            compose_file,
        )
        self.assertIn(
            "traefik.http.routers.launchplane-odoo-web-cm-testing-shinycomputers-com-c93dcbe8-websecure.entrypoints=websecure",
            compose_file,
        )
        self.assertIn(
            "traefik.http.routers.launchplane-odoo-web-cm-testing-shinycomputers-com-c93dcbe8-websecure.tls=true",
            compose_file,
        )
        self.assertNotIn(".tls.certresolver=", compose_file)
        self.assertIn(
            "traefik.http.services.launchplane-odoo-web-cm-testing-shinycomputers-com-c93dcbe8-websecure.loadbalancer.server.port=8069",
            compose_file,
        )

    def test_render_odoo_raw_compose_file_adds_letsencrypt_resolver(self) -> None:
        compose_file = control_plane_dokploy.render_odoo_raw_compose_file(
            image_reference="ghcr.io/cbusillo/odoo-tenant-cm@sha256:abc123",
            domain_hosts=("pr-45.cm-preview.example.test",),
            domain_certificate_type="letsencrypt",
        )

        self.assertIn(
            "traefik.http.routers.launchplane-odoo-web-pr-45-cm-preview-example-test-a09f0256-websecure.tls.certresolver=letsencrypt",
            compose_file,
        )

    def test_render_odoo_raw_compose_file_serializes_image_as_yaml_scalar(self) -> None:
        compose_file = control_plane_dokploy.render_odoo_raw_compose_file(
            image_reference=("ghcr.io/cbusillo/odoo-tenant-cm@sha256:abc123\n  privileged: true")
        )

        self.assertIn(
            'image: "ghcr.io/cbusillo/odoo-tenant-cm@sha256:abc123\\n  privileged: true"',
            compose_file,
        )
        self.assertNotIn("\n  privileged: true\n", compose_file)

    def test_render_odoo_raw_compose_file_omits_traefik_labels_without_domains(self) -> None:
        compose_file = control_plane_dokploy.render_odoo_raw_compose_file(
            image_reference="ghcr.io/cbusillo/odoo-tenant-cm@sha256:abc123"
        )

        self.assertNotIn("traefik.http.routers", compose_file)
        self.assertNotIn("traefik.http.services", compose_file)
        self.assertNotIn("traefik.enable=true", compose_file)
        self.assertIn("dokploy-network:", compose_file)

    def test_render_odoo_raw_compose_file_normalizes_and_dedupes_domain_labels(self) -> None:
        compose_file = control_plane_dokploy.render_odoo_raw_compose_file(
            image_reference="ghcr.io/cbusillo/odoo-tenant-cm@sha256:abc123",
            domain_hosts=(
                " CM-Testing.shinycomputers.com ",
                "cm-testing.shinycomputers.com",
            ),
            runtime_port=8069,
        )

        self.assertEqual(compose_file.count("Host(`cm-testing.shinycomputers.com`)"), 2)

    def test_render_odoo_raw_compose_file_rejects_invalid_domain_label_host(self) -> None:
        with self.assertRaisesRegex(click.ClickException, "invalid domain host"):
            control_plane_dokploy.render_odoo_raw_compose_file(
                image_reference="ghcr.io/cbusillo/odoo-tenant-cm@sha256:abc123",
                domain_hosts=("bad`host.example",),
            )

    def test_render_odoo_raw_compose_file_can_avoid_host_port_publishing(self) -> None:
        compose_file = control_plane_dokploy.render_odoo_raw_compose_file(
            image_reference="ghcr.io/cbusillo/odoo-tenant-cm@sha256:abc123",
            domain_hosts=("pr-45.cm-preview.example.test",),
            publish_host_ports=False,
        )

        self.assertNotIn("ODOO_WEB_HOST_PORT", compose_file)
        self.assertNotIn("ODOO_LONGPOLL_HOST_PORT", compose_file)
        self.assertIn("traefik.enable=true", compose_file)
        self.assertIn("traefik.http.routers", compose_file)
        self.assertIn("traefik.http.services", compose_file)

    def test_sync_dokploy_compose_raw_source_updates_and_verifies_hash(self) -> None:
        compose_file = control_plane_dokploy.render_odoo_raw_compose_file(
            image_reference="ghcr.io/cbusillo/odoo-tenant-cm@sha256:abc123"
        )
        update_payloads: list[dict[str, object]] = []

        def fake_dokploy_request(**kwargs: object) -> dict[str, object]:
            update_payloads.append(dict(kwargs))
            return {"status": "ok"}

        with (
            patch("control_plane.dokploy.api.dokploy_request", side_effect=fake_dokploy_request),
            patch(
                "control_plane.dokploy.api.fetch_dokploy_target_payload",
                return_value={
                    "name": "cm-testing",
                    "environmentId": "env-123",
                    "sourceType": "raw",
                    "composePath": "docker-compose.yml",
                    "composeFile": compose_file,
                },
            ),
        ):
            evidence = control_plane_dokploy.sync_dokploy_compose_raw_source(
                host="https://dokploy.example.com",
                token="token-123",
                compose_id="compose-123",
                compose_name="cm-testing",
                target_payload={
                    "name": "cm-testing",
                    "environmentId": "env-123",
                    "sourceType": "git",
                    "composeFile": "",
                    "autoDeploy": False,
                },
                compose_file=compose_file,
            )

        self.assertEqual(len(update_payloads), 1)
        payload = cast("dict[str, object]", update_payloads[0]["payload"])
        self.assertEqual(payload["sourceType"], "raw")
        self.assertEqual(payload["composePath"], "docker-compose.yml")
        self.assertEqual(payload["composeFile"], compose_file)
        self.assertEqual(evidence["source_type"], "raw")
        self.assertEqual(evidence["compose_path"], "docker-compose.yml")
        self.assertEqual(
            evidence["compose_sha256"], control_plane_dokploy.compose_file_sha256(compose_file)
        )
        self.assertEqual(evidence["changed"], "true")

    def test_ensure_compose_web_domain_route_updates_existing_route(self) -> None:
        requests: list[dict[str, object]] = []

        def fake_dokploy_request(**kwargs: object) -> object:
            requests.append(dict(kwargs))
            if kwargs["path"] == "/api/domain.byComposeId":
                return [
                    {
                        "domainId": "domain-cm-testing",
                        "host": "cm-testing.shinycomputers.com",
                        "serviceName": "old-service",
                        "port": 3000,
                    }
                ]
            return {"domainId": "domain-cm-testing"}

        with patch("control_plane.dokploy.api.dokploy_request", side_effect=fake_dokploy_request):
            domain_id = control_plane_dokploy.ensure_compose_web_domain_route(
                host="https://dokploy.example.com",
                token="token-123",
                compose_id="compose-cm-testing",
                domain_host="cm-testing.shinycomputers.com",
                runtime_port=8069,
            )

        self.assertEqual(domain_id, "domain-cm-testing")
        self.assertEqual(
            [request["path"] for request in requests],
            ["/api/domain.byComposeId", "/api/domain.update"],
        )
        update_payload = cast("dict[str, object]", requests[1]["payload"])
        self.assertEqual(update_payload["domainId"], "domain-cm-testing")
        self.assertEqual(update_payload["composeId"], "compose-cm-testing")
        self.assertEqual(update_payload["domainType"], "compose")
        self.assertEqual(update_payload["serviceName"], "web")
        self.assertEqual(update_payload["port"], 8069)
        self.assertEqual(update_payload["https"], True)
        self.assertEqual(update_payload["certificateType"], "none")
        self.assertEqual(update_payload["path"], "/")
        self.assertEqual(update_payload["internalPath"], "/")

    def test_ensure_compose_web_domain_route_creates_missing_route(self) -> None:
        requests: list[dict[str, object]] = []

        def fake_dokploy_request(**kwargs: object) -> object:
            requests.append(dict(kwargs))
            if kwargs["path"] == "/api/domain.byComposeId":
                return []
            return {"domainId": "domain-created"}

        with patch("control_plane.dokploy.api.dokploy_request", side_effect=fake_dokploy_request):
            domain_id = control_plane_dokploy.ensure_compose_web_domain_route(
                host="https://dokploy.example.com",
                token="token-123",
                compose_id="compose-cm-testing",
                domain_host="cm-testing.shinycomputers.com",
                runtime_port=8069,
            )

        self.assertEqual(domain_id, "domain-created")
        self.assertEqual(
            [request["path"] for request in requests],
            ["/api/domain.byComposeId", "/api/domain.create"],
        )
        create_payload = cast("dict[str, object]", requests[1]["payload"])
        self.assertEqual(create_payload["host"], "cm-testing.shinycomputers.com")
        self.assertEqual(create_payload["composeId"], "compose-cm-testing")
        self.assertEqual(create_payload["serviceName"], "web")
        self.assertEqual(create_payload["port"], 8069)
        self.assertEqual(create_payload["https"], True)
        self.assertEqual(create_payload["certificateType"], "none")

    def test_ensure_compose_web_domain_route_allows_certificate_type_override(self) -> None:
        requests: list[dict[str, object]] = []

        def fake_dokploy_request(**kwargs: object) -> object:
            requests.append(dict(kwargs))
            if kwargs["path"] == "/api/domain.byComposeId":
                return []
            return {"domainId": "domain-created"}

        with patch("control_plane.dokploy.api.dokploy_request", side_effect=fake_dokploy_request):
            control_plane_dokploy.ensure_compose_web_domain_route(
                host="https://dokploy.example.com",
                token="token-123",
                compose_id="compose-cm-testing",
                domain_host="cm-testing.shinycomputers.com",
                runtime_port=8069,
                certificate_type="letsencrypt",
            )

        create_payload = cast("dict[str, object]", requests[1]["payload"])
        self.assertEqual(create_payload["certificateType"], "letsencrypt")

    def test_service_render_authz_policy_uses_explicit_policy_source(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            policy_dir = control_plane_root / "config"
            policy_dir.mkdir(parents=True)
            policy_file = policy_dir / "launchplane-authz.toml"
            policy_text = """
schema_version = 1

[[github_actions]]
repository = "cbusillo/launchplane"
workflow_refs = ["cbusillo/launchplane/.github/workflows/deploy-launchplane.yml@refs/heads/main"]
event_names = ["workflow_dispatch"]
products = ["launchplane"]
contexts = ["launchplane"]
actions = ["launchplane_service_deploy.execute"]
""".strip()
            policy_file.write_text(policy_text, encoding="utf-8")

            result = runner.invoke(
                main,
                [
                    "service",
                    "render-authz-policy",
                    "--policy-file",
                    str(policy_file),
                    "--control-plane-root",
                    str(control_plane_root),
                ],
            )

        self.assertEqual(result.exit_code, 0, msg=result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["policy_file"], str(policy_file))
        self.assertEqual(
            payload["policy_b64"],
            base64.b64encode(policy_text.encode("utf-8")).decode("ascii"),
        )
        self.assertEqual(
            payload["policy_sha256"], hashlib.sha256(policy_text.encode("utf-8")).hexdigest()
        )
        self.assertEqual(payload["github_actions_rule_count"], 1)

    def test_service_sync_bootstrap_policy_updates_live_target_env(self) -> None:
        runner = CliRunner()
        captured_env_updates: list[dict[str, object]] = []
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            policy_dir = control_plane_root / "config"
            policy_dir.mkdir(parents=True)
            policy_file = policy_dir / "launchplane-authz.toml"
            policy_text = """
schema_version = 1

[[github_actions]]
repository = "cbusillo/launchplane"
workflow_refs = ["cbusillo/launchplane/.github/workflows/deploy-launchplane.yml@refs/heads/main"]
event_names = ["workflow_dispatch"]
products = ["launchplane"]
contexts = ["launchplane"]
actions = ["launchplane_service_deploy.execute"]
""".strip()
            policy_file.write_text(policy_text, encoding="utf-8")
            policy_b64 = base64.b64encode(policy_text.encode("utf-8")).decode("ascii")

            with (
                patch(
                    "control_plane.dokploy.source.read_dokploy_config",
                    return_value=("https://dokploy.example.com", "token-123"),
                ),
                patch(
                    "control_plane.dokploy.api.fetch_dokploy_target_payload",
                    return_value=self._target_payload(
                        env_text=(
                            "DOCKER_IMAGE_REFERENCE=ghcr.io/every/launchplane@sha256:old\n"
                            "LAUNCHPLANE_POLICY_B64=dGVzdA==\n"
                            "LAUNCHPLANE_POLICY_TOML=schema_version = 1\n"
                            "LAUNCHPLANE_POLICY_FILE=/etc/launchplane/policy.toml\n"
                        ),
                    ),
                ),
                patch(
                    "control_plane.dokploy.api.update_dokploy_target_env",
                    side_effect=lambda **kwargs: captured_env_updates.append(kwargs),
                ),
            ):
                result = runner.invoke(
                    main,
                    [
                        "service",
                        "sync-bootstrap-policy",
                        "--target-type",
                        "compose",
                        "--target-id",
                        "compose-123",
                        "--policy-file",
                        str(policy_file),
                        "--control-plane-root",
                        str(control_plane_root),
                        "--apply",
                    ],
                )

        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertEqual(len(captured_env_updates), 1)
        env_text = str(captured_env_updates[0]["env_text"])
        self.assertIn(f"LAUNCHPLANE_POLICY_B64={policy_b64}", env_text)
        self.assertNotIn("LAUNCHPLANE_POLICY_TOML=", env_text)
        self.assertNotIn("LAUNCHPLANE_POLICY_FILE=", env_text)
        payload = json.loads(result.output)
        self.assertEqual(payload["status"], "ok")
        self.assertTrue(payload["changed"])
        self.assertEqual(payload["desired_policy_file"], str(policy_file))
        self.assertEqual(
            payload["desired_policy_sha256"],
            hashlib.sha256(policy_text.encode("utf-8")).hexdigest(),
        )

    def test_build_dokploy_data_workflow_script_injects_workflow_environment(self) -> None:
        script = control_plane_dokploy._build_dokploy_data_workflow_script(
            compose_app_name="opw-prod",
            database_name="opw_prod",
            filestore_path="/volumes/data/filestore",
            clear_stale_lock=False,
            data_workflow_lock_path="/volumes/data/.data_workflow_in_progress",
            workflow_environment_overrides={
                ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY: "payload-value",
                "EXTRA_WORKFLOW_VALUE": "https://opw-prod.example.com",
            },
            required_workflow_environment_keys=("ODOO_OVERRIDE_SECRET__ADDON__SHOPIFY__API_TOKEN",),
            protected_shopify_store_keys=("yps-your-part-supplier",),
        )

        self.assertIn(
            f"workflow_environment+=(-e {ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY}=payload-value)",
            script,
        )
        self.assertIn(
            "workflow_environment+=(-e EXTRA_WORKFLOW_VALUE=https://opw-prod.example.com)",
            script,
        )
        self.assertIn(
            "required_workflow_environment_keys+=(ODOO_OVERRIDE_SECRET__ADDON__SHOPIFY__API_TOKEN)",
            script,
        )
        self.assertIn(
            f"required_workflow_environment_keys+=({ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY})",
            script,
        )
        self.assertIn('docker exec         "${workflow_environment[@]}"', script)
        self.assertIn('"${script_runner_container_id}"         /bin/bash -lc', script)
        self.assertIn("odoo_instance_overrides_payload_present=true", script)
        self.assertIn("protected_shopify_store_keys+=(yps-your-part-supplier)", script)
        self.assertIn("Missing required Odoo override environment key", script)
        self.assertIn("Protected Shopify store key is not allowed on this Dokploy lane.", script)
        self.assertIn("trap exit_trap EXIT", script)
        self.assertIn("exit_trap() {", script)
        self.assertIn('local exit_status="$?"', script)
        self.assertIn('if [ "${web_was_running}" != "1" ]; then', script)
        self.assertIn('docker start "${web_container_id}" >/dev/null || true', script)
        self.assertIn("workflow_output_file=$(mktemp)", script)
        self.assertIn('workflow_pipeline_status=("${PIPESTATUS[@]}")', script)
        self.assertIn("workflow_exit_status=${workflow_pipeline_status[0]}", script)
        self.assertIn("workflow_output_status=${workflow_pipeline_status[1]}", script)
        self.assertIn("Odoo post-deploy maintenance readback markers:", script)
        self.assertIn("grep -E '^(", script)
        self.assertIn("odoo_instance_overrides_payload_present", script)
        self.assertIn("website_bootstrap_applied", script)
        self.assertIn("website_bootstrap_domain_matches_canonical", script)
        self.assertNotIn("website_bootstrap_[a-z0-9_]+", script)
        self.assertIn('if [ "$workflow_exit_status" -ne 0 ]; then', script)
        self.assertIn("start_web_container\ntrap - EXIT", script)
        self.assertNotIn("restart_web_on_success", script)
        self.assertIn('"${workflow_environment[@]}"', script)

    def test_extract_odoo_post_deploy_readback_markers_allows_only_safe_marker_values(self) -> None:
        markers = control_plane_dokploy.extract_odoo_post_deploy_readback_markers(
            {
                "logs": "\n".join(
                    (
                        "website_bootstrap_domain_matches_canonical=true",
                        "website_bootstrap_website_id=1",
                        "odoo_instance_overrides_payload_present=true",
                        "website_bootstrap_secret=token-value",
                        "website_bootstrap_secret=123456",
                        "website_bootstrap_included=false",
                        "website_bootstrap_domain_set=123",
                        "ODOO_DB_PASSWORD=secret",
                        "random_line=true",
                    )
                )
            }
        )

        self.assertEqual(
            markers,
            {
                "website_bootstrap_domain_matches_canonical": "true",
                "website_bootstrap_website_id": "1",
                "odoo_instance_overrides_payload_present": "true",
            },
        )

    def test_service_inspect_dokploy_target_fails_closed_on_missing_runtime_contract(self) -> None:
        runner = CliRunner()

        with (
            patch(
                "control_plane.dokploy.source.read_dokploy_config",
                return_value=("https://dokploy.example.com", "token-123"),
            ),
            patch(
                "control_plane.dokploy.api.fetch_dokploy_target_payload",
                return_value=self._target_payload(
                    env_text="DOCKER_IMAGE_REFERENCE=ghcr.io/example/launchplane@sha256:old\n",
                    custom_git_ssh_key_id="",
                ),
            ),
        ):
            result = runner.invoke(
                main,
                [
                    "service",
                    "inspect-dokploy-target",
                    "--target-type",
                    "compose",
                    "--target-id",
                    "compose-123",
                ],
            )

        self.assertNotEqual(result.exit_code, 0, msg=result.output)
        payload_text = result.output.split("Error:", 1)[0].strip()
        payload = json.loads(payload_text) if payload_text else {}
        self.assertIn(
            "Dokploy target uses an SSH git remote but has no customGitSSHKeyId configured.",
            payload.get("blockers", []),
        )
        self.assertIn(
            "Launchplane service target is missing LAUNCHPLANE_DATABASE_URL.",
            payload.get("blockers", []),
        )
        self.assertIn(
            "Launchplane service target is missing LAUNCHPLANE_POLICY_* or LAUNCHPLANE_POLICY_FILE. Startup fails closed without an explicit policy input.",
            payload.get("blockers", []),
        )
        self.assertIn("Launchplane service Dokploy target preflight failed", result.output)

    def test_service_deploy_dokploy_image_is_removed(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["service", "deploy-dokploy-image", "--help"])

        self.assertNotEqual(result.exit_code, 0, msg=result.output)
        self.assertIn("No such command 'deploy-dokploy-image'", result.output)


if __name__ == "__main__":
    unittest.main()
