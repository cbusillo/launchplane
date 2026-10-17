"""Unwitnessed writer dispatches are fenced on the host before any no-effect adoption."""

from contextlib import redirect_stdout
from datetime import datetime, timedelta
import io
from pathlib import Path
from typing import Any
import unittest
from unittest.mock import patch

from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.dokploy.post_deploy import ODOO_POST_DEPLOY_SCHEDULE_TRIGGER
from control_plane.integration_allowances import canonical_sha256
from control_plane.production_recipient import custody
from control_plane.production_recipient_cutover import WRITER_LIBRARY, WRITER_RECEIPT_LIBRARY
from control_plane.production_recipient_writer_control import (
    RecipientWriterAdapter,
    RecipientWriterRequest,
    WRITER_ROUTE,
)
from control_plane.production_recipient_writer_fence import (
    RecipientWriterFenceRequest,
    fence_writer_operation,
)
from control_plane.provider_operations import run_durable_provider_operation
from tests import test_production_recipient_cutover as cutover_fixtures
from tests import test_production_recipient_writer_control as writer_fixtures


def run_host(
    base: Path, spec: dict[str, Any], docker: Any = AssertionError("Docker is not expected")
) -> dict[str, str]:
    """Run the actual fixed writer program, as the scheduled host job would."""
    scope: dict[str, Any] = {}
    output = io.StringIO()
    with patch("subprocess.run", side_effect=docker), redirect_stdout(output):
        exec(WRITER_LIBRARY, scope)
        scope["coordinate_writer"](spec, base=base)
    return dict(line.split("=", 1) for line in output.getvalue().splitlines())


def read_host(base: Path, spec: dict[str, Any]) -> dict[str, str]:
    scope: dict[str, Any] = {}
    output = io.StringIO()
    with patch("subprocess.run", side_effect=AssertionError("Reads are read-only")):
        with redirect_stdout(output):
            exec(WRITER_RECEIPT_LIBRARY, scope)
            scope["read_writer_receipt"](spec, base=base)
    return dict(line.split("=", 1) for line in output.getvalue().splitlines())


class WriterFenceHarness:
    """Shared fence steps around one recipient fixture store and host custody root."""

    store: Any
    product: str
    context: str
    base: Path

    def reservation(self, record_id: str) -> LaunchplaneIdempotencyRecord:
        from control_plane.production_recipient_writer_fence import read_reservation

        return read_reservation(self.store, record_id)

    def expired(self, record_id: str) -> str:
        reservation = self.reservation(record_id)
        return (
            datetime.fromisoformat(reservation.lease_expires_at.replace("Z", "+00:00"))
            + timedelta(seconds=1)
        ).isoformat()

    def fence(
        self, record_id: str, *, project: str, mode: str = "apply", reviewed: str = ""
    ) -> Any:
        record = custody(self.store, self.product, self.context)
        assert record is not None
        request = RecipientWriterFenceRequest(
            context=self.context,
            preparation_plan_sha256=record.plan_sha256,
            operation_id=record_id,
            reason="The uncertain writer dispatch never persisted a host witness",
        )

        def host_fence(
            root: Any, projected: Any, *, old: bool, action: str, checkpoint: Any, binding: str
        ) -> None:
            checkpoint(ODOO_POST_DEPLOY_SCHEDULE_TRIGGER)
            run_host(
                self.base,
                {
                    "project": project,
                    "service": projected.request.application_service,
                    "binding": binding,
                    "action": action,
                },
            )

        def host_read(
            root: Any, projected: Any, spec: dict[str, Any], *args: Any, **kwargs: Any
        ) -> dict[str, str]:
            self.reads.append(projected.target_id)  # type: ignore[attr-defined]
            return read_host(self.base, spec)

        with (
            patch(
                "control_plane.production_recipient_writer_fence.writer_action",
                side_effect=host_fence,
            ),
            patch(
                "control_plane.production_recipient_writer_fence.run_recipient_program",
                side_effect=host_read,
            ),
        ):
            if mode == "apply" and not reviewed:
                reviewed = fence_writer_operation(
                    Path("."), self.store, self.product, request, lambda _phase: None
                ).plan_sha256
            return fence_writer_operation(
                Path("."),
                self.store,
                self.product,
                request.model_copy(update={"mode": mode, "reviewed_plan_sha256": reviewed}),
                lambda _phase: None,
            )


class TestingWriterFenceTests(WriterFenceHarness, unittest.TestCase):
    database_url = ""

    def setUp(self) -> None:
        self.control = writer_fixtures.RecipientWriterControlTests()
        self.control.database_url = self.database_url
        self.control.setUp()
        self.addCleanup(self.control.doCleanups)
        f = self.control.fixture
        self.store = f.store
        self.product = f.record.product
        self.context = f.record.context
        self.base = self.control.host.root / "writer-custody"
        self.reads: list[str] = []
        self.dispatched: list[str] = []

    def dispatch(self, key: str, action: str = "pause", pause: str = "", lost: bool = True) -> Any:
        f = self.control.fixture
        request = RecipientWriterRequest(
            context=self.context,
            preparation_plan_sha256=f.record.plan_sha256,
            binding_id=self.control.binding.binding_id,
            action=action,  # type: ignore[arg-type]
            pause_operation_id=pause,
            reason="Coordinate the recorded API writer",
        )
        adapter = RecipientWriterAdapter(
            f.fixture.root, self.store, self.product, request, "fixture-writer"
        )
        adapter.request = request.model_copy(
            update={"mode": "apply", "reviewed_plan_sha256": adapter.current()[2].plan_sha256}
        )

        def queued(root: Any, record: Any, *, old: bool, action: str, checkpoint: Any) -> None:
            if not lost:
                return self.control.writer(
                    root, record, old=old, action=action, checkpoint=checkpoint
                )
            checkpoint(ODOO_POST_DEPLOY_SCHEDULE_TRIGGER)
            self.dispatched.append(record.execution["writer_binding"])
            raise TimeoutError("schedule trigger reply lost before the host job ran")

        with (
            patch(
                "control_plane.production_recipient_host.run_recipient_program",
                side_effect=lambda root, projected, spec, *args, **kwargs: read_host(
                    self.base, spec
                ),
            ),
            patch("control_plane.production_recipient_writer_control.require_disposable_lane"),
            patch(
                "control_plane.production_recipient_writer_control.writer_action",
                side_effect=queued,
            ),
        ):
            return run_durable_provider_operation(
                store=self.store,
                scope="writer-fixture",
                route_path=WRITER_ROUTE.format(product=self.product),
                idempotency_key=key,
                request_fingerprint=canonical_sha256(adapter.request.model_dump(mode="json")),
                lease_owner="writer",
                response_trace_id="launchplane_req_writer",
                adapter=adapter,
            )

    def late_pause(self, binding: str) -> None:
        host = self.control.host
        run_host(
            self.base,
            {**host.spec, "action": "pause", "binding": binding},
            docker=host.docker,
        )

    def test_unwitnessed_pause_is_fenced_then_adopted_without_effect(self) -> None:
        self.assertEqual(self.dispatch("lost-pause").response_status_code, 409)
        record = custody(self.store, self.product, self.context)
        assert record is not None
        operation = record.writer_custody[-1]["record_id"]
        with self.assertRaises(ValueError):
            self.fence(operation, project="testing-peer", mode="dry-run")
        with patch.object(
            self.store, "_database_mutation_timestamp", return_value=self.expired(operation)
        ):
            result = self.fence(operation, project="testing-peer")
            self.assertTrue(result.fenced)
            self.assertEqual(
                self.fence(operation, project="testing-peer", reviewed=result.plan_sha256), result
            )
        self.assertEqual(result.witness_binding, self.dispatched[0])
        with self.assertRaises(ValueError):
            self.late_pause(self.dispatched[0])
        self.assertTrue(self.control.host.row["State"]["Running"])
        self.assertEqual(self.control.host.effects, [])
        replay = self.dispatch("lost-pause")
        self.assertEqual(replay.response_status_code, 202, replay.response_payload)
        self.assertFalse(replay.response_payload["result"]["completed"])
        with self.assertRaises(ValueError):
            self.dispatch("resume-fenced", "resume", operation, lost=False)
        self.assertEqual(self.control.host.effects, [])
        readback = self.fence(operation, project="testing-peer", mode="readback")
        self.assertEqual(readback.plan_sha256, result.plan_sha256)
        self.assertEqual(set(self.reads), {"testing-peer-id"})
        fresh = self.dispatch("next-pause", lost=False)
        self.assertEqual(fresh.response_status_code, 202, fresh.response_payload)
        self.assertFalse(self.control.host.row["State"]["Running"])

    def test_witnessed_pause_is_never_fenced(self) -> None:
        original = self.control.writer

        def stop_then_lose(root: Any, record: Any, **kwargs: Any) -> None:
            original(root, record, **kwargs)
            raise TimeoutError("reply lost after the host stop")

        with patch.object(self.control, "writer", side_effect=stop_then_lose):
            self.assertEqual(self.dispatch("witnessed", lost=False).response_status_code, 409)
        record = custody(self.store, self.product, self.context)
        assert record is not None
        operation = record.writer_custody[-1]["record_id"]
        with patch.object(
            self.store, "_database_mutation_timestamp", return_value=self.expired(operation)
        ):
            with self.assertRaises(ValueError):
                self.fence(operation, project="testing-peer")
        self.assertEqual(custody(self.store, self.product, self.context), record)
        self.assertFalse(self.control.host.row["State"]["Running"])
        self.assertEqual(self.control.host.effects, ["stop"])

    def test_original_worker_retaking_the_operation_refuses_the_fence_commit(self) -> None:
        self.assertEqual(self.dispatch("raced-pause").response_status_code, 409)
        record = custody(self.store, self.product, self.context)
        assert record is not None
        operation = record.writer_custody[-1]["record_id"]
        expired = self.expired(operation)
        with patch.object(self.store, "_database_mutation_timestamp", return_value=expired):
            reviewed = self.fence(operation, project="testing-peer", mode="dry-run").plan_sha256
        original = run_host

        def retake(base: Path, spec: dict[str, Any], **kwargs: Any) -> dict[str, str]:
            evidence = original(base, spec, **kwargs)
            self.store.retry_reconciled_mutation(
                reservation=self.reservation(operation), lease_owner="writer", lease_seconds=300
            )
            return evidence

        with (
            patch.object(self.store, "_database_mutation_timestamp", return_value=expired),
            patch(f"{__name__}.run_host", side_effect=retake),
        ):
            with self.assertRaises(ValueError):
                self.fence(operation, project="testing-peer", reviewed=reviewed)
        self.assertEqual(custody(self.store, self.product, self.context), record)
        with self.assertRaises(ValueError):
            self.late_pause(self.dispatched[0])
        self.assertTrue(self.control.host.row["State"]["Running"])


class CutoverWriterFenceTests(WriterFenceHarness, unittest.TestCase):
    database_url = ""

    def setUp(self) -> None:
        self.cutover = cutover_fixtures.RecipientCutoverTests()
        self.cutover.database_url = self.database_url
        self.cutover.setUp()
        self.addCleanup(self.cutover.doCleanups)
        self.store = self.cutover.store
        self.product = self.cutover.record.product
        self.context = self.cutover.record.context
        self.base = self.cutover.fixture.root.resolve() / "writer-custody"
        self.reads: list[str] = []
        self.dispatched: list[str] = []

    def lose(self, old: bool) -> Any:
        def writer(*args: Any, **kwargs: Any) -> None:
            if kwargs["old"] is old and kwargs["action"] == "pause":
                kwargs["checkpoint"](ODOO_POST_DEPLOY_SCHEDULE_TRIGGER)
                self.dispatched.append(kwargs["binding"])
                raise TimeoutError("schedule trigger reply lost before the host job ran")
            self.cutover.writer(*args, **kwargs)

        return writer

    def operation(self, route: str, key: str) -> str:
        reservation = self.store.read_idempotency_record(
            scope="client-release", route_path=route, idempotency_key=key
        )
        assert reservation is not None
        return str(reservation.record_id)

    def test_unwitnessed_old_pause_is_fenced_and_release_settles_without_effect(self) -> None:
        with patch(
            "control_plane.production_recipient_cutover.writer_action", side_effect=self.lose(True)
        ):
            self.assertEqual(self.cutover.run_release().response_status_code, 409)
        operation = self.operation("/fixture/recipient-release", "accepted-release")
        with patch.object(
            self.store, "_database_mutation_timestamp", return_value=self.expired(operation)
        ):
            self.assertEqual(self.fence(operation, project="old-production").operation, "cutover")
        record = custody(self.store, self.product, self.context)
        assert record is not None
        self.assertEqual(record.phase, "preflight_refused")
        with self.assertRaises(ValueError):
            self.cutover.writer(
                None,
                record,
                old=True,
                action="pause",
                checkpoint=lambda _phase: None,
                binding=self.dispatched[0],
            )
        self.assertTrue(self.cutover.old_running)
        result = self.cutover.run_release()
        self.assertEqual(result.response_status_code, 202, result.response_payload)
        self.assertEqual(result.response_payload["result"]["promotion_status"], "fail")
        self.assertEqual(result.response_payload["result"]["writer_effect"], "none")
        self.assertEqual(self.cutover.deploys, 0)
        self.assertTrue(self.cutover.old_running)
        self.assertEqual(
            self.fence(operation, project="old-production", mode="readback").witness_binding,
            self.dispatched[0],
        )
        self.assertEqual(self.reads, [record.retained_target_id.target_id])

    def test_unwitnessed_inverse_pause_is_fenced_and_replacement_keeps_serving(self) -> None:
        from control_plane.production_recipient_inverse import RecipientInverseAdapter

        self.assertEqual(self.cutover.run_release().response_status_code, 202)

        def inverse() -> Any:
            current = custody(self.store, self.product, self.context)
            assert current is not None
            adapter = RecipientInverseAdapter(
                self.cutover.fixture.root,
                self.store,
                current,
                "client-release-fixture",
                self.cutover.validate_release,
            )
            return run_durable_provider_operation(
                store=self.store,
                scope="client-release",
                route_path="/fixture/inverse",
                idempotency_key="lost-inverse",
                request_fingerprint="d" * 64,
                lease_owner="inverse",
                response_trace_id="launchplane_req_inverse",
                adapter=adapter,
            )

        with patch(
            "control_plane.production_recipient_inverse.writer_action",
            side_effect=self.lose(False),
        ):
            self.assertEqual(inverse().response_status_code, 409)
            operation = self.operation("/fixture/inverse", "lost-inverse")
            published = custody(self.store, self.product, self.context)
            assert published is not None
            self.assertEqual(published.phase, "reverting")
            with patch.object(
                self.store, "_database_mutation_timestamp", return_value=self.expired(operation)
            ):
                self.fence(operation, project=published.app_name)
            with self.assertRaises(ValueError):
                self.cutover.writer(
                    None,
                    published,
                    old=False,
                    action="pause",
                    checkpoint=lambda _phase: None,
                    binding=self.dispatched[0],
                )
            self.assertTrue(self.cutover.new_running)
            result = inverse()
        self.assertEqual(result.response_status_code, 202, result.response_payload)
        self.assertEqual(result.response_payload["result"]["rollback_status"], "fail")
        current = custody(self.store, self.product, self.context)
        assert current is not None
        self.assertEqual(current.phase, "published")
        self.assertTrue(self.cutover.new_running)
        self.assertFalse(self.cutover.old_running)
