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
        self.effects: list[str] = []
        self.fail_recreate = ""
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
                    "env_file": [{"path": str(self.files[project])}]
                }
                self.containers[name] = {
                    "State": {"Running": False, "Restarting": False},
                    "Config": {
                        "Image": slot.image,
                        "Env": raw.splitlines(),
                        "Labels": {
                            "com.docker.compose.project": project,
                            "com.docker.compose.service": service,
                            "com.docker.compose.project.working_dir": str(directory),
                            "com.docker.compose.project.config_files": str(
                                directory / "compose.yaml"
                            ),
                        },
                    },
                }
                targets.append(SlotWriterContainer(writer, slot.slot_id, name, slot.image))
        self.writers = ContainerSlotWriters(self.plan, tuple(targets), self.execute, lambda _: 0)
        self.startup = DokploySlotStartup(
            host="http://provider.invalid",
            token="inert-provider",
            writers=self.writers,
            execute=self.execute,
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
        if argv[:2] == ("python3", "-c"):
            assert stdin is not None
            request = json.loads(stdin)
            if request["apply"]:
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
                if project == self.fail_recreate:
                    raise TimeoutError("recreation reply lost")
                for writer in self.plan.writers:
                    key = project, writer.writer_id
                    old = self.current[key]
                    name = old + "-recreated"
                    container = json.loads(json.dumps(self.containers.pop(old)))
                    container["Config"]["Env"] = [
                        line
                        for line in self.files[project].read_text().splitlines()
                        if not line.startswith("#")
                    ]
                    if self.wrong_image:
                        container["Config"]["Image"] = "wrong-image"
                    self.containers[name] = container
                    self.current[key] = name
                # This adapter's supported operation must keep dependencies and
                # all consumers stopped. A restart cannot refresh creation env.
                self.assertIn("--no-start", argv)
                self.assertIn("--no-deps", argv)
                return ""
            if "ps" in argv:
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
