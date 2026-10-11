"""Build bounded synthetic artifacts from explicit, already-local Odoo inputs."""

import ast
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
from typing import Any

from control_plane.contracts.artifact_identity import ArtifactIdentityManifest
from control_plane.contracts.artifact_release_compatibility import (
    ReleaseInputFile,
    ReleaseModuleDeclaration,
)
from tests.test_release_compatibility import artifact
from control_plane.release_compatibility import release_examined_inputs_sha256


def docker(*arguments: str, program: str | None = None, timeout: int = 180) -> str:
    result = subprocess.run(
        ["docker", *arguments], input=program, text=True, capture_output=True, timeout=timeout
    )
    if result.returncode:
        # Inputs are synthetic and contain no real keys; preserve fixture
        # diagnostics without ever querying unrelated host/container logs.
        raise RuntimeError(result.stderr[-6000:] + result.stdout[-6000:])
    return result.stdout.strip()


def build_fixture_images(
    root: Path, *, base_image: str, inputs: Path, prefix: str
) -> tuple[ArtifactIdentityManifest, ...]:
    template = Path(__file__).parent / "cutover_probe"
    graph_text = docker(
        "run",
        "--rm",
        "--pull=never",
        "--network",
        "none",
        "-v",
        str(inputs / "docker/scripts") + ":/fixture/scripts:ro",
        "-v",
        str(template) + ":/fixture/addons/cutover_probe:ro",
        "-v",
        str(inputs / "launchplane/addons") + ":/fixture/health:ro",
        "-v",
        str(inputs / "launchplane_settings") + ":/fixture/shared/launchplane_settings:ro",
        "--entrypoint",
        "/venv/bin/python",
        base_image,
        "-c",
        "import sys,json; from pathlib import Path; sys.path.insert(0,'/fixture/scripts'); "
        "from odoo_release_inventory import module_graph; "
        "g=module_graph([Path('/fixture/health'),Path('/fixture/shared'),Path('/opt/extra_addons'),Path('/odoo/addons'),Path('/odoo/odoo/addons')]); "
        "print(json.dumps({k:sorted(v) for k,v in g.items()}))",
    )
    graph: dict[str, list[str]] = json.loads(graph_text)
    graph["cutover_probe"] = ["website"]
    modules = tuple(
        ReleaseModuleDeclaration(name=name, depends=tuple(depends))
        for name, depends in sorted(graph.items())
    )
    architecture = docker("image", "inspect", "--format", "{{.Architecture}}", base_image)
    platform = {"arm64": "aarch64-unknown-linux-gnu", "amd64": "x86_64-unknown-linux-gnu"}[
        architecture
    ]
    requirements = root / "runtime-requirements.txt"
    subprocess.run(
        [
            "uv",
            "export",
            "--quiet",
            "--frozen",
            "--no-dev",
            "--no-emit-project",
            "--project",
            str(inputs / "docker/runtime-python"),
            "-o",
            str(requirements),
        ],
        check=True,
    )
    libraries = root / "runtime-libraries"
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--require-hashes",
            "--python-version",
            "3.13",
            "--python-platform",
            platform,
            "--target",
            str(libraries),
            "-r",
            str(requirements),
        ],
        check=True,
        capture_output=True,
    )
    manifests = []
    for run_id, name in enumerate(("old", "static", "database"), 1):
        build = root / name
        build.mkdir()
        addon = build / "addon" / "cutover_probe"
        shutil.copytree(template, addon)
        shutil.copytree(inputs / "launchplane_settings", build / "addon/launchplane_settings")
        for path in addon.glob("*.py.in"):
            path.rename(path.with_suffix(""))
        if name != "old":
            (addon / "static/site.css").write_text("body {font-family:sans-serif;color:#146}")
        if name == "database":
            path = addon / "probe.xml"
            path.write_text(path.read_text().replace(">one<", ">two<"))
        files = []
        for path in sorted(addon.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(addon).as_posix()
            kind: Any = "static" if relative.startswith("static/") else "code"
            semantic = ""
            if relative == "__manifest__.py":
                kind = "manifest_assets"
                metadata = ast.literal_eval(path.read_text())
                semantic = hashlib.sha256(json.dumps(metadata, sort_keys=True).encode()).hexdigest()
            elif path.suffix == ".xml":
                kind = "database_data"
            files.append(
                ReleaseInputFile(
                    path="addons/cutover_probe/" + relative,
                    sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                    kind=kind,
                    module="cutover_probe",
                    manifest_database_sha256=semantic,
                )
            )
        manifest = artifact(
            name,
            files=tuple(files),
            modules=modules,
            install_modules=("cutover_probe", "launchplane_runtime_health", "launchplane_settings"),
        )
        assert manifest.source_build is not None
        manifest.source_build.run_id = run_id
        assert manifest.release_compatibility is not None
        if name == "database":
            manifest.release_compatibility = manifest.release_compatibility.model_copy(
                update={
                    "read_write_compatible": False,
                    "examined_inputs_sha256": release_examined_inputs_sha256(manifest),
                    "database_update_modules": ("cutover_probe",),
                }
            )
        image_name = prefix
        manifest.image.repository = image_name
        (build / "declaration.json").write_text(manifest.release_compatibility.model_dump_json())
        shutil.copytree(inputs / "docker/scripts", build / "scripts")
        shutil.copytree(libraries, build / "runtime-libraries")
        shutil.copytree(inputs / "launchplane/addons", build / "health")
        shutil.copy(inputs / "scripts/launchplane-readiness.py", build / "readiness.py")
        (build / "Dockerfile").write_text(
            f"FROM {base_image}\nUSER root\nCOPY scripts /volumes/scripts\n"
            "COPY runtime-libraries /venv/lib/python3.13/site-packages\n"
            "COPY health /opt/launchplane/addons\nCOPY addon /opt/fixture-addons\n"
            "COPY readiness.py /opt/launchplane-readiness.py\n"
            "COPY declaration.json /opt/launchplane/evidence/release-compatibility.json\n"
        )
        docker("build", "--network", "none", "-t", image_name + ":" + name, str(build))
        digests = json.loads(
            docker("image", "inspect", "--format", "{{json .RepoDigests}}", image_name + ":" + name)
        )
        manifest.image.digest = next(
            value.split("@", 1)[1] for value in digests if value.startswith(image_name + "@")
        )
        manifests.append(ArtifactIdentityManifest.model_validate(manifest.model_dump()))
    return tuple(manifests)
