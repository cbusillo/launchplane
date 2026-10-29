"""Writer-control receipts support repeatable cycles against actual fixed host code."""

from contextlib import redirect_stdout, nullcontext
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
from control_plane.dokploy.post_deploy import ODOO_POST_DEPLOY_SCHEDULE_TRIGGER
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
            f.profile = f.profile.model_copy(
                update={
                    "lanes": tuple(
                        lane.model_copy(update={"context": lane.context + "-testing-peer"})
                        if lane.instance == "testing"
                        else lane
                        for lane in f.profile.lanes
                    )
                }
            )
            f.store.write_product_profile_record(f.profile)
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
        checkpoint(ODOO_POST_DEPLOY_SCHEDULE_TRIGGER)
        binding = record.execution["writer_binding"]
        self.bindings.append(binding)
        scope: dict[str, Any] = {}
        with patch("subprocess.run", side_effect=self.host.docker), redirect_stdout(io.StringIO()):
            exec(WRITER_LIBRARY, scope)
            scope["coordinate_writer"](
                {**self.host.spec, "action": action, "binding": binding},
                base=self.host.root / "writer-custody",
            )

    def run_action(
        self,
        key: str,
        action: Literal["pause", "resume"],
        pause: str = "",
        *,
        disposable: bool = True,
    ) -> Any:
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
            patch("control_plane.production_recipient_writer_control.require_disposable_lane")
            if disposable
            else nullcontext(),
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
        assert result.record is not None
        capsule = f.store.read_provider_target_origin(result.record)
        assert capsule is not None
        self.assertEqual(
            (
                capsule.origin.product,
                capsule.origin.context,
                capsule.origin.instance,
                capsule.origin.target_id,
            ),
            (f.record.product, self.binding.context, "testing", "testing-peer-id"),
        )
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

    def test_carried_pause_can_restore_its_writer_after_original_key_supersession(self) -> None:
        from control_plane.production_recipient import custody
        from control_plane.production_recipient_continuation import (
            RecipientContinuationRequest,
            continuation_plan,
            supersede_recipient,
        )

        first = self.run_action("pause-before-drift", "pause")
        f = self.fixture
        current = custody(f.store, f.record.product, f.record.context)
        assert current is not None
        f.store.write_product_profile_record(
            f.fixture.profile.model_copy(update={"updated_at": "2026-10-16T11:00:00Z"})
        )
        request = RecipientContinuationRequest(
            context=current.context,
            original_key=current.original_key,
            expected_custody_sha256=canonical_sha256(current.model_dump(mode="json")),
            replacement=current.request.model_copy(
                update={"mode": "dry-run", "reviewed_plan_sha256": ""}
            ),
            reason="Retain and fence the original namespace after drift",
        )
        draft = continuation_plan(
            f.fixture.root, f.store, current.product, request, current.actor, current.scope
        )[2]
        with patch(
            "control_plane.production_recipient_continuation.run_recipient_program",
            return_value={"recipient_execution_fenced": current.plan_sha256},
        ):
            supersede_recipient(
                f.fixture.root,
                f.store,
                current.product,
                request.model_copy(
                    update={"mode": "apply", "reviewed_plan_sha256": draft["plan_sha256"]}
                ),
                current.actor,
                current.scope,
                lambda _phase: None,
            )
        replacement = custody(f.store, current.product, current.context)
        assert replacement is not None
        self.assertFalse(replacement.activation)
        self.assertTrue(replacement.writer_custody)
        f.record = replacement
        self.run_action("restore-carried-pause", "resume", first["pause_operation_id"])
        self.assertTrue(self.host.row["State"]["Running"])

    def test_budget_allows_parking_a_peer_and_refuses_resuming_it(self) -> None:
        from control_plane.production_recipient import custody

        f = self.fixture
        current = custody(f.store, f.record.product, f.record.context)
        assert current is not None
        f.store.write_production_recipient(
            expected=current.model_dump(mode="json"),
            replacement=current.model_copy(update={"phase": "bootstrapping"}).model_dump(
                mode="json"
            ),
        )
        first = self.run_action("park-under-budget", "pause")
        self.assertFalse(self.host.row["State"]["Running"])
        with self.assertRaises(ValueError):
            self.run_action("resume-under-budget", "resume", first["pause_operation_id"])
        self.assertFalse(self.host.row["State"]["Running"])
        self.assertEqual(self.host.effects, ["stop"])

    def test_missing_disposable_evidence_refuses_before_any_writer_effect(self) -> None:
        with self.assertRaises(ValueError):
            self.run_action("unqualified-pause", "pause", disposable=False)
        self.assertTrue(self.host.row["State"]["Running"])
        self.assertEqual(self.host.effects, [])
