"""Opt-in combined Odoo/devkit/Traefik/browser proof on empty local data."""

from collections.abc import Iterator
from typing import Any, cast
from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
import unittest
from urllib.request import ProxyHandler, Request, build_opener
from urllib.error import HTTPError
from urllib.parse import urlsplit
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

import uvicorn

from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.release_tuple_record import ReleaseTupleRecord
from control_plane.contracts.promotion_record import DeploymentEvidence
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.odoo_cutover_storage import DeploymentCutoverStore
from control_plane.odoo_cutover_plan import cutover_record_id, resolve_profile_cutover_plan
from control_plane.dokploy.cutover_factory import DokployOdooCutoverProviderFactory
from tests.support.profiles import _odoo_preview_profile_payload
from control_plane.contracts.odoo_cutover_lane import OdooCutoverLane, OdooSlotCoordinates
from control_plane.contracts.promotion_record import HealthcheckEvidence

from control_plane.contracts.artifact_identity import ArtifactIdentityManifest
from control_plane.contracts.odoo_cutover import (
    OdooCutoverPlan,
    OdooMaintenanceReceipt,
    OdooSlotReadiness,
    OdooRecoveryTuple,
    OdooSlot,
)
from control_plane.contracts.prepared_public_site import (
    WRITER_KINDS,
    CapturedPublicResponse,
    PreparedPublicSite,
    PublicSiteBinding,
    PublicSitePlan,
    WriterObservation,
    WriterTarget,
)
from control_plane.contracts.runtime_identity import (
    RuntimeIdentity,
    runtime_identity_from_health_payload,
)
from control_plane.dokploy.odoo_cutover import OdooSlotDriver
from control_plane.dokploy.post_deploy import (
    _build_dokploy_odoo_backup_gate_script,
    _build_dokploy_odoo_backup_verification_script,
    extract_odoo_backup_gate_result,
    extract_odoo_backup_verification_result,
)
from control_plane.dokploy.slot_compose import render_odoo_app_slot_compose
from control_plane.dokploy.slot_writers import ContainerSlotWriters, SlotWriterContainer
from control_plane.dokploy.slot_maintenance import (
    maintenance_payload,
    payload_digest,
    run_planned_slot_maintenance,
)
from control_plane.dokploy.slot_readiness import verify_odoo_slot_readiness
from control_plane.dokploy.traefik_cutover import (
    TraefikFileRoute,
    TraefikRouteTarget,
    route_payload,
)
from control_plane.prepared_public_site import prepare_public_site, prepared_public_app
from control_plane.release_compatibility import classify_release_database_compatibility
from control_plane.workflows.odoo_cutover import execute_odoo_cutover, rollback_compatible_cutover
from tests.support.odoo_cutover_images import docker


@unittest.skipUnless(
    os.environ.get("LAUNCHPLANE_TEST_CUTOVER_MANIFESTS")
    and os.environ.get("LAUNCHPLANE_TEST_TRAEFIK_IMAGE"),
    "isolated cutover artifacts not selected",
)
class OdooCutoverRehearsalTests(unittest.TestCase):
    def test_both_release_classes_with_real_maintenance_native_readiness_and_public_continuity(
        self,
    ) -> None:
        self.prefix = "lp-3266-" + uuid4().hex[:10]
        self.root = Path("state/cutover-rehearsal").resolve() / self.prefix
        self.root.mkdir()
        self.control_store = PostgresRecordStore(
            database_url=f"sqlite+pysqlite:///{self.root}/control.sqlite"
        )
        self.control_store.ensure_schema()
        self.addCleanup(self.control_store.close)
        self.data = self.root / "data"
        self.data.mkdir()
        self.data.chmod(0o777)
        self.evidence = Path(
            os.environ.get("LAUNCHPLANE_TEST_CUTOVER_EVIDENCE", str(self.root / "evidence"))
        ).resolve()
        self.evidence.mkdir(parents=True, exist_ok=True)
        self.containers: list[str] = []
        self.networks: list[str] = []
        self.servers: list[uvicorn.Server] = []
        self.server_threads: list[threading.Thread] = []
        self.addCleanup(self.cleanup)
        self.network = self.prefix + "-internal"
        self.proxy_network = self.prefix + "-proxy"
        for name, arguments in ((self.network, ["--internal"]), (self.proxy_network, [])):
            docker("network", "create", *arguments, name)
            self.networks.append(name)
        database = self.prefix + "-database"
        self.containers.append(database)
        docker(
            "run",
            "-d",
            "--pull=never",
            "--name",
            database,
            "--network",
            self.network,
            "--network-alias",
            "database",
            "--tmpfs",
            "/var/lib/postgresql/data",
            "-e",
            "POSTGRES_USER=odoo",
            "-e",
            "POSTGRES_DB=fixture",
            "-e",
            "POSTGRES_HOST_AUTH_METHOD=trust",
            "postgres:17",
        )
        manifests = Path(os.environ["LAUNCHPLANE_TEST_CUTOVER_MANIFESTS"])
        old, static, changed = (
            ArtifactIdentityManifest.model_validate_json((manifests / (name + ".json")).read_text())
            for name in ("old", "static", "database")
        )
        self.old_manifest = old
        self.addons = "/opt/fixture-addons,/opt/launchplane/addons,/opt/extra_addons,/odoo/addons,/odoo/odoo/addons"
        self.old_runner = self.start_runner(old, "old-runner", project=self.prefix + "-old")
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                self.sql("SELECT 1")
                break
            except RuntimeError:
                time.sleep(0.2)
        self.sql(
            "CREATE TABLE cutover_hook_events(database_name text,event text)", database="postgres"
        )
        boot = self.start_web(old, "bootstrap", None, initialize=True)
        self.wait_web(boot)
        self.database_uuid = self.sql(
            "SELECT value FROM ir_config_parameter WHERE key='database.uuid'"
        )
        self.sql(
            "UPDATE website SET domain='http://fixture.invalid'; UPDATE ir_cron SET active=false"
        )
        docker(
            "exec",
            "-i",
            self.old_runner,
            "/odoo/odoo-bin",
            "shell",
            "--config",
            "/dev/null",
            "-d",
            "fixture",
            "--db_host",
            "database",
            "--db_user",
            "odoo",
            "--addons-path",
            self.addons,
            "--data-dir",
            "/volumes/data",
            "--max-cron-threads",
            "0",
            "--no-http",
            program="env['website.assets'].with_context(website_id=1).make_scss_customization('/website/static/src/scss/options/user_values.scss', {key: chr(39)+'SYSTEM_FONTS'+chr(39) for key in ('font','headings-font','navbar-font','buttons-font')}); env.cr.commit()",
        )
        docker("stop", boot)
        # Old is stopped before fixture credentials are installed; every later
        # registry-capable process sees the lane-safe value already committed.
        self.sql(
            "INSERT INTO ir_config_parameter(key,value) VALUES ('printnode.api_key','inert-lane-sentinel') ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value"
        )
        self.old_identity = self.identity(old, "old", "previous-release")
        self.old_web = self.start_web(old, "old", self.old_identity, project=self.prefix + "-old")
        self.wait_web(self.old_web)
        self.old_origin = self.origin(self.old_web, 8069)
        self.old_url = "http://" + self.old_web + ":8069"
        self.worker_containers: dict[tuple[str, str], str] = {}
        self.writer_ticks: dict[str, int] = {}
        self.owner = "old"
        self.samples: list[str] = []
        self.errors: list[str] = []
        self.stop = threading.Event()
        self.setup_proxy()
        thread = threading.Thread(target=self.read_loop)
        thread.start()

        def stop_reader() -> None:
            self.stop.set()
            thread.join(10)

        self.addCleanup(stop_reader)
        # A real accepted public write and attachment before either release.
        self.http(self.public + "/fixture-submit", data=b"message=accepted-before-cut")
        for manifest, db_change in ((static, False), (changed, True)):
            release = self.prefix + ("-db" if db_change else "-compatible")
            self.plan = self.make_plan(old, manifest, release)
            self.record_id = cutover_record_id(self.plan)
            self.new_runner = self.start_runner(manifest, "runner-" + release, guard=True)
            self.new_web = ""
            self.private_origin = ""
            self.site: PreparedPublicSite | None = None
            driver = OdooSlotDriver(
                exclusive=self.exclusive,
                prepare_candidate=self.prepare_candidate,
                prepare_public=self.prepare_public,
                backup=self.backup,
                maintenance=self.maintenance,
                readiness=self.readiness,
                observe_public=self.observe_public,
                stage_public=self.stage_public,
                observe_prepared=self.observe_prepared,
                fence_and_drain=self.fence,
                handoff_writers=self.handoff,
                drain_old=self.drain,
                lane=OdooCutoverLane(
                    route_id=self.plan.route_id,
                    host=self.plan.host,
                    database="fixture",
                    database_uuid=self.database_uuid,
                    prepared_origin="http://host.docker.internal:" + str(self.prepared_port),
                    slots=cast(
                        tuple[OdooSlotCoordinates, OdooSlotCoordinates],
                        tuple(
                            OdooSlotCoordinates(
                                slot_id=slot.slot_id,
                                compose_id=slot.compose_id,
                                private_url=slot.private_url,
                            )
                            for slot in (self.plan.old, self.plan.new)
                        ),
                    ),
                    writers=self.plan.writers,
                ),
                verify_endpoint=self.verify_endpoint,
                load_prepared=self.load_prepared,
            )
            factory = DokployOdooCutoverProviderFactory(
                self.control_store, driver=lambda plan, lane: driver, route=lambda lane: self.route
            )
            provider = factory(self.plan)
            store = DeploymentCutoverStore(
                self.control_store,
                DeploymentRecord(
                    record_id=self.plan.new.runtime_identity.deployment_record_id,
                    context="fixture",
                    instance="isolated",
                    source_git_ref=self.plan.candidate.source_commit,
                    deploy=DeploymentEvidence(
                        deploy_mode="atomic-two-slot",
                        target_type="compose",
                        target_name=self.plan.new.slot_id,
                    ),
                ),
            )
            self.cutover_store = store
            result = None
            for _attempt in range(8):
                try:
                    result = execute_odoo_cutover(
                        self.plan, store, provider, record_id=self.record_id
                    )
                    break
                except ValueError as error:
                    if "not yet verified" not in str(error):
                        raise
                    time.sleep(0.2)
            assert result is not None
            assert result.maintenance is not None
            self.assertEqual(result.step, "complete")
            self.assertEqual(self.owner, self.plan.new.slot_id)
            self.assertEqual(
                self.sql(
                    "SELECT value FROM ir_config_parameter WHERE key='cutover.fixture.accepted'"
                ),
                "accepted-before-cut",
            )
            if db_change:
                self.assertEqual(
                    self.sql(
                        "SELECT value FROM ir_config_parameter WHERE key='cutover.fixture.version'"
                    ),
                    "two",
                )
                self.assertEqual(result.maintenance.update_modules, ("cutover_probe",))
                assert result.pause is not None and result.pause.duration_seconds is not None
                self.assertGreater(result.pause.duration_seconds, 0)
                with self.assertRaisesRegex(ValueError, "coherent tuple restore"):
                    rollback_compatible_cutover(store, provider, self.record_id)
            else:
                self.assertEqual(result.maintenance.update_modules, ())
                for _attempt in range(8):
                    try:
                        rolled = rollback_compatible_cutover(store, provider, self.record_id)
                        break
                    except ValueError as error:
                        if "not yet verified" not in str(error):
                            raise
                        time.sleep(0.2)
                self.assertEqual(rolled.step, "rolled_back")
                self.assertEqual(self.owner, "old")
            (self.evidence / (release + ".json")).write_text(result.model_dump_json(indent=2))
        self.stop.set()
        thread.join(10)
        self.assertFalse(self.errors, self.errors)
        self.assertTrue(any("prepared-public" in value for value in self.samples))
        self.assertTrue(any("two" in value for value in self.samples))
        self.assertGreater(
            int(self.sql("SELECT count(*) FROM cutover_hook_events", database="postgres")), 0
        )
        (self.evidence / "summary.json").write_text(
            json.dumps(
                {
                    "samples": len(self.samples),
                    "errors": self.errors,
                    "old_retained": self.old_web,
                    "native_readiness": True,
                    "full_planned_entrypoint": True,
                    "infrastructure_backup": "isolated independent replica; no live provider",
                },
                indent=2,
            )
        )
        print(
            json.dumps(
                {
                    "samples": len(self.samples),
                    "errors": len(self.errors),
                    "evidence": str(self.evidence),
                }
            )
        )

    def execute(self, argv: tuple[str, ...], program: str | None) -> str:
        return docker(*argv[1:], program=program)

    def image(self, manifest: ArtifactIdentityManifest) -> str:
        return manifest.image.repository + "@" + manifest.image.digest

    def identity(
        self, manifest: ArtifactIdentityManifest, slot: str, release: str
    ) -> RuntimeIdentity:
        return RuntimeIdentity(
            product="fixture",
            context="fixture",
            instance="isolated",
            deployment_record_id=release,
            artifact_id=manifest.artifact_id,
            source_git_ref=manifest.source_commit,
            image_reference=self.image(manifest),
            slot=slot,
            release_id=release,
            image_digest=manifest.image.digest,
            database_uuid=self.database_uuid,
        )

    def environment(self, guard: bool = False) -> list[str]:
        import base64

        override = {
            "config_parameters": [
                {
                    "key": "printnode.api_key",
                    "value": {
                        "source": "secret_binding",
                        "environment_variable": "DEVKIT_LANE_KEY",
                    },
                }
            ]
        }
        env = {
            "ODOO_DB_HOST": "database",
            "ODOO_DB_USER": "odoo",
            "ODOO_DB_PASSWORD": "inert-fixture-password",
            "ODOO_MASTER_PASSWORD": "isolated-fixture-master-passphrase-3266",
            "ODOO_ADMIN_PASSWORD": "isolated-fixture-admin-passphrase-3266",
            "ODOO_DB_NAME": "fixture",
            "ODOO_FILESTORE_PATH": "/volumes/data/filestore",
            "ODOO_ADDONS_PATH": self.addons,
            "ODOO_MAX_CRON_THREADS": "0",
            "PLATFORM_INSTANCE": "testing",
            "ODOO_MODULE_MAINTENANCE_TIMEOUT_SECONDS": "120",
            "DEVKIT_LANE_KEY": "inert-lane-sentinel",
            "ENV_OVERRIDE_DISABLE_CRON": "true",
            "ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64": base64.b64encode(
                json.dumps(override).encode()
            ).decode(),
        }
        if guard:
            env["CUTOVER_GUARD_REQUIRED"] = "1"
        return [part for key, value in env.items() for part in ("-e", key + "=" + value)]

    def start_runner(
        self,
        manifest: ArtifactIdentityManifest,
        name: str,
        *,
        guard: bool = False,
        project: str = "",
    ) -> str:
        container = self.prefix + "-" + name
        self.containers.append(container)
        docker(
            "run",
            "-d",
            "--pull=never",
            "--name",
            container,
            "--network",
            self.network,
            "--label",
            "com.docker.compose.project=" + (project or container),
            "--label",
            "com.docker.compose.service=script-runner",
            "-v",
            str(self.data) + ":/volumes/data",
            *self.environment(guard),
            "--entrypoint",
            "/bin/sh",
            self.image(manifest),
            "-c",
            "exec sleep infinity",
        )
        return container

    def start_web(
        self,
        manifest: ArtifactIdentityManifest,
        name: str,
        identity: RuntimeIdentity | None,
        *,
        initialize: bool = False,
        project: str = "",
        command: str = "",
    ) -> str:
        container = self.prefix + "-" + name
        self.containers.append(container)
        extra = (
            []
            if identity is None
            else [
                "-e",
                "LAUNCHPLANE_RUNTIME_IDENTITY_JSON=" + json.dumps(self.native_identity(identity)),
            ]
        )
        arguments = [
            "--config",
            "/dev/null",
            "--database",
            "fixture",
            "--db-filter",
            "^fixture$",
            "--db_host",
            "database",
            "--db_user",
            "odoo",
            "--addons-path",
            self.addons,
            "--data-dir",
            "/volumes/data",
            "--http-interface",
            "0.0.0.0",
            "--max-cron-threads",
            "0",
            "--without-demo",
            "all",
        ]
        if initialize:
            arguments += [
                "-i",
                "website,cutover_probe,launchplane_runtime_health,launchplane_settings",
            ]
        docker(
            "run",
            "-d",
            "--pull=never",
            "--name",
            container,
            "--network",
            self.proxy_network,
            "-p",
            "127.0.0.1::8069",
            "--label",
            "com.docker.compose.project=" + (project or container),
            "--label",
            "com.docker.compose.service=web",
            "-v",
            str(self.data) + ":/volumes/data",
            *self.environment(identity is not None and identity.slot != "old"),
            *extra,
            "--health-cmd",
            "true",
            "--entrypoint",
            "/bin/sh" if command else "/odoo/odoo-bin",
            self.image(manifest),
            *(["-c", command] if command else arguments),
        )
        docker("network", "connect", self.network, container)
        return container

    def origin(self, container: str, port: int) -> str:
        ports = json.loads(
            docker("inspect", "--format", "{{json .NetworkSettings.Ports}}", container)
        )
        return "http://127.0.0.1:" + str(ports[str(port) + "/tcp"][0]["HostPort"])

    def http(self, url: str, data: bytes | None = None) -> tuple[int, bytes, dict[str, str]]:
        with build_opener(ProxyHandler({})).open(
            Request(
                url,
                data=data,
                headers={
                    "Host": "fixture.invalid",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            ),
            timeout=15,
        ) as response:
            return response.status, response.read(), dict(response.headers.items())

    def wait_web(self, container: str) -> None:
        deadline = time.monotonic() + 100
        while time.monotonic() < deadline:
            try:
                if self.http(self.origin(container, 8069) + "/")[0] == 200:
                    return
            except (OSError, ValueError):
                pass
            time.sleep(0.2)
        raise AssertionError(
            "fixture failed to serve: " + docker("logs", "--tail", "40", container)
        )

    def sql(self, query: str, *, database: str = "fixture") -> str:
        return docker(
            "exec",
            self.prefix + "-database",
            "psql",
            "-U",
            "odoo",
            "-d",
            database,
            "-tA",
            "-c",
            query,
        )

    def make_plan(
        self, old: ArtifactIdentityManifest, new: ArtifactIdentityManifest, release: str
    ) -> OdooCutoverPlan:
        slots = []
        for name, manifest in (("old", old), ("new-" + release, new)):
            identity = (
                self.old_identity if name == "old" else self.identity(manifest, name, release)
            )
            slots.append(
                OdooSlot(
                    slot_id=name,
                    compose_id=name,
                    binding=PublicSiteBinding(
                        product_id="fixture",
                        environment_id="isolated",
                        release_id=release,
                        artifact_id=manifest.artifact_id,
                        image_digest=manifest.image.digest,
                        database="fixture",
                    ),
                    image=self.image(manifest),
                    private_url=self.old_url
                    if name == "old"
                    else "http://" + self.prefix + "-" + name + ":8069",
                    runtime_identity=identity,
                )
            )
        provisional = OdooCutoverPlan(
            release_id=release,
            route_id="fixture",
            host="fixture.invalid",
            production=old,
            candidate=new,
            compatibility=classify_release_database_compatibility(production=old, candidate=new),
            old=slots[0],
            new=slots[1],
            writers=tuple(WriterTarget(writer_id=kind, kind=kind) for kind in WRITER_KINDS),
        )

        config = OdooCutoverLane(
            route_id=provisional.route_id,
            host=provisional.host,
            database="fixture",
            database_uuid=self.database_uuid,
            prepared_origin="http://host.docker.internal:" + str(self.prepared_port),
            slots=(
                OdooSlotCoordinates(
                    **slots[0].model_dump(include={"slot_id", "compose_id", "private_url"})
                ),
                OdooSlotCoordinates(
                    **slots[1].model_dump(include={"slot_id", "compose_id", "private_url"})
                ),
            ),
            writers=provisional.writers,
        )
        profile = _odoo_preview_profile_payload("fixture")
        profile["preview"] = {"enabled": False}
        profile["lanes"] = [
            {
                "context": "fixture",
                "instance": "isolated",
                "base_url": "http://fixture.invalid",
                "health_url": "http://fixture.invalid/launchplane/health",
                "odoo_cutover": config.model_dump(mode="json"),
            }
        ]
        self.control_store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(profile)
        )
        for manifest in (old, new):
            self.control_store.write_artifact_manifest(manifest)
        self.control_store.write_environment_inventory(
            EnvironmentInventory(
                context="fixture",
                instance="isolated",
                source_git_ref=old.source_commit,
                deploy=DeploymentEvidence(
                    deploy_mode="atomic-two-slot",
                    target_type="compose",
                    target_name="old",
                    status="pass",
                ),
                runtime_identity=self.old_identity,
                updated_at=datetime.now().isoformat(),
                deployment_record_id=self.old_identity.deployment_record_id,
            )
        )
        self.control_store.write_release_tuple_record(
            ReleaseTupleRecord(
                tuple_id="old-" + release,
                context="fixture",
                channel="isolated",
                artifact_id=old.artifact_id,
                repo_shas={"example/fixture": old.source_commit},
                provenance="ship",
                minted_at=datetime.now().isoformat(),
            )
        )
        compiled = resolve_profile_cutover_plan(
            self.control_store,
            product="fixture",
            context="fixture",
            instance="isolated",
            artifact_id=new.artifact_id,
            release_id=release,
        )
        assert compiled is not None
        return compiled

    @contextmanager
    def exclusive(self, plan: OdooCutoverPlan) -> Iterator[None]:
        yield

    def setup_proxy(self) -> None:
        dynamic = self.root / "dynamic"
        dynamic.mkdir()
        target = TraefikRouteTarget(
            route_id="fixture",
            host="fixture.invalid",
            entrypoints=("web",),
            route_file=dynamic / "fixture.yaml",
            api_url="http://127.0.0.1",
            tls=False,
        )
        target.route_file.write_text(json.dumps(route_payload(target, self.old_url)))
        proxy = self.prefix + "-traefik"
        self.containers.append(proxy)
        docker(
            "run",
            "-d",
            "--pull=never",
            "--name",
            proxy,
            "--network",
            self.proxy_network,
            "-p",
            "127.0.0.1::80",
            "-p",
            "127.0.0.1::8080",
            "-v",
            str(dynamic) + ":/dynamic:ro",
            os.environ["LAUNCHPLANE_TEST_TRAEFIK_IMAGE"],
            "--api.insecure=true",
            "--providers.file.directory=/dynamic",
            "--providers.file.watch=true",
            "--entrypoints.web.address=:80",
        )
        docker("network", "connect", self.network, proxy)
        self.public = self.origin(proxy, 80)
        self.route = TraefikFileRoute(
            target.model_copy(update={"api_url": self.origin(proxy, 8080)})
        )
        import socket

        with socket.socket() as selected:
            selected.bind(("127.0.0.1", 0))
            self.prepared_port = selected.getsockname()[1]
        deadline = time.monotonic() + 20
        while True:
            try:
                if (
                    self.route.observed_target() == self.old_url
                    and self.http(self.public)[0] == 200
                ):
                    break
            except (OSError, ValueError):
                pass
            if time.monotonic() > deadline:
                raise AssertionError("proxy failed")
            time.sleep(0.1)

    def read_loop(self) -> None:
        while not self.stop.is_set():
            try:
                status, body, headers = self.http(self.public)
                self.samples.append(
                    headers.get("X-Launchplane-Served-Mode", "odoo") + ":" + body.decode()
                )
                if status != 200:
                    self.errors.append(str(status))
            except Exception as error:
                self.errors.append(type(error).__name__)
            self.stop.wait(0.1)

    def prepare_candidate(self, plan: OdooCutoverPlan) -> None:
        # Execute the rendered startup guard as a real process. No module
        # registry may start before the verified complete receipt exists.
        payload = json.loads(
            render_odoo_app_slot_compose(plan, data_volume="fixture", private_network=self.network)
        )
        command = payload["services"]["web"]["command"].replace("$$", "$")
        self.new_web = self.start_web(
            plan.candidate, plan.new.slot_id, plan.new.runtime_identity, command=command
        )
        time.sleep(0.3)
        self.assertEqual(docker("inspect", "--format", "{{.State.Running}}", self.new_web), "true")
        self.assertEqual(docker("logs", self.new_web), "")
        self.writers = self.writer_control(plan)
        self.writers.handoff(plan.old)
        self.wait_writer_ticks(plan.old)

    def capture(self, path: str) -> CapturedPublicResponse:
        status, body, headers = self.http(self.old_origin + path)
        return CapturedPublicResponse(
            binding=self.plan.old.binding,
            anonymous=True,
            public=True,
            status=status,
            body=body,
            content_type=headers.get("Content-Type", "").split(";")[0],
            headers=headers,
        )

    def prepare_public(self, plan: OdooCutoverPlan) -> PreparedPublicSite:
        public = PublicSitePlan(
            binding=plan.old.binding,
            origin="http://fixture.invalid",
            public_routes=("/", "/contact", "/prices"),
            retained_assets=("/cutover_probe/static/site.css",),
            excluded_prefixes=("/my", "/web", "/launchplane"),
            writers=plan.writers,
            notice="We are updating the site. Browsing is available; forms and signed-in activity are temporarily paused.",
        )
        site = prepare_public_site(public, self)
        return site

    def load_prepared(self, pause_id: str) -> PreparedPublicSite:
        record = self.cutover_store.load(pause_id)
        assert record.public_site is not None
        return record.public_site

    def stage_public(self, site: PreparedPublicSite, pause_id: str) -> str:
        if not self.servers:
            server = uvicorn.Server(
                uvicorn.Config(
                    prepared_public_app(site),
                    host="0.0.0.0",
                    port=self.prepared_port,
                    log_level="error",
                )
            )
            thread = threading.Thread(target=server.run)
            thread.start()
            self.servers.append(server)
            self.server_threads.append(thread)
            deadline = time.monotonic() + 10
            while not server.started:
                if time.monotonic() > deadline:
                    raise RuntimeError("prepared fixture failed")
                time.sleep(0.05)
        self.site = site
        with self.assertRaises(HTTPError) as blocked:
            self.http(
                "http://127.0.0.1:" + str(self.prepared_port) + "/fixture-submit",
                data=b"message=must-not-commit",
            )
        self.assertEqual(blocked.exception.code, 423)
        self.browser("http://127.0.0.1:" + str(self.prepared_port), None, prepared=True)
        return "http://host.docker.internal:" + str(self.prepared_port)

    def observe_prepared(self, pause_id: str) -> str:
        status, _body, headers = self.http(self.public)
        return (
            headers.get("X-Launchplane-Public-Copy", "")
            if status == 200 and pause_id == self.record_id
            else ""
        )

    def fence(self, site: PreparedPublicSite, pause_id: str) -> tuple[WriterObservation, ...]:
        observations = self.writers.fence(site, pause_id)
        self.assertEqual(
            self.sql(
                "SELECT count(*) FROM pg_stat_activity WHERE datname='fixture' AND pid<>pg_backend_pid()"
            ),
            "0",
        )
        self.writer_ticks = {
            writer.writer_id: int(
                self.sql(
                    "SELECT count(*) FROM cutover_worker_ticks WHERE writer='"
                    + writer.writer_id
                    + "'"
                )
            )
            for writer in self.plan.writers
            if writer.kind != "web"
        }
        self.owner = ""
        return observations

    def backup(self, plan: OdooCutoverPlan) -> OdooRecoveryTuple:
        name = plan.release_id
        script = _build_dokploy_odoo_backup_gate_script(
            compose_app_name=self.prefix + "-old",
            backup_nonce="a" * 64,
            database_name="fixture",
            filestore_path="/volumes/data/filestore",
            backup_root="/volumes/data/backups",
            backup_record_id=name,
            expected_web_state="exited"
            if plan.compatibility.classification == "database_changing"
            else "running",
        )
        captured_process = subprocess.run(
            ["bash", "-s"], input=script, text=True, capture_output=True, timeout=80
        )
        self.assertEqual(captured_process.returncode, 0, captured_process.stderr)
        output = captured_process.stdout
        captured = extract_odoo_backup_gate_result(output)
        backup = "/volumes/data/backups/fixture/" + name
        verification = _build_dokploy_odoo_backup_verification_script(
            compose_app_name=self.prefix + "-old",
            verification_nonce="b" * 64,
            backup_record_id=name,
            database_name="fixture",
            filestore_path="/volumes/data/filestore",
            backup_dir=backup,
            database_dump_path=backup + "/fixture.dump",
            filestore_archive_path=backup + "/fixture-filestore.tar.gz",
            manifest_path=backup + "/manifest.json",
            capture_evidence={k: str(v) for k, v in captured.items() if k != "backup_nonce"},
        )
        result = subprocess.run(
            ["bash", "-s"],
            input=verification,
            text=True,
            capture_output=True,
            check=True,
            timeout=80,
        )
        self.assertEqual(
            extract_odoo_backup_verification_result(result.stdout)["verification_status"], "pass"
        )
        source = self.data / "backups/fixture" / name
        replica = self.root / "independent" / name
        replica.mkdir(parents=True)
        hashes = {}
        for path in source.iterdir():
            if path.is_file():
                shutil.copyfile(path, replica / path.name)
                hashes[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
                self.assertEqual(
                    hashlib.sha256((replica / path.name).read_bytes()).hexdigest(),
                    hashes[path.name],
                )
        return OdooRecoveryTuple(
            binding=plan.old.binding,
            image=plan.old.image,
            logical_record_id=name,
            infrastructure_record_id="isolated-replica-" + name,
            database_dump_sha256=hashes["fixture.dump"],
            filestore_archive_sha256=hashes["fixture-filestore.tar.gz"],
            recovery_point_at=datetime.fromisoformat(str(captured["recovery_point_at"])),
            coherence_verified=True,
            restore_verified=True,
            independent_backup_verified=True,
        )

    def maintenance(self, plan: OdooCutoverPlan) -> OdooMaintenanceReceipt:
        events = self.sql("SELECT count(*) FROM cutover_hook_events", database="postgres")
        wrong = plan.model_copy(
            update={
                "old": plan.old.model_copy(
                    update={
                        "runtime_identity": plan.old.runtime_identity.model_copy(
                            update={"database_uuid": "other-physical-database"}
                        )
                    }
                ),
                "new": plan.new.model_copy(
                    update={
                        "runtime_identity": plan.new.runtime_identity.model_copy(
                            update={"database_uuid": "other-physical-database"}
                        )
                    }
                ),
            }
        )
        with self.assertRaisesRegex(RuntimeError, "selected database UUID"):
            run_planned_slot_maintenance(
                wrong, runner_container=self.new_runner, execute=self.execute
            )
        fake_id = plan.release_id + "-fake-footer"
        fake = plan.model_copy(
            update={
                "release_id": fake_id,
                "old": plan.old.model_copy(
                    update={"binding": plan.old.binding.model_copy(update={"release_id": fake_id})}
                ),
                "new": plan.new.model_copy(
                    update={
                        "binding": plan.new.binding.model_copy(update={"release_id": fake_id}),
                        "runtime_identity": plan.new.runtime_identity.model_copy(
                            update={"release_id": fake_id}
                        ),
                    }
                ),
            }
        )
        payload = maintenance_payload(fake)
        directory = "/volumes/data/.launchplane-cutovers/" + payload_digest(payload)
        self.execute(
            (
                "docker",
                "exec",
                "-i",
                self.new_runner,
                "/venv/bin/python",
                "-c",
                "import sys,json;from pathlib import Path;r=json.load(sys.stdin);p=Path(r['directory']);p.mkdir(parents=True,exist_ok=True);(p/'plan.json').write_text(json.dumps(r['payload'],sort_keys=True,separators=(',',':')));(p/'attempt.json').write_text('started');(p/'receipt.json').write_text(json.dumps({'state':'passed'}))",
            ),
            json.dumps({"directory": directory, "payload": payload}),
        )
        with self.assertRaisesRegex(RuntimeError, "boundary.json"):
            run_planned_slot_maintenance(
                fake, runner_container=self.new_runner, execute=self.execute
            )
        self.assertEqual(
            self.sql("SELECT count(*) FROM cutover_hook_events", database="postgres"), events
        )
        if plan.compatibility.classification == "database_changing":
            self.assertEqual(
                docker("inspect", "--format", "{{.State.Running}}", self.old_web), "false"
            )
            self.sql(
                "INSERT INTO ir_config_parameter(key,value,create_uid,write_uid) VALUES ('printnode.api_key','inert-unstripped-sentinel',1,1) ON CONFLICT(key) DO UPDATE SET value='inert-unstripped-sentinel'"
            )
        receipt = run_planned_slot_maintenance(
            plan, runner_container=self.new_runner, execute=self.execute
        )
        self.assertEqual(
            self.sql("SELECT value FROM ir_config_parameter WHERE key='printnode.api_key'"),
            "inert-lane-sentinel",
        )
        # Replay consumes the same receipt; it must not create a second registry.
        events = self.sql("SELECT count(*) FROM cutover_hook_events", database="postgres")
        self.assertEqual(
            run_planned_slot_maintenance(
                plan, runner_container=self.new_runner, execute=self.execute
            ),
            receipt,
        )
        self.assertEqual(
            self.sql("SELECT count(*) FROM cutover_hook_events", database="postgres"), events
        )
        for writer_id, count in self.writer_ticks.items():
            self.assertEqual(
                int(
                    self.sql(
                        "SELECT count(*) FROM cutover_worker_ticks WHERE writer='" + writer_id + "'"
                    )
                ),
                count,
            )
        docker("start", self.new_web)
        self.wait_web(self.new_web)
        self.private_origin = self.origin(self.new_web, 8069)
        return receipt

    def native_identity(self, identity: RuntimeIdentity) -> dict[str, Any]:
        value = identity.model_dump(mode="json")
        value["source_revision"] = value.pop("source_git_ref")
        return value

    def browser(
        self, origin: str, contract: dict[str, Any] | None, *, prepared: bool = False
    ) -> dict[str, Any]:
        paths = (
            [route["path"] for route in contract["routes"]]
            if contract
            else ["/", "/prices", "/contact"]
        )
        digest = (
            hashlib.sha256(
                json.dumps(
                    contract, sort_keys=True, ensure_ascii=False, separators=(",", ":")
                ).encode()
            ).hexdigest()
            if contract
            else ""
        )
        script = Path("tests/support/cutover_browser.mjs").read_text()
        upstream = origin

        class BrowserProxy(BaseHTTPRequestHandler):
            def do_GET(proxy) -> None:
                proxy.forward()

            def do_POST(proxy) -> None:
                proxy.forward()

            def forward(proxy) -> None:
                parsed = urlsplit(proxy.path)
                if parsed.hostname != "fixture.invalid":
                    proxy.send_error(403)
                    return
                data = (
                    proxy.rfile.read(int(proxy.headers.get("Content-Length", "0")))
                    if proxy.command == "POST"
                    else None
                )
                req = Request(
                    upstream + parsed.path + ("?" + parsed.query if parsed.query else ""),
                    data=data,
                    headers={
                        "Host": "fixture.invalid",
                        "Content-Type": proxy.headers.get(
                            "Content-Type", "application/x-www-form-urlencoded"
                        ),
                    },
                    method=proxy.command,
                )
                try:
                    response = build_opener(ProxyHandler({})).open(req, timeout=20)
                except HTTPError as error:
                    response = error
                with response:
                    body = response.read()
                    proxy.send_response(response.status)
                    for key in (
                        "Content-Type",
                        "Content-Security-Policy",
                        "X-Launchplane-Public-Copy",
                    ):
                        if response.headers.get(key):
                            proxy.send_header(key, response.headers[key])
                    proxy.send_header("Content-Length", str(len(body)))
                    proxy.end_headers()
                    proxy.wfile.write(body)

            def log_message(proxy, format: str, *args: object) -> None:
                pass

        proxy = ThreadingHTTPServer(("127.0.0.1", 0), BrowserProxy)
        proxy_thread = threading.Thread(target=proxy.serve_forever)
        proxy_thread.start()
        value = {
            "origin": "http://fixture.invalid",
            "proxy": "http://127.0.0.1:" + str(proxy.server_port),
            "host": "fixture.invalid",
            "paths": paths,
            "contract_sha256": digest,
            "prepared": prepared,
            "screenshot": str(
                self.evidence / (("prepared" if prepared else self.plan.new.slot_id) + ".png")
            ),
        }
        try:
            result = subprocess.run(
                ["pnpm", "exec", "node", "--input-type=module", "-e", script],
                cwd="frontend",
                input=json.dumps(value),
                text=True,
                capture_output=True,
                timeout=90,
            )
        finally:
            proxy.shutdown()
            proxy.server_close()
            proxy_thread.join(10)
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt: dict[str, Any] = json.loads(result.stdout)
        return receipt

    def readiness(self, plan: OdooCutoverPlan, slot: OdooSlot) -> OdooSlotReadiness:
        old = slot.slot_id == "old"
        container = self.old_web if old else self.new_web
        if old and docker("inspect", "--format", "{{.State.Running}}", container) != "true":
            docker("start", container)
            self.wait_web(container)
            self.old_origin = self.origin(container, 8069)
        identity = self.native_identity(slot.runtime_identity)
        release = {
            "runtime_identity": identity,
            "update_id": payload_digest(maintenance_payload(plan)),
            "updated_modules": list(plan.compatibility.update_modules) if not old else [],
            "status": "pass",
            "fences": {
                "credentials_committed": True,
                "mail_disabled": True,
                "integrations_disabled": True,
            },
        }
        encoded = json.dumps(release).replace("'", "''")
        self.sql(
            "INSERT INTO ir_config_parameter(key,value,create_uid,write_uid) VALUES ('launchplane.readiness.release','"
            + encoded
            + "',1,1) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value"
        )
        contract = {
            "schema_version": 1,
            "database": "fixture",
            "database_uuid": self.database_uuid,
            "host": plan.host,
            "website_id": 1,
            "runtime_identity": identity,
            "modules": ["base", "web", "website", "cutover_probe", "launchplane_runtime_health"],
            "release": release,
            "routes": [
                {"path": "/", "kind": "homepage", "contains": "Isolated Odoo release"},
                {"path": "/web/login", "kind": "login", "contains": "oe_login_form"},
                {"path": "/contact", "kind": "public", "contains": "Isolated Odoo release"},
            ],
        }
        origin = self.old_origin if old else self.private_origin
        browser = self.browser(origin, contract)
        return verify_odoo_slot_readiness(
            plan,
            slot,
            web_container=container,
            contract=contract,
            browser_receipt=browser,
            execute=self.execute,
            retained_assets_verified=self.http(self.old_origin + "/cutover_probe/static/site.css")[
                0
            ]
            == 200
            if old or plan.compatibility.classification == "compatible"
            else self.http(self.private_origin + "/cutover_probe/static/site.css")[0] == 200,
        )

    def verify_endpoint(self, plan: OdooCutoverPlan, slot: OdooSlot) -> HealthcheckEvidence:
        program = (
            "import json,urllib.request; request=urllib.request.Request("
            + repr(slot.private_url + "/launchplane/health")
            + ",headers={'Host':"
            + repr(plan.host)
            + "}); response=urllib.request.urlopen(request,timeout=10); assert response.status==200; print(response.read().decode())"
        )
        payload = json.loads(docker("exec", self.old_runner, "/venv/bin/python", "-c", program))
        observed = runtime_identity_from_health_payload(payload)
        self.assertEqual(observed, slot.runtime_identity)
        return HealthcheckEvidence(
            verified=True,
            status="pass",
            urls=(slot.private_url + "/launchplane/health",),
            timeout_seconds=10,
            runtime_identity_status="match",
            observed_runtime_identity=observed,
        )

    def observe_public(
        self, plan: OdooCutoverPlan, target: str
    ) -> tuple[str, PublicSiteBinding, HealthcheckEvidence | None]:
        if target == plan.old.private_url:
            slot = plan.old
        elif target == plan.new.private_url:
            slot = plan.new
        elif target == "http://host.docker.internal:" + str(
            self.prepared_port
        ) and self.observe_prepared(self.record_id):
            return "prepared", plan.old.binding, None
        else:
            raise ValueError("unknown applied target")
        _status, body, _headers = self.http(self.public + "/launchplane/health")
        observed = runtime_identity_from_health_payload(json.loads(body))
        if observed != slot.runtime_identity:
            raise ValueError("wrong public identity")
        return (
            slot.slot_id,
            slot.binding,
            HealthcheckEvidence(
                verified=True,
                status="pass",
                urls=(self.public + "/launchplane/health",),
                timeout_seconds=15,
                observed_runtime_identity=observed,
                runtime_identity_status="match",
            ),
        )

    def handoff(self, plan: OdooCutoverPlan, slot: OdooSlot) -> tuple[tuple[str, int], ...]:
        owners = self.writers.handoff(slot)
        self.wait_writer_ticks(slot)
        # Replaying the handoff observes the same live processes; no duplicate
        # container or database advisory-lock owner can be created.
        self.assertEqual(self.writers.handoff(slot), owners)
        self.owner = slot.slot_id
        return owners

    def writer_control(self, plan: OdooCutoverPlan) -> ContainerSlotWriters:
        self.sql(
            "CREATE TABLE IF NOT EXISTS cutover_worker_ticks(writer text,slot text,process integer,at timestamptz default clock_timestamp())"
        )
        targets = []
        for slot, web in ((plan.old, self.old_web), (plan.new, self.new_web)):
            for writer in plan.writers:
                if writer.kind == "web":
                    container = web
                else:
                    key = (slot.slot_id, writer.writer_id)
                    if key not in self.worker_containers:
                        container = (
                            self.prefix
                            + "-writer-"
                            + hashlib.sha256(repr(key).encode()).hexdigest()[:12]
                        )
                        self.containers.append(container)
                        program = Path("tests/support/cutover_worker.py.in").read_text()
                        docker(
                            "create",
                            "--pull=never",
                            "--name",
                            container,
                            "--network",
                            self.network,
                            "-v",
                            str(self.data) + ":/volumes/data",
                            *self.environment(slot != plan.old),
                            "-e",
                            "CUTOVER_WRITER=" + writer.writer_id,
                            "-e",
                            "CUTOVER_SLOT=" + slot.slot_id,
                            "-e",
                            "CUTOVER_WORKER_KIND=" + writer.kind,
                            "--entrypoint",
                            "/venv/bin/python",
                            slot.image,
                            "-c",
                            program,
                        )
                        self.worker_containers[key] = container
                    container = self.worker_containers[key]
                targets.append(
                    SlotWriterContainer(
                        writer=writer, slot_id=slot.slot_id, container=container, image=slot.image
                    )
                )
        return ContainerSlotWriters(plan, tuple(targets), self.execute, self.active_writer_jobs)

    def active_writer_jobs(self, target: SlotWriterContainer) -> int:
        if target.writer.kind == "web":
            # All registry-capable writers use this DB; the full pause below
            # additionally verifies that no sessions survive the entire fence.
            return (
                0
                if docker("inspect", "--format", "{{.State.Running}}", target.container) == "false"
                else 1
            )
        application = target.slot_id + ":" + target.writer.writer_id
        return int(
            self.sql(
                "SELECT count(*) FROM pg_stat_activity WHERE datname='fixture' AND application_name='"
                + application
                + "'"
            )
        )

    def wait_writer_ticks(self, slot: OdooSlot) -> None:
        deadline = time.monotonic() + 30
        for writer in self.plan.writers:
            if writer.kind == "web":
                continue
            before = int(
                self.sql(
                    "SELECT count(*) FROM cutover_worker_ticks WHERE writer='"
                    + writer.writer_id
                    + "' AND slot='"
                    + slot.slot_id
                    + "'"
                )
            )
            while (
                int(
                    self.sql(
                        "SELECT count(*) FROM cutover_worker_ticks WHERE writer='"
                        + writer.writer_id
                        + "' AND slot='"
                        + slot.slot_id
                        + "'"
                    )
                )
                <= before
            ):
                if time.monotonic() > deadline:
                    raise AssertionError(
                        "fixture writer failed: "
                        + docker("logs", self.worker_containers[(slot.slot_id, writer.writer_id)])
                    )
                time.sleep(0.1)
            self.assertEqual(
                self.active_writer_jobs(
                    next(
                        target
                        for target in self.writers.targets
                        if target.slot_id == slot.slot_id and target.writer == writer
                    )
                ),
                1,
            )

    def drain(self, plan: OdooCutoverPlan) -> bool:
        # docker stop completed after public traffic moved; the retained project
        # is still available for private restart/readiness during switch-back.
        return docker("inspect", "--format", "{{.State.Running}}", self.old_web) == "false"

    def cleanup(self) -> None:
        for server in self.servers:
            server.should_exit = True
        for thread in self.server_threads:
            thread.join(10)
        for container in reversed(self.containers):
            subprocess.run(["docker", "rm", "-f", container], capture_output=True, timeout=30)
        for network in reversed(self.networks):
            subprocess.run(["docker", "network", "rm", network], capture_output=True, timeout=30)
