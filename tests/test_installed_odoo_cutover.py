import base64
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import shutil
import time
import threading
from typing import Any
import unittest
from unittest.mock import patch
from urllib.request import ProxyHandler, Request, build_opener
from urllib.error import HTTPError

from cryptography.fernet import Fernet

from control_plane.contracts.artifact_identity import ArtifactIdentityManifest
from control_plane.contracts.cutover_host import OdooCutoverHostRuntime
from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.odoo_cutover import OdooCutoverPlan
from control_plane.contracts.odoo_cutover_lane import OdooCutoverLane, OdooSlotCoordinates
from control_plane.contracts.odoo_prod_promotion_operation import (
    OdooProdPromotionOperationRecord,
    build_odoo_prod_promotion_operation_id,
    odoo_prod_promotion_request_fingerprint,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.promotion_record import (
    DeploymentEvidence,
)
from control_plane.contracts.release_tuple_record import ReleaseTupleRecord
from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.dokploy.cutover_execution import bind_cutover_execution
from control_plane.dokploy.cutover_transport import CutoverHostTransport
from control_plane.dokploy.service_cutover import service_odoo_cutover_factory
from control_plane.dokploy.traefik_cutover import TraefikRouteTarget, route_payload
from control_plane.odoo_cutover_plan import cutover_record_id, resolve_profile_cutover_plan
from control_plane.odoo_cutover_storage import DeploymentCutoverStore
from control_plane.workflows.odoo_cutover import execute_odoo_cutover, rollback_compatible_cutover
from control_plane.workflows.odoo_prod_promotion import (
    OdooProdPromotionRequest,
    _build_promotion_record,
)
from tests.support.profiles import _odoo_preview_profile_payload
from tests.test_dokploy import _write_dokploy_managed_secrets
from tests.test_odoo_cutover_website_settings import snapshot
from tests.test_odoo_prod_promotion_operation import _operation
from tests import test_production_promotion_backup as backup_fixtures
from control_plane.contracts.prepared_public_site import WriterTarget, WRITER_KINDS


@unittest.skipUnless(
    os.environ.get("LAUNCHPLANE_INSTALLED_CUTOVER_FIXTURE"),
    "installed isolated host fixture not selected",
)
class InstalledOdooCutoverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(os.environ["LAUNCHPLANE_INSTALLED_CUTOVER_FIXTURE"])
        self.config = json.loads((self.root / "config.json").read_text())
        self.prefix = self.config["prefix"]
        self.network = self.config["network"]
        self.remote = CutoverHostTransport(
            host="http://" + self.prefix + "-provider:8080",
            token="inert-fixture-token",
            server_id="isolated",
            guard=lambda: None,
        )
        self.projects: list[str] = []
        self.addCleanup(self.cleanup)
        self.backup_fixture = backup_fixtures.ProductionPromotionBackupTests()
        self.backup_fixture.setUp()
        self.addCleanup(self.backup_fixture.doCleanups)
        self.store = self.backup_fixture.capture()
        self.enterContext(
            patch.dict(
                os.environ,
                {
                    "LAUNCHPLANE_DATABASE_URL": self.store.database_url,
                    "LAUNCHPLANE_MASTER_ENCRYPTION_KEY": Fernet.generate_key().decode(),
                    "LAUNCHPLANE_PREPARED_PUBLIC_ROOT": "/prepared",
                },
            )
        )
        _write_dokploy_managed_secrets(
            store=self.store,
            host="http://" + self.prefix + "-provider:8080",
            token="inert-fixture-token",
        )
        self.manifests = tuple(
            ArtifactIdentityManifest.model_validate_json(
                (self.root / "manifests" / (name + ".json")).read_text()
            )
            for name in ("old", "static", "database")
        )
        self.addons = "/opt/fixture-addons,/opt/launchplane/addons,/opt/extra_addons,/odoo/addons,/odoo/odoo/addons"
        self.environment = dict(
            ODOO_DB_HOST=self.prefix + "-database",
            ODOO_DB_USER="odoo",
            ODOO_DB_PASSWORD="inert-fixture-password",
            ODOO_DB_NAME="fixture",
            ODOO_DATA_DIR="/volumes/data",
            ODOO_FILESTORE_PATH="/volumes/data/filestore",
            ODOO_ADDONS_PATH=self.addons,
            ODOO_MASTER_PASSWORD="isolated-fixture-master-passphrase-4844",
            ODOO_ADMIN_PASSWORD="isolated-fixture-admin-passphrase-4844",
            ODOO_MAX_CRON_THREADS="0",
            ODOO_WORKERS="0",
            PLATFORM_INSTANCE="prod",
        )
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
        self.environment["DEVKIT_LANE_KEY"] = "inert-lane-sentinel"
        self.environment["ODOO_MODULE_MAINTENANCE_TIMEOUT_SECONDS"] = "120"
        self.environment["ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64"] = base64.b64encode(
            json.dumps(override).encode()
        ).decode()
        if not self.config.get("website_settings", True):
            self.environment.pop("ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64")
        self.old_project = self.prefix + "-old"
        self.new_project = self.prefix + "-new"
        self.projects.extend((self.old_project, self.new_project))
        self.write_targets(self.initial_compose())
        self.command(
            (
                "docker",
                "compose",
                "--project-name",
                self.old_project,
                "--project-directory",
                str(self.root / self.old_project),
                "--file",
                str(self.root / self.old_project / "compose.yml"),
                "up",
                "-d",
                "--pull",
                "never",
                "--no-build",
                "script-runner",
            )
        )
        self.old_runner = self.container(self.old_project, "script-runner")
        self.wait_sql()
        self.command(
            (
                "docker",
                "exec",
                self.old_runner,
                "/odoo/odoo-bin",
                "-c",
                "/dev/null",
                "-d",
                "fixture",
                "--db_host",
                self.prefix + "-database",
                "--db_user",
                "odoo",
                "--addons-path",
                self.addons,
                "--data-dir",
                "/volumes/data",
                "--max-cron-threads",
                "0",
                "--no-http",
                "--without-demo",
                "all",
                "--stop-after-init",
                "-i",
                "website,cutover_probe,launchplane_runtime_health"
                + (",launchplane_settings" if self.config.get("website_settings", True) else ""),
            )
        )
        self.sql(
            "UPDATE website SET domain='http://fixture.invalid'; UPDATE ir_cron SET active=false; INSERT INTO ir_config_parameter(key,value) VALUES ('printnode.api_key','inert-lane-sentinel'),('odoo_devkit.credential_boundary_commit','isolated-initial-committed-boundary') ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value"
        )
        self.command(
            (
                "docker",
                "exec",
                "-i",
                self.old_runner,
                "/odoo/odoo-bin",
                "shell",
                "-c",
                "/dev/null",
                "-d",
                "fixture",
                "--db_host",
                self.prefix + "-database",
                "--db_user",
                "odoo",
                "--addons-path",
                self.addons,
                "--data-dir",
                "/volumes/data",
                "--max-cron-threads",
                "0",
                "--no-http",
            ),
            "env['website.assets'].with_context(website_id=1).make_scss_customization('/website/static/src/scss/options/user_values.scss', {key: chr(39)+'SYSTEM_FONTS'+chr(39) for key in ('font','headings-font','navbar-font','buttons-font')}); env.cr.commit()",
        )
        self.database_uuid = self.sql(
            "SELECT value FROM ir_config_parameter WHERE key='database.uuid'"
        )
        self.sql("CREATE TABLE cutover_hook_events(database_name text,event text)", "postgres")
        self.old_identity = self.identity(self.manifests[0], "old", "previous")
        self.start_old()
        self.wait_public()
        self.command(
            (
                "docker",
                "compose",
                "--project-name",
                self.old_project,
                "--project-directory",
                str(self.root / self.old_project),
                "--file",
                str(self.root / self.old_project / "compose.yml"),
                "up",
                "-d",
                "--pull",
                "never",
                "--no-build",
                "scheduler",
            )
        )
        self.native_tick(self.old_project)
        self.operation = self.create_operation()
        self.original_operation_id = self.operation.operation_id
        self.checkpoints: list[str] = []
        self.plan = self.seed_plan(self.manifests[2 if self.config.get("database_changing") else 1])

    def command(self, argv: tuple[str, ...], stdin: str | None = None) -> str:
        return self.remote.read(argv, stdin)

    def container(self, project: str, service: str) -> str:
        values = self.command(
            (
                "docker",
                "ps",
                "-aq",
                "--filter",
                "label=com.docker.compose.project=" + project,
                "--filter",
                "label=com.docker.compose.service=" + service,
            )
        ).split()
        if len(values) != 1:
            raise ValueError("fixture container is not unique")
        return values[0]

    def sql(self, statement: str, database: str = "fixture") -> str:
        program = "import json,os,sys,psycopg2; v=json.load(sys.stdin); c=psycopg2.connect(host=os.environ['ODOO_DB_HOST'],user='odoo',dbname=v['database']); c.autocommit=True; q=c.cursor(); q.execute(v['sql']); print('\\n'.join(str(row[0]) for row in q.fetchall()) if q.description else '')"
        return self.command(
            ("docker", "exec", "-i", self.old_runner, "/venv/bin/python", "-c", program),
            json.dumps({"sql": statement, "database": database}),
        ).strip()

    def wait_sql(self) -> None:
        deadline = time.monotonic() + 45
        while True:
            try:
                self.sql("SELECT 1", "postgres")
                return
            except Exception:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.5)

    def image(self, manifest: ArtifactIdentityManifest) -> str:
        return manifest.image.repository + "@" + manifest.image.digest

    def initial_compose(self) -> dict[str, Any]:
        base = {
            "image": self.image(self.manifests[0]),
            "entrypoint": ["/bin/sh", "-c"],
            "env_file": [".env"],
            "volumes": ["data:/volumes/data"],
            "networks": ["private"],
            "labels": {"traefik.enable": "false"},
            "healthcheck": {"disable": True},
        }
        return {
            "services": {
                "web": {
                    **base,
                    "command": [
                        "exec /venv/bin/python /volumes/scripts/run_odoo_startup.py -c /tmp/platform.odoo.conf"
                    ],
                },
                "scheduler": {
                    **base,
                    "command": [
                        "exec /venv/bin/python /volumes/scripts/run_odoo_startup.py -c /tmp/platform.odoo.conf --scheduler --ready-file /volumes/data/old-ready --ready-token aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
                    ],
                },
                "script-runner": {**base, "command": ["exec sleep infinity"]},
            },
            "volumes": {"data": {"external": True, "name": self.config["data_volume"]}},
            "networks": {"private": {"external": True, "name": self.network}},
        }

    def write_targets(self, compose: dict[str, Any]) -> None:
        values = {}
        for slot, project in (("old", self.old_project), ("new", self.new_project)):
            directory = self.root / project
            directory.mkdir()
            env = "".join(key + "=" + value + "\n" for key, value in self.environment.items())
            definition = json.dumps(compose)
            values[slot] = dict(
                appName=project,
                serverId="isolated",
                domains=[],
                autoDeploy=False,
                composeType="docker-compose",
                sourceType="raw",
                composeFile=definition,
                env=env,
                directory=str(directory),
                deployments=[],
            )
            (directory / "compose.yml").write_text(definition)
            (directory / ".env").write_text(env)
        (self.root / "targets.json").write_text(json.dumps(values))

    def identity(
        self, manifest: ArtifactIdentityManifest, slot: str, release: str
    ) -> RuntimeIdentity:
        return RuntimeIdentity(
            product="example-product",
            context="example-product",
            instance="prod",
            deployment_record_id="previous-deployment",
            artifact_id=manifest.artifact_id,
            source_git_ref=manifest.source_commit,
            image_reference=self.image(manifest),
            image_digest=manifest.image.digest,
            database_uuid=self.database_uuid,
            slot=slot,
            release_id=release,
        )

    def start_old(self) -> None:
        values = json.loads((self.root / "targets.json").read_text())
        for item in values.values():
            item["env"] += "CUTOVER_GUARD_REQUIRED=1\n"
            (Path(item["directory"]) / ".env").write_text(item["env"])
        identity = self.old_identity.model_dump(mode="json")
        identity["source_revision"] = identity.pop("source_git_ref")
        definition = json.loads(values["old"]["composeFile"])
        for name in ("web", "scheduler"):
            definition["services"][name]["environment"] = {
                "LAUNCHPLANE_RUNTIME_IDENTITY_JSON": json.dumps(identity),
                "ODOO_MAX_CRON_THREADS": "0" if name == "web" else "1",
            }
        definition["services"]["script-runner"]["environment"] = {
            "LAUNCHPLANE_RUNTIME_IDENTITY_JSON": json.dumps(identity)
        }
        values["old"]["composeFile"] = json.dumps(definition)
        (self.root / "targets.json").write_text(json.dumps(values))
        (self.root / self.old_project / "compose.yml").write_text(json.dumps(definition))
        self.command(
            (
                "docker",
                "exec",
                self.old_runner,
                "/venv/bin/python",
                "-c",
                "from pathlib import Path; Path('/volumes/data/old-ready').write_text('aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa')",
            )
        )
        self.command(
            (
                "docker",
                "compose",
                "--project-name",
                self.old_project,
                "--project-directory",
                str(self.root / self.old_project),
                "--file",
                str(self.root / self.old_project / "compose.yml"),
                "up",
                "-d",
                "--pull",
                "never",
                "--no-build",
                "web",
                "script-runner",
            )
        )
        self.old_runner = self.container(self.old_project, "script-runner")
        target = TraefikRouteTarget(
            route_id="isolated-route",
            host="fixture.invalid",
            route_file=self.root / "routes/lane.yaml",
            api_url="http://" + self.prefix + "-proxy:8080",
            entrypoints=("web",),
            tls=False,
        )
        target.route_file.write_text(
            json.dumps(route_payload(target, "http://" + self.old_project + "-web-1:8069"))
        )

    def wait_public(self) -> None:
        deadline = time.monotonic() + 120
        while True:
            try:
                with build_opener(ProxyHandler({})).open(
                    Request(
                        "http://fixture.invalid/launchplane/health",
                        headers={"Host": "fixture.invalid"},
                    ),
                    timeout=5,
                ) as response:
                    if response.status == 200:
                        return
            except HTTPError as error:
                error.close()
                web = self.container(self.old_project, "web")
                if (
                    self.command(
                        ("docker", "inspect", "--format", "{{.State.Running}}", web)
                    ).strip()
                    != "true"
                ):
                    self.capture_fixture_logs()
                    raise RuntimeError(
                        "isolated serving startup exited; its own logs are retained"
                    ) from error
                if time.monotonic() >= deadline:
                    raise
            except OSError:
                if time.monotonic() >= deadline:
                    raise
            time.sleep(1)

    def native_tick(self, project: str) -> None:
        query = "SELECT coalesce(json_object_agg(key,value::int),'{}'::json)::text FROM ir_config_parameter WHERE key LIKE 'cutover.fixture.native_ticks.%'"
        before = json.loads(self.sql(query))
        self.sql(
            "UPDATE ir_cron SET active=true,nextcall=now()-interval '1 minute' WHERE id IN (SELECT res_id FROM ir_model_data WHERE module='cutover_probe' AND name LIKE 'native_tick_%')"
        )
        self.sql("NOTIFY cron_trigger, 'fixture'", "postgres")
        deadline = time.monotonic() + 90
        while True:
            observed = json.loads(self.sql(query))
            if all(
                observed.get("cutover.fixture.native_ticks." + role, 0)
                > before.get("cutover.fixture.native_ticks." + role, 0)
                for role in WRITER_KINDS
                if role != "web"
            ):
                break
            if time.monotonic() >= deadline:
                raise TimeoutError("real native role jobs did not all tick")
            time.sleep(1)
        scheduler = self.container(project, "scheduler")
        result = self.command(
            (
                "docker",
                "exec",
                scheduler,
                "/venv/bin/python",
                "-c",
                "from pathlib import Path; ports={int(line.split()[1].rsplit(':',1)[1],16) for table in ('/proc/net/tcp','/proc/net/tcp6') for line in Path(table).read_text().splitlines()[1:] if line.split()[3]=='0A'}; assert not ports & {8069,8072}; print('no-http')",
            )
        )
        self.assertEqual(result.strip(), "no-http")

    def create_operation(self) -> OdooProdPromotionOperationRecord:
        original = _operation("isolated-installed-release")
        request = original.request.model_copy(
            update={
                "product": "example-product",
                "context": "example-product",
                "infrastructure_backup_record_id": "backup-example",
            }
        )
        operation = OdooProdPromotionOperationRecord.model_validate(
            {
                **original.model_dump(),
                "product": "example-product",
                "context": "example-product",
                "request": request,
                "request_fingerprint": odoo_prod_promotion_request_fingerprint(request),
                "operation_id": build_odoo_prod_promotion_operation_id(
                    product="example-product",
                    context="example-product",
                    idempotency_key=original.idempotency_key,
                    idempotency_scope=original.idempotency_scope,
                ),
                "authorization": original.authorization.model_copy(
                    update={"product": "example-product", "context": "example-product"}
                ),
            }
        )
        self.store.create_odoo_prod_promotion_operation_record_if_no_active_lane(operation)
        now = datetime.now(UTC)
        claimed = self.store.claim_next_odoo_prod_promotion_operation_record(
            lease_owner="isolated-original-worker",
            claimed_at=now.isoformat(),
            lease_expires_at=(now + timedelta(hours=2)).isoformat(),
        )
        assert claimed is not None
        return claimed

    def checkpoint(self, phase: str) -> None:
        current = self.store.read_odoo_prod_promotion_operation_record(self.original_operation_id)
        if (
            current.status != "running"
            or current.lease_owner != "isolated-original-worker"
            or current.product != self.plan.new.binding.product_id
            or current.context != self.plan.new.runtime_identity.context
            or current.request.request_id != self.plan.release_id
            or datetime.fromisoformat(current.lease_expires_at) <= datetime.now(UTC)
        ):
            raise RuntimeError("original isolated operation lost its lease or target")
        self.checkpoints.append(phase)

    def seed_plan(self, candidate: ArtifactIdentityManifest) -> OdooCutoverPlan:
        writers = tuple(
            WriterTarget.model_validate({"writer_id": kind, "kind": kind})
            for kind in ("web", "cron", "mail", "queue", "integration", "asset_gc")
        )
        runtime = OdooCutoverHostRuntime.model_validate(
            dict(
                route=dict(
                    route_file=str(self.root / "routes/lane.yaml"),
                    api_url="http://" + self.prefix + "-proxy:8080",
                    entrypoints=["web"],
                    tls=False,
                ),
                public_origin="http://fixture.invalid",
                authority_directory=str(self.root / "authority"),
                data_volume=self.config["data_volume"],
                private_network=self.network,
                prepared_volume=self.config["prepared_volume"],
                prepared_container=self.prefix + "-prepared",
                prepared_image=self.config["prepared_image"],
                restore_image=self.config["database_image"],
                filestore_path="/volumes/data/filestore",
                backup_root="/volumes/data/backups/launchplane",
                website_id=1,
                modules=[
                    "base",
                    "web",
                    "website",
                    "cutover_probe",
                    "launchplane_runtime_health",
                ],
                readiness_routes=[
                    dict(path="/", kind="homepage", contains="Isolated Odoo release"),
                    dict(path="/web/login", kind="login", contains="oe_login_form"),
                    dict(path="/contact", kind="public", contains="Isolated Odoo release"),
                ],
                public_routes=["/", "/contact", "/prices"],
                retained_assets=["/cutover_probe/static/mark.svg"],
                excluded_prefixes=["/web", "/my"],
                notice="Updates are being applied. Please return shortly.",
                cron_writer_bindings=[
                    dict(
                        writer_id=writer.writer_id,
                        cron_xmlids=["cutover_probe.native_tick_" + writer.kind],
                    )
                    for writer in writers
                    if writer.kind != "web"
                ],
                readiness_timeout_seconds=120,
            )
        )
        lane = OdooCutoverLane(
            route_id="isolated-route",
            host="fixture.invalid",
            database="fixture",
            database_uuid=self.database_uuid,
            prepared_origin="http://" + self.prefix + "-prepared:8080",
            slots=(
                OdooSlotCoordinates(
                    slot_id="old",
                    compose_id="old",
                    private_url="http://" + self.old_project + "-web-1:8069",
                ),
                OdooSlotCoordinates(
                    slot_id="new",
                    compose_id="new",
                    private_url="http://" + self.new_project + "-web-1:8069",
                ),
            ),
            writers=writers,
            host_runtime=runtime,
        )
        profile = _odoo_preview_profile_payload("example-product")
        profile.update(
            production_use="prelaunch",
            preview={"enabled": False},
            lanes=[
                dict(
                    context="example-product",
                    instance="prod",
                    base_url="http://fixture.invalid",
                    health_url="http://fixture.invalid/launchplane/health",
                    odoo_cutover=lane.model_dump(mode="json"),
                )
            ],
        )
        self.store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(profile)
        )
        for manifest in (self.manifests[0], candidate):
            self.store.write_artifact_manifest(manifest)
        self.store.write_release_tuple_record(
            ReleaseTupleRecord(
                tuple_id="original-prod",
                context="example-product",
                channel="prod",
                artifact_id=self.manifests[0].artifact_id,
                repo_shas={"fixture": self.manifests[0].source_commit},
                provenance="ship",
                minted_at=datetime.now(UTC).isoformat(),
            )
        )
        self.store.write_environment_inventory(
            EnvironmentInventory(
                context="example-product",
                instance="prod",
                source_git_ref=self.old_identity.source_git_ref,
                runtime_identity=self.old_identity,
                deploy=DeploymentEvidence(
                    target_type="compose",
                    target_name=self.old_project,
                    deploy_mode="atomic-two-slot",
                    status="pass",
                ),
                deployment_record_id=self.old_identity.deployment_record_id,
                updated_at=datetime.now(UTC).isoformat(),
            )
        )
        plan = resolve_profile_cutover_plan(
            self.store,
            product="example-product",
            context="example-product",
            instance="prod",
            artifact_id=candidate.artifact_id,
            release_id=self.operation.request.request_id,
        )
        assert plan is not None
        if self.config.get("website_settings", True):
            plan = OdooCutoverPlan.model_validate(
                {**plan.model_dump(), "website_settings": snapshot(plan)}
            )
        gate = self.backup_fixture.require(self.store)
        self.store.write_promotion_record(
            _build_promotion_record(
                record_id="promotion-cutover-" + cutover_record_id(plan),
                request=OdooProdPromotionRequest(
                    context="example-product",
                    artifact_id=candidate.artifact_id,
                    backup_record_id="backup-example",
                    infrastructure_backup_record_id="backup-example",
                ),
                deployment_record=None,
                deployment_status="pending",
                backup_gate=self.store.read_backup_gate_record("backup-example"),
                infrastructure_backup=gate,
            )
        )
        return plan

    def capture_fixture_logs(self) -> None:
        program = "import json,subprocess,sys; v=json.load(sys.stdin); r=subprocess.run(['docker','logs','--tail','100',v['container']],capture_output=True,text=True); print(json.dumps({'output':(r.stdout+r.stderr)[-64000:],'returncode':r.returncode}))"
        for project in self.projects:
            ids = self.command(
                ("docker", "ps", "-aq", "--filter", "label=com.docker.compose.project=" + project)
            ).split()
            for container in ids:
                output = self.command(
                    ("python3", "-c", program), json.dumps({"container": container})
                )
                (self.root / (container + "-logs.json")).write_text(output)

    def cleanup(self) -> None:
        self.capture_fixture_logs()
        for project in self.projects:
            directory = self.root / project
            if (directory / "compose.yml").exists():
                self.command(
                    (
                        "docker",
                        "compose",
                        "--project-name",
                        project,
                        "--project-directory",
                        str(directory),
                        "--file",
                        str(directory / "compose.yml"),
                        "down",
                        "--remove-orphans",
                    )
                )

    def observe_public_continuity(self) -> None:
        self.public_samples: list[dict[str, Any]] = []
        self.public_errors: list[str] = []
        stop = threading.Event()

        def observe() -> None:
            opener = build_opener(ProxyHandler({}))
            while not stop.is_set():
                try:
                    with opener.open(
                        Request("http://fixture.invalid/", headers={"Host": "fixture.invalid"}),
                        timeout=5,
                    ) as response:
                        body = response.read()
                        pause = response.headers.get("x-launchplane-pause-id", "")
                        if response.status != 200 or b"Isolated Odoo release" not in body:
                            self.public_errors.append("public root did not serve the declared site")
                        self.public_samples.append(dict(status=response.status, pause_id=pause))
                    if pause:
                        try:
                            with opener.open(
                                Request(
                                    "http://fixture.invalid/fixture-submit",
                                    data=b"message=must-stay-paused",
                                    headers={"Host": "fixture.invalid"},
                                ),
                                timeout=5,
                            ) as response:
                                self.public_errors.append("prepared serving accepted a submission")
                        except HTTPError as error:
                            if error.code != 423:
                                self.public_errors.append(
                                    "prepared submission did not return its pause response"
                                )
                            error.close()
                except HTTPError as error:
                    self.public_errors.append("public root status " + str(error.code))
                    error.close()
                except OSError as error:
                    self.public_errors.append(type(error).__name__)
                stop.wait(0.2)

        thread = threading.Thread(target=observe)
        thread.start()

        def finish() -> None:
            stop.set()
            thread.join(10)
            if thread.is_alive():
                raise TimeoutError("public fixture observer did not stop")
            (self.root / "public-observations.json").write_text(
                json.dumps(dict(samples=self.public_samples, errors=self.public_errors), indent=2)
            )

        self.finish_public_observation = finish
        self.addCleanup(finish)

    def test_installed_host_forward_rollback_native_writers_and_durable_pause(self) -> None:
        self.assertIsNone(shutil.which("docker"))
        factory = service_odoo_cutover_factory(self.store, self.root)
        provider = factory(self.plan)
        deployment = DeploymentRecord(
            record_id=self.plan.new.runtime_identity.deployment_record_id,
            context="example-product",
            instance="prod",
            source_git_ref=self.plan.candidate.source_commit,
            deploy=DeploymentEvidence(
                target_type="compose", target_name="new", deploy_mode="atomic-two-slot"
            ),
        )
        progress = DeploymentCutoverStore(self.store, deployment)
        self.observe_public_continuity()
        with bind_cutover_execution(self.plan, self.checkpoint):
            completed = execute_odoo_cutover(
                self.plan, progress, provider, record_id=cutover_record_id(self.plan)
            )
        self.assertEqual(completed.step, "complete")
        self.assertIsNotNone(completed.backup)
        if completed.pause is not None:
            self.assertEqual(
                {item.writer for item in completed.pause.writers}, set(self.plan.writers)
            )
            self.assertTrue(
                all(
                    item.evidence_id and item.fenced and item.active_jobs == 0
                    for item in completed.pause.writers
                )
            )
        self.native_tick(self.new_project)
        if self.config.get("database_changing"):
            with self.assertRaisesRegex(ValueError, "coherent"):
                rollback_compatible_cutover(progress, factory(self.plan), completed.record_id)
            rolled = completed
        else:
            restarted = factory(self.plan)
            with bind_cutover_execution(self.plan, self.checkpoint):
                rolled = rollback_compatible_cutover(progress, restarted, completed.record_id)
            self.assertEqual(rolled.step, "rolled_back")
            self.native_tick(self.old_project)
        self.assertEqual(
            self.store.read_odoo_prod_promotion_operation_record(
                self.original_operation_id
            ).idempotency_key,
            self.operation.idempotency_key,
        )
        self.finish_public_observation()
        self.assertTrue(self.public_samples)
        self.assertEqual(self.public_errors, [])
        if completed.pause is not None:
            self.assertTrue(
                any(sample["pause_id"] == completed.record_id for sample in self.public_samples)
            )
        (self.root / "result.json").write_text(
            json.dumps(
                {
                    "status": "pass",
                    "operation_id": self.original_operation_id,
                    "cutover": rolled.model_dump(mode="json"),
                    "checkpoint_count": len(self.checkpoints),
                    "website_settings_qualified": self.config.get("website_settings", True),
                },
                indent=2,
            )
        )
