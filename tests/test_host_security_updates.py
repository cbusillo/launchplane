from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest

from control_plane.contracts.host_security_updates import (
    HostSecurityUpdateEvidence,
    SECURITY_UPDATE_MAX_AGE_SECONDS,
)
from control_plane import host_security_updates as security
from control_plane.dokploy import native_copy_stream
from control_plane.contracts.public_ingress_monitoring import health_monitor_freshness_seconds


class HostSecurityUpdateTests(unittest.TestCase):
    def test_only_a_successful_updater_result_and_apt_completion_can_be_fresh(self) -> None:
        now = datetime.now(timezone.utc).timestamp()
        for result_age, completion_age, expected in (
            (10, 9, "fresh"),
            (0, None, "missing"),
            (0, 100, "unknown"),
            (None, 0, "unknown"),
            (SECURITY_UPDATE_MAX_AGE_SECONDS + 1, SECURITY_UPDATE_MAX_AGE_SECONDS, "fresh"),
            (SECURITY_UPDATE_MAX_AGE_SECONDS + 2, SECURITY_UPDATE_MAX_AGE_SECONDS + 1, "stale"),
            (-100, -100, "unknown"),
        ):
            with self.subTest(result_age=result_age, completion_age=completion_age):
                evidence = security.evaluate_security_update_stamps(
                    observed_at=now,
                    updater_result_at=now - result_age if result_age is not None else None,
                    completed_at=now - completion_age if completion_age is not None else None,
                )
                self.assertEqual(evidence.status, expected)

    def test_read_program_observes_files_without_running_the_updater(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            stamps = root / "var/lib/apt/periodic"
            stamps.mkdir(parents=True)
            (stamps / "unattended-upgrades-stamp").touch()
            (stamps / "upgrade-stamp").touch()
            evidence = security.read_security_update_stamps(root=root)
            self.assertEqual(evidence.status, "fresh")
            (stamps / "upgrade-stamp").unlink()
            self.assertEqual(security.read_security_update_stamps(root=root).status, "missing")

    def test_unavailable_frames_and_clock_skew_never_become_fresh(self) -> None:
        now = datetime.now(timezone.utc).timestamp()
        fresh = HostSecurityUpdateEvidence(
            status="fresh",
            observed_at=now,
            completed_at=now - 1,
        )
        for frames in (
            [],
            [{"type": "failed"}],
            [{"type": "complete", "result": fresh.model_dump()}, {"type": "failed"}],
        ):
            with (
                self.subTest(frames=frames),
                patch.object(
                    native_copy_stream,
                    "_read_remote",
                    side_effect=lambda **kwargs: [kwargs["receive"](frame) for frame in frames],
                ),
            ):
                self.assertEqual(
                    security.read_host_security_updates(
                        host="provider", token="token", server_id="host"
                    ).status,
                    "unknown",
                )
        future = fresh.model_copy(update={"observed_at": now + 3600})
        with patch.object(
            native_copy_stream,
            "_read_remote",
            side_effect=lambda **kwargs: kwargs["receive"](
                {"type": "complete", "result": future.model_dump()}
            ),
        ):
            self.assertEqual(
                security.read_host_security_updates(
                    host="provider", token="token", server_id="host"
                ).status,
                "unknown",
            )

    def test_shared_hosts_are_read_once_and_fresh_evidence_clears_each_product_warning(
        self,
    ) -> None:
        from control_plane.storage.postgres import PostgresRecordStore
        from control_plane.contracts.deploy_target import ProviderTargetRecord
        from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
        from tests.test_product_read_service import _profile_payload
        from control_plane.contracts.product_environment_read_model import (
            build_product_site_overview,
        )

        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(database_url=f"sqlite+pysqlite:///{directory}/state.db")
            self.addCleanup(store.close)
            store.ensure_schema()
            profiles = []
            for product in ("one", "two"):
                profile = LaunchplaneProductProfileRecord.model_validate(
                    {**_profile_payload(product=product), "production_use": "live"}
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
                self.assertEqual(
                    len(security.host_security_update_warnings(store, profile, lane)), 1
                )
            now = datetime.now(timezone.utc).timestamp()
            stale = HostSecurityUpdateEvidence(
                status="stale",
                observed_at=now,
                completed_at=now - SECURITY_UPDATE_MAX_AGE_SECONDS - 10,
            )
            lanes = frozenset(
                (profile.product, lane.context, lane.instance)
                for profile in profiles
                for lane in profile.lanes
            )
            with (
                patch.object(security, "read_host_security_updates", return_value=stale) as reader,
                patch(
                    "control_plane.host_network_capacity.source.read_dokploy_config",
                    return_value=("provider", "token"),
                ),
                patch(
                    "control_plane.host_network_capacity.api.fetch_dokploy_target_payload",
                    return_value={"serverId": "shared"},
                ),
            ):
                from control_plane import health_monitor_scheduler as scheduler

                with (
                    patch.object(scheduler, "run_public_ingress_monitor_once"),
                    patch.object(scheduler, "public_ingress_notification_drivers"),
                ):
                    scheduler.HealthMonitorScheduler(store)._run_monitor(lanes)

                self.assertEqual(reader.call_count, 1)
                for profile in profiles:
                    lane = next(lane for lane in profile.lanes if lane.instance == "prod")
                    self.assertEqual(
                        len(security.host_security_update_warnings(store, profile, lane)), 1
                    )
                    overview = build_product_site_overview(
                        record_store=store, product=profile.product, action_allowed=lambda *_: True
                    )
                    production = next(
                        environment
                        for environment in overview.environments
                        if environment.environment == "prod"
                    )
                    self.assertTrue(
                        any(
                            warning in production.warnings
                            for warning in security.host_security_update_warnings(
                                store, profile, lane
                            )
                        )
                    )
                reader.return_value = stale.model_copy(
                    update={"status": "fresh", "completed_at": now - 1}
                )
                security.collect_host_security_updates(
                    store, lanes=lanes, should_stop=lambda: False
                )
                self.assertEqual(reader.call_count, 2)
                for profile in profiles:
                    lane = next(lane for lane in profile.lanes if lane.instance == "prod")
                    self.assertFalse(security.host_security_update_warnings(store, profile, lane))
                    self.assertTrue(
                        security.host_security_update_warnings(
                            store,
                            profile,
                            lane,
                            now=now + health_monitor_freshness_seconds("prod") + 1,
                        )
                    )
                reader.side_effect = OSError("unreachable")
                security.collect_host_security_updates(
                    store, lanes=lanes, should_stop=lambda: False
                )
                for profile in profiles:
                    lane = next(lane for lane in profile.lanes if lane.instance == "prod")
                    self.assertEqual(
                        len(security.host_security_update_warnings(store, profile, lane)), 1
                    )

    def test_generated_read_program_runs_with_only_the_standard_library(self) -> None:
        from contextlib import redirect_stdout
        from io import StringIO
        import json
        import pathlib
        import sys

        with TemporaryDirectory() as directory:
            root = Path(directory)
            stamps = root / "var/lib/apt/periodic"
            stamps.mkdir(parents=True)
            (stamps / "unattended-upgrades-stamp").touch()
            (stamps / "upgrade-stamp").touch()
            output = StringIO()
            original = pathlib.Path

            def fixture_path(value: str) -> Path:
                return root if value == "/" else original(value)

            with (
                patch.object(pathlib, "Path", side_effect=fixture_path),
                patch.object(sys, "argv", ["reader", "{}", "FRAME:"]),
                redirect_stdout(output),
            ):
                exec(security.SECURITY_UPDATE_PROGRAM, {})
            result = json.loads(output.getvalue().removeprefix("FRAME:"))
            self.assertEqual(result["type"], "complete")
            self.assertEqual(result["result"]["status"], "fresh")

    def test_host_movement_discards_the_prior_hosts_success(self) -> None:
        from control_plane.storage.postgres import PostgresRecordStore
        from control_plane.contracts.deploy_target import ProviderTargetRecord
        from control_plane.host_network_capacity import read_bound_lane_host

        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(database_url=f"sqlite+pysqlite:///{directory}/state.db")
            self.addCleanup(store.close)
            store.ensure_schema()
            store.write_provider_target_record(
                ProviderTargetRecord(
                    context="site",
                    instance="prod",
                    provider_id="dokploy",
                    target_category="compose",
                    target_id="site",
                    display_name="site",
                    updated_at=datetime.now(timezone.utc).isoformat(),
                )
            )
            with (
                patch(
                    "control_plane.host_network_capacity.source.read_dokploy_config",
                    return_value=("provider", "token"),
                ),
                patch(
                    "control_plane.host_network_capacity.api.fetch_dokploy_target_payload",
                    side_effect=[{"serverId": "old"}, {"serverId": "new"}],
                ),
            ):
                with self.assertRaises(ValueError):
                    read_bound_lane_host(
                        root=Path(directory),
                        store=store,
                        context="site",
                        instance="prod",
                        read=lambda *_: "fresh",
                    )
