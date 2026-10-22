from contextlib import redirect_stdout
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch


from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.dokploy.api import JsonObject
from control_plane.dokploy import native_copy_stream
from control_plane.dokploy import memory_capacity
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.generic_web_preview import _ensure_application
from control_plane.workflows.odoo_preview_runtime import (
    _resolve_or_create_compose,
    build_odoo_preview_dokploy_dry_run,
    OdooPreviewDokployDryRunRequest,
)
from tests.test_odoo_preview_runtime import _endpoint_spec, _runtime_plan


class PreviewMemoryCapacityTests(unittest.TestCase):
    def test_host_program_uses_smallest_actual_memory_budget_and_refuses_unknown(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            proc = root / "proc"
            cgroup = root / "cgroup"
            (proc / "self").mkdir(parents=True)
            (cgroup / "worker").mkdir(parents=True)
            (proc / "self/cgroup").write_text("0::/worker\n")
            (proc / "meminfo").write_text("MemAvailable: 100 kB\n")
            (cgroup / "memory.max").write_text("24000")
            (cgroup / "memory.current").write_text("20000")
            (cgroup / "worker/memory.max").write_text("max")
            (cgroup / "worker/memory.current").write_text("1000")
            paths = {"/proc": proc, "/sys/fs/cgroup": cgroup}

            def run_program() -> dict[str, object]:
                output = io.StringIO()
                with (
                    patch("pathlib.Path", side_effect=lambda path: paths.get(path, Path(path))),
                    patch("sys.argv", ["program", "{}", "frame:"]),
                    redirect_stdout(output),
                ):
                    exec(memory_capacity.MEMORY_CAPACITY_PROGRAM, {})
                return dict(json.loads(output.getvalue().removeprefix("frame:")))

            self.assertEqual(run_program(), {"type": "complete", "result": 4000})
            (cgroup / "worker/memory.max").write_text("3000")
            self.assertEqual(run_program()["result"], 2000)
            (proc / "meminfo").write_text("MemAvailable: 1 kB\n")
            self.assertEqual(run_program()["result"], 1024)
            (cgroup / "memory.current").unlink()
            self.assertEqual(run_program()["type"], "failed")
            (cgroup / "memory.current").write_text("20000")
            (cgroup / "memory.max").write_text("max")
            (cgroup / "worker/memory.max").write_text("max")
            (proc / "meminfo").write_text("MemTotal: 24 kB\nMemAvailable: 100 kB\nMemFree: 4 kB\n")
            (proc / "self/mountinfo").write_text(
                f"1 0 0:1 /proc/meminfo {proc / 'meminfo'} rw - fuse.lxcfs lxcfs rw\n"
            )
            self.assertEqual(run_program()["result"], 4096)
            (proc / "self/mountinfo").write_text("")
            self.assertEqual(run_program()["type"], "failed")
            (cgroup / "memory.max").unlink()
            (cgroup / "memory.current").unlink()
            self.assertEqual(run_program()["result"], 100 * 1024)

    def test_service_record_floor_controls_selected_host_and_boundary(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = f"sqlite+pysqlite:///{Path(directory) / 'state.sqlite3'}"
            store = PostgresRecordStore(database_url=database_url)
            self.addCleanup(store.close)
            store.ensure_schema()
            store.write_runtime_environment_record(
                RuntimeEnvironmentRecord(
                    scope="context",
                    context="launchplane",
                    env={memory_capacity.PREVIEW_MEMORY_FLOORS_KEY: '{"host-a": 5000}'},
                    updated_at="2026-10-22T13:00:00Z",
                )
            )
            with patch.object(
                memory_capacity, "read_host_memory_headroom", return_value=4999
            ) as read:
                with self.assertRaises(memory_capacity.PreviewMemoryCapacityRefused) as refused:
                    memory_capacity.require_preview_host_memory_capacity(
                        host="https://provider.invalid",
                        token="fixture",
                        server_id="host-a",
                        database_url=database_url,
                    )
                self.assertIn("5000", str(refused.exception))
                self.assertIn("4999", str(refused.exception))
                self.assertEqual(read.call_args.kwargs["server_id"], "host-a")
                read.return_value = 5000
                memory_capacity.require_preview_host_memory_capacity(
                    host="https://provider.invalid",
                    token="fixture",
                    server_id="host-a",
                    database_url=database_url,
                )
                read.reset_mock()
                memory_capacity.require_preview_host_memory_capacity(
                    host="https://provider.invalid",
                    token="fixture",
                    server_id="host-b",
                    database_url=database_url,
                )
                read.assert_not_called()

    def test_invalid_service_policy_refuses_without_host_read(self) -> None:
        for raw in ('{"host-a":0}', '{"host-a":true}', "[]", "invalid", 5000):
            store = Mock()
            store.list_runtime_environment_records.return_value = (
                RuntimeEnvironmentRecord(
                    scope="context",
                    context="launchplane",
                    env={memory_capacity.PREVIEW_MEMORY_FLOORS_KEY: raw},
                    updated_at="fixture",
                ),
            )
            with (
                self.subTest(raw=raw),
                patch.object(memory_capacity, "read_host_memory_headroom") as read,
            ):
                with self.assertRaises(memory_capacity.PreviewMemoryCapacityRefused):
                    memory_capacity.require_preview_host_memory_capacity(
                        host="provider", token="fixture", server_id="host-a", record_store=store
                    )
                read.assert_not_called()

    def test_verireel_refuses_before_database_bootstrap(self) -> None:
        from control_plane.workflows.verireel_preview_driver import execute_verireel_preview_refresh
        from tests.test_verireel_preview_driver import _template_target, _refresh_request

        with (
            patch(
                "control_plane.workflows.verireel_preview_driver.dokploy_source.read_dokploy_config",
                return_value=("provider", "fixture"),
            ),
            patch(
                "control_plane.workflows.verireel_preview_driver._template_application_payload",
                return_value=(
                    _template_target(),
                    {
                        "serverId": "host-a",
                        "env": "DATABASE_URL=postgresql://fixture:fixture@db:5432/testing",
                    },
                ),
            ),
            patch(
                "control_plane.workflows.verireel_preview_driver._resolve_preview_url",
                return_value="https://preview.invalid",
            ),
            patch(
                "control_plane.workflows.verireel_preview_driver._find_application_by_name",
                return_value=None,
            ),
            patch.object(
                memory_capacity,
                "require_preview_host_memory_capacity",
                side_effect=memory_capacity.PreviewMemoryCapacityRefused("short"),
            ) as admit,
            patch(
                "control_plane.workflows.verireel_preview_driver._run_application_command"
            ) as bootstrap,
            patch("control_plane.workflows.verireel_preview_driver._ensure_application") as create,
        ):
            with self.assertRaises(memory_capacity.PreviewMemoryCapacityRefused):
                execute_verireel_preview_refresh(
                    control_plane_root=Path("."), request=_refresh_request()
                )
            bootstrap.assert_not_called()
            create.assert_not_called()
            self.assertEqual(admit.call_args.kwargs["server_id"], "host-a")

    def test_remote_observation_is_complete_and_preserves_denial(self) -> None:
        for frames in (
            [],
            [{"type": "failed"}],
            [{"type": "complete", "result": True}],
            [{"type": "complete", "result": -1}],
            [{"type": "complete", "result": 1}] * 2,
        ):
            with (
                self.subTest(frames=frames),
                patch.object(native_copy_stream, "_read_remote") as remote,
            ):
                remote.side_effect = lambda **kwargs: [kwargs["receive"](frame) for frame in frames]
                with self.assertRaises(memory_capacity.PreviewMemoryCapacityRefused):
                    memory_capacity.read_host_memory_headroom(
                        host="provider", token="fixture", server_id="host-a"
                    )
        denial = native_copy_stream.NativeSourceAuthorizationDenied("fixture refusal")
        with patch.object(native_copy_stream, "_read_remote", side_effect=denial):
            with self.assertRaises(native_copy_stream.NativeSourceAuthorizationDenied) as refused:
                memory_capacity.read_host_memory_headroom(
                    host="provider", token="fixture", server_id="host-a"
                )
        self.assertIs(refused.exception, denial)

    def test_new_odoo_preview_refuses_before_any_mutation(self) -> None:
        plan = build_odoo_preview_dokploy_dry_run(
            request=OdooPreviewDokployDryRunRequest(
                runtime_plan=_runtime_plan(),
                endpoint_spec=_endpoint_spec(),
                template_compose_id="template",
                environment_id="environment",
            )
        )
        with (
            patch(
                "control_plane.workflows.odoo_preview_runtime._find_compose_id_by_environment_and_name",
                return_value="",
            ) as find,
            patch("control_plane.workflows.odoo_preview_runtime.require_bridge_network_capacity"),
            patch.object(
                memory_capacity,
                "require_preview_host_memory_capacity",
                side_effect=memory_capacity.PreviewMemoryCapacityRefused("short"),
            ) as admit,
            patch(
                "control_plane.workflows.odoo_preview_runtime.dokploy_api.dokploy_request"
            ) as mutate,
        ):
            checkpoint = Mock()
            with self.assertRaises(memory_capacity.PreviewMemoryCapacityRefused):
                _resolve_or_create_compose(
                    host="provider",
                    token="fixture",
                    plan=plan,
                    template_payload={"serverId": "host-a"},
                    steps=[],
                    provider_effect_checkpoint=checkpoint,
                )
            checkpoint.assert_not_called()
            mutate.assert_not_called()
            self.assertEqual(admit.call_args.kwargs["server_id"], "host-a")
            find.return_value = "existing"
            admit.reset_mock()
            self.assertEqual(
                _resolve_or_create_compose(
                    host="provider",
                    token="fixture",
                    plan=plan,
                    template_payload={"serverId": "host-a"},
                    steps=[],
                    provider_effect_checkpoint=checkpoint,
                ),
                ("existing", False),
            )
            admit.assert_not_called()

    def test_new_application_preview_refuses_without_cleanup_or_create(self) -> None:
        with (
            patch(
                "control_plane.workflows.generic_web_preview._find_application_by_name",
                return_value=None,
            ) as find,
            patch.object(
                memory_capacity,
                "require_preview_host_memory_capacity",
                side_effect=memory_capacity.PreviewMemoryCapacityRefused("short"),
            ) as admit,
            patch(
                "control_plane.workflows.generic_web_preview.dokploy_api.dokploy_request"
            ) as mutate,
            patch(
                "control_plane.workflows.generic_web_preview._fetch_application",
                return_value={"applicationId": "existing"},
            ),
        ):

            def ensure() -> tuple[JsonObject, bool]:
                return _ensure_application(
                    host="provider",
                    token="fixture",
                    application_name="preview",
                    app_name="preview",
                    description="preview",
                    template_application={"environmentId": "environment", "serverId": "host-a"},
                )

            with self.assertRaises(memory_capacity.PreviewMemoryCapacityRefused):
                ensure()
            mutate.assert_not_called()
            self.assertEqual(admit.call_args.kwargs["server_id"], "host-a")
            find.return_value = {"applicationId": "existing"}
            admit.reset_mock()
            self.assertEqual(ensure(), ({"applicationId": "existing"}, False))
            admit.assert_not_called()
