"""Freeze task-owned source for the existing locked native-run manifest."""

import argparse
import inspect
import json
from pathlib import Path
import shutil

import yaml

from control_plane.contracts.testing_candidate import testing_candidate_namespace
from control_plane.dokploy.compose import (
    held_odoo_web_container_command,
    render_odoo_raw_compose_file,
)
from control_plane.dokploy.testing_retained_copy import (
    retained_container_snapshot,
    retained_copy_host_program,
)
from control_plane.integration_readback import INTEGRATION_READBACK_PASSED_PATH
from control_plane.odoo_testing_resources import OdooTestingResources
from control_plane.retained_volume_capture import retained_volume_identity
from control_plane.retained_volume_sweep_program import RETAINED_VOLUME_SWEEP_HOST_PROGRAM
from control_plane.testing_candidate_retention_host import NATIVE_TESTING_RETENTION_INSPECTION


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--image", required=True, help="Retained immutable nested fixture image reference"
    )
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    resources = OdooTestingResources(
        product="fixture",
        context="native-fixture",
        operation_id="failed-original",
        target_id="fabricated-target",
        namespace=testing_candidate_namespace("fixture", "native-fixture", "failed-original"),
    )
    identity = {
        "context": resources.context,
        "instance": "testing",
        "artifact_id": "fabricated-artifact",
    }
    payload = {
        "resources": resources.model_dump(mode="json"),
        "volumes": resources.volumes,
        "image": args.image,
        "identity": identity,
        "failed_candidate": {
            "resources": resources.model_dump(mode="json"),
            "image": args.image,
            "web_command": held_odoo_web_container_command(),
            "web_gate_path": INTEGRATION_READBACK_PASSED_PATH,
        },
        "rendered_web_command": yaml.safe_load(
            render_odoo_raw_compose_file(
                image_reference=args.image, hold_web_until_integration_readback=True
            )
        )["services"]["web"]["command"],
        "inspection": NATIVE_TESTING_RETENTION_INSPECTION,
        "lifecycle": retained_copy_host_program(),
        "sweep": RETAINED_VOLUME_SWEEP_HOST_PROGRAM,
        "modules": {
            "control_plane.retained_volume_sweep_program": "RETAINED_VOLUME_SWEEP_HOST_PROGRAM="
            + repr(RETAINED_VOLUME_SWEEP_HOST_PROGRAM),
            "control_plane.retained_volume_capture": "from __future__ import annotations\n"
            + inspect.getsource(retained_volume_identity),
            "control_plane.dokploy": "__path__=[]\n",
            "control_plane.dokploy.testing_retained_copy": "from __future__ import annotations\n"
            + inspect.getsource(retained_container_snapshot),
        },
    }
    (args.output / "programs.json").write_text(json.dumps(payload))
    fixture = Path(__file__).resolve().parent
    for name in ("native_testing_retention_fixture.py", "native_testing_retention_engine.py"):
        shutil.copyfile(fixture / name, args.output / name)
    tests = args.output / "tests"
    tests.mkdir()
    (tests / "__init__.py").touch()
    shutil.copyfile(
        fixture.parents[1] / "test_retained_volume_sweep_program.py",
        tests / "test_retained_volume_sweep_program.py",
    )


if __name__ == "__main__":
    main()
