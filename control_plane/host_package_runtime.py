import argparse
import base64
import hashlib
import json
from pathlib import Path
import sys
import zipfile


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


def main() -> None:
    parser = argparse.ArgumentParser(prog="launchplane-host")
    parser.add_argument("action", nargs="?", default="version")
    parser.add_argument("--input", default="e30=")
    parser.add_argument("--prefix", default="")
    arguments = parser.parse_args()
    package = Path(sys.argv[0])
    version = hashlib.sha256(package.read_bytes()).hexdigest()
    print("launchplane_host_version=" + version, flush=True)
    if arguments.action == "version":
        return
    with zipfile.ZipFile(package) as archive:
        actions = json.loads(archive.read("actions.json"))
        if arguments.action not in actions:
            raise ValueError("Unknown packaged host action")
        spec = expand(json.loads(base64.b64decode(arguments.input, validate=True)), archive)
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
    main()
