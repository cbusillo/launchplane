import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from control_plane.dokploy.cutover_host_state import CutoverHostState
from control_plane.dokploy.service_cutover import installed_cutover_host
from control_plane.dokploy.cutover_host import DokployCutoverHost
from tests.support.prepared_public_fixture import WebsiteFixture
from tests.test_odoo_cutover import release_plan


class CutoverHostStateTests(unittest.TestCase):
    def test_installed_host_discovery_constructs_without_credentials_or_provider_effects(
        self,
    ) -> None:
        with patch("control_plane.dokploy.cutover_host.read_dokploy_config") as config:
            self.assertIsInstance(installed_cutover_host(), DokployCutoverHost)
        config.assert_not_called()

    def test_original_execution_survives_restart_and_refuses_new_payload_or_placement(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            fixture = WebsiteFixture(root)
            self.addCleanup(fixture.engine.dispose)
            plan = release_plan(fixture)
            directory = root / "authority"
            directory.mkdir()

            def execute(argv: tuple[str, ...], stdin: str | None) -> str:
                return subprocess.run(
                    [sys.executable, *argv[1:]],
                    input=stdin,
                    text=True,
                    capture_output=True,
                    check=True,
                ).stdout

            effects = Mock(side_effect=execute)
            state = CutoverHostState(plan, str(directory), execute, effects, {"server": "isolated"})
            state.admit_execution(recovery=False)
            original = state.expected_execution()
            restarted = CutoverHostState(
                plan, str(directory), execute, effects, {"server": "isolated"}
            )
            restarted.verify_execution(require_existing=True)
            before = effects.call_count
            with (
                patch(
                    "control_plane.dokploy.cutover_host_state.maintenance_payload",
                    return_value={"package": "changed"},
                ),
                self.assertRaises(ValueError),
            ):
                restarted.admit_execution(recovery=True)
            self.assertEqual(effects.call_count, before)
            moved = CutoverHostState(plan, str(directory), execute, effects, {"server": "other"})
            with self.assertRaises(ValueError):
                moved.admit_execution(recovery=True)
            self.assertEqual(
                json.loads((directory / (state.record_id + "-execution.json")).read_text()),
                original,
            )

    def test_lane_images_remain_protected_without_a_running_worker(self) -> None:
        from control_plane.contracts.cutover_host import OdooCutoverHostRuntime
        from control_plane.contracts.odoo_cutover_lane import OdooCutoverLane, OdooSlotCoordinates
        from control_plane.contracts.protected_artifacts import build_protected_artifact_set
        from control_plane.storage.filesystem import FilesystemRecordStore
        from tests.support.protected_artifacts import _profile

        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            fixture = WebsiteFixture(root)
            self.addCleanup(fixture.engine.dispose)
            plan = release_plan(fixture)
            runtime = OdooCutoverHostRuntime.model_validate(
                dict(
                    route=dict(
                        route_file=str(root / "lane.yaml"),
                        api_url="http://proxy.invalid",
                        entrypoints=["web"],
                        tls=False,
                    ),
                    public_origin="http://fixture.invalid",
                    authority_directory=str(root / "authority"),
                    data_volume="fixture-data",
                    private_network="fixture-private",
                    prepared_volume="fixture-public",
                    prepared_container="fixture-prepared",
                    prepared_image="fixture-server@sha256:" + "1" * 64,
                    restore_image="fixture-database@sha256:" + "2" * 64,
                    filestore_path="/volumes/data/filestore",
                    backup_root="/volumes/data/backups",
                    website_id=1,
                    modules=["base", "web", "website", "launchplane_runtime_health"],
                    readiness_routes=[
                        dict(path="/", kind="homepage", contains="Home"),
                        dict(path="/web/login", kind="login", contains="Login"),
                        dict(path="/contact", kind="public", contains="Contact"),
                    ],
                    public_routes=["/"],
                    retained_assets=[],
                    excluded_prefixes=["/web"],
                    notice="Please return shortly.",
                )
            )
            lane = OdooCutoverLane(
                route_id=plan.route_id,
                host=plan.host,
                database=plan.old.binding.database,
                database_uuid=plan.old.runtime_identity.database_uuid,
                prepared_origin="http://prepared.invalid",
                slots=(
                    OdooSlotCoordinates(
                        slot_id=plan.old.slot_id,
                        compose_id=plan.old.compose_id,
                        private_url=plan.old.private_url,
                    ),
                    OdooSlotCoordinates(
                        slot_id=plan.new.slot_id,
                        compose_id=plan.new.compose_id,
                        private_url=plan.new.private_url,
                    ),
                ),
                writers=plan.writers,
                host_runtime=runtime,
            )
            profile = _profile()
            profile = profile.model_copy(
                update={"lanes": (profile.lanes[0].model_copy(update={"odoo_cutover": lane}),)}
            )
            store = FilesystemRecordStore(state_dir=root / "records")
            store.write_product_profile_record(profile)
            protected = build_protected_artifact_set(store, product=profile.product)
            self.assertTrue(
                {runtime.prepared_image, runtime.restore_image} <= set(protected.image_references)
            )
            self.assertTrue(
                {runtime.prepared_image.split("@")[1], runtime.restore_image.split("@")[1]}
                <= set(protected.image_digests)
            )
            self.assertEqual(
                build_protected_artifact_set(store, product="another-product").entries, ()
            )
