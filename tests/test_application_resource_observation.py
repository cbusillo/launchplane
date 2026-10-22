from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from control_plane.contracts.application_resource_observation import ApplicationResourceObservation
from control_plane.dokploy.application_resources import APPLICATION_RESOURCE_OBSERVATION_PROGRAM
from tests.support.application_resources import ApplicationResourceFixture


class ApplicationResourceObservationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.fixture = ApplicationResourceFixture(Path(temporary.name))
        self.payload: dict[str, object] = {
            "application": self.fixture.application,
            "application_id": "application1",
            "server_id": "registered-server",
            "configured_volumes": ["owned-state"],
            "configured_unsupported_mounts": False,
        }

    def observe(self) -> ApplicationResourceObservation:
        frame = self.fixture.run(APPLICATION_RESOURCE_OBSERVATION_PROGRAM, self.payload)
        self.assertEqual(frame["type"], "complete")
        return ApplicationResourceObservation.model_validate(frame["result"])

    def test_named_state_presence_is_reported_without_reading_or_exporting_files(self) -> None:
        private_name = "synthetic-private-filename"
        private_value = "synthetic-private-value"
        retained = self.fixture.data / private_name
        retained.write_text(private_value)
        observed = self.observe()
        self.assertEqual(observed.status, "observed")
        self.assertEqual(observed.nonempty_volume_count, 1)
        self.assertEqual(observed.empty_volume_count, 0)
        self.assertEqual(
            observed.image_declared_volume_count, len(self.fixture.image_row["Volumes"])
        )
        self.assertFalse(observed.service_quiescent)
        self.assertIs(observed.unsupported_mounts_present, False)
        self.assertNotIn(private_name, observed.model_dump_json())
        self.assertNotIn(private_value, observed.model_dump_json())
        self.assertEqual(retained.read_text(), private_value)

    def test_empty_existing_volume_is_observation_only_and_requires_no_new_resource(self) -> None:
        self.fixture.service["Mode"]["Replicated"]["Replicas"] = 0
        self.fixture.tasks["task1"].update(DesiredState="shutdown", Status={"State": "failed"})
        observed = self.observe()
        self.assertEqual(observed.status, "observed")
        self.assertEqual(observed.empty_volume_count, 1)
        self.assertTrue(observed.service_quiescent)
        self.assertEqual(list(self.fixture.data.iterdir()), [])

    def test_exited_container_is_included_when_no_task_is_running(self) -> None:
        self.fixture.containers["c" * 64] = {
            "Id": "c" * 64,
            "Name": "/" + self.fixture.application + ".1.old-task",
            "Labels": {
                "com.docker.swarm.service.id": self.fixture.service["ID"],
                "com.docker.swarm.task.id": "retained-task",
            },
            "Mounts": [{"Type": "volume", "Name": "owned-state"}],
            "Status": "exited",
        }
        observed = self.observe()
        self.assertEqual(observed.extant_container_count, 1)
        self.assertFalse(observed.service_quiescent)

    def test_remote_task_duplicate_inventory_and_changed_inventory_remain_unknown(self) -> None:
        for change in ("remote-task", "duplicate-volumes", "changed-containers"):
            with self.subTest(change=change):
                self.fixture.tasks["task1"]["NodeID"] = (
                    "remote-node" if change == "remote-task" else "node1"
                )
                self.fixture.volumes = (
                    ["owned-state", "owned-state"]
                    if change == "duplicate-volumes"
                    else ["owned-state"]
                )
                self.fixture.change_containers = change == "changed-containers"
                self.fixture.container_list_calls = 0
                observed = self.observe()
                self.assertEqual(observed.status, "unknown")
                self.assertIsNone(observed.empty_volume_count)

    def test_unavailable_or_redirected_backing_is_not_reported_empty(self) -> None:
        for change in ("missing-volume", "remote-driver", "driver-options", "redirected-backing"):
            with self.subTest(change=change):
                self.fixture.volumes = [] if change == "missing-volume" else ["owned-state"]
                self.fixture.volume.update(
                    Driver="remote" if change == "remote-driver" else "local",
                    Options={"type": "nfs"} if change == "driver-options" else None,
                    Mountpoint=str(self.fixture.root)
                    if change == "redirected-backing"
                    else str(self.fixture.data),
                )
                observed = self.observe()
                self.assertEqual(observed.unavailable_volume_count, 1)
                self.assertEqual(observed.empty_volume_count, 0)

    def test_daemon_denial_is_explicit_without_retaining_its_detail(self) -> None:
        self.fixture.denied = True
        frame = self.fixture.run(APPLICATION_RESOURCE_OBSERVATION_PROGRAM, self.payload)
        self.assertEqual(frame, {"type": "denied"})
        self.fixture.denied = False
        self.fixture.image_error = b"authorization denied by plugin: synthetic-private-detail"
        frame = self.fixture.run(APPLICATION_RESOURCE_OBSERVATION_PROGRAM, self.payload)
        self.assertEqual(frame, {"type": "denied"})

    def test_image_aliases_match_and_missing_image_metadata_preserves_storage_observation(
        self,
    ) -> None:
        digest = self.fixture.image.rpartition("@")[2]
        self.fixture.service["Image"] = "example:build@" + digest
        self.fixture.image_row["RepoDigests"] = ["docker.io/library/example@" + digest]
        observed = self.observe()
        self.assertEqual(observed.status, "observed")
        self.assertEqual(
            observed.image_declared_volume_count, len(self.fixture.image_row["Volumes"])
        )
        for unavailable in ("mismatched", "missing"):
            with self.subTest(unavailable=unavailable):
                self.fixture.image_row["RepoDigests"] = ["different-repository@" + digest]
                self.fixture.image_error = b"No such image" if unavailable == "missing" else None
                observed = self.observe()
                self.assertEqual(observed.status, "observed")
                self.assertEqual(observed.empty_volume_count, 1)
                self.assertIsNone(observed.image_declared_volume_count)

    def test_repeated_unsupported_mounts_are_reported_as_presence(self) -> None:
        self.payload["configured_unsupported_mounts"] = True
        self.fixture.service["Mounts"].append(
            {"Type": "bind", "Source": str(self.fixture.root), "Target": "/external"}
        )
        observed = self.observe()
        self.assertEqual(observed.status, "observed")
        self.assertIs(observed.unsupported_mounts_present, True)

    def test_service_quiescence_does_not_claim_other_volume_consumers_are_excluded(self) -> None:
        self.fixture.service["Mode"]["Replicated"]["Replicas"] = 0
        self.fixture.tasks["task1"].update(DesiredState="shutdown", Status={"State": "failed"})
        identifier = "b" * 64
        self.fixture.containers[identifier] = {
            "Id": identifier,
            "Name": "/other-service.writer",
            "Labels": {"com.docker.swarm.service.id": "other-service"},
            "Mounts": [{"Type": "volume", "Name": "owned-state"}],
            "Status": "running",
        }
        observed = self.observe()
        self.assertEqual(observed.status, "observed")
        self.assertTrue(observed.service_quiescent)
        self.assertEqual(observed.extant_container_count, 0)

    def test_incomplete_cluster_or_ambiguous_container_does_not_produce_counts(self) -> None:
        self.fixture.nodes.append({"ID": "node2", "Self": False})
        self.assertEqual(self.observe().status, "unknown")
        self.fixture.nodes.pop()
        self.fixture.containers["d" * 64] = {
            "Id": "d" * 64,
            "Name": "/" + self.fixture.application + ".1.unattributed",
            "Labels": {},
            "Mounts": [{"Type": "volume", "Name": "unrelated-state"}],
            "Status": "exited",
        }
        self.assertEqual(self.observe().status, "unknown")

    def test_task_reference_cannot_hide_an_extant_foreign_container(self) -> None:
        identifier = "e" * 64
        self.fixture.service["Mode"]["Replicated"]["Replicas"] = 0
        self.fixture.tasks["task1"].update(
            DesiredState="shutdown",
            Status={"State": "failed", "ContainerStatus": {"ContainerID": identifier}},
        )
        self.fixture.containers[identifier] = {
            "Id": identifier,
            "Name": "/foreign-service.1.foreign-task",
            "Labels": {
                "com.docker.swarm.service.id": "foreign-service",
                "com.docker.swarm.task.id": "foreign-task",
            },
            "Mounts": [],
            "Status": "exited",
        }
        observed = self.observe()
        self.assertEqual(observed.status, "unknown")
        self.assertIsNone(observed.service_quiescent)

    def test_symlinked_backing_is_unavailable_without_following_it(self) -> None:
        redirected = self.fixture.root / "retained"
        redirected.mkdir()
        (redirected / "private-name").write_text("private-content")
        self.fixture.data.rmdir()
        self.fixture.data.symlink_to(redirected, target_is_directory=True)
        observed = self.observe()
        self.assertEqual(observed.unavailable_volume_count, 1)
        self.assertEqual(observed.empty_volume_count, 0)
        self.assertNotIn("private", observed.model_dump_json())

    def test_matching_task_container_is_observed_and_keeps_sample_nonservice_quiescent(
        self,
    ) -> None:
        identifier = "f" * 64
        self.fixture.service["Mode"]["Replicated"]["Replicas"] = 0
        self.fixture.tasks["task1"].update(
            DesiredState="shutdown",
            Status={"State": "failed", "ContainerStatus": {"ContainerID": identifier}},
        )
        self.fixture.containers[identifier] = {
            "Id": identifier,
            "Name": "/" + self.fixture.application + ".1.task1",
            "Labels": {
                "com.docker.swarm.service.id": self.fixture.service["ID"],
                "com.docker.swarm.task.id": "task1",
            },
            "Mounts": [{"Type": "volume", "Name": "owned-state"}],
            "Status": "exited",
        }
        observed = self.observe()
        self.assertEqual(observed.status, "observed")
        self.assertEqual(observed.extant_container_count, 1)
        self.assertFalse(observed.service_quiescent)
        self.fixture.tasks["task2"] = dict(self.fixture.tasks["task1"], ID="task2")
        self.assertEqual(self.observe().status, "unknown")
        del self.fixture.tasks["task2"]
        self.fixture.tasks["task1"]["Status"]["ContainerStatus"]["ContainerID"] = "a" * 64
        self.assertEqual(self.observe().status, "unknown")

    def test_malformed_task_container_reference_is_unknown(self) -> None:
        malformed_values: tuple[object, ...] = (False, 0, [], "")
        for malformed in malformed_values:
            with self.subTest(malformed=malformed):
                self.fixture.tasks["task1"]["Status"]["ContainerStatus"] = malformed
                self.assertEqual(self.observe().status, "unknown")
