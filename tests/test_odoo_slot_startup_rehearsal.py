"""Opt-in real Compose qualification of stopped startup and durable recovery."""

import base64
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from typing import Any
import unittest
from unittest.mock import patch
from uuid import uuid4

from control_plane.contracts.odoo_cutover import OdooCutoverPlan
from control_plane.dokploy import api
from control_plane.dokploy.slot_startup import DokploySlotStartup
from control_plane.dokploy.slot_writers import ContainerSlotWriters, SlotWriterContainer
from control_plane.odoo_instance_overrides import ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY
from control_plane.release_compatibility import classify_release_database_compatibility
from tests.test_odoo_cutover_website_settings import WebsiteProvider


@unittest.skipUnless(
    os.environ.get("LAUNCHPLANE_TEST_STARTUP_IMAGE"),
    "isolated Compose startup image not selected",
)
class SlotStartupRehearsalTests(unittest.TestCase):
    def setUp(self) -> None:
        Path("state").mkdir(exist_ok=True)
        temporary = TemporaryDirectory(dir="state")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        fixture = WebsiteProvider(self.root / "fixture")
        self.addCleanup(fixture.engine.dispose)
        self.image = os.environ["LAUNCHPLANE_TEST_STARTUP_IMAGE"]
        if "@sha256:" not in self.image:
            raise ValueError("startup rehearsal requires an explicit immutable local image")
        self.run_command(("docker", "image", "inspect", self.image))
        original = fixture.cutover
        repository, digest = self.image.split("@", 1)
        manifests = [
            original.production.model_copy(deep=True),
            original.candidate.model_copy(deep=True),
        ]
        for manifest in manifests:
            manifest.image.repository, manifest.image.digest = repository, digest
        slots = [
            slot.model_copy(
                update={
                    "image": self.image,
                    "binding": slot.binding.model_copy(update={"image_digest": digest}),
                    "runtime_identity": slot.runtime_identity.model_copy(
                        update={
                            "image_reference": self.image,
                            "image_digest": digest,
                        }
                    ),
                }
            )
            for slot in (original.old, original.new)
        ]
        self.plan = OdooCutoverPlan.model_validate(
            {
                **original.model_dump(),
                "old": slots[0],
                "new": slots[1],
                "production": manifests[0],
                "candidate": manifests[1],
                "compatibility": classify_release_database_compatibility(
                    production=manifests[0],
                    candidate=manifests[1],
                ),
            }
        )
        self.projects: dict[str, tuple[str, Path]] = {}
        self.environments: dict[str, str] = {}
        self.effects: list[tuple[str, ...]] = []
        self.lose_reply = False
        self.directory = self.root / "authority"
        self.directory.mkdir(mode=0o700)
        self.addCleanup(self.cleanup_projects)
        targets = []
        prefix = "lp-startup-" + uuid4().hex[:10]
        for slot in (self.plan.old, self.plan.new):
            project = prefix + "-" + slot.slot_id
            directory = self.root / slot.slot_id
            directory.mkdir()
            self.projects[slot.compose_id] = project, directory
            payload = {
                "instance": "prod",
                "config_parameters": [],
                "addon_settings": [{"inert": "preserve"}],
            }
            raw = (
                "UNRELATED_SECRET=inert-rehearsal-sentinel\n"
                + ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY
                + "="
                + base64.b64encode(json.dumps(payload).encode()).decode()
                + "\n"
            )
            self.environments[slot.compose_id] = raw
            (directory / ".env").write_text(raw)
            # Every service is disposable, has no network, ports or data mounts,
            # and stays stopped. No Odoo registry or outbound code runs here.
            compose = {
                "services": {
                    writer.writer_id: {
                        "image": self.image,
                        "command": ["sleep", "infinity"],
                        "network_mode": "none",
                        "env_file": [".env"],
                    }
                    for writer in self.plan.writers
                }
            }
            (directory / "compose.yaml").write_text(json.dumps(compose))
            argv = self.compose(slot.compose_id)
            self.run_command(argv + ("up", "--no-start", "--pull", "never", "--no-build"))
            for writer in self.plan.writers:
                container = self.run_command(
                    argv + ("ps", "--all", "--quiet", writer.writer_id)
                ).strip()
                self.assertTrue(container)
                targets.append(SlotWriterContainer(writer, slot.slot_id, container, self.image))
        self.writers = ContainerSlotWriters(
            self.plan, tuple(targets), self.execute, active_jobs=lambda _target: 0
        )
        self.startup = self.adapter()
        self.enterContext(
            patch.object(api, "fetch_dokploy_target_payload", side_effect=self.target)
        )
        self.enterContext(patch.object(api, "update_dokploy_target_env", side_effect=self.update))

    @staticmethod
    def run_command(argv: tuple[str, ...], stdin: str | None = None) -> str:
        result = subprocess.run(
            argv, input=stdin, text=True, capture_output=True, timeout=90, check=False
        )
        if result.returncode:
            raise RuntimeError(result.stderr[-4000:])
        return result.stdout

    def compose(self, compose_id: str) -> tuple[str, ...]:
        project, directory = self.projects[compose_id]
        return (
            "docker",
            "compose",
            "--project-name",
            project,
            "--project-directory",
            str(directory),
            "--file",
            str(directory / "compose.yaml"),
        )

    def cleanup_projects(self) -> None:
        for compose_id in self.projects:
            self.run_command(self.compose(compose_id) + ("down", "--remove-orphans"))

    def target(self, **kwargs: Any) -> dict[str, Any]:
        compose_id = kwargs["target_id"]
        return {
            "appName": self.projects[compose_id][0],
            "domains": [],
            "env": self.environments[compose_id],
        }

    def update(self, **kwargs: Any) -> None:
        self.environments[kwargs["target_id"]] = kwargs["env_text"]
        self.effects.append(("provider-env", kwargs["target_id"]))

    def execute(self, argv: tuple[str, ...], stdin: str | None) -> str:
        if argv[:2] == ("python3", "-c"):
            argv = (sys.executable, *argv[1:])
        if "up" in argv or argv[:3] == ("docker", "container", "rm"):
            self.effects.append(argv)
        result = self.run_command(argv, stdin)
        if "up" in argv and self.lose_reply:
            self.lose_reply = False
            raise TimeoutError("injected lost reply after real Compose recreation")
        return result

    def adapter(self) -> DokploySlotStartup:
        return DokploySlotStartup(
            host="https://provider.invalid",
            token="inert",
            writers=self.writers,
            execute=self.execute,
            authority_directory=str(self.directory),
        )

    def assert_stopped_settings(self, *, rollback: bool) -> None:
        intent = self.plan.rollback_website_settings if rollback else self.plan.website_settings
        assert intent is not None
        for target in self.writers.targets:
            observed = json.loads(self.run_command(("docker", "inspect", target.container)))[0]
            self.assertFalse(observed["State"]["Running"])
            environment = dict(item.split("=", 1) for item in observed["Config"]["Env"])
            self.assertEqual(environment["UNRELATED_SECRET"], "inert-rehearsal-sentinel")
            payload = json.loads(
                base64.b64decode(environment[ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY])
            )
            self.assertEqual(payload["website_settings"], intent.settings.model_dump(mode="json"))
            self.assertEqual(payload["addon_settings"], [{"inert": "preserve"}])

    def test_forward_lost_reply_and_reconstructed_rollback_inventory(self) -> None:
        assert self.plan.website_settings is not None
        assert self.plan.rollback_website_settings is not None
        before = self.writers.targets
        self.startup.preflight(self.plan)
        self.assertFalse(self.effects)
        self.lose_reply = True
        with self.assertRaises(TimeoutError):
            self.startup(self.plan, self.plan.new, self.plan.website_settings)
        self.assertTrue(tuple(self.directory.glob("*.json")))
        # Rebuild against the original, now-deleted IDs. Only durable project
        # authority plus fresh Docker inventory may recover them.
        self.writers = ContainerSlotWriters(self.plan, before, self.execute, lambda _target: 0)
        self.startup = self.adapter()
        guard_calls = []
        readback = self.startup.recover(
            self.plan,
            self.plan.old,
            self.plan.rollback_website_settings,
            verify_guard=lambda plan: guard_calls.append(plan),
            active_jobs=lambda _slot, _writer: 0,
        )
        self.assertTrue(guard_calls)
        self.assertEqual(readback.containers, self.writers.targets)
        self.assertNotEqual(before, self.writers.targets)
        self.assert_stopped_settings(rollback=True)

    def test_zero_and_duplicate_stopped_inventory_converge_but_foreign_remnant_holds(self) -> None:
        assert self.plan.website_settings is not None
        assert self.plan.rollback_website_settings is not None
        self.startup(self.plan, self.plan.new, self.plan.website_settings)
        stale = self.writers.targets
        missing, duplicate = stale[0], stale[-1]
        self.run_command(("docker", "container", "rm", missing.container))
        observed = json.loads(self.run_command(("docker", "inspect", duplicate.container)))[0]
        labels = observed["Config"]["Labels"]
        arguments: tuple[str, ...] = ("docker", "create", "--network", "none")
        for key, value in labels.items():
            arguments += ("--label", key + "=" + value)
        # A matched image/project with a foreign one-off identity still holds.
        foreign = self.run_command(
            arguments
            + ("--label", "com.docker.compose.oneoff=True", self.image, "sleep", "infinity")
        ).strip()
        self.writers = ContainerSlotWriters(self.plan, stale, self.execute, lambda _target: 0)
        self.startup = self.adapter()
        self.effects.clear()
        with self.assertRaises(ValueError):
            self.startup.recover(
                self.plan,
                self.plan.old,
                self.plan.rollback_website_settings,
                verify_guard=lambda _plan: None,
                active_jobs=lambda _slot, _writer: 0,
            )
        self.assertFalse(self.effects)
        self.run_command(("docker", "container", "rm", foreign))
        self.run_command(arguments + (self.image, "sleep", "infinity"))
        self.startup.recover(
            self.plan,
            self.plan.old,
            self.plan.rollback_website_settings,
            verify_guard=lambda _plan: None,
            active_jobs=lambda _slot, _writer: 0,
        )
        self.assert_stopped_settings(rollback=True)
