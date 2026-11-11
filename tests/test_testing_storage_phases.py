from contextlib import contextmanager, nullcontext
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any, Iterator, cast
import unittest
from unittest.mock import patch

# noinspection PyProtectedMember
from control_plane._odoo_testing_storage_program import STORAGE_MAINTENANCE_LIBRARY
from control_plane.contracts.odoo_testing_drill import OdooTestingDrillStorageBudget
from control_plane.testing_storage_admission import (
    TESTING_STORAGE_CAPACITY_UNAVAILABLE,
    BackingMeasurement,
    TestingStorageCapacityRefused,
    require_migrated_restore_capacity,
)
from tests.test_testing_storage_admission import migrated_pair


class MigratedStoragePhaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.budget = OdooTestingDrillStorageBudget(
            database_bytes=40, filestore_bytes=160, staging_bytes=250, reserve_bytes=30
        )
        source = migrated_pair()
        self.source = source.model_copy(
            update={
                "database": source.database.model_copy(update={"size": 10}),
                "filestore": source.filestore.model_copy(update={"size": 90}),
                "filestore_expanded_bytes": 150,
            }
        )

    def admit(self, free: int, *, separate: bool = False) -> None:
        roles = {
            "volumes": BackingMeasurement("pool:fixture", free),
            "writable": BackingMeasurement("runner:fixture" if separate else "pool:fixture", free),
            "snapshot": BackingMeasurement(
                "snapshot:fixture" if separate else "pool:fixture", free
            ),
        }
        require_migrated_restore_capacity(roles, self.budget, self.source, clean_snapshot=True)

    @contextmanager
    def native(self, free: int) -> Iterator[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]]:
        namespace: dict[str, Any] = {"json": json}
        exec(STORAGE_MAINTENANCE_LIBRARY, namespace)
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            for role in ("database", "data", "runner", "snapshot"):
                (root / role).mkdir()
            namespace["docker"] = lambda *_args: json.dumps(
                [{"Mountpoint": str(root / ("database" if _args[-1].endswith("-db") else "data"))}]
            )
            namespace["clean_snapshot_root"] = lambda *_args, **_kwargs: root / "snapshot" / "copy"
            namespace["filestore_clone_proved"] = lambda *_args: True
            spec = {
                "generation": {
                    "namespace": "fixture",
                    "source_mode": "container-export",
                    "migrated_source": self.source.model_dump(mode="json"),
                    "clean_snapshot_enabled": True,
                },
                "copy_binding_sha256": "a" * 64,
            }
            containers = {
                "script-runner": (
                    "runner",
                    {"GraphDriver": {"Data": {"UpperDir": str(root / "runner")}}},
                )
            }
            with patch.object(
                os, "statvfs", return_value=SimpleNamespace(f_bavail=free, f_frsize=1)
            ):
                yield namespace, spec, containers

    def test_same_backing_admits_nonoverlapping_pair_and_full_snapshot(self) -> None:
        self.admit(440)
        with self.assertRaises(TestingStorageCapacityRefused):
            self.admit(439)

    def test_native_capacity_observation_covers_current_restore_phase(self) -> None:
        for free, expected in ((340, True), (339, False)):
            with self.subTest(free=free), self.native(free) as (namespace, spec, containers):
                observations: list[dict[str, Any]] = []
                self.assertEqual(
                    namespace["storage_capacity"](
                        spec,
                        containers,
                        self.budget.model_dump(),
                        runner_staging_bytes=self.source.database.size,
                        data_staging_bytes=self.source.database.size + self.source.filestore.size,
                        observations=observations,
                    ),
                    expected,
                )
                self.assertEqual(len(observations), 1)
                self.assertEqual(observations[0]["required_additional_bytes"], 310)
                self.assertEqual(observations[0]["reserve_bytes"], self.budget.reserve_bytes)

    def test_separate_backings_still_require_staging_and_each_full_reserve(self) -> None:
        self.admit(330, separate=True)
        with self.assertRaises(TestingStorageCapacityRefused):
            self.admit(329, separate=True)

    def test_partial_and_complete_uploads_do_not_charge_a_future_snapshot_again(self) -> None:
        self.admit(440)
        for uploaded in (0, 37, 100):
            with (
                self.subTest(uploaded=uploaded),
                self.native(440 - uploaded) as (namespace, spec, containers),
            ):
                data = Path(
                    json.loads(namespace["docker"]("volume", "inspect", "fixture-data"))[0][
                        "Mountpoint"
                    ]
                )
                identity = self.source.binding_sha256[:32]
                staging = data / (".lp-native-" + identity)
                staging.mkdir()
                database = min(uploaded, self.source.database.size)
                (staging / "database.artifact.partial").write_bytes(b"d" * database)
                (staging / "filestore.artifact.partial").write_bytes(b"f" * (uploaded - database))
                remaining = namespace["remaining_migrated_staging"](
                    data, identity, self.source.model_dump(mode="json")
                )
                self.assertTrue(
                    namespace["storage_capacity"](
                        spec,
                        containers,
                        self.budget.model_dump(),
                        runner_staging_bytes=self.source.database.size,
                        data_staging_bytes=remaining,
                    )
                )
                self.assertTrue(
                    namespace["storage_capacity"](
                        spec,
                        containers,
                        self.budget.model_dump(),
                        runner_staging_bytes=self.source.database.size,
                    )
                )

    def test_unknown_phase_order_keeps_staging_and_snapshot_overlapping(self) -> None:
        for free, expected in ((440, False), (540, True)):
            with self.subTest(free=free), self.native(free) as (namespace, spec, containers):
                del spec["generation"]["migrated_source"]
                self.assertEqual(
                    namespace["storage_capacity"](
                        spec,
                        containers,
                        self.budget.model_dump(),
                        runner_staging_bytes=self.source.database.size,
                        data_staging_bytes=self.source.database.size + self.source.filestore.size,
                    ),
                    expected,
                )

    def test_missing_backing_or_capacity_is_unavailable_instead_of_free_space(self) -> None:
        for backing in (None, BackingMeasurement("", 1000), BackingMeasurement("pool:fixture", -1)):
            with self.subTest(backing=backing):
                roles = {
                    "volumes": BackingMeasurement("pool:fixture", 1000),
                    "snapshot": BackingMeasurement("pool:fixture", 1000),
                }
                if backing is not None:
                    roles["writable"] = backing
                with self.assertRaises(TestingStorageCapacityRefused) as refused:
                    require_migrated_restore_capacity(
                        roles, self.budget, self.source, clean_snapshot=True
                    )
                self.assertEqual(refused.exception.code, TESTING_STORAGE_CAPACITY_UNAVAILABLE)

    def test_full_database_and_filestore_bounds_survive_small_compressed_inputs(self) -> None:
        self.source = self.source.model_copy(
            update={
                "database": self.source.database.model_copy(update={"size": 1}),
                "filestore": self.source.filestore.model_copy(update={"size": 1}),
                "filestore_expanded_bytes": 1,
            }
        )
        with self.assertRaises(TestingStorageCapacityRefused):
            self.admit(430)
        self.admit(431)


class SnapshotPhaseBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        from unittest.mock import Mock
        import shutil
        from control_plane._odoo_clean_snapshot_program import CLEAN_TESTING_SNAPSHOT_LIBRARY

        self.root = Path(self.enterContext(TemporaryDirectory())).resolve()
        self.data = self.root / "data"
        self.filestore = self.data / "filestore" / "fixture"
        self.filestore.mkdir(parents=True)
        (self.filestore / "attachment").write_bytes(b"fixture attachment")
        self.staging = self.data / (".lp-native-" + "b" * 32)
        self.staging.mkdir()
        self.namespace: dict[str, Any] = {"os": os}
        exec(CLEAN_TESTING_SNAPSHOT_LIBRARY + STORAGE_MAINTENANCE_LIBRARY, self.namespace)
        self.spec = {
            "copy_binding_sha256": "a" * 64,
            "database_name": "fixture",
            "generation": {
                "namespace": "fixture",
                "source_mode": "container-export",
                "source_binding_sha256": "b" * 64,
                "migrated_source": {"kind": "migrated"},
                "drill_storage_budget": {
                    "database_bytes": 40,
                    "filestore_bytes": 160,
                    "staging_bytes": 250,
                    "reserve_bytes": 30,
                },
            },
        }
        self.containers: dict[str, Any] = {"web": ("web", {}), "database": ("db", {})}

        def docker(*arguments: str) -> str:
            if arguments[0] == "inspect":
                return json.dumps([{"State": {"Running": False}}])
            return json.dumps([{"Mountpoint": str(self.data)}])

        def dump(_arguments: list[str], destination: Path, _bound: int) -> tuple[int, bytes, bool]:
            destination.write_bytes(b"snapshot dump")
            return 0, b"", False

        self.namespace["docker"] = docker
        self.namespace["capture_snapshot_file"] = Mock(side_effect=dump)
        self.namespace["copy_filestore"] = Mock(
            side_effect=lambda src, dst, _clone: shutil.copytree(src, dst)
        )
        self.namespace["filestore_clone_proved"] = lambda *_args: True

    def capture(self, free: int | None) -> dict[str, str]:
        with (
            patch.object(Path, "home", return_value=self.root),
            patch.object(os, "statvfs", return_value=SimpleNamespace(f_bavail=free, f_frsize=1))
            if free is not None
            else nullcontext(),
        ):
            return cast(
                dict[str, str],
                self.namespace["capture_clean_snapshot"](self.spec, self.containers, True),
            )

    def test_incomplete_cleanup_refuses_before_snapshot_dump_or_copy(self) -> None:
        for name in (
            "database.artifact",
            "filestore.artifact.partial",
            ".lp-native-" + "b" * 32 + "-partial",
        ):
            with self.subTest(name=name):
                staged = self.staging / name
                staged.write_bytes(b"retained staging")
                with self.assertRaises(RuntimeError):
                    self.capture(1000)
                self.assertEqual(staged.read_bytes(), b"retained staging")
                staged.unlink()
        self.namespace["capture_snapshot_file"].assert_not_called()
        self.namespace["copy_filestore"].assert_not_called()

    def test_cleanup_does_not_substitute_for_fresh_physical_free_space(self) -> None:
        for remaining in (0, 229):
            with self.subTest(remaining=remaining):
                staged = self.staging / "database.artifact"
                staged.write_bytes(b"fixture")
                with staged.open("rb") as pinned:
                    staged.unlink()
                    self.assertEqual(pinned.read(), b"fixture")
                    with self.assertRaises(RuntimeError):
                        self.capture(remaining)
        self.namespace["capture_snapshot_file"].assert_not_called()
        self.namespace["copy_filestore"].assert_not_called()

    def test_fresh_full_snapshot_demand_and_reserve_allow_capture_without_cleanup_history(
        self,
    ) -> None:
        self.staging.rmdir()
        receipt = self.capture(230)
        self.assertEqual(receipt["snapshot_id"], self.spec["copy_binding_sha256"])
        self.namespace["capture_snapshot_file"].assert_called_once()
        self.namespace["copy_filestore"].assert_called_once()

    def test_capacity_recheck_failure_stops_before_snapshot_effects(self) -> None:
        with patch.object(os, "statvfs", side_effect=OSError("fictional capacity read failed")):
            with self.assertRaises(OSError):
                self.capture(None)
        self.namespace["capture_snapshot_file"].assert_not_called()
        self.namespace["copy_filestore"].assert_not_called()


class MigratedCleanupPhaseTests(unittest.TestCase):
    def test_cleanup_removes_only_owned_staging_and_keeps_runner_copy(self) -> None:
        from tests.test_migrated_testing_source import MigratedArtifactConsumptionTests

        fixture = MigratedArtifactConsumptionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        capture = fixture.stage()
        with self.assertRaises(SystemExit):
            fixture.fixture.consume(capture, mode="prepare")
        dump = fixture.fixture.mapped_path(fixture.fixture.dump_path)
        runner_bytes = dump.read_bytes()
        (fixture.staging / "filestore.artifact.partial").write_bytes(b"interrupted upload")
        with self.assertRaises(SystemExit) as completed:
            fixture.fixture.consume(capture, mode="cleanup")
        self.assertEqual(completed.exception.code, 0)
        self.assertFalse(any(fixture.staging.iterdir()))
        self.assertEqual(dump.read_bytes(), runner_bytes)
        self.assertEqual(
            (fixture.filestore / fixture.fixture.name).read_bytes(), fixture.fixture.attachment
        )

    def test_cleanup_refuses_aliased_artifacts_and_retains_the_external_original(self) -> None:
        from tests.test_migrated_testing_source import MigratedArtifactConsumptionTests

        fixture = MigratedArtifactConsumptionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        capture = fixture.stage()
        original = fixture.fixture.root / "original-database"
        artifact = fixture.staging / "database.artifact"
        artifact.rename(original)
        artifact.symlink_to(original)
        with self.assertRaises(RuntimeError):
            fixture.fixture.consume(capture, mode="cleanup")
        self.assertTrue(artifact.is_symlink())
        self.assertEqual(original.read_bytes(), b"custom database fixture")


class RetainedCapacityConsumerTests(unittest.TestCase):
    def test_retained_receiver_observation_uses_current_phase_without_effects(self) -> None:
        import base64
        from control_plane.testing_candidate_retention_host import (
            NATIVE_TESTING_RETENTION_INSPECTION,
        )
        from tests.test_testing_candidate_release import (
            CurrentTestingReleaseProgramTests,
            NAMESPACE,
            resources,
        )

        model = MigratedStoragePhaseTests()
        model.setUp()
        fixture = CurrentTestingReleaseProgramTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        runner = fixture.root / "runner"
        runner.mkdir()
        fixture.state["containers"]["candidate-runner"] = {
            "Config": {
                "Labels": {
                    "com.docker.compose.project": NAMESPACE + "-abc123",
                    "com.docker.compose.service": "script-runner",
                }
            },
            "Mounts": [],
            "GraphDriver": {"Data": {"UpperDir": str(runner)}},
        }
        fixture.save()
        payload = base64.b64encode(
            json.dumps(
                {
                    "mode": "dry-run",
                    "resources": resources(),
                    "project": NAMESPACE,
                    "database_name": "fixture",
                    "copy_binding_sha256": "a" * 64,
                    "generation": {
                        "namespace": NAMESPACE,
                        "source_mode": "container-export",
                        "source_binding_sha256": model.source.binding_sha256,
                        "migrated_source": model.source.model_dump(mode="json"),
                        "drill_storage_budget": model.budget.model_dump(),
                        "clean_snapshot_enabled": True,
                    },
                }
            ).encode()
        ).decode()
        program = (
            "import os\nfrom pathlib import Path\nfrom types import SimpleNamespace\n"
            f"Path.home=classmethod(lambda cls:Path({str(fixture.root)!r}))\n"
            "os.statvfs=lambda path:SimpleNamespace(f_bavail=440,f_frsize=1,f_fsid=1)\n"
            + NATIVE_TESTING_RETENTION_INSPECTION
        )
        result = fixture.run_host_program(program, payload)
        self.assertEqual(result["capacity_refusal"], "")
        self.assertEqual(len(result["capacity"]), 1)
        capacity = result["capacity"][0]
        self.assertEqual(capacity["required_additional_bytes"], 310)
        self.assertEqual(capacity["reserve_bytes"], model.budget.reserve_bytes)
        self.assertEqual(capacity["deficit_bytes"], 0)
        self.assertEqual(fixture.removals(), [])


class RestorePhaseConsumerTests(unittest.TestCase):
    def test_generated_receiver_inspection_checks_phased_demand_before_streaming(self) -> None:
        from tests.test_odoo_testing_restore import FreshTestingHostProgramTests

        model = MigratedStoragePhaseTests()
        model.setUp()
        fixture = FreshTestingHostProgramTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.spec["inspection_only"] = True
        fixture.spec["generation"].update(
            source_mode="container-export",
            source_binding_sha256=model.source.binding_sha256,
            migrated_source=model.source.model_dump(mode="json"),
            clean_snapshot_enabled=True,
            drill_storage_budget=model.budget.model_dump(),
        )
        fixture.containers["script-runner"]["GraphDriver"] = {
            "Data": {"UpperDir": str(fixture.root)}
        }
        for free, expected in ((340, 0), (339, 1)):
            with (
                self.subTest(free=free),
                patch.object(
                    os, "statvfs", return_value=SimpleNamespace(f_bavail=free, f_frsize=1)
                ),
            ):
                status, output = fixture.run_program()
                self.assertEqual(status, expected, output)
                self.assertNotIn(["docker", "stop", "web"], fixture.calls)

    def test_restore_stops_after_failed_cleanup_or_lost_boundary_capacity(self) -> None:
        import base64
        import subprocess
        from tests.test_migrated_testing_source import MigratedArtifactConsumptionTests
        from tests.test_odoo_testing_restore import FreshTestingHostProgramTests

        for failure in ("cleanup", "physical_capacity"):
            with self.subTest(failure=failure):
                fixture = FreshTestingHostProgramTests()
                fixture.setUp()
                self.addCleanup(fixture.doCleanups)
                migrated = MigratedArtifactConsumptionTests()
                migrated.setUp()
                self.addCleanup(migrated.doCleanups)
                migrated.fixture.database = fixture.database
                capture = migrated.stage()
                original_data = migrated.filestore.parent.parent
                fixture.data_backing.rmdir()
                original_data.rename(fixture.data_backing)
                migrated.staging = fixture.data_backing / migrated.staging.name
                migrated.filestore = fixture.data_backing / "filestore" / fixture.database
                original_path = migrated.fixture.mapped_path

                def mapped_path(value: str) -> Path:
                    path = Path(value)
                    if path.is_relative_to("/volumes/data"):
                        return fixture.data_backing / path.relative_to("/volumes/data")
                    return original_path(value)

                capture.update(
                    generation_binding_sha256=fixture.spec["copy_binding_sha256"],
                    source_binding_sha256="f" * 64,
                )
                fixture.spec["native_capture"] = capture
                fixture.spec["generation"].update(
                    source_mode="container-export",
                    source_binding_sha256=capture["source_binding_sha256"],
                    migrated_source={
                        **capture["source_observation"],
                        "database": capture["database"],
                        "filestore": capture["filestore"],
                    },
                    clean_snapshot_enabled=True,
                    drill_storage_budget={
                        "database_bytes": 1024,
                        "filestore_bytes": 1024,
                        "staging_bytes": 1024,
                        "reserve_bytes": 1024,
                    },
                )
                fixture.containers["script-runner"]["GraphDriver"] = {
                    "Data": {"UpperDir": str(fixture.root)}
                }
                original = fixture.command
                modes: list[str] = []
                free = 100000

                def consume(
                    arguments: list[str], **kwargs: Any
                ) -> subprocess.CompletedProcess[bytes]:
                    nonlocal free
                    if arguments[:2] == ["docker", "stop"]:
                        fixture.containers["web"]["State"]["Running"] = False
                    if kwargs.get("input") != fixture.spec["native_files_program"].encode():
                        return original(arguments, **kwargs)
                    receiver = json.loads(base64.b64decode(arguments[-1]))
                    modes.append(receiver["mode"])
                    if receiver["mode"] == "cleanup" and failure == "cleanup":
                        return subprocess.CompletedProcess(
                            arguments, 1, stdout=b"", stderr=b"fictional cleanup failure"
                        )
                    with patch.object(migrated.fixture, "mapped_path", side_effect=mapped_path):
                        try:
                            migrated.fixture.consume(
                                receiver["capture"],
                                receiver["mode"],
                                fixture.spec["native_files_program"],
                            )
                        except SystemExit as completed:
                            self.assertEqual(completed.code, 0)
                    if receiver["mode"] == "cleanup":
                        free = 3071
                    return subprocess.CompletedProcess(arguments, 0, stdout=b"", stderr=b"")

                with (
                    patch.object(fixture, "command", side_effect=consume),
                    patch.object(
                        os,
                        "statvfs",
                        side_effect=lambda _path: SimpleNamespace(
                            f_bavail=free, f_frsize=1, f_blocks=free, f_bfree=free
                        ),
                    ),
                    patch("subprocess.Popen") as dump,
                ):
                    status, output = fixture.run_program()
                self.assertNotEqual(status, 0, output)
                self.assertEqual(modes, ["prepare", "cleanup"], output)
                dump.assert_not_called()
                self.assertNotIn(["docker", "start", "web"], fixture.calls)
                self.assertEqual(bool(list(migrated.staging.iterdir())), failure == "cleanup")
                self.assertEqual(
                    (migrated.filestore / migrated.fixture.name).read_bytes(),
                    migrated.fixture.attachment,
                )
