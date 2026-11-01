from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import shutil
import time
from typing import Any, cast
from unittest.mock import patch
from urllib.parse import urlsplit, urlunsplit

from control_plane import retained_volume_capture
from control_plane import generic_web_testing_rebuild as native_rebuild
from control_plane.dokploy import api, native_copy_stream
from control_plane.dokploy_inventory import TARGET_COLLECTIONS
from control_plane.retained_volume_capture import retained_volume_engine_identity
from control_plane.retained_volume_sweep_program import RETAINED_VOLUME_SWEEP_HOST_PROGRAM
from control_plane.contracts.product_profile_record import (
    ProductGenericWebTestingState,
    ProductLaneHealthCheck,
    ProductLaneHealthMonitoringPolicy,
)
from control_plane.contracts.retained_volume import RetainedVolumeBinding
from control_plane.testing_activation import activation_record
from control_plane.testing_rebuild_service import NativeTestingRebuildPending
from control_plane.traffic_switch import MAX_READINESS_AGE_SECONDS
from control_plane.testing_lane_hold import apply_testing_hold_plan
from tests import test_generic_web_testing_rebuild as rebuild
from tests import test_retained_volume_sweep_program as sweep_program


class GenericWebTestingVolumeRebuildFixture(rebuild.GenericWebTestingRebuildFlowFixture):
    with_unresolved_predecessor = True

    def configure_serving_application(self) -> None:
        self.sweep = sweep_program.RetainedVolumeSweepProgramTests()
        self.sweep.setUp()
        self.addCleanup(self.sweep.doCleanups)
        self.flow.storage.root = self.sweep.docker_root / "volumes"
        self.captured: dict[str, dict[str, Any]] = {}
        self.lose_volume_reply = False
        self.cleanup_elapsed = 0.0
        read_text = Path.read_text

        def read(path: Path, *args: Any, **kwargs: Any) -> str:
            if path == Path("/proc/sys/kernel/random/boot_id"):
                return "fixture-boot"
            return read_text(path, *args, **kwargs)

        self.enterContext(patch.object(Path, "read_text", read))
        self.flow.prepare()
        self.assertEqual(self.flow.activate().status, "completed")
        current = activation_record(self.store, self.flow.product, self.flow.context)
        assert current is not None and current.request.application is not None
        self.original = current
        self.original_volume = next(
            item["volumeName"] for item in self.flow.payloads[current.target_id]["mounts"]
        )
        profile = self.store.read_product_profile_record(self.flow.product)
        lane = next(item for item in profile.lanes if item.instance == "testing")
        lane.generic_web_testing_state = ProductGenericWebTestingState.model_validate(
            current.request.application.state.model_dump()
        )
        self.address = lane.base_url
        if not self.address:
            endpoint = next(
                endpoint
                for endpoint in self.store.list_private_health_endpoint_records()
                if (endpoint.product, endpoint.context, endpoint.instance)
                == (profile.product, lane.context, "testing")
            )
            address = urlsplit(endpoint.url)
            self.address = urlunsplit((address.scheme, address.netloc, "", "", ""))
        self.store.write_product_profile_record(profile)
        self.enterContext(
            patch.object(
                retained_volume_capture, "capture_retained_volumes", side_effect=self.capture_volume
            )
        )
        remote = native_copy_stream.run_managed_server_program
        provider = api.dokploy_request

        def projects(**_kwargs: Any) -> list[dict[str, Any]]:
            result = json.loads(json.dumps(self.flow.projects))
            for project in result:
                project.setdefault("env", "")
                for environment in project["environments"]:
                    for collection in TARGET_COLLECTIONS:
                        environment.setdefault(collection, [])
            return cast(list[dict[str, Any]], result)

        def transport(**kwargs: Any) -> Any:
            if kwargs["path"] == "/api/environment.one":
                for project in self.flow.projects:
                    for environment in project["environments"]:
                        if environment["environmentId"] == kwargs["query"]["environmentId"]:
                            return {**environment, "projectId": project["projectId"], "env": ""}
                raise AssertionError(kwargs)
            return provider(**kwargs)

        def sweep(**kwargs: Any) -> None:
            if kwargs["program"] != RETAINED_VOLUME_SWEEP_HOST_PROGRAM:
                remote(**kwargs)
                return
            payload = kwargs["payload"]
            name = payload["volume"]
            persisted = json.loads(self.sweep.state_path.read_text())
            for field in ("calls", "raw_calls", "rm_args"):
                if field in persisted:
                    self.sweep.state[field] = persisted[field]
            self.sweep.state.update(self.captured[name])
            self.sweep.state.update(
                containers=list(self.flow.containers),
                services=[row["ID"] for row in self.flow.services.values()],
                task_per_service=True,
                clock=time.monotonic() + self.cleanup_elapsed,
                removed=name not in self.flow.storage.volumes,
                extra_volumes=[volume for volume in self.flow.storage.volumes if volume != name],
                container_ref=any(
                    mount.get("Name") == name
                    for container in self.flow.containers.values()
                    for mount in container.get("Mounts", [])
                ),
                service_ref=any(
                    mount.get("Source") == name
                    for service in self.flow.services.values()
                    for mount in service["Spec"]["TaskTemplate"]["ContainerSpec"].get("Mounts", [])
                ),
                task_ref=any(
                    mount.get("Source") == name
                    for task in self.flow.tasks.values()
                    for mount in task["Spec"]["ContainerSpec"].get("Mounts", [])
                ),
            )
            self.sweep.write_state()

            def send() -> None:
                result = self.sweep.run_program(
                    payload["mode"],
                    identity=payload["identity"],
                    operation=payload["operation"],
                    engine_identity=payload["engine_identity"],
                    retirement=payload.get("retirement"),
                )
                if json.loads(self.sweep.state_path.read_text()).get("removed"):
                    volume = self.flow.storage.volumes.pop(name, None)
                    if volume is not None:
                        shutil.rmtree(Path(str(volume["Mountpoint"])).parent)
                if result["status"] == "removed" and self.lose_volume_reply:
                    self.lose_volume_reply = False
                    raise OSError("Lost volume removal response after effect")
                kwargs["receive"]({"type": "complete", "result": result})

            guard = kwargs.get("dispatch_guard")
            if guard:
                guard(send)
            else:
                send()

        self.enterContext(patch.object(api, "fetch_dokploy_projects", side_effect=projects))
        self.enterContext(patch.object(api, "dokploy_request", side_effect=transport))
        self.enterContext(
            patch.object(native_copy_stream, "run_managed_server_program", side_effect=sweep)
        )

    def capture_volume(self, **kwargs: Any) -> tuple[tuple[RetainedVolumeBinding, ...], bool]:
        payload = kwargs["payload"]
        name = payload["mounts"][0]["volumeName"]
        metadata = self.flow.storage.volumes[name]
        self.sweep.state.update(name=name, volume_labels=metadata["Labels"])
        self.sweep.data = Path(str(metadata["Mountpoint"]))
        self.captured[name] = {"name": name, "volume_labels": metadata["Labels"]}
        return (
            RetainedVolumeBinding(
                volume_name=name,
                application_id=payload["applicationId"],
                application_name=payload["appName"],
                server_id=payload["serverId"],
                volume_identity_sha256=self.sweep.identity(),
                engine_identity_sha256=retained_volume_engine_identity("engine-one"),
            ),
        ), True


class GenericWebTestingVolumeRebuildTests(GenericWebTestingVolumeRebuildFixture):
    def test_rebuild_uses_fresh_state_and_retires_the_original_volume_after_health(self) -> None:
        self.cleanup_elapsed = MAX_READINESS_AGE_SECONDS + 1
        previous = self.original
        volume = self.original_volume
        for attempt in (1, 2):
            original_path = Path(str(self.flow.storage.volumes[volume]["Mountpoint"]))
            (original_path / "old-data").write_text("Retained previous testing data")
            result = self.execute(attempt)
            self.assertEqual(result.health_status, "pass", result.error_message)
            self.assertEqual(result.post_deploy_status, "pass")
            self.assertEqual(result.health_url, self.address)
            current = activation_record(self.store, self.flow.product, self.flow.context)
            assert current is not None
            current_mount = self.flow.payloads[current.target_id]["mounts"][0]
            self.assertNotEqual(current_mount["volumeName"], volume)
            self.assertNotIn(volume, self.flow.storage.volumes)
            self.assertNotIn(previous.target_id, self.flow.payloads)
            self.assertFalse(
                (
                    Path(str(self.flow.storage.volumes[current_mount["volumeName"]]["Mountpoint"]))
                    / "old-data"
                ).exists()
            )
            self.assertEqual(
                self.flow.normal(f"after-volume-rebuild-{attempt}").status, "completed"
            )
            previous, volume = current, current_mount["volumeName"]


class PrivateWebTestingVolumeRebuildTests(GenericWebTestingVolumeRebuildTests):
    def test_private_record_change_after_own_hold_finishes_refused_without_candidate_effects(
        self,
    ) -> None:
        apply = apply_testing_hold_plan

        def changed(**kwargs: Any) -> Any:
            result = apply(**kwargs)
            if kwargs["request"].hold:
                endpoint = self.store.list_private_health_endpoint_records()[0]
                self.store.write_private_health_endpoint_record(
                    endpoint.model_copy(update={"updated_at": datetime.now(UTC).isoformat()})
                )
            return result

        before = set(self.flow.payloads)
        with patch.object(native_rebuild, "apply_testing_hold_plan", side_effect=changed):
            result = self.execute(1)
        self.assertEqual(result.deploy_status, "fail")
        self.assertEqual(result.error_code, "testing_rebuild_admission_changed")
        self.assertEqual(set(self.flow.payloads), before)
        self.assertIn(self.original_volume, self.flow.storage.volumes)
        self.assertIsNone(
            self.store.read_dokploy_target_record(
                context_name=self.flow.context, instance_name="testing"
            ).policies.staff_testing_hold
        )

    def test_private_record_refresh_after_cutover_keeps_original_recovery_and_current_health(
        self,
    ) -> None:
        def refresh() -> None:
            endpoint = self.store.list_private_health_endpoint_records()[0]
            self.store.write_private_health_endpoint_record(
                endpoint.model_copy(update={"updated_at": datetime.now(UTC).isoformat()})
            )

        self.after_retirement = refresh
        result = self.execute(1)
        self.assertEqual(result.health_status, "pass", result.error_message)
        self.assertEqual(result.post_deploy_status, "pass", result.error_message)
        self.assertNotIn(self.original_volume, self.flow.storage.volumes)
        self.assertIsNone(
            self.store.read_dokploy_target_record(
                context_name=self.flow.context, instance_name="testing"
            ).policies.staff_testing_hold
        )

    def configure_route(self) -> Any:
        route, _, _ = self.flow.private_addressed()
        profile = self.flow.store.read_product_profile_record(self.flow.product)
        lane = next(item for item in profile.lanes if item.instance == "testing")
        endpoint = next(
            endpoint
            for endpoint in self.flow.store.list_private_health_endpoint_records()
            if (endpoint.product, endpoint.context, endpoint.instance)
            == (profile.product, lane.context, "testing")
        )
        lane.health_monitoring = ProductLaneHealthMonitoringPolicy(
            monitoring_intent="private",
            checks=(
                ProductLaneHealthCheck(
                    name="application",
                    kind="private_http",
                    private_endpoint_key=endpoint.endpoint_key,
                ),
            ),
        )
        self.flow.store.write_product_profile_record(profile)
        return route


class TestingVolumeRebuildRecoveryTests(GenericWebTestingVolumeRebuildFixture):
    def test_lost_volume_removal_response_uses_original_key_readback(self) -> None:
        self.lose_volume_reply = True
        with self.assertRaises(NativeTestingRebuildPending):
            self.execute(1)
        self.assertNotIn(self.original_volume, self.flow.storage.volumes)
        with (
            patch.object(
                self.store,
                "_database_mutation_timestamp",
                return_value=(datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
            ),
            patch.object(
                native_rebuild,
                "inspect_application_state",
                side_effect=ValueError("Transient incomplete original service inventory"),
            ),
            self.assertRaises(NativeTestingRebuildPending),
        ):
            self.execute(1)
        with patch.object(
            self.store,
            "_database_mutation_timestamp",
            return_value=(datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
        ):
            result = self.execute(1)
        self.assertEqual(result.health_status, "pass", result.error_message)
        state = json.loads(self.sweep.state_path.read_text())
        self.assertEqual(state["calls"].count(["volume", "rm", self.original_volume]), 1)
        self.assertEqual(self.flow.normal("after-volume-readback").status, "completed")

    def test_alias_retains_old_volume_until_next_rebuild_can_prove_cleanup(self) -> None:
        self.sweep.state["alias"] = True
        result = self.execute(1)
        self.assertEqual(result.health_status, "pass", result.error_message)
        self.assertIn(self.original_volume, self.flow.storage.volumes)
        self.assertIn(self.original.target_id, result.runtime_source["retained_predecessors"])
        self.retained_predecessors.add(self.original.target_id)
        self.sweep.state["alias"] = False
        result = self.execute(2)
        self.assertEqual(result.health_status, "pass", result.error_message)
        self.assertNotIn(self.original_volume, self.flow.storage.volumes)
        self.assertEqual(self.flow.normal("after-retained-volume").status, "completed")

    def test_failed_candidate_preserves_original_volume_and_serving_application(self) -> None:
        self.flow.failed_image_type = "application"
        path = Path(str(self.flow.storage.volumes[self.original_volume]["Mountpoint"]))
        (path / "keep").write_text("original data")
        result = self.execute(1)
        self.assertEqual(result.deploy_status, "fail")
        self.assertEqual(result.target_id, self.original.target_id)
        self.assertEqual((path / "keep").read_text(), "original data")
        self.assertIn(self.original.target_id, self.flow.payloads)
        self.assertNotIn("rm_args", json.loads(self.sweep.state_path.read_text()))
