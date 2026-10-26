import base64
from dataclasses import dataclass
from functools import cache
import hashlib
import io
import json
from pathlib import Path
import shlex
import zipfile


@dataclass(frozen=True)
class HostPackage:
    content: bytes
    version: str
    programs: dict[str, str]
    actions: dict[str, str]


@cache
def host_package() -> HostPackage:
    from control_plane.odoo_testing_restore import (
        FRESH_TESTING_RESTORE_HOST_PROGRAM,
        FRESH_TESTING_STALE_LOCK_PROGRAM,
        NATIVE_TESTING_FILES_PROGRAM,
        TESTING_COPY_RUNNER,
        rehearsal_copy_runner,
    )
    from control_plane.odoo_upgrade_rehearsal import rehearsal_host_program
    from control_plane.odoo_testing_resources import ODOO_TESTING_RESOURCE_PROGRAM
    from control_plane.dokploy.native_copy_stream import (
        NATIVE_ODOO_SOURCE_READ_PROGRAM,
        NATIVE_TESTING_CHUNK_WRITE_PROGRAM,
    )
    from control_plane._odoo_rehearsal_preflight_program import REHEARSAL_PREFLIGHT_PROGRAM
    from control_plane.preview_clean_snapshot import PREVIEW_CLEAN_SNAPSHOT_PROGRAM
    from control_plane.integration_readback import INTEGRATION_READBACK_PROGRAM
    from control_plane._odoo_website_settings_program import WEBSITE_SETTINGS_PROGRAM

    programs = {
        "restore": FRESH_TESTING_RESTORE_HOST_PROGRAM,
        "rehearsal": rehearsal_host_program(),
        "resources": ODOO_TESTING_RESOURCE_PROGRAM,
        "capture": NATIVE_ODOO_SOURCE_READ_PROGRAM,
        "chunk": NATIVE_TESTING_CHUNK_WRITE_PROGRAM,
        "preflight": REHEARSAL_PREFLIGHT_PROGRAM,
        "preview_snapshot": PREVIEW_CLEAN_SNAPSHOT_PROGRAM,
        "stale_lock": FRESH_TESTING_STALE_LOCK_PROGRAM,
        "native_files": NATIVE_TESTING_FILES_PROGRAM,
        "copy_runner": TESTING_COPY_RUNNER,
        "preview_runner": "expected_copy_instance = 'preview'\n" + TESTING_COPY_RUNNER,
        "sanitize_runner": rehearsal_copy_runner(sanitize_source_only=True),
        "upgrade_runner": rehearsal_copy_runner(sanitize_source_only=False),
        "readback": INTEGRATION_READBACK_PROGRAM,
        "website_settings": WEBSITE_SETTINGS_PROGRAM,
    }
    actions = {
        name: name
        for name in (
            "restore",
            "rehearsal",
            "resources",
            "capture",
            "chunk",
            "preflight",
            "preview_snapshot",
        )
    }
    members = {
        "__main__.py": Path(__file__).with_name("host_package_runtime.py").read_bytes(),
        "installer.py": Path(__file__).with_name("host_package_installer.py").read_bytes(),
        "actions.json": json.dumps(actions, sort_keys=True).encode(),
        **{"programs/" + name + ".py": program.encode() for name, program in programs.items()},
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in sorted(members.items()):
            member = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            member.compress_type = zipfile.ZIP_DEFLATED
            member.external_attr = 0o600 << 16
            archive.writestr(member, content)
    content = output.getvalue()
    return HostPackage(content, hashlib.sha256(content).hexdigest(), programs, actions)


def compact_host_input(value: object) -> object:
    if isinstance(value, str):
        for name, program in host_package().programs.items():
            if value == program:
                return {"launchplane_host_program": name}
        return value
    if isinstance(value, dict):
        if "launchplane_host_program" in value:
            raise ValueError("Packaged program references are reserved")
        return {key: compact_host_input(item) for key, item in value.items()}
    if isinstance(value, list):
        return [compact_host_input(item) for item in value]
    return value


def host_command(action: str, spec: dict[str, object], *, prefix: str = "") -> str:
    package = host_package()
    if action not in package.actions:
        raise ValueError("Unknown packaged host action")
    encoded = base64.b64encode(
        json.dumps(compact_host_input(spec), sort_keys=True).encode()
    ).decode()
    path = '"$HOME/.local/state/launchplane-host/' + package.version + '.pyz"'
    arguments = [action, "--input", encoded]
    if prefix:
        arguments.extend(["--prefix", prefix])
    return "python3 " + path + " " + shlex.join(arguments)


def host_schedule(action: str, spec: dict[str, object]) -> str:
    return "#!/usr/bin/env bash\nset -euo pipefail\nexec " + host_command(action, spec) + "\n"


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(prog="launchplane-host-package")
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    package = host_package()
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_bytes(package.content)
    print("launchplane_host_version=" + package.version)


if __name__ == "__main__":
    main()
