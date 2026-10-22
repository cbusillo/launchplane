import socket
from urllib import request as urllib_request
import configparser
import importlib
import os
import base64
import subprocess
from dataclasses import replace
from email.message import Message
from pathlib import Path
from typing import Any, cast

from control_plane.contracts.runtime_identity import RUNTIME_IDENTITY_ENV_KEY
from control_plane import odoo_shopify_identity as comparison
from control_plane import odoo_runtime_reads as reads

import contextlib
import io
import json
import sys
import unittest
from unittest.mock import MagicMock, patch

from control_plane import odoo_shopify_identity_probe as probe


class ShopifyProbeTests(unittest.TestCase):
    def run_probe(self, payload: dict[str, object], response: object) -> dict[str, str]:
        self.opener = MagicMock()
        stream = self.opener.open.return_value.__enter__.return_value
        stream.read.return_value = json.dumps(response).encode()
        with patch.object(probe, "build_opener", return_value=self.opener):
            return probe.read_identity(payload)

    def test_identity_only_query_returns_hashes_without_the_token_or_store_values(self) -> None:
        result = self.run_probe(
            {
                "domain": "testing-store.myshopify.com",
                "api_version": "2026-01",
                "token": "fixture-secret",
            },
            {
                "data": {
                    "shop": {
                        "id": "gid://shopify/Shop/123",
                        "myshopifyDomain": "testing-store.myshopify.com",
                    }
                }
            },
        )
        self.assertEqual(result["status"], "verified")
        request = self.opener.open.call_args.args[0]
        self.assertEqual(json.loads(request.data), {"query": probe.QUERY})
        self.assertEqual(self.opener.open.call_count, 1)
        for private in ("fixture-secret", "gid://shopify/Shop/123", "testing-store.myshopify.com"):
            self.assertNotIn(private, json.dumps(result))

    def test_untrusted_destinations_are_refused_before_sending_the_token(self) -> None:
        for domain in (
            "127.0.0.1",
            "shop.myshopify.com.evil.invalid",
            "shop.myshopify.com/path",
            "shop.myshopify.com@evil.invalid",
        ):
            with self.subTest(domain=domain):
                result = self.run_probe(
                    {"domain": domain, "api_version": "2026-01", "token": "fixture-secret"}, {}
                )
                self.assertEqual(result["status"], "unavailable")
                self.opener.open.assert_not_called()

    def test_missing_malformed_or_error_response_cannot_prove_identity(self) -> None:
        for response in (
            {},
            {"errors": [{"message": "fixture-secret"}]},
            {"data": {"shop": {"id": "bad", "myshopifyDomain": "testing-store.myshopify.com"}}},
        ):
            with self.subTest(response=response):
                self.assertEqual(
                    self.run_probe(
                        {
                            "domain": "testing-store.myshopify.com",
                            "api_version": "2026-01",
                            "token": "fixture-secret",
                        },
                        response,
                    )["status"],
                    "unavailable",
                )

    def test_program_suppresses_private_exceptions(self) -> None:
        with (
            patch.object(sys, "stdin", io.StringIO('{"token":"fixture-secret"}')),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            probe.main()
        self.assertNotIn("fixture-secret", output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["status"], "unavailable")


class ShopifyComparisonTests(unittest.TestCase):
    def setUp(self) -> None:
        from tests.test_odoo_import_diagnostics import DiagnosticFixture, STAMP
        from control_plane.contracts.odoo_instance_override_record import OdooInstanceOverrideRecord
        from control_plane.contracts.dokploy_target_record import (
            DokployTargetStaffTestingHold,
            DokployTargetIntegrationAllowance,
        )
        from control_plane import odoo_shopify_identity as comparison

        self.comparison = comparison
        self.fixture = DiagnosticFixture()
        self.store = self.fixture.store
        target = self.store.read_dokploy_target_record.return_value
        self.target = target.model_copy(
            update={
                "policies": target.policies.model_copy(
                    update={
                        "staff_testing_hold": DokployTargetStaffTestingHold(
                            reason="fixture", purpose="isolation_staging"
                        ),
                        "integration_allowances": (
                            DokployTargetIntegrationAllowance(
                                integration="shopify", kind="dev_store", reason="fixture"
                            ),
                        ),
                    }
                )
            }
        )
        self.store.read_dokploy_target_record.return_value = self.target
        self.store.list_dokploy_target_id_records.return_value = (
            self.store.read_dokploy_target_id_record.return_value,
        )
        self.settings = OdooInstanceOverrideRecord.model_validate(
            {
                "context": self.fixture.context,
                "instance": "testing",
                "updated_at": STAMP,
                "addon_settings": [
                    {
                        "addon": "shopify",
                        "setting": key,
                        "value": {"source": "literal", "value": value},
                    }
                    for key, value in {
                        "shop_url_key": "fixture-testing",
                        "api_version": "2026-01",
                        "test_store": True,
                    }.items()
                ]
                + [
                    {
                        "addon": "shopify",
                        "setting": key,
                        "value": {"source": "secret_binding", "secret_binding_id": key},
                    }
                    for key in ("api_token", "webhook_key")
                ],
            }
        )
        self.production = self.settings.model_copy(
            update={
                "instance": "prod",
                "addon_settings": (
                    self.settings.addon_settings[0].model_copy(
                        update={
                            "value": self.settings.addon_settings[0].value.model_copy(
                                update={"value": "fixture-production"}
                            )
                        }
                    ),
                ),
            }
        )
        self.store.read_odoo_instance_override_record.side_effect = lambda **kw: (
            self.settings if kw["instance_name"] == "testing" else self.production
        )
        self.bindings = {}
        for key, original in zip(("api_token", "webhook_key"), self.fixture.bindings):
            self.bindings[key] = original.model_copy(
                update={
                    "binding_id": key,
                    "binding_key": "ODOO_OVERRIDE_SECRET__ADDON__SHOPIFY__" + key.upper(),
                }
            )
        self.store.list_secret_bindings.side_effect = lambda **kw: tuple(self.bindings.values())
        self.store.read_secret_record.side_effect = lambda secret_id: next(
            record for record in self.fixture.records if record.secret_id == secret_id
        )
        self.production_runtime_identity = self.fixture.identity.model_copy(
            update={"instance": "prod"}
        )
        self.store.read_environment_inventory.side_effect = lambda **kw: (
            self.fixture.inventory
            if kw["instance_name"] == "testing"
            else self.fixture.inventory.model_copy(
                update={"instance": "prod", "runtime_identity": self.production_runtime_identity}
            )
        )
        self.environment = {
            "PLATFORM_INSTANCE": "testing",
            RUNTIME_IDENTITY_ENV_KEY: self.fixture.identity.model_dump_json(),
        }
        self.real_runtime_identity = comparison.runtime_identity
        self.observation: dict[str, str] = {}
        network = patch.object(
            socket, "create_connection", side_effect=AssertionError("No network in fixture")
        )
        network.start()
        self.addCleanup(network.stop)
        self.calls = []
        for patcher in (
            patch.object(
                reads,
                "connect_runtime",
                side_effect=lambda **kw: (
                    self.fixture.connection
                    if kw["selection"].identity.instance == "testing"
                    else replace(self.fixture.connection, selection=kw["selection"])
                ),
            ),
            patch.object(
                reads,
                "inspect_runtime_container",
                side_effect=lambda *args: (
                    self.environment
                    if args[-1].instance == "testing"
                    else {
                        "PLATFORM_INSTANCE": "prod",
                        "ODOO_DB_NAME": "fixture-db",
                        RUNTIME_IDENTITY_ENV_KEY: self.production_runtime_identity.model_dump_json(),
                    }
                ),
            ),
            patch.object(comparison, "_decrypt_secret_value", return_value="fixture-secret"),
            patch.object(
                comparison,
                "runtime_identity",
                side_effect=lambda connection, selected, *args: (
                    self.observation
                    if selected
                    else {
                        "status": "verified",
                        "domain_sha256": comparison.digest("fixture-production.myshopify.com"),
                        "shop_id_sha256": comparison.digest("gid://shopify/Shop/456"),
                    }
                ),
            ),
        ):
            self.calls.append(patcher.start())
            self.addCleanup(patcher.stop)
        self.observation = {
            "status": "verified",
            "domain_sha256": comparison.digest("fixture-testing.myshopify.com"),
            "shop_id_sha256": comparison.digest("gid://shopify/Shop/123"),
        }

    def run_comparison(self) -> comparison.ShopifyIdentityResult:
        from pathlib import Path

        return self.comparison.compare_identity(
            store=self.store,
            profile=self.fixture.profile,
            control_plane_root=Path("."),
            database_url=None,
        )

    def test_held_testing_can_prove_separation_without_provider_environment_delivery(self) -> None:
        result = self.run_comparison()
        self.assertEqual(result.separation, "separate")
        self.assertEqual(
            result.production_domain_sha256,
            self.comparison.digest("fixture-production.myshopify.com"),
        )
        self.assertFalse(result.applied)
        self.assertNotIn("fixture-secret", result.model_dump_json())
        self.assertEqual(
            self.store.read_dokploy_target_record.return_value.policies.staff_testing_hold.purpose,
            "isolation_staging",
        )
        reads_seen = self.calls[-1].call_args_list
        testing_index = next(
            index for index, call in enumerate(reads_seen) if call.args[1] is not None
        )
        self.assertTrue(any(call.args[1] is None for call in reads_seen[testing_index + 1 :]))

    def test_equal_or_unavailable_identity_never_proves_separation(self) -> None:
        for observation in (
            {"status": "unavailable"},
            {"status": "token_rejected"},
            {
                **self.observation,
                "domain_sha256": self.comparison.digest("fixture-production.myshopify.com"),
            },
        ):
            with self.subTest(observation=observation):
                self.observation = observation
                self.assertNotEqual(self.run_comparison().separation, "separate")

    def test_staff_hold_missing_production_or_foreign_target_prevents_probe(self) -> None:
        self.store.read_dokploy_target_record.return_value = self.target.model_copy(
            update={
                "policies": self.target.policies.model_copy(update={"staff_testing_hold": None})
            }
        )
        self.assertEqual(self.run_comparison().separation, "unknown")
        self.calls[-1].assert_not_called()

    def test_same_shop_id_under_different_domains_never_proves_separation(self) -> None:
        self.observation["shop_id_sha256"] = self.comparison.digest("gid://shopify/Shop/456")
        self.assertNotEqual(self.run_comparison().separation, "separate")

    def test_authority_drift_discards_success(self) -> None:
        def observe(connection: object, selected: object, *args: object) -> dict[str, str]:
            if selected is None:
                return {
                    "status": "verified",
                    "domain_sha256": comparison.digest("fixture-production.myshopify.com"),
                    "shop_id_sha256": comparison.digest("gid://shopify/Shop/456"),
                }
            self.settings = self.settings.model_copy(update={"source_label": "changed"})
            return self.observation

        self.calls[-1].side_effect = observe
        self.assertEqual(self.run_comparison().separation, "unknown")

    def test_foreign_or_production_classified_secret_prevents_decryption(self) -> None:
        self.bindings["api_token"] = self.bindings["api_token"].model_copy(
            update={"declared_secret_class": "prod_only"}
        )
        self.assertEqual(self.run_comparison().separation, "unknown")
        self.calls[-2].assert_not_called()

    def test_unsafe_recipient_or_credential_authority_never_decrypts_or_probes(self) -> None:
        original_bindings = dict(self.bindings)
        original_record_read = self.store.read_secret_record.side_effect
        original_versions = dict(self.fixture.versions)
        original_target_ids = self.store.list_dokploy_target_id_records.return_value
        for kind in (
            "target_alias",
            "credential_shared",
            "missing_allowance",
            "staff_hold",
            "missing_production",
            "record_context",
            "record_scope",
            "version_owner",
        ):
            with self.subTest(kind=kind):
                self.store.read_dokploy_target_record.return_value = self.target
                self.store.list_dokploy_target_id_records.return_value = original_target_ids
                self.store.read_product_profile_record.return_value = self.fixture.profile
                self.store.list_product_profile_records.return_value = (self.fixture.profile,)
                self.bindings = dict(original_bindings)
                self.fixture.versions = dict(original_versions)
                self.store.read_secret_record.side_effect = original_record_read
                self.calls[-2].reset_mock()
                self.calls[-1].reset_mock()
                if kind == "target_alias":
                    self.store.list_dokploy_target_id_records.return_value = (
                        *original_target_ids,
                        original_target_ids[0].model_copy(update={"instance": "prod"}),
                    )
                elif kind == "credential_shared":
                    self.bindings["foreign"] = self.bindings["api_token"].model_copy(
                        update={
                            "binding_id": "foreign",
                            "instance": "prod",
                            "declared_secret_class": "prod_only",
                        }
                    )
                elif kind in {"missing_allowance", "staff_hold"}:
                    changes = (
                        {"integration_allowances": ()}
                        if kind == "missing_allowance"
                        else {
                            "staff_testing_hold": self.target.policies.staff_testing_hold.model_copy(
                                update={"purpose": "staff_testing"}
                            )
                        }
                    )
                    self.store.read_dokploy_target_record.return_value = self.target.model_copy(
                        update={"policies": self.target.policies.model_copy(update=changes)}
                    )
                elif kind == "missing_production":
                    profile = self.fixture.profile.model_copy(
                        update={
                            "lanes": tuple(
                                lane
                                for lane in self.fixture.profile.lanes
                                if lane.instance != "prod"
                            )
                        }
                    )
                    self.store.read_product_profile_record.return_value = profile
                    self.store.list_product_profile_records.return_value = (profile,)
                elif kind in {"record_context", "record_scope"}:
                    changes = (
                        {"context": "foreign"}
                        if kind == "record_context"
                        else {"scope": "context", "instance": ""}
                    )
                    self.store.read_secret_record.side_effect = lambda secret_id: (
                        original_record_read(secret_id).model_copy(update=changes)
                    )
                else:
                    version_id = original_record_read(
                        self.bindings["api_token"].secret_id
                    ).current_version_id
                    self.fixture.versions[version_id] = self.fixture.versions[
                        version_id
                    ].model_copy(update={"secret_id": "foreign"})
                self.assertEqual(self.run_comparison().separation, "unknown")
                self.calls[-2].assert_not_called()
                self.calls[-1].assert_not_called()

    def execute_program(self, program: str, input_text: str, argv: list[str] | None = None) -> str:
        with (
            patch.object(sys, "stdin", io.StringIO(input_text)),
            patch.object(sys, "argv", argv or ["probe"]),
            contextlib.redirect_stdout(io.StringIO()) as output,
            patch.dict(sys.modules, {"control_plane.odoo_instance_overrides": None}),
        ):
            exec(compile(program, "<identity-probe>", "exec"), {"__name__": "__main__"})
        return output.getvalue()

    def test_real_remote_program_chain_consumes_token_only_on_stdin_and_detects_drift(self) -> None:
        commands = []
        inspections = 0
        drift = False
        current_environment = dict(self.environment)

        def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            nonlocal inspections
            commands.append(command)
            self.assertNotIn("fixture-secret", repr(command))
            if command[:2] == ["docker", "inspect"]:
                inspections += 1
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=json.dumps(
                        [
                            {
                                "Id": self.fixture.connection.evidence.container_id,
                                "Config": {
                                    "Image": self.fixture.identity.image_reference,
                                    "Labels": {
                                        "com.docker.compose.project": self.fixture.connection.app_name,
                                        "com.docker.compose.service": "web",
                                    },
                                    "Env": [
                                        key + "=" + value
                                        for key, value in current_environment.items()
                                    ],
                                },
                                "State": {
                                    "Running": True,
                                    "Pid": inspections
                                    if drift
                                    and current_environment["PLATFORM_INSTANCE"] == "testing"
                                    else 1,
                                },
                            }
                        ]
                    ),
                )
            with patch.dict(os.environ, current_environment, clear=True):
                return subprocess.CompletedProcess(
                    command, 0, stdout=self.execute_program(command[-1], kwargs["input"])
                )

        def remote(**kwargs: Any) -> None:
            nonlocal current_environment
            identity = kwargs["payload"]["identity"]
            current_environment = {
                "PLATFORM_INSTANCE": identity["instance"],
                "ODOO_DB_NAME": "fixture-db",
                RUNTIME_IDENTITY_ENV_KEY: json.dumps(identity),
            }
            encoded = base64.b64encode(json.dumps(kwargs["payload"]).encode()).decode()
            output = self.execute_program(kwargs["program"], "", ["host", encoded, "FRAME:"])
            kwargs["receive"](json.loads(output.removeprefix("FRAME:")))

        database = MagicMock()
        database.connect.return_value.cursor.return_value.__enter__.return_value.fetchmany.return_value = [
            ("shopify.shop_url_key", "https://admin.shopify.com/store/fixture-production/settings"),
            ("shopify.api_token", "fixture-production-secret"),
            ("shopify.api_version", "2026-01"),
        ]
        opener = MagicMock()

        def open_request(request: Any, **kwargs: Any) -> Any:
            production = request.get_header("X-shopify-access-token") == "fixture-production-secret"
            response = MagicMock()
            response.__enter__.return_value.read.return_value = json.dumps(
                {
                    "data": {
                        "shop": {
                            "id": "gid://shopify/Shop/456"
                            if production
                            else "gid://shopify/Shop/123",
                            "myshopifyDomain": "fixture-production.myshopify.com"
                            if production
                            else "fixture-testing.myshopify.com",
                        }
                    }
                }
            ).encode()
            return response

        opener.open.side_effect = open_request
        with (
            patch.object(self.comparison, "runtime_identity", self.real_runtime_identity),
            patch.object(configparser.ConfigParser, "read", side_effect=lambda path: [path]),
            patch.object(
                configparser.ConfigParser,
                "__getitem__",
                return_value={
                    "db_name": "fixture-db",
                    "db_host": "database",
                    "db_user": "fixture-user",
                    "db_password": "fixture-production-secret",
                },
            ),
            patch.object(importlib, "import_module", return_value=database),
            patch.object(self.comparison, "_read_remote", side_effect=remote),
            patch.object(subprocess, "run", side_effect=run),
            patch.object(urllib_request, "build_opener", return_value=opener),
        ):
            self.assertEqual(self.run_comparison().separation, "separate")
            testing_requests = [
                call
                for call in opener.open.call_args_list
                if call.args[0].get_header("X-shopify-access-token") == "fixture-secret"
            ]
            self.assertEqual(len(testing_requests), 1)
            drift = True
            self.assertEqual(self.run_comparison().separation, "unknown")
        self.assertTrue(
            all(command[:2] in (["docker", "inspect"], ["docker", "exec"]) for command in commands)
        )


class ShopifyHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_requires_both_lane_reads_and_rejects_overrides(self) -> None:
        from fastapi import FastAPI, HTTPException
        from control_plane.http_routes.odoo_runtime import (
            OdooRuntimeReadDependencies,
            register_odoo_runtime_read_routes,
        )
        from control_plane.http_routes.support import ReadRouteDependencies, ApiRouteRegistrar
        from tests.support.auth import _identity
        from tests.support.http import request
        from tests.test_odoo_import_diagnostics import DiagnosticFixture

        fixture = DiagnosticFixture()
        allowed = MagicMock(return_value=True)

        def error(**kwargs: Any) -> HTTPException:
            return HTTPException(kwargs["status_code"], detail={"code": kwargs["code"]})

        common = ReadRouteDependencies(
            read_identity=lambda: _identity(),
            get_record_store=lambda: fixture.store,
            next_trace_id=lambda: "fixture-trace",
            authorization_allows=allowed,
            http_error=error,
            error_response_model=comparison.ShopifyIdentityResult,
        )
        app = FastAPI()
        register_odoo_runtime_read_routes(
            cast(ApiRouteRegistrar, app),
            dependencies=OdooRuntimeReadDependencies(
                common=common, control_plane_root=Path("."), database_url=None
            ),
        )
        path = comparison.ROUTE.format(product=fixture.profile.product, environment="testing")
        with patch.object(
            comparison, "compare_identity", return_value=comparison.ShopifyIdentityResult()
        ) as execute:
            response = await request(app, "GET", path)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(
                {call.kwargs["target"].instances for call in allowed.call_args_list},
                {("testing",), ("prod",)},
            )
            execute.reset_mock()
            for bad in (
                path.replace("/testing/", "/prod/"),
                path + "?token=fixture-secret",
                path + "?query=mutation",
            ):
                self.assertEqual((await request(app, "GET", bad)).status_code, 400)
            self.assertEqual((await request(app, "POST", path, payload={})).status_code, 405)
            for refused in ("testing", "prod"):
                allowed.side_effect = lambda **kw: kw["target"].instances != (refused,)
                self.assertEqual((await request(app, "GET", path)).status_code, 403)
            execute.assert_not_called()


class ShopifyCallerTests(unittest.TestCase):
    def test_caller_exits_unsuccessfully_without_complete_separation_and_hides_errors(self) -> None:
        import argparse
        import runpy
        import urllib.error

        script = runpy.run_path(
            str(Path(__file__).resolve().parents[1] / "scripts/read-shopify-identity.py"),
            run_name="caller_test",
        )
        transport = {
            "resolve_settings": MagicMock(
                return_value={"service_url": "https://service.invalid", "token": "fixture-private"}
            ),
            "validate_service_url": MagicMock(),
            "request_launchplane_read": MagicMock(),
        }
        args = argparse.Namespace(
            operator_helper=Path(__file__).resolve().parents[1]
            / "scripts/read-shopify-identity.py",
            product="fixture-product",
            config=None,
            env_config=None,
            url=None,
        )
        with (
            patch.object(argparse.ArgumentParser, "parse_args", return_value=args),
            patch.object(
                Path, "resolve", return_value=Path("/fixture/launchplane-write-action.py")
            ),
            patch.object(runpy, "run_path", return_value=transport),
        ):
            for separation, expected in (("unknown", 1), ("same", 1), ("separate", 0)):
                transport["request_launchplane_read"].return_value = {
                    "trace_id": "fixture",
                    "result": {
                        "separation": separation,
                        "probe": "verified",
                        "testing_domain_sha256": comparison.digest("testing"),
                        "testing_shop_id_sha256": comparison.digest("shop"),
                        "production_domain_sha256": comparison.digest("prod"),
                        "production_shop_id_sha256": comparison.digest("prod-id"),
                    },
                }
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(script["main"](), expected)
                self.assertNotIn("fixture-private", output.getvalue())
                self.assertEqual(
                    transport["request_launchplane_read"].call_args.kwargs["query"], {}
                )
            transport["request_launchplane_read"].side_effect = urllib.error.HTTPError(
                "https://service.invalid", 403, "fixture-private", Message(), io.BytesIO()
            )
            with contextlib.redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(script["main"](), 1)
            self.assertNotIn("fixture-private", errors.getvalue())
            self.assertTrue(transport["request_launchplane_read"].side_effect.fp.closed)
            transport["request_launchplane_read"].side_effect = KeyError("fixture-private")
            with contextlib.redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(script["main"](), 1)
            self.assertNotIn("fixture-private", errors.getvalue())


class ProductionIdentityTests(unittest.TestCase):
    def test_applied_production_identity_is_read_in_a_read_only_transaction_and_hashed(
        self,
    ) -> None:
        from control_plane import odoo_shopify_production_probe as production
        from control_plane.dokploy.compose import ODOO_WEB_CONFIG_PATH
        from control_plane.odoo_instance_overrides import SHOPIFY_STORE_SETTINGS

        config = {
            "db_name": "fixture-db",
            "db_host": "database",
            "db_user": "fixture-user",
            "db_password": "fixture-secret",
        }
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchmany.return_value = [
            ("shopify.shop_url_key", "fixture-production"),
            ("shopify.api_token", "fixture-production-secret"),
            ("shopify.api_version", "2026-01"),
        ]
        database = MagicMock()
        database.connect.return_value = connection
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value = json.dumps(
            {
                "data": {
                    "shop": {
                        "id": "gid://shopify/Shop/456",
                        "myshopifyDomain": "fixture-production.myshopify.com",
                    }
                }
            }
        ).encode()
        with (
            patch.object(probe, "build_opener", return_value=opener),
            patch.object(configparser.ConfigParser, "read", return_value=[ODOO_WEB_CONFIG_PATH]),
            patch.object(configparser.ConfigParser, "__getitem__", return_value=config),
            patch.dict(os.environ, {"ODOO_DB_NAME": "fixture-db"}),
            patch.object(importlib, "import_module", return_value=database),
        ):
            spec: dict[str, object] = {
                "config_path": ODOO_WEB_CONFIG_PATH,
                "store_keys": ["shopify." + key for key in SHOPIFY_STORE_SETTINGS],
            }
            result = production.read_identity(spec)
            self.assertEqual(
                result,
                {
                    "status": "verified",
                    "domain_sha256": comparison.digest("fixture-production.myshopify.com"),
                    "shop_id_sha256": comparison.digest("gid://shopify/Shop/456"),
                },
            )
            connection.rollback.assert_called_once()
            connection.close.assert_called_once()
            cursor.fetchmany.return_value[0] = (
                "shopify.shop_url_key",
                "https://admin.shopify.com/store/fixture-production/settings",
            )
            self.assertEqual(production.read_identity(spec), result)
            cursor.fetchmany.return_value.append(
                ("shopify.store_url", "another-production.myshopify.com")
            )
            with self.assertRaises(ValueError):
                production.read_identity(spec)
        self.assertNotIn("fixture-secret", json.dumps(result))
