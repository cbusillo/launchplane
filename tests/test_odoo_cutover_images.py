"""Local fixture image selection must bind one concrete published platform."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import subprocess
from typing import Any
import unittest
from unittest.mock import patch

from tests.support import odoo_cutover_images as images


class FixtureBaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.reference = "example.invalid/odoo@sha256:" + "a" * 64
        self.concrete: dict[str, Any] = {
            "Id": "sha256:" + "b" * 64,
            "RepoDigests": [self.reference],
            "Os": "linux",
            "Architecture": "amd64",
            "RootFS": {"Layers": ["base-layer"]},
        }
        self.index: dict[str, Any] = {"Id": "sha256:" + "a" * 64, "RepoDigests": [self.reference]}

    def test_old_concrete_image_needs_no_new_inspect_flag(self) -> None:
        with patch.object(images, "docker", return_value=json.dumps([self.concrete])) as read:
            parent, selected, arguments = images.fixture_base(self.reference, None)
        self.assertEqual(parent, self.concrete)
        self.assertEqual(selected, self.concrete)
        self.assertEqual(arguments, ())
        read.assert_called_once_with("image", "inspect", self.reference)

    def test_index_requires_explicit_platform_before_any_runtime_action(self) -> None:
        with patch.object(images, "docker", return_value=json.dumps([self.index])) as read:
            with self.assertRaisesRegex(ValueError, "target_platform"):
                images.fixture_base(self.reference, None)
        read.assert_called_once_with("image", "inspect", self.reference)

    def test_explicit_platform_resolves_metadata_through_same_parent(self) -> None:
        with patch.object(
            images, "docker", side_effect=[json.dumps([self.index]), json.dumps([self.concrete])]
        ) as read:
            parent, selected, arguments = images.fixture_base(self.reference, "linux/amd64")
        self.assertEqual(parent, self.index)
        self.assertEqual(selected, self.concrete)
        self.assertEqual(arguments, ("--platform", "linux/amd64"))
        self.assertEqual(read.call_count, 2)
        read.assert_called_with("image", "inspect", "--platform", "linux/amd64", self.reference)

    def test_unavailable_or_unsupported_cli_selection_is_not_retried(self) -> None:
        with patch.object(
            images,
            "docker",
            side_effect=[json.dumps([self.index]), RuntimeError("unsupported flag")],
        ) as read:
            with self.assertRaisesRegex(RuntimeError, "unsupported flag"):
                images.fixture_base(self.reference, "linux/amd64")
        self.assertEqual(read.call_count, 2)

    def test_mismatched_platform_parent_or_missing_layers_refuses(self) -> None:
        updates: tuple[dict[str, Any], ...] = (
            {"Architecture": "arm64"},
            {"Os": "windows"},
            {"RepoDigests": ["other@sha256:" + "c" * 64]},
            {"RootFS": {}},
        )
        for update in updates:
            with (
                self.subTest(update=update),
                patch.object(
                    images,
                    "docker",
                    side_effect=[
                        json.dumps([self.index]),
                        json.dumps([{**self.concrete, **update}]),
                    ],
                ) as read,
            ):
                with self.assertRaises(ValueError):
                    images.fixture_base(self.reference, "linux/amd64")
                self.assertEqual(read.call_count, 2)

    def test_invalid_selector_or_mutable_parent_refuses_without_docker(self) -> None:
        for reference, platform in (
            (self.reference, "linux/ppc64le"),
            ("odoo:latest", "linux/amd64"),
        ):
            with (
                self.subTest(reference=reference, platform=platform),
                patch.object(images, "docker") as read,
            ):
                with self.assertRaises(ValueError):
                    images.fixture_base(reference, platform)
                read.assert_not_called()

    def test_selected_child_drives_probes_wheels_builds_and_layer_check(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs = root / "inputs"
            for directory in ("docker/scripts", "docker/runtime-python", "launchplane/addons"):
                (inputs / directory).mkdir(parents=True)
            calls: list[tuple[str, ...]] = []
            wheels: list[str] = []

            def run(arguments: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                if "--target" in arguments:
                    Path(arguments[arguments.index("--target") + 1]).mkdir()
                    wheels.append(arguments[arguments.index("--python-platform") + 1])
                return subprocess.CompletedProcess(arguments, 0, "", "")

            def docker(*arguments: str, **kwargs: object) -> str:
                calls.append(arguments)
                if arguments[:2] == ("image", "inspect"):
                    if "--format" in arguments:
                        return json.dumps(["fixture@sha256:" + "d" * 64])
                    if arguments[-1] == self.reference:
                        return json.dumps(
                            [self.concrete if "--platform" in arguments else self.index]
                        )
                    return json.dumps(
                        [{**self.concrete, "RootFS": {"Layers": ["base-layer", "fixture-layer"]}}]
                    )
                if arguments[0] == "run":
                    return (
                        json.dumps(
                            {
                                "base": [],
                                "web": ["base"],
                                "website": ["web"],
                                "launchplane_runtime_health": ["base"],
                            }
                        )
                        if "module_graph" in arguments[-1]
                        else "20"
                    )
                return ""

            with (
                patch.object(images, "docker", side_effect=docker),
                patch("tests.support.odoo_cutover_images.subprocess.run", side_effect=run),
            ):
                result = images.build_fixture_images(
                    root,
                    base_image=self.reference,
                    inputs=inputs,
                    prefix="fixture",
                    include_settings=False,
                    target_platform="linux/amd64",
                )
            self.assertEqual(len(result), 3)
            self.assertEqual(wheels, [images.UV_PLATFORMS["amd64"]])
            for call in calls:
                if call[0] in {"run", "build"} or (
                    call[:2] == ("image", "inspect") and call[-1] != self.reference
                ):
                    self.assertEqual(call[call.index("--platform") + 1], "linux/amd64")
            self.assertIn(
                (
                    "tag",
                    self.index["Id"],
                    "launchplane-cutover-base:" + self.index["Id"].removeprefix("sha256:"),
                ),
                calls,
            )
