import base64
from dataclasses import dataclass
from functools import cache
import hashlib
import io
import json
from pathlib import Path
import shlex
import re
import zipfile

from control_plane.host_package_runtime import program_version, HOST_PACKAGE_VERSION_MARKER

HOST_PACKAGE_CHUNK_BYTES = 24 * 1024
HOST_SCHEDULE_HEADER = "#!/usr/bin/env bash\nset -euo pipefail\n"
HOST_PACKAGE_PATH_PREFIX = "$HOME/.local/state/launchplane-host/"
HOST_INPUT_DELIMITER = "LP_HOST_INPUT"


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
    from control_plane.testing_activation import PORT_INSPECTION_PROGRAM

    programs = {
        "activation": (
            "import base64,json,sys\nactivation=json.loads(base64.b64decode(sys.argv[1]))\n"
            "sys.argv=[sys.argv[0],base64.b64encode(json.dumps(activation['resources']).encode()).decode()]\n"
            + ODOO_TESTING_RESOURCE_PROGRAM
            + "\nsys.argv=[sys.argv[0],base64.b64encode(json.dumps(activation['ports']).encode()).decode()]\n"
            + PORT_INSPECTION_PROGRAM
        ),
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
            "activation",
            "restore",
            "rehearsal",
            "resources",
            "capture",
            "chunk",
            "preflight",
            "preview_snapshot",
        )
    }
    return build_package(programs, actions, include_installer=True)


@cache
def native_package(action: str, names: tuple[str, ...]) -> HostPackage:
    full = host_package()
    return build_package(
        {name: full.programs[name] for name in names},
        {action: full.actions[action]},
        include_installer=False,
    )


def build_package(
    programs: dict[str, str], actions: dict[str, str], *, include_installer: bool
) -> HostPackage:
    from control_plane.odoo_upgrade_rehearsal import REHEARSAL_HOST_FILENAME

    members = {
        "__main__.py": Path(__file__).with_name("host_package_runtime.py").read_bytes(),
        "filenames.json": json.dumps(
            {
                name: REHEARSAL_HOST_FILENAME
                if name == "rehearsal"
                else "<launchplane-host:" + name + ">"
                for name in actions
            },
            sort_keys=True,
        ).encode(),
        "actions.json": json.dumps(actions, sort_keys=True).encode(),
        **{"programs/" + name + ".py": program.encode() for name, program in programs.items()},
    }
    if include_installer:
        members["installer.py"] = Path(__file__).with_name("host_package_installer.py").read_bytes()
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


def host_command(action: str, spec: dict[str, object]) -> str:
    package = host_package()
    if action not in package.actions:
        raise ValueError("Unknown packaged host action")
    compact = compact_host_input(spec)
    path = '"' + HOST_PACKAGE_PATH_PREFIX + package.version + '.pyz"'
    arguments = [
        action,
        "--program-version",
        program_version(action, compact, package.programs),
        "--input",
        "-",
    ]
    return "python3 " + path + " " + shlex.join(arguments)


def host_schedule(action: str, spec: dict[str, object]) -> str:
    return (
        HOST_SCHEDULE_HEADER
        + "exec "
        + host_command(action, spec)
        + " <<'"
        + HOST_INPUT_DELIMITER
        + "'\n"
        + json.dumps(compact_host_input(spec), sort_keys=True)
        + "\n"
        + HOST_INPUT_DELIMITER
        + "\n"
    )


@dataclass(frozen=True)
class HostInvocation:
    package_version: str
    action: str
    program_version: str
    spec: dict[str, object]


def host_invocation(script: object) -> HostInvocation | None:
    normalized = script.rstrip() + "\n" if isinstance(script, str) else script
    result = parse_host_invocation(normalized)
    if result is None and isinstance(script, str) and HOST_PACKAGE_PATH_PREFIX in script:
        raise ValueError("Malformed packaged host invocation")
    return result


def parse_host_invocation(script: object) -> HostInvocation | None:
    if not isinstance(script, str) or not script.startswith(HOST_SCHEDULE_HEADER):
        return None
    command, separator, tail = script[len(HOST_SCHEDULE_HEADER) :].partition(
        " <<'" + HOST_INPUT_DELIMITER + "'\n"
    )
    ending = "\n" + HOST_INPUT_DELIMITER + "\n"
    if not separator or not tail.endswith(ending):
        return None
    try:
        arguments = shlex.split(command)
        if (
            len(arguments) != 8
            or arguments[:2] != ["exec", "python3"]
            or arguments[4] != "--program-version"
            or arguments[6:] != ["--input", "-"]
        ):
            return None
        path = re.fullmatch(
            re.escape(HOST_PACKAGE_PATH_PREFIX) + r"([a-f0-9]{64})\.pyz", arguments[2]
        )
        if path is None or not re.fullmatch(r"[a-f0-9]{64}", arguments[5]):
            return None
        spec = json.loads(tail[: -len(ending)])
        if not isinstance(spec, dict):
            return None
    except (ValueError, TypeError):
        return None
    return HostInvocation(path.group(1), arguments[3], arguments[5], spec)


def host_schedules_match(observed: object, expected: str) -> bool:
    if observed == expected:
        return True
    current = host_invocation(expected)
    if current is None:
        return False
    try:
        previous = host_invocation(observed)
    except ValueError:
        return False
    if previous is not None:
        return (previous.action, previous.program_version, previous.spec) == (
            current.action,
            current.program_version,
            current.spec,
        )
    if current.action != "resources":
        return False
    encoded = base64.b64encode(json.dumps(current.spec, sort_keys=True).encode()).decode()
    legacy = (
        HOST_SCHEDULE_HEADER
        + "python3 - "
        + encoded
        + " <<'LP_TESTING_RESOURCES'\n"
        + host_package().programs["resources"]
        + "\nLP_TESTING_RESOURCES\n"
    )
    return observed == legacy


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(prog="launchplane-host-package")
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    package = host_package()
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_bytes(package.content)
    print(HOST_PACKAGE_VERSION_MARKER + "=" + package.version)


if __name__ == "__main__":
    main()
