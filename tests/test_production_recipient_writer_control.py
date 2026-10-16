"""Writer-control receipts support repeatable cycles against actual fixed host code."""

from contextlib import redirect_stdout
import io
import json
from typing import Any, Literal
import unittest
from unittest.mock import patch

from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.production_recipient_writer_control import (
    RecipientWriterAdapter,
    RecipientWriterRequest,
    WRITER_ROUTE,
)
from control_plane.production_recipient_cutover import WRITER_LIBRARY
from control_plane.provider_operations import run_durable_provider_operation
from control_plane.integration_allowances import canonical_sha256
from tests import test_production_recipient_cutover as cutover_fixtures
from tests import test_production_recipient_writer as host_fixtures


class RecipientWriterControlTests(unittest.TestCase):
    database_url = ""

    def setUp(self) -> None:
        self.fixture = cutover_fixtures.RecipientCutoverTests()
        self.fixture.database_url = self.database_url
        original = self.fixture.seed

        def seed() -> None:
            original()
            case = self.fixture.publication_case
            f = case.fixture
            lane = next(lane for lane in f.profile.lanes if lane.instance == "testing")
            target = f.old.model_copy(
                update={
                    "context": lane.context,
                    "instance": "testing",
                    "target_name": "testing-peer",
                }
            )
            target_id = f.old_id.model_copy(
                update={
                    "context": lane.context,
                    "instance": "testing",
                    "target_id": "testing-peer-id",
                }
            )
            provider = ProviderTargetRecord.from_dokploy_records(
                target_record=target, target_id_record=target_id
            )
            f.store.write_dokploy_target_record(target)
            f.store.write_dokploy_target_id_record(target_id)
            f.store.write_provider_target_record(provider)
            identity = case.inventory.runtime_identity
            assert identity is not None
            self.identity = identity.model_copy(
                update={"context": lane.context, "instance": "testing"}
            )
            f.store.write_environment_inventory(
                case.inventory.model_copy(
                    update={
                        "context": lane.context,
                        "instance": "testing",
                        "runtime_identity": self.identity,
                    }
                )
            )
            binding = f.store.list_secret_bindings()[0]
            self.binding = binding.model_copy(
                update={
                    "binding_id": "testing-api-peer",
                    "context": lane.context,
                    "instance": "testing",
                }
            )
            f.store.write_secret_binding(self.binding)
            f.provider["testing-peer-id"] = {
                "composeId": "testing-peer-id",
                "appName": "testing-peer",
                "serverId": "testing-host",
                "env": "",
                "composeFile": "testing-source",
            }

        with patch.object(self.fixture, "seed", side_effect=seed):
            self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.host = host_fixtures.RecipientWriterProgramTests()
        self.host.setUp()
        self.addCleanup(self.host.doCleanups)
        self.host.row["Config"]["Env"] = [
            "LAUNCHPLANE_RUNTIME_IDENTITY_JSON=" + json.dumps(self.identity.model_dump(mode="json"))
        ]
        self.host.row["Config"]["Labels"] = {
            "com.docker.compose.project": "testing-peer",
            "com.docker.compose.service": self.fixture.record.request.application_service,
        }
        self.host.spec.update(
            project="testing-peer",
            service=self.fixture.record.request.application_service,
            expected_identity=self.identity.model_dump(mode="json"),
        )
        self.bindings: list[str] = []

    def writer(self, root: Any, record: Any, *, old: bool, action: str, checkpoint: Any) -> None:
        checkpoint("post_deploy_schedule_trigger")
        binding = record.execution["writer_binding"]
        self.bindings.append(binding)
        scope: dict[str, Any] = {}
        with patch("subprocess.run", side_effect=self.host.docker), redirect_stdout(io.StringIO()):
            exec(WRITER_LIBRARY, scope)
            scope["coordinate_writer"](
                {**self.host.spec, "action": action, "binding": binding},
                base=self.host.root / "writer-custody",
            )

    def run_action(self, key: str, action: Literal["pause", "resume"], pause: str = "") -> Any:
        f = self.fixture
        request = RecipientWriterRequest(
            context=f.record.context,
            preparation_plan_sha256=f.record.plan_sha256,
            binding_id=self.binding.binding_id,
            action=action,
            pause_operation_id=pause,
            reason="Coordinate the recorded API writer",
        )
        adapter = RecipientWriterAdapter(
            f.fixture.root, f.store, f.record.product, request, "fixture-writer"
        )
        plan = adapter.current()[2]
        adapter.request = request.model_copy(
            update={"mode": "apply", "reviewed_plan_sha256": plan.plan_sha256}
        )
        with (
            patch("control_plane.production_recipient_writer_control.require_disposable_lane"),
            patch(
                "control_plane.production_recipient_writer_control.writer_action",
                side_effect=self.writer,
            ),
        ):
            result = run_durable_provider_operation(
                store=f.store,
                scope="writer-fixture",
                route_path=WRITER_ROUTE.format(product=f.record.product),
                idempotency_key=key,
                request_fingerprint=canonical_sha256(adapter.request.model_dump(mode="json")),
                lease_owner="writer",
                response_trace_id="launchplane_req_writer",
                adapter=adapter,
            )
        self.assertEqual(result.response_status_code, 202, result.response_payload)
        return result.response_payload["result"]

    def test_two_pause_resume_cycles_bind_each_resume_to_its_original_pause(self) -> None:
        first = self.run_action("pause-1", "pause")
        self.assertFalse(self.host.row["State"]["Running"])
        self.run_action("resume-1", "resume", first["pause_operation_id"])
        self.assertTrue(self.host.row["State"]["Running"])
        second = self.run_action("pause-2", "pause")
        self.assertFalse(self.host.row["State"]["Running"])
        self.run_action("resume-2", "resume", second["pause_operation_id"])
        self.assertTrue(self.host.row["State"]["Running"])
        self.assertEqual(self.bindings[0], self.bindings[1])
        self.assertEqual(self.bindings[2], self.bindings[3])
        self.assertNotEqual(self.bindings[0], self.bindings[2])
        self.assertEqual(self.host.effects, ["stop", "start", "stop", "start"])
