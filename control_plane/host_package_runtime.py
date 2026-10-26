import argparse
import base64
import hashlib
import json
import io
import re
from pathlib import Path
import sys
import zipfile

HOST_PACKAGE_VERSION_MARKER = "launchplane_host_version"


def program_names(value: object) -> set[str]:
    if isinstance(value, dict):
        if set(value) == {"launchplane_host_program"}:
            return {str(value["launchplane_host_program"])}
        return set().union(*(program_names(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(program_names(item) for item in value))
    return set()


def program_version(action: str, spec: object, programs: dict[str, str]) -> str:
    sources = {name: programs[name] for name in program_names(spec) | {action}}
    return hashlib.sha256(json.dumps(sources, sort_keys=True).encode()).hexdigest()


def expand(value: object, archive: zipfile.ZipFile) -> object:
    if isinstance(value, dict):
        if set(value) == {"launchplane_host_program"}:
            name = value["launchplane_host_program"]
            if not isinstance(name, str) or not name.isidentifier():
                raise ValueError("Invalid packaged program reference")
            return archive.read("programs/" + name + ".py").decode()
        return {key: expand(item, archive) for key, item in value.items()}
    if isinstance(value, list):
        return [expand(item, archive) for item in value]
    return value


def main(package_content: bytes | None = None) -> None:
    parser = argparse.ArgumentParser(prog="launchplane-host")
    parser.add_argument("action", nargs="?", default="version")
    parser.add_argument("--input", default="-")
    parser.add_argument("--program-version", default="")
    parser.add_argument("--prefix", default="")
    arguments = parser.parse_args()
    package = Path(sys.argv[0])
    content = package.read_bytes() if package_content is None else package_content
    version = hashlib.sha256(content).hexdigest()
    if re.fullmatch(r"[a-f0-9]{64}", package.stem) and package.stem != version:
        raise ValueError("Installed host package version differs")
    print(HOST_PACKAGE_VERSION_MARKER + "=" + version, flush=True)
    if arguments.action == "version":
        return
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        actions = json.loads(archive.read("actions.json"))
        if arguments.action not in actions:
            raise ValueError("Unknown packaged host action")
        raw = (
            json.load(sys.stdin)
            if arguments.input == "-"
            else json.loads(base64.b64decode(arguments.input, validate=True))
        )
        programs = {
            name: archive.read("programs/" + name + ".py").decode()
            for name in program_names(raw) | {actions[arguments.action]}
        }
        if (
            arguments.program_version
            and program_version(arguments.action, raw, programs) != arguments.program_version
        ):
            raise ValueError("Packaged host action version differs")
        spec = expand(raw, archive)
        program = archive.read("programs/" + actions[arguments.action] + ".py").decode()
    sys.argv = [arguments.action, base64.b64encode(json.dumps(spec).encode()).decode()]
    if arguments.prefix:
        sys.argv.append(arguments.prefix)
    exec(
        compile(
            program,
            "<rehearsal>"
            if arguments.action == "rehearsal"
            else "<launchplane-host:" + arguments.action + ">",
            "exec",
        ),
        {"spec": spec},
    )


if __name__ == "__main__":
    main(globals().get("HOST_PACKAGE_CONTENT"))
