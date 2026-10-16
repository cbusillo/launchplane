import asyncio
from copy import deepcopy
import json
import os
import subprocess
import sys
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
from control_plane.dokploy.api import fetch_dokploy_target_payload as actual_fetch_target
from control_plane.dokploy.compose import (
    fetch_dokploy_converted_compose_file as actual_fetch_compose,
)
from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord, PublicHostEffect
from control_plane.contracts.private_health_endpoint_record import PrivateHealthEndpointRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.private_testing_transport import (
    ROUTE,
    PrivateTestingTransportRequest,
    nginx_entrypoint,
    plan_private_testing_transport,
)
from control_plane.service_auth import BearerIdentityConfig, LaunchplaneAuthzPolicy
from control_plane.storage.postgres import PostgresRecordStore
from tests.http_app_test_support import _RejectingVerifier
from tests.support.http import request as http_request
from tests.support.auth import scoped_agent_transport
from tests.support.profiles import _generic_site_profile_payload
from tests.support.stores import sqlite_database_url


class PrivateTestingEntrypointTests(unittest.TestCase):
    def test_entrypoint_materializes_its_own_file_and_never_interprets_payload(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "must-not-execute"
            payload = {"literal": f"$(touch {marker}) `touch {marker}` $runtime_backend\n'quoted'"}
            stub = root / "fixture program.py"
            stub.write_text(
                "import json, sys\nfrom pathlib import Path\n"
                "source = Path(sys.argv[sys.argv.index('-c') + 1])\n"
                "print(json.dumps(json.loads(source.read_text())))\n"
            )
            result = subprocess.run(
                nginx_entrypoint(
                    json.dumps(payload),
                    configuration_file=str(root / "own generated config"),
                    nginx_binary=sys.executable,
                    nginx_arguments=(str(stub),),
                ),
                env={"PATH": os.defpath},
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(json.loads(result.stdout), payload)
            self.assertFalse(marker.exists())


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
            "composeType": "docker-compose",
            "isolatedDeployment": False,
            "randomize": False,
            "serviceNetworks": [],
            "mounts": [],
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
                side_effect=self.rendered_compose,
            )
        )

    def rendered_compose(self, **_kwargs: object) -> str:
        document = deepcopy(self.compose)
        document.setdefault("name", self.placement["appName"])
        document.setdefault("networks", {"dokploy-network": {"external": True}})
        for service_name, service in document.get("services", {}).items():
            service.setdefault(
                "labels",
                {
                    "dokploy.application.id": self.placement["composeId"],
                    "dokploy.service": service_name,
                },
            )
        return yaml.safe_dump(document)

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
        result = self.plan()
        self.assertFalse(result["applied"])
        self.assertFalse(result["transport_verified"])
        self.assertFalse(result["activation_supported"])
        endpoint = PrivateHealthEndpointRecord.model_validate(
            {**result["endpoint_proposal"], "updated_at": self.profile.updated_at}
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
        self.converted.side_effect = self.rendered_compose
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

    def test_installed_listener_can_be_rereviewed_after_runtime_redeploy(self) -> None:
        original = self.plan()
        for section, additions in original["overlay"].items():
            self.compose.setdefault(section, {}).update(additions)
        self.compose["services"]["bridge"]["image"] = "fixture@sha256:" + "f" * 64
        target = self.store.read_dokploy_target_record(
            context_name="example-site", instance_name="testing"
        )
        self.store.write_dokploy_target_record(
            target.model_copy(update={"source_label": "redeploy"})
        )
        refreshed = self.plan()
        self.assertNotEqual(refreshed["plan_sha256"], original["plan_sha256"])
        self.assertTrue(
            self.plan(mode="readback", expected_plan_sha256=refreshed["plan_sha256"])[
                "configuration_matches"
            ]
        )

    def test_host_sharing_options_and_unknown_compose_extensions_are_refused(self) -> None:
        clean = deepcopy(self.compose)
        for key, value in (
            ("pid", "host"),
            ("ipc", "host"),
            ("userns_mode", "host"),
            ("cap_add", ["NET_ADMIN"]),
            ("security_opt", ["seccomp:unconfined"]),
            ("cgroup", "host"),
            ("runtime", "custom-runtime"),
            ("env_file", ["/prod.env"]),
        ):
            self.compose = deepcopy(clean)
            self.compose["services"]["bridge"][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.plan()
        self.compose = deepcopy(clean)
        self.compose["name"] = "production-project"
        with self.assertRaises(ValueError):
            self.plan()

    def test_public_declarations_and_provider_domains_refuse_private_plan(self) -> None:
        self.store.write_product_profile_record(
            self.profile.model_copy(update={"public_website": "required"})
        )
        with self.assertRaises(ValueError):
            self.plan()
        self.store.write_product_profile_record(self.profile)
        target = self.store.read_dokploy_target_record(
            context_name="example-site", instance_name="testing"
        )
        for changes in (
            {"domains": ("public.example.test",)},
            {"public_hosts": ("public.example.test",)},
            {
                "pending_public_host_effects": (
                    PublicHostEffect(
                        host="public.example.test",
                        provider_host="https://provider.test",
                        target_id="fixture-testing",
                        runtime_port=self.profile.runtime_port,
                    ),
                )
            },
        ):
            self.store.write_dokploy_target_record(target.model_copy(update=changes))
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.plan()
        self.store.write_dokploy_target_record(target)
        for domains in (None, [{"host": "public.example.test"}]):
            self.placement["domains"] = domains
            with self.subTest(domains=domains), self.assertRaises(ValueError):
                self.plan()

    def test_planning_provider_transport_is_get_only(self) -> None:
        self.provider.side_effect = actual_fetch_target
        self.converted.side_effect = actual_fetch_compose
        reads: list[str] = []

        def provider(**kwargs: Any) -> Any:
            self.assertEqual(kwargs.get("method", "GET"), "GET")
            path = kwargs["path"]
            reads.append(path)
            if path == "/api/compose.one":
                return deepcopy(self.placement)
            if path == "/api/compose.getConvertedCompose":
                return self.rendered_compose()
            self.fail(f"Unexpected provider operation: {path}")

        with patch("control_plane.dokploy.api.dokploy_request", side_effect=provider):
            self.assertFalse(self.plan()["applied"])
        self.assertTrue(reads)

    def test_swarm_unknown_mode_and_provider_isolation_overrides_are_refused(self) -> None:
        placement = deepcopy(self.placement)
        for change in (
            {"composeType": "stack"},
            {"composeType": None},
            {"isolatedDeployment": True},
            {"randomize": True},
            {"serviceNetworks": [{"networkId": "production"}]},
            {"mounts": [{"source": "/production"}]},
            {"mounts": None},
        ):
            self.placement = {**placement, **change}
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.plan()

    def test_foreign_provider_metadata_and_attached_shared_network_are_refused(self) -> None:
        original = deepcopy(self.compose)
        for change in (
            {"labels": {"dokploy.application.id": "foreign", "dokploy.service": "bridge"}},
            {"labels": {"dokploy.application.id": "fixture-testing", "dokploy.service": "foreign"}},
            {"labels": ["malformed"]},
            {"networks": ["dokploy-network"]},
        ):
            self.compose = deepcopy(original)
            self.compose["services"]["bridge"].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.plan()
        self.compose = deepcopy(original)
        self.compose["networks"] = {
            "dokploy-network": {"external": True},
            "foreign": {"external": True},
        }
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
                path.write_text(json.dumps({**self.payload, "mode": "apply"}))
                result = runner.invoke(main, args)
                self.assertNotEqual(result.exit_code, 0)
                self.assertEqual(post.call_count, 1)
                path.write_text(json.dumps(self.payload))
                args[args.index("--product") + 1] = "-invalid"
                self.assertNotEqual(runner.invoke(main, args).exit_code, 0)
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
            self.converted.side_effect = click.ClickException("fixture provider timeout")
            unavailable = await http_request(
                app,
                "POST",
                ROUTE.format(product="example-site"),
                headers=headers,
                payload=self.payload,
            )
            self.assertEqual(unavailable.status_code, 503)

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
