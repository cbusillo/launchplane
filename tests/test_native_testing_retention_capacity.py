from types import SimpleNamespace
from typing import Any
import json
import unittest
from unittest.mock import Mock

from control_plane._odoo_testing_storage_program import STORAGE_MAINTENANCE_LIBRARY


class NativeTestingRetentionCapacityTests(unittest.TestCase):
    def run_capacity(
        self, *, free: int, staged: int, capture: bool = False
    ) -> tuple[bool, list[dict[str, Any]]]:
        namespace: dict[str, Any] = {"json": json}
        exec(STORAGE_MAINTENANCE_LIBRARY, namespace)

        class Backing:
            def __init__(self, path: object) -> None:
                self.path = str(path)

            def __str__(self) -> str:
                return self.path

            def resolve(self, **_kwargs: object) -> "Backing":
                return self

            def stat(self) -> SimpleNamespace:
                return SimpleNamespace(st_dev=7)

            def exists(self) -> bool:
                return True

            def read_text(self) -> str:
                return "42 1 0:7 / /store rw - zfs tank/testing rw"

            @property
            def parent(self) -> "Backing":
                return self

        namespace["Path"] = Backing
        namespace["os"] = SimpleNamespace(
            makedev=lambda _major, minor: minor,
            statvfs=lambda _path: SimpleNamespace(f_bavail=free, f_frsize=1),
        )
        namespace["docker"] = lambda *args: json.dumps([{"Mountpoint": args[-1]}])
        namespace["clean_snapshot_root"] = lambda *_args, **_kwargs: Backing("snapshot")
        namespace["filestore_clone_proved"] = Mock(
            side_effect=AssertionError("read must not probe")
        )
        containers = {
            "script-runner": ("runner", {"GraphDriver": {"Data": {"UpperDir": "runner"}}})
        }
        observations: list[dict[str, Any]] = []
        result = namespace["storage_capacity"](
            {
                "generation": {
                    "namespace": "candidate",
                    "source_mode": "container-export",
                    "clean_snapshot_enabled": capture,
                },
                "copy_binding_sha256": "copy",
                "staging_directory": "host",
            },
            containers,
            {
                "database_bytes": 10,
                "filestore_bytes": 80,
                "staging_bytes": 500,
                "reserve_bytes": 20,
            },
            runner_staging_bytes=3,
            host_staging_bytes=0,
            data_staging_bytes=staged,
            probe_clones=False,
            observations=observations,
        )
        return bool(result), observations

    def test_projection_uses_receiver_demand_and_one_reserve_per_shared_backing(self) -> None:
        passed, rows = self.run_capacity(free=105, staged=7)
        self.assertFalse(passed)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["required_additional_bytes"], 100)
        self.assertEqual(rows[0]["reserve_bytes"], 20)
        self.assertEqual(rows[0]["free_bytes"], 105)
        self.assertEqual(rows[0]["deficit_bytes"], 15)

    def test_already_staged_pair_is_not_charged_twice_and_capture_is_explicit(self) -> None:
        passed, staged = self.run_capacity(free=113, staged=0)
        self.assertTrue(passed)
        self.assertEqual(staged[0]["required_additional_bytes"], 93)
        passed, capture = self.run_capacity(free=113, staged=0, capture=True)
        self.assertFalse(passed)
        self.assertEqual(capture[0]["required_additional_bytes"], 183)
        self.assertEqual(capture[0]["deficit_bytes"], 90)
