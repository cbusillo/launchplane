import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast
import unittest
from unittest.mock import patch

import click
from click.testing import CliRunner
from pydantic import ValidationError
import yaml

from control_plane.cli import main
from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.private_health_endpoint_record import PrivateHealthEndpointRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.private_testing_transport import (
    ROUTE,
    PrivateTestingTransportRequest,
    plan_private_testing_transport,
)
from control_plane.service_auth import BearerIdentityConfig, LaunchplaneAuthzPolicy
from control_plane.storage.postgres import PostgresRecordStore
from tests.http_app_test_support import _RejectingVerifier
from tests.support.http import request as http_request
from tests.support.auth import scoped_agent_transport
from tests.support.profiles import _generic_site_profile_payload
from tests.support.stores import sqlite_database_url


class PrivateTestingTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = PostgresRecordStore(database_url=sqlite_database_url(self.root / "db.sqlite"))
        self.addCleanup(self.store.close)
        self.store.ensure_schema()
        payload = _generic_site_profile_payload()
        payload.update(public_website="none", runtime_port=8787, health_path="/health")
        payload["preview"] = {"enabled": False}
        payload["lanes"] = [
            {"instance": lane, "context": "example-site"} for lane in ("testing", "prod")
        ]
        self.profile = LaunchplaneProductProfileRecord.model_validate(payload)
        self.store.write_product_profile_record(self.profile)
        for instance in ("testing", "prod"):
            target = DokployTargetRecord(
                context="example-site",
                instance=instance,
                target_name=instance,
                target_type="compose",
                updated_at=self.profile.updated_at,
            )
            target_id = DokployTargetIdRecord(
                context="example-site",
                instance=instance,
                target_id=f"fixture-{instance}",
                updated_at=self.profile.updated_at,
            )
            self.store.write_dokploy_target_record(target)
            self.store.write_dokploy_target_id_record(target_id)
            self.store.write_provider_target_record(
                ProviderTargetRecord.from_dokploy_records(
                    target_record=target, target_id_record=target_id
                )
            )
        self.payload = {
            "context": "example-site",
            "endpoint_key": "testing-bridge",
            "private_ip": "10.77.0.9",
            "listener_port": 9443,
            "runtime_service": "bridge",
            "nginx_image": "nginx@sha256:" + "a" * 64,
            "ca_bundle_sha256": "b" * 64,
            "certificate_sha256": "c" * 64,
            "monitor_ca_bundle_path": "/run/private-trust/ca-bundle.pem",
            "receiver_ca_bundle_path": "/Users/fixture/private-trust/ca-bundle.pem",
            "trust_review": "fixture purpose-scoped private issuer review",
        }
        self.compose: dict[str, Any] = {
            "services": {
                "bridge": {"image": "fixture@sha256:" + "d" * 64, "volumes": ["state:/state"]}
            },
            "volumes": {"state": {}},
        }
        self.placement: dict[str, object] = {
            "composeId": "fixture-testing",
            "serverId": "fixture-private-server",
            "appName": "fixture-testing-app",
            "domains": [],
        }
        self.enterContext(
            patch(
                "control_plane.dokploy.source.read_dokploy_config",
                return_value=("https://provider.test", "fixture-only-token"),
            )
        )
        self.provider = self.enterContext(
            patch(
                "control_plane.dokploy.api.fetch_dokploy_target_payload",
                side_effect=lambda **_kwargs: deepcopy(self.placement),
            )
        )
        self.converted = self.enterContext(
            patch(
                "control_plane.dokploy.compose.fetch_dokploy_converted_compose_file",
                side_effect=lambda **_kwargs: yaml.safe_dump(self.compose),
            )
        )

    def plan(self, **changes: object) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            plan_private_testing_transport(
                self.store,
                root=self.root,
                product="example-site",
                request=PrivateTestingTransportRequest.model_validate({**self.payload, **changes}),
            ),
        )

    def test_private_no_website_plan_and_exact_readback_without_mutations(self) -> None:
        before = self.store.read_dokploy_target_record(
            context_name="example-site", instance_name="prod"
        )
        with patch("control_plane.dokploy.api.dokploy_request") as write:
            result = self.plan()
            self.assertFalse(result["applied"])
            self.assertFalse(result["transport_verified"])
            self.assertFalse(result["activation_supported"])
            endpoint = PrivateHealthEndpointRecord.model_validate(
                {**result["endpoint"], "updated_at": self.profile.updated_at}
            )
            self.assertEqual(endpoint.instance, "testing")
            self.assertEqual(
                result["server_url"].replace("wss:", "https:").split("/agent-")[0],
                endpoint.url.removesuffix("/health"),
            )
            self.assertEqual(self.plan()["plan_sha256"], result["plan_sha256"])
            # Simulate a separately authorized provider activation, not an apply API.
            for section, additions in result["overlay"].items():
                self.compose.setdefault(section, {}).update(additions)
            readback = self.plan(mode="readback", expected_plan_sha256=result["plan_sha256"])
            self.assertTrue(readback["configuration_matches"])
            self.assertFalse(readback["certificate_verified"])
            write.assert_not_called()
        self.assertEqual(
            before,
            self.store.read_dokploy_target_record(
                context_name="example-site", instance_name="prod"
            ),
        )
        self.assertEqual(self.store.list_private_health_endpoint_records(), ())

    def test_readback_rejects_absent_partial_or_changed_configuration_and_stale_review(
        self,
    ) -> None:
        original = self.plan()
        with self.assertRaises(ValueError):
            self.plan(mode="readback", expected_plan_sha256=original["plan_sha256"])
        overlay = original["overlay"]
        self.compose["services"].update(overlay["services"])
        with self.assertRaises(ValueError):
            self.plan(mode="readback", expected_plan_sha256=original["plan_sha256"])
        for section, additions in overlay.items():
            self.compose.setdefault(section, {}).update(additions)
        self.compose["services"]["bridge"]["image"] = "fixture@sha256:" + "e" * 64
        with self.assertRaises(ValueError):
            self.plan(mode="readback", expected_plan_sha256=original["plan_sha256"])
        with self.assertRaises(ValueError):
            self.plan(mode="readback")

    def test_incomplete_source_or_provider_failure_can_be_replanned_without_effects(self) -> None:
        original = self.plan()
        self.converted.side_effect = click.ClickException("fixture timeout")
        with self.assertRaises(click.ClickException):
            self.plan()
        self.converted.side_effect = lambda **_kwargs: yaml.safe_dump(self.compose)
        self.assertEqual(self.plan()["plan_sha256"], original["plan_sha256"])
        self.compose = {}
        with self.assertRaises(ValueError):
            self.plan()

    def test_production_wrong_context_and_shared_target_are_refused(self) -> None:
        for changes in ({"instance": "prod"}, {"context": "another-product"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.plan(**changes)
        prod = self.store.read_dokploy_target_id_record(
            context_name="example-site", instance_name="prod"
        )
        self.store.write_dokploy_target_id_record(
            prod.model_copy(update={"target_id": "fixture-testing"})
        )
        with self.assertRaises(ValueError):
            self.plan()

    def test_public_listener_and_config_injection_are_refused_before_provider_reads(self) -> None:
        for changes in (
            {"private_ip": "0.0.0.0"},
            {"private_ip": "8.8.8.8"},
            {"private_ip": "127.0.0.1"},
            {"private_ip": "100.64.0.1"},
            {"private_ip": "192.0.2.1"},
            {"runtime_service": "bridge;evil"},
            {"monitor_ca_bundle_path": "/run/../production.pem"},
            {"nginx_image": "nginx:latest"},
            {"mode": "apply"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.plan(**changes)
        self.provider.assert_not_called()

    def test_shared_network_ports_and_storage_are_refused(self) -> None:
        clean = deepcopy(self.compose)
        changes = (
            {"ports": ["443:8787"]},
            {"network_mode": "host"},
            {"networks": ["production"]},
            {"volumes": ["/production:/state"]},
            {"volumes": ["/var/run/docker.sock:/var/run/docker.sock"]},
            {"volumes_from": ["prod"]},
            {"privileged": True},
            {"labels": {"traefik.enable": "true"}},
        )
        for change in changes:
            self.compose = deepcopy(clean)
            self.compose["services"]["bridge"].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.plan()
        self.compose = deepcopy(clean)
        self.compose["volumes"] = {"state": {"external": True, "name": "production-state"}}
        with self.assertRaises(ValueError):
            self.plan()

    def test_provider_or_source_drift_invalidates_review(self) -> None:
        original = self.plan()
        self.placement["serverId"] = "another-host"
        with self.assertRaises(ValueError):
            self.plan(expected_plan_sha256=original["plan_sha256"])
        self.placement["composeId"] = "production-target"
        with self.assertRaises(ValueError):
            self.plan()

    def test_cli_validates_before_http_and_forwards_only_inert_mode(self) -> None:
        path = self.root / "plan.json"
        path.write_text(json.dumps(self.payload))
        runner = CliRunner()
        args = [
            "ingress",
            "private-testing-plan",
            "--service-url",
            "https://launchplane.test",
            "--product",
            "example-site",
            "--payload-file",
            str(path),
        ]
        with patch.dict(os.environ, {"LAUNCHPLANE_SERVICE_TOKEN": "fictional-fixture-only"}):
            with patch(
                "control_plane.cli_ingress._post_launchplane_service_json",
                return_value={"result": {"applied": False}},
            ) as post:
                result = runner.invoke(main, args)
                self.assertEqual(result.exit_code, 0, result.output)
                self.assertEqual(post.call_args.kwargs["payload"]["mode"], "dry-run")
                self.assertNotIn("fictional-fixture-only", result.output)
                path.write_text(json.dumps({**self.payload, "mode": "apply"}))
                result = runner.invoke(main, args)
                self.assertNotEqual(result.exit_code, 0)
                self.assertEqual(post.call_count, 1)

    def test_service_plan_uses_authorization_and_refuses_apply_or_production(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=LaunchplaneAuthzPolicy(schema_version=2),
            record_store_factory=lambda: self.store,
            control_plane_root_path=self.root,
            bearer_identity_config=BearerIdentityConfig(
                local_operator_token="fictional-test-operator",
                local_operator_subject="local-owner-agent",
                local_operator_token_label="local-owner-write",
            ),
        )
        headers = {"Authorization": "Bearer fictional-test-operator"}

        async def exercise() -> None:
            denied = await http_request(
                app, "POST", ROUTE.format(product="example-site"), payload=self.payload
            )
            self.assertEqual(denied.status_code, 401)
            for change in ({"mode": "apply"}, {"instance": "prod"}):
                response = await http_request(
                    app,
                    "POST",
                    ROUTE.format(product="example-site"),
                    headers=headers,
                    payload={**self.payload, **change},
                )
                self.assertEqual(response.status_code, 400)
            response = await http_request(
                app,
                "POST",
                ROUTE.format(product="example-site"),
                headers=headers,
                payload=self.payload,
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertFalse(response.json()["result"]["applied"])

        asyncio.run(exercise())

    def test_scoped_caller_without_lane_plan_authority_cannot_read_provider(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=LaunchplaneAuthzPolicy(schema_version=2),
            record_store_factory=lambda: self.store,
            control_plane_root_path=self.root,
            bearer_identity_config=BearerIdentityConfig(
                local_operator_token="fictional-scoped-token",
                local_operator_subject="fixture-client",
                local_operator_token_label="fixture-client",
            ),
        )
        with scoped_agent_transport():
            response = asyncio.run(
                http_request(
                    app,
                    "POST",
                    ROUTE.format(product="example-site"),
                    payload=self.payload,
                    headers={"Authorization": "Bearer fictional-scoped-token"},
                )
            )
        self.assertEqual(response.status_code, 403)
        self.provider.assert_not_called()
