"""Exercise the durable env program and the stopped recreation adapter."""

import base64
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from typing import Any
import unittest
from unittest.mock import patch

from control_plane.contracts.odoo_cutover import OdooCutoverPlan
from control_plane.contracts.prepared_public_site import WriterTarget
from control_plane._odoo_website_settings_program import WEBSITE_INDEXING_PARAMETER
from control_plane.dokploy import api
from control_plane.dokploy.slot_startup import DokploySlotStartup, merge_startup_environment
from control_plane.dokploy.slot_writers import ContainerSlotWriters, SlotWriterContainer
from control_plane.odoo_instance_overrides import ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY
from tests.test_odoo_cutover_website_settings import WebsiteProvider


class SlotStartupTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.fixture = WebsiteProvider(self.root / "fixture")
        self.addCleanup(self.fixture.engine.dispose)
        self.plan = self.fixture.cutover
        self.environments: dict[str, str] = {}
        self.files: dict[str, Path] = {}
        self.configs: dict[str, dict[str, Any]] = {}
        self.containers: dict[str, dict[str, Any]] = {}
        self.current: dict[tuple[str, str], str] = {}
        self.templates: dict[tuple[str, str], dict[str, Any]] = {}
        self.interruption = ""
        self.authority_directory = self.root / "authority"
        self.authority_directory.mkdir()
        self.effects: list[str] = []
        self.fail_recreate = ""
        self.fail_readback = ""
        self.wrong_image = self.bad_config = self.lost_env = False
        targets = []
        payload = {
            "instance": "prod",
            "config_parameters": [
                {"key": "unrelated.setting", "value": {"source": "literal", "value": "keep-me"}}
            ],
            "addon_settings": [{"keep": "inert-secret-binding"}],
        }
        encoded = base64.b64encode(json.dumps(payload).encode()).decode()
        for slot in (self.plan.old, self.plan.new):
            project = slot.compose_id
            directory = self.root / project
            directory.mkdir()
            self.files[project] = directory / ".env"
            raw = (
                "# Preserve this comment\nUNRELATED_SECRET=inert-secret\n"
                + ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY
                + "="
                + encoded
                + "\n"
            )
            self.files[project].write_text(raw)
            self.environments[project] = raw
            self.configs[project] = {"services": {}}
            for writer in self.plan.writers:
                service = writer.writer_id
                name = project + "-" + service
                self.current[project, service] = name
                self.configs[project]["services"][service] = {
                    "image": slot.image,
                    "env_file": [{"path": str(self.files[project])}],
                }
                self.containers[name] = {
                    "State": {"Running": False, "Restarting": False},
                    "Config": {
                        "Image": slot.image,
                        "Env": raw.splitlines(),
                        "Labels": {
                            "com.docker.compose.project": project,
                            "com.docker.compose.service": service,
                            "com.docker.compose.container-number": "1",
                            "com.docker.compose.oneoff": "False",
                            "com.docker.compose.project.working_dir": str(directory),
                            "com.docker.compose.project.config_files": str(
                                directory / "compose.yaml"
                            ),
                        },
                    },
                }
                self.templates[project, service] = json.loads(json.dumps(self.containers[name]))
                targets.append(SlotWriterContainer(writer, slot.slot_id, name, slot.image))
        self.writers = ContainerSlotWriters(self.plan, tuple(targets), self.execute, lambda _: 0)
        self.startup = DokploySlotStartup(
            host="http://provider.invalid",
            token="inert-provider",
            writers=self.writers,
            execute=self.execute,
            authority_directory=str(self.authority_directory),
        )
        self.enterContext(
            patch.object(api, "fetch_dokploy_target_payload", side_effect=self.target)
        )
        self.enterContext(patch.object(api, "update_dokploy_target_env", side_effect=self.update))

    def target(self, **kwargs: Any) -> dict[str, Any]:
        project = kwargs["target_id"]
        return {"appName": project, "domains": [], "env": self.environments[project]}

    def update(self, **kwargs: Any) -> None:
        self.effects.append("provider-env:" + kwargs["target_id"])
        if not self.lost_env:
            self.environments[kwargs["target_id"]] = kwargs["env_text"]

    def execute(self, argv: tuple[str, ...], stdin: str | None) -> str:
        if argv[:2] == ("docker", "inspect"):
            return json.dumps([self.containers[argv[2]]])
        if argv[:2] == ("docker", "ps"):
            project = argv[-3].split("=", 2)[-1]
            service = argv[-1].split("=", 2)[-1]
            if project == self.fail_readback:
                raise TimeoutError("inventory readback reply lost")
            return "\n".join(
                name
                for name, value in self.containers.items()
                if value["Config"]["Labels"]["com.docker.compose.project"] == project
                and value["Config"]["Labels"]["com.docker.compose.service"] == service
            )
        if argv[:3] == ("docker", "container", "rm"):
            self.effects.append("remove:" + argv[3])
            del self.containers[argv[3]]
            return argv[3]
        if argv[:2] == ("python3", "-c"):
            assert stdin is not None
            request = json.loads(stdin)
            if request.get("apply"):
                self.effects.append("host-env:" + request["path"])
            return subprocess.run(
                [sys.executable, "-c", argv[2]],
                input=stdin,
                text=True,
                capture_output=True,
                check=True,
            ).stdout
        if argv[:2] == ("docker", "compose"):
            project = argv[argv.index("--project-name") + 1]
            if "config" in argv:
                if self.bad_config:
                    return json.dumps(
                        {"services": {name: {} for name in self.configs[project]["services"]}}
                    )
                return json.dumps(self.configs[project])
            if "up" in argv:
                self.effects.append("recreate:" + project)
                for writer in self.plan.writers:
                    key = project, writer.writer_id
                    old = self.current[key]
                    name = old + "-recreated"
                    matching = [
                        name
                        for name, value in self.containers.items()
                        if value["Config"]["Labels"]["com.docker.compose.project"] == project
                        and value["Config"]["Labels"]["com.docker.compose.service"]
                        == writer.writer_id
                    ]
                    template = self.containers[matching[0]] if matching else self.templates[key]
                    container = json.loads(json.dumps(template))
                    for candidate in matching:
                        del self.containers[candidate]
                    if self.interruption == "zero":
                        raise TimeoutError("interrupted after removal")
                    container["Config"]["Env"] = [
                        line
                        for line in self.files[project].read_text().splitlines()
                        if not line.startswith("#")
                    ]
                    if self.wrong_image:
                        container["Config"]["Image"] = "wrong-image"
                    self.containers[name] = container
                    self.current[key] = name
                    if self.interruption == "multiple":
                        self.containers[name + "-intermediate"] = json.loads(json.dumps(container))
                        raise TimeoutError("interrupted with multiple stopped service containers")
                if project == self.fail_recreate:
                    raise TimeoutError("recreation reply lost after replacing containers")
                # This adapter's supported operation must keep dependencies and
                # all consumers stopped. A restart cannot refresh creation env.
                self.assertIn("--no-start", argv)
                self.assertIn("--no-deps", argv)
                return ""
            if "ps" in argv:
                if project == self.fail_readback:
                    raise TimeoutError("inventory readback reply lost")
                return self.current[project, argv[-1]]
        raise AssertionError("unexpected startup command")

    def test_forward_and_rollback_preserve_values_and_refresh_shared_inventory(self) -> None:
        before = self.writers.targets
        files = {key: path.read_text() for key, path in self.files.items()}
        self.startup.preflight(self.plan)
        self.assertFalse(self.effects)
        self.assertEqual(files, {key: path.read_text() for key, path in self.files.items()})
        for slot, intent in (
            (self.plan.new, self.plan.website_settings),
            (self.plan.old, self.plan.rollback_website_settings),
        ):
            assert intent is not None
            readback = self.startup(self.plan, slot, intent)
            self.assertEqual(readback.selected_digest, intent.settings.digest)
            self.assertEqual(readback.retained_digest, intent.settings.digest)
            self.assertEqual(readback.containers, self.writers.targets)
            self.assertNotEqual(before, readback.containers)
            for project, path in self.files.items():
                self.assertIn("UNRELATED_SECRET=inert-secret", path.read_text())
                for raw in (path.read_text(), self.environments[project]):
                    payload = json.loads(
                        base64.b64decode(
                            api.parse_dokploy_env_text(raw)[ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY]
                        )
                    )
                    self.assertEqual(
                        payload["website_settings"], intent.settings.model_dump(mode="json")
                    )
                    self.assertEqual(payload["addon_settings"], [{"keep": "inert-secret-binding"}])
                    params = {
                        item["key"]: item["value"]["value"] for item in payload["config_parameters"]
                    }
                    self.assertEqual(params["unrelated.setting"], "keep-me")
                    self.assertEqual(
                        params[WEBSITE_INDEXING_PARAMETER],
                        "1" if intent.settings.public_indexing_enabled else "0",
                    )
            self.assertTrue(all(not item["State"]["Running"] for item in self.containers.values()))

    def test_preflight_refuses_missing_env_consumption_without_effects(self) -> None:
        self.bad_config = True
        with self.assertRaises(ValueError):
            self.startup.preflight(self.plan)
        self.assertFalse(self.effects)

    def test_running_writers_and_wrong_recreated_image_cannot_return_success(self) -> None:
        assert self.plan.website_settings is not None
        first = self.writers.targets[0]
        self.containers[first.container]["State"]["Running"] = True
        with self.assertRaises(ValueError):
            self.startup(self.plan, self.plan.new, self.plan.website_settings)
        self.assertFalse(self.effects)
        self.containers[first.container]["State"]["Running"] = False
        self.wrong_image = True
        with self.assertRaises(ValueError):
            self.startup(self.plan, self.plan.new, self.plan.website_settings)

    def test_partial_recreation_refreshes_known_inventory_and_keeps_uncertainty(self) -> None:
        assert self.plan.website_settings is not None
        original = self.writers.targets
        self.fail_recreate = self.plan.new.compose_id
        with self.assertRaises(TimeoutError):
            self.startup(self.plan, self.plan.new, self.plan.website_settings)
        self.assertNotEqual(self.writers.targets, original)
        self.assertTrue(all(not item["State"]["Running"] for item in self.containers.values()))
        self.assertTrue(all(item.container in self.containers for item in self.writers.targets))
        self.fail_recreate = ""
        assert self.plan.rollback_website_settings is not None
        self.startup(self.plan, self.plan.old, self.plan.rollback_website_settings)

    def test_changed_compose_image_and_later_env_override_refuse_before_any_write(self) -> None:
        assert self.plan.website_settings is not None
        definition = next(iter(self.configs[self.plan.old.compose_id]["services"].values()))
        for image in ("other@sha256:" + "a" * 64, "${ODOO_IMAGE}"):
            definition["image"] = image
            with (
                self.subTest(image=image),
                self.assertRaisesRegex(ValueError, "Compose writer image"),
            ):
                self.startup(self.plan, self.plan.new, self.plan.website_settings)
            self.assertFalse(self.effects)
        definition["image"] = self.plan.old.image
        definition["env_file"].append({"path": "/another.env"})
        with self.assertRaisesRegex(ValueError, "without an override"):
            self.startup.preflight(self.plan)
        self.assertFalse(self.effects)

    def test_lost_rollback_recreation_and_readback_recover_without_deleted_ids(self) -> None:
        assert self.plan.website_settings is not None
        assert self.plan.rollback_website_settings is not None
        self.startup(self.plan, self.plan.new, self.plan.website_settings)
        self.fail_recreate = self.plan.old.compose_id
        self.fail_readback = self.plan.old.compose_id
        with self.assertRaises(TimeoutError):
            self.startup(self.plan, self.plan.old, self.plan.rollback_website_settings)
        # The next read recovers via the previously observed Compose authority,
        # before inspecting IDs removed by the uncertain recreation.
        self.fail_recreate = self.fail_readback = ""
        self.startup(self.plan, self.plan.old, self.plan.rollback_website_settings)
        self.assertTrue(all(item.container in self.containers for item in self.writers.targets))

    def test_provider_readback_disagreement_stops_before_file_or_container_changes(self) -> None:
        assert self.plan.website_settings is not None
        self.lost_env = True
        original = {name: path.read_text() for name, path in self.files.items()}
        with self.assertRaises(ValueError):
            self.startup(self.plan, self.plan.new, self.plan.website_settings)
        self.assertEqual(original, {name: path.read_text() for name, path in self.files.items()})
        self.assertFalse(any(effect.startswith("recreate:") for effect in self.effects))

    def test_ambiguous_payload_is_refused_without_dropping_values(self) -> None:
        raw = ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY + "=\n"
        assert self.plan.website_settings is not None
        with self.assertRaises(ValueError):
            merge_startup_environment(
                raw + raw,
                self.plan.website_settings.settings.model_dump(mode="json"),
                ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY,
                WEBSITE_INDEXING_PARAMETER,
            )

    def restart_adapter(self) -> DokploySlotStartup:
        # Preserve the stale, admitted writer identities, as a reconstructed
        # host binding would; recovery may never inspect those deleted IDs.
        writers = ContainerSlotWriters(self.plan, self.writers.targets, self.execute, lambda _: 0)
        self.writers = writers
        return DokploySlotStartup(
            host="http://provider.invalid",
            token="inert-provider",
            writers=writers,
            execute=self.execute,
            authority_directory=str(self.authority_directory),
        )

    def recover(self, *, jobs: int = 0) -> None:
        assert self.plan.rollback_website_settings is not None
        self.startup.recover(
            self.plan,
            self.plan.old,
            self.plan.rollback_website_settings,
            verify_guard=lambda plan: self.assertEqual(plan, self.plan),
            active_jobs=lambda slot, writer: jobs,
        )

    def test_recovery_after_restart_converges_zero_and_multiple_without_old_ids(self) -> None:
        assert self.plan.website_settings is not None
        for mode in ("zero", "multiple"):
            with self.subTest(mode=mode):
                self.interruption = mode
                with self.assertRaises((TimeoutError, ValueError)):
                    self.startup(self.plan, self.plan.new, self.plan.website_settings)
                self.assertTrue(list(self.authority_directory.glob("*.json")))
                # Both the adapter and its in-memory project map are discarded.
                self.startup = self.restart_adapter()
                self.interruption = ""
                self.recover()
                self.assertEqual(
                    set(self.containers), {target.container for target in self.writers.targets}
                )
                self.assertTrue(
                    all(not value["State"]["Running"] for value in self.containers.values())
                )
                assert self.plan.rollback_website_settings is not None
                from control_plane.dokploy.slot_website_settings import (
                    _verify_startup_containers,
                    StartupSettingsReadback,
                )

                _verify_startup_containers(
                    self.plan,
                    self.plan.rollback_website_settings,
                    StartupSettingsReadback(
                        self.plan.rollback_website_settings.settings.digest,
                        self.plan.rollback_website_settings.settings.digest,
                        self.writers.targets,
                    ),
                    self.execute,
                )

    def interrupt(self) -> None:
        assert self.plan.website_settings is not None
        self.interruption = "multiple"
        with self.assertRaises(ValueError):
            self.startup(self.plan, self.plan.new, self.plan.website_settings)
        self.interruption = ""
        self.startup = self.restart_adapter()
        self.effects.clear()

    def test_recovery_refuses_foreign_running_oneoff_or_undrained_before_effects(self) -> None:
        for change in ("image", "running", "service-authority", "oneoff", "jobs"):
            with self.subTest(change=change):
                self.interrupt()
                container = next(iter(self.containers.values()))
                original = json.loads(json.dumps(container))
                if change == "image":
                    container["Config"]["Image"] = "foreign@sha256:" + "f" * 64
                if change == "running":
                    container["State"]["Running"] = True
                if change == "service-authority":
                    container["Config"]["Labels"]["com.docker.compose.project.config_files"] = (
                        "/foreign/compose.yaml"
                    )
                if change == "oneoff":
                    container["Config"]["Labels"]["com.docker.compose.oneoff"] = "True"
                with self.assertRaises(ValueError):
                    self.recover(jobs=1 if change == "jobs" else 0)
                self.assertFalse(self.effects)
                container.clear()
                container.update(original)
                self.recover()

    def test_missing_corrupt_or_changed_authority_and_guard_refuse_recovery(self) -> None:
        with self.assertRaisesRegex(ValueError, "previously admitted"):
            self.recover()
        self.interrupt()
        path = next(self.authority_directory.glob("*.json"))
        original = path.read_text()
        for mutation in ("corrupt", "plan", "project", "writers", "path"):
            saved = json.loads(original)
            if mutation == "plan":
                saved["plan_sha256"] = "foreign"
            if mutation == "project":
                saved["projects"][0]["project"] = "foreign"
            if mutation == "writers":
                saved["projects"][0]["services"] = []
            if mutation == "path":
                saved["projects"][0]["directory"] = "/admitted/../foreign"
            path.write_text("invalid" if mutation == "corrupt" else json.dumps(saved))
            with (
                self.subTest(mutation=mutation),
                self.assertRaises((ValueError, subprocess.CalledProcessError)),
            ):
                self.recover()
            self.assertFalse(self.effects)
        path.write_text(original)
        assert self.plan.rollback_website_settings is not None

        def refused(_plan: object) -> None:
            raise ValueError("exclusive operation/write pause not held")

        with self.assertRaisesRegex(ValueError, "not held"):
            self.startup.recover(
                self.plan,
                self.plan.old,
                self.plan.rollback_website_settings,
                verify_guard=refused,
                active_jobs=lambda *_: 0,
            )
        self.assertFalse(self.effects)
        self.recover()

    def test_recovery_lost_reply_is_held_then_fresh_pass_converges(self) -> None:
        self.interrupt()
        self.fail_recreate = self.plan.old.compose_id
        with self.assertRaises(TimeoutError):
            self.recover()
        self.startup = self.restart_adapter()
        self.fail_recreate = ""
        self.recover()
        self.assertEqual(
            set(self.containers), {target.container for target in self.writers.targets}
        )

    def test_new_process_recovers_from_durable_authority_and_stale_inventory(self) -> None:
        self.interrupt()
        request = {
            "plan": self.plan.model_dump(mode="json"),
            "environments": self.environments,
            "files": {key: str(value) for key, value in self.files.items()},
            "configs": self.configs,
            "containers": self.containers,
            "current": [[*key, value] for key, value in self.current.items()],
            "templates": [[*key, value] for key, value in self.templates.items()],
            "authority_directory": str(self.authority_directory),
            "writers": [
                {
                    "writer": target.writer.model_dump(mode="json"),
                    "slot_id": target.slot_id,
                    "container": target.container,
                    "image": target.image,
                }
                for target in self.writers.targets
            ],
        }
        process = subprocess.run(
            [
                sys.executable,
                "-c",
                "import json,sys; from tests.test_odoo_slot_startup import recover_in_fresh_process; "
                "print(json.dumps(recover_in_fresh_process(json.load(sys.stdin))))",
            ],
            input=json.dumps(request),
            text=True,
            capture_output=True,
            check=True,
        )
        result = json.loads(process.stdout)
        self.assertEqual(set(result["containers"]), set(result["targets"]))
        self.assertTrue(result["stopped"])
        self.assertTrue(result["verified"])


def recover_in_fresh_process(request: dict[str, Any]) -> dict[str, Any]:
    """File-backed host simulation in a genuinely fresh adapter process."""
    fixture = SlotStartupTests()
    fixture.plan = OdooCutoverPlan.model_validate(request["plan"])
    fixture.environments = request["environments"]
    fixture.files = {key: Path(value) for key, value in request["files"].items()}
    fixture.configs, fixture.containers = request["configs"], request["containers"]
    fixture.current = {(project, service): value for project, service, value in request["current"]}
    fixture.templates = {
        (project, service): value for project, service, value in request["templates"]
    }
    fixture.authority_directory = Path(request["authority_directory"])
    fixture.effects = []
    fixture.interruption = fixture.fail_recreate = fixture.fail_readback = ""
    fixture.wrong_image = fixture.bad_config = fixture.lost_env = False
    targets = tuple(
        SlotWriterContainer(
            WriterTarget.model_validate(target["writer"]),
            target["slot_id"],
            target["container"],
            target["image"],
        )
        for target in request["writers"]
    )
    fixture.writers = ContainerSlotWriters(fixture.plan, targets, fixture.execute, lambda _: 0)
    fixture.startup = fixture.restart_adapter()
    with (
        patch.object(api, "fetch_dokploy_target_payload", side_effect=fixture.target),
        patch.object(api, "update_dokploy_target_env", side_effect=fixture.update),
    ):
        assert fixture.plan.rollback_website_settings is not None
        readback = fixture.startup.recover(
            fixture.plan,
            fixture.plan.old,
            fixture.plan.rollback_website_settings,
            verify_guard=lambda _: None,
            active_jobs=lambda *_: 0,
        )
    return {
        "containers": list(fixture.containers),
        "targets": [target.container for target in readback.containers],
        "stopped": all(not value["State"]["Running"] for value in fixture.containers.values()),
        "verified": readback.selected_digest
        == fixture.plan.rollback_website_settings.settings.digest,
    }
