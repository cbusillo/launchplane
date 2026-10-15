"""Production preparation host, HTTP and terminal boundaries run with isolated fixtures."""

import asyncio
import base64
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import runpy
import sys
from tempfile import TemporaryDirectory
from typing import Any, cast
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi import FastAPI, HTTPException
from httpx2 import ASGITransport, AsyncClient
from pydantic import BaseModel

from control_plane.action_effects import action_effect
from control_plane.contracts.production_recipient import ProductionRecipientPlan
from control_plane.http_routes.production_recipient import register_production_recipient_routes
from control_plane.http_routes.support import ApiRouteRegistrar, ReadRouteDependencies
from control_plane.production_recipient import ROUTE, plan
from control_plane.production_recipient_resources import RESOURCE_PROGRAM, resource_spec
from control_plane.service_auth import LocalAdminIdentity, TerminalAgentIdentity
from tests import test_odoo_testing_resources as resources_support
from tests import test_production_recipient as recipient_support


class HostProgramTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = resources_support.TestingResourceProgramTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        recipient = recipient_support.ProductionRecipientTests()
        recipient.setUp()
        self.addCleanup(recipient.doCleanups)
        record = recipient.adapter.current()
        record = record.model_copy(update={"app_name": record.namespace})
        self.spec: dict[str, Any] = {**resource_spec(record), "prepare": True}

    def run_program(self) -> tuple[int, str]:
        output = io.StringIO()
        code = 0
        with (
            patch.object(
                sys, "argv", ["program", base64.b64encode(json.dumps(self.spec).encode()).decode()]
            ),
            patch("subprocess.run", side_effect=self.fixture.docker),
            patch("socket.socket", return_value=Mock()),
            redirect_stdout(output),
        ):
            try:
                exec(compile(RESOURCE_PROGRAM, "<production-resources>", "exec"), {})
            except SystemExit as error:
                code = error.code if isinstance(error.code, int) else 1
        return code, output.getvalue()

    def test_new_empty_resources_replay_and_preserve_existing_data(self) -> None:
        production = self.fixture.root / "production-db" / "_data"
        production.mkdir(parents=True)
        data = production / "business-data"
        data.write_text("recoverable old production")
        self.fixture.containers["production"] = {
            "Config": {"Labels": {}},
            "HostConfig": {"PortBindings": {"3306/tcp": [{"HostPort": "3307"}]}},
            "Mounts": [{"Name": "production-db", "Source": str(production)}],
        }
        original = json.loads(json.dumps(self.fixture.containers))
        self.assertEqual(self.run_program()[0], 0)
        self.fixture.calls.clear()
        self.spec["prepare"] = False
        self.assertEqual(self.run_program()[0], 0)
        self.assertEqual(self.fixture.containers, original)
        self.assertEqual(data.read_text(), "recoverable old production")
        self.assertFalse(any(call[:2] == ["volume", "create"] for call in self.fixture.calls))

    def test_missing_or_foreign_or_needed_volume_is_not_adopted_or_deleted(self) -> None:
        self.spec["prepare"] = False
        self.assertNotEqual(self.run_program()[0], 0)
        self.assertFalse(self.fixture.volumes)
        self.spec["prepare"] = True
        self.assertEqual(self.run_program()[0], 0)
        name = self.spec["volumes"]["database"]
        data = Path(str(self.fixture.volumes[name]["Mountpoint"])) / "needed-copy"
        data.write_text("preserve")
        self.assertNotEqual(self.run_program()[0], 0)
        self.assertEqual(data.read_text(), "preserve")
        self.fixture.volumes[name]["Labels"] = {"owner": "someone-else"}
        self.assertNotEqual(self.run_program()[0], 0)
        self.assertEqual(data.read_text(), "preserve")

    def test_stopped_container_mount_or_port_reference_blocks_preparation(self) -> None:
        self.assertEqual(self.run_program()[0], 0)
        name = self.spec["volumes"]["database"]
        self.fixture.containers["stopped"] = {
            "Config": {"Labels": {}},
            "HostConfig": {"PortBindings": {}},
            "Mounts": [{"Name": name, "Source": self.fixture.volumes[name]["Mountpoint"]}],
        }
        self.assertNotEqual(self.run_program()[0], 0)
        self.fixture.containers["stopped"]["Mounts"] = []
        cast(dict[str, Any], self.fixture.containers["stopped"]["HostConfig"])["PortBindings"] = {
            "3306/tcp": [{"HostPort": str(self.spec["ports"][0])}]
        }
        self.assertNotEqual(self.run_program()[0], 0)
        self.assertFalse(
            any(
                call[0] in {"rm", "stop", "start"} or call[:2] == ["volume", "rm"]
                for call in self.fixture.calls
            )
        )

    def test_readonly_host_monitor_parent_is_distinct_from_a_resource_consumer(self) -> None:
        self.fixture.containers["monitor"] = {
            "Config": {"Labels": {}},
            "HostConfig": {"PortBindings": {}},
            "Mounts": [{"Type": "bind", "RW": False, "Source": str(self.fixture.root)}],
        }
        self.assertEqual(self.run_program()[0], 0)
        name = self.spec["volumes"]["database"]
        mounts = self.fixture.containers["monitor"]["Mounts"]
        assert isinstance(mounts, list)
        mounts[0]["Source"] = self.fixture.volumes[name]["Mountpoint"]
        self.assertNotEqual(self.run_program()[0], 0)

    def test_dynamic_and_ranged_port_inventory_checks_actual_allocations(self) -> None:
        requested = self.spec["ports"][0]
        row: dict[str, Any] = {
            "Config": {"Labels": {}},
            "HostConfig": {"PortBindings": {"80/tcp": [{"HostPort": ""}]}},
            "NetworkSettings": {"Ports": {"80/tcp": [{"HostPort": "18001"}]}},
            "Mounts": [{"Type": "bind", "Source": ""}],
        }
        self.fixture.containers["dynamic"] = row
        self.assertEqual(self.run_program()[0], 0)
        row["NetworkSettings"]["Ports"]["80/tcp"][0]["HostPort"] = str(requested)
        self.assertNotEqual(self.run_program()[0], 0)
        row["NetworkSettings"]["Ports"] = {}
        row["HostConfig"]["PortBindings"]["80/tcp"][0]["HostPort"] = (
            f"{requested - 1}-{requested + 1}"
        )
        self.assertNotEqual(self.run_program()[0], 0)
        row["HostConfig"]["PortBindings"]["80/tcp"][0]["HostPort"] = "18000-18002"
        self.assertEqual(self.run_program()[0], 0)
        row["HostConfig"]["PortBindings"] = None
        row["NetworkSettings"] = None
        self.assertEqual(self.run_program()[0], 0)


class HttpBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = recipient_support.ProductionRecipientTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.identity: Any = LocalAdminIdentity("admin", "fixture")
        self.allowed = True
        self.allowed_effects = {"read", "plan", "write"}
        self.requested: list[dict[str, Any]] = []
        self.mutate = AsyncMock()
        app = FastAPI()

        def authorize(**kwargs: Any) -> bool:
            self.requested.append(kwargs)
            return self.allowed and action_effect(kwargs["action"]) in self.allowed_effects

        dependencies = ReadRouteDependencies(
            read_identity=lambda: self.identity,
            get_record_store=lambda: self.fixture.store,
            next_trace_id=lambda: "launchplane_req_fixture",
            authorization_allows=authorize,
            http_error=lambda **kwargs: HTTPException(
                status_code=kwargs["status_code"],
                detail={"code": kwargs["code"], "message": kwargs["message"]},
            ),
            error_response_model=BaseModel,
        )
        register_production_recipient_routes(
            cast(ApiRouteRegistrar, app),
            dependencies=dependencies,
            mutation_identity=lambda: self.identity,
            actor=lambda identity: "admin:fixture",
            root=self.fixture.root,
            run_provider_mutation=self.mutate,
        )
        self.app = app

    def post(self, body: dict[str, Any]) -> Any:
        async def send() -> Any:
            async with AsyncClient(
                transport=ASGITransport(app=self.app), base_url="https://fixture.invalid"
            ) as client:
                return await client.post(ROUTE.format(product=self.fixture.product), json=body)

        return asyncio.run(send())

    def test_planning_is_read_authority_and_does_not_write_custody_or_provider(self) -> None:
        self.allowed_effects = {"read", "plan"}
        body = self.fixture.body.model_copy(
            update={"mode": "dry-run", "reviewed_plan_sha256": ""}
        ).model_dump(mode="json")
        response = self.post(body)
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(self.fixture.effects, [])
        self.assertIsNone(
            self.fixture.store.read_production_recipient(self.fixture.product, self.fixture.context)
        )

    def test_terminal_or_denied_existing_setup_authority_never_mutates(self) -> None:
        self.identity = TerminalAgentIdentity("agent", "fixture", director_agent=True)
        response = self.post(self.fixture.body.model_dump(mode="json"))
        self.assertEqual(response.status_code, 403)
        self.identity = LocalAdminIdentity("admin", "fixture")
        self.allowed = False
        self.assertEqual(self.post(self.fixture.body.model_dump(mode="json")).status_code, 403)
        self.mutate.assert_not_awaited()
        self.assertEqual(self.fixture.effects, [])

    def test_sqlite_apply_and_nonproduction_payload_refuse(self) -> None:
        self.assertEqual(self.post(self.fixture.body.model_dump(mode="json")).status_code, 503)
        self.assertEqual(
            self.post(
                {**self.fixture.body.model_dump(mode="json"), "instance": "testing"}
            ).status_code,
            422,
        )
        self.mutate.assert_not_awaited()

    def test_readback_uses_only_provider_reads_without_a_host_schedule(self) -> None:
        self.assertEqual(self.fixture.run_operation().response_status_code, 202)
        self.allowed_effects = {"read", "plan"}
        before = list(self.fixture.effects)
        with (
            patch(
                "control_plane.http_routes.production_recipient.idempotency_scope",
                return_value=self.fixture.adapter.scope,
            ),
            patch("control_plane.production_recipient.inspect_resources") as host_check,
            patch("control_plane.production_recipient_resources.inspect_resources", new=host_check),
        ):
            response = self.post(
                self.fixture.body.model_copy(update={"mode": "readback"}).model_dump(mode="json")
            )
        self.assertEqual(response.status_code, 202, response.text)
        host_check.assert_not_called()
        self.assertEqual(self.fixture.effects, before)

    def test_malformed_product_compose_refuses_without_a_server_error_or_effect(self) -> None:
        for deploy in (None, {"resources": {"limits": {"cpus": {}, "memory": "2G"}}}):
            with self.subTest(deploy=deploy):
                self.fixture.document["services"]["database"]["deploy"] = deploy
                body = self.fixture.body.model_copy(update={"mode": "dry-run"}).model_dump(
                    mode="json"
                )
                self.assertEqual(self.post(body).status_code, 409)
        self.assertFalse(self.fixture.effects)


class TerminalClientTests(unittest.TestCase):
    def test_saved_review_is_private_bound_and_original_key_replay_is_readable(self) -> None:
        client = runpy.run_path(
            str(Path(__file__).resolve().parents[1] / "scripts/prepare-production-recipient.py")
        )
        fixture = recipient_support.ProductionRecipientTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        record = fixture.adapter.current()
        result = ProductionRecipientPlan.model_validate(plan(record))
        post = Mock(
            side_effect=[
                {
                    "status": "accepted",
                    "trace_id": "trace-" + str(index),
                    "result": result.model_dump(mode="json"),
                }
                for index in range(3)
            ]
        )
        transport: dict[str, Any] = {
            "resolve_settings": lambda args: {
                "service_url": "https://service.invalid",
                "token": "private-fixture-token",
            },
            "validate_service_url": lambda url: None,
            "request_launchplane": post,
            "public_trace_id": lambda trace: trace,
        }
        with TemporaryDirectory() as directory:
            root = Path(directory)
            helper = root / "launchplane-write-action.py"
            helper.touch()
            request, evidence = root / "request.json", root / "review.json"
            request.write_text(record.request.model_dump_json())
            for mode in ("dry-run", "apply", "apply"):
                args = [
                    "client",
                    mode,
                    "--operator-helper",
                    str(helper),
                    "--product",
                    fixture.product,
                    "--request-file",
                    str(request),
                    "--evidence-file",
                    str(evidence),
                ]
                if mode == "apply":
                    args += ["--reviewed-dry-run", "--idempotency-key", "original-key"]
                output = io.StringIO()
                with (
                    patch("runpy.run_path", return_value=transport),
                    patch("sys.argv", args),
                    redirect_stdout(output),
                ):
                    self.assertEqual(client["main"](), 0, output.getvalue())
                self.assertNotIn("private-fixture-token", output.getvalue())
            self.assertEqual(evidence.stat().st_mode & 0o077, 0)
            self.assertEqual(
                post.call_args.kwargs["body"]["reviewed_plan_sha256"], result.plan_sha256
            )
            request.write_text(
                record.request.model_copy(update={"context": "different"}).model_dump_json()
            )
            with (
                patch("runpy.run_path", return_value=transport),
                patch("sys.argv", args),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(client["main"](), 1)
            self.assertEqual(post.call_count, 3)
