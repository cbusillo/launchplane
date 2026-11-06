from contextlib import redirect_stdout
from io import StringIO
import json
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch
from collections.abc import Callable

from control_plane import host_security_updates as security
from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.product_environment_read_model import build_product_site_overview
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.public_ingress_monitoring import health_monitor_freshness_seconds
from control_plane.dokploy import native_copy_stream
from control_plane.health_monitor_scheduler import HealthMonitorScheduler
from control_plane.storage.postgres import PostgresRecordStore
from tests.test_product_read_service import _profile_payload


class HostSecurityUpdateTests(unittest.TestCase):
    def test_read_program_observes_success_stamp_without_running_updates(self) -> None:
        import pathlib
        import sys

        for completed_at in (None, 1000.0):
            with self.subTest(completed_at=completed_at):
                output = StringIO()
                with (
                    patch.object(pathlib, "Path") as path,
                    patch.object(sys, "argv", ["reader", "{}", "FRAME:"]),
                    redirect_stdout(output),
                ):
                    path.return_value.stat.return_value.st_mtime = completed_at
                    if completed_at is None:
                        path.return_value.stat.side_effect = FileNotFoundError
                    exec(security.SECURITY_UPDATE_PROGRAM, {})
                result = json.loads(output.getvalue().removeprefix("FRAME:"))
                self.assertEqual(result["type"], "complete")
                self.assertEqual(result["result"]["completed_at"], completed_at)

    def test_invalid_remote_evidence_and_denial_never_become_observed(self) -> None:
        for frames, error, status in (
            ([], None, "unknown"),
            (
                [{"type": "complete", "result": {"observed_at": time.time() + 3600}}],
                None,
                "unknown",
            ),
            ([{"type": "complete", "result": {"observed_at": float("nan")}}], None, "unknown"),
            ([], OSError("unreachable"), "unknown"),
            ([], native_copy_stream.NativeSourceAuthorizationDenied("denied"), "denied"),
        ):
            with self.subTest(status=status, frames=frames):

                def remote(
                    *, receive: Callable[[dict[str, object]], None], **kwargs: object
                ) -> None:
                    if error is not None:
                        raise error
                    for frame in frames:
                        receive(dict(frame))

                with patch.object(native_copy_stream, "_read_remote", side_effect=remote):
                    evidence = security.read_host_security_updates("provider", "fixture", "host")
                self.assertEqual(evidence.status, status)

    def test_shared_host_reads_clear_warnings_and_leave_uptime_history_alone(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(database_url=f"sqlite+pysqlite:///{directory}/state.db")
            self.addCleanup(store.close)
            store.ensure_schema()
            profiles = []
            for product in ("one", "two"):
                payload = _profile_payload(product=product)
                profile = LaunchplaneProductProfileRecord.model_validate(
                    {**payload, "production_use": "live"}
                )
                profile = profile.model_copy(
                    update={
                        "lanes": tuple(
                            lane.model_copy(update={"context": f"{product}-{lane.instance}"})
                            for lane in profile.lanes
                        )
                    }
                )
                store.write_product_profile_record(profile)
                profiles.append(profile)
                lane = next(lane for lane in profile.lanes if lane.instance == "prod")
                store.write_provider_target_record(
                    ProviderTargetRecord(
                        context=lane.context,
                        instance=lane.instance,
                        provider_id="dokploy",
                        target_category="compose",
                        target_id=product,
                        display_name=product,
                        updated_at=profile.updated_at,
                    )
                )
            now = time.time()
            with (
                patch.object(security, "read_host_security_updates") as reader,
                patch(
                    "control_plane.host_network_capacity.source.read_dokploy_config",
                    return_value=("provider", "fixture"),
                ),
                patch(
                    "control_plane.host_network_capacity.api.fetch_dokploy_target_payload",
                    return_value={"serverId": "shared"},
                ) as placement,
                patch("control_plane.production_delivery_monitor.monitor_production_delivery_once"),
                patch("control_plane.testing_delivery_monitor.monitor_testing_delivery_once"),
            ):
                for status, completed_at, warned in (
                    ("observed", None, True),
                    ("observed", now - security.MAX_UPDATE_AGE_SECONDS - 1, True),
                    ("observed", now - 1, False),
                    ("unknown", now - 1, True),
                    ("denied", None, True),
                    ("observed", now + 100, True),
                    ("observed", now - 1, False),
                ):
                    reader.return_value = security.HostSecurityUpdateEvidence.model_validate(
                        {"observed_at": now, "completed_at": completed_at, "status": status}
                    )
                    reader.reset_mock()
                    HealthMonitorScheduler(store)._run_delivery_monitor()
                    self.assertEqual(reader.call_count, 1)
                    for profile in profiles:
                        overview = build_product_site_overview(
                            record_store=store,
                            product=profile.product,
                            action_allowed=lambda *_: True,
                        )
                        production = next(
                            item for item in overview.environments if item.environment == "prod"
                        )
                        lane = next(lane for lane in profile.lanes if lane.instance == "prod")
                        warnings = security.host_security_update_warnings(store, profile, lane)
                        self.assertEqual(len(warnings), int(warned))
                        self.assertTrue(all(warning in production.warnings for warning in warnings))
                        self.assertTrue(
                            security.host_security_update_warnings(
                                store,
                                profile,
                                lane,
                                now=now + health_monitor_freshness_seconds("prod") + 1,
                            )
                        )
                        receipt = store.read_idempotency_record(
                            scope=security.SCOPE,
                            route_path=security.ROUTE,
                            idempotency_key=f"{lane.context}:prod",
                        )
                        self.assertIsNotNone(receipt)
                reader.side_effect = OSError("unreachable")
                security.collect_host_security_updates(store, should_stop=lambda: False)
                for profile in profiles:
                    lane = next(lane for lane in profile.lanes if lane.instance == "prod")
                    self.assertTrue(security.host_security_update_warnings(store, profile, lane))
                reader.side_effect = None
                placement.side_effect = lambda **kwargs: {
                    "serverId": "moved" if placement.call_count % 2 == 0 else "shared"
                }
                security.collect_host_security_updates(store, should_stop=lambda: False)
                for profile in profiles:
                    lane = next(lane for lane in profile.lanes if lane.instance == "prod")
                    self.assertTrue(security.host_security_update_warnings(store, profile, lane))
            self.assertFalse(store.list_public_ingress_observation_records())
