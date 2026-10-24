from pathlib import Path
import subprocess
import sys
import shutil
import tarfile
import tempfile
from typing import Any


def main() -> None:
    parent = Path("/volumes/data/filestore")
    Path("/volumes/logs").mkdir(parents=True)
    root = Path("/volumes/custody")
    source = root / "source"
    source.mkdir(parents=True)
    attachment = source / "attachment"
    attachment.write_bytes(b"synthetic attachment")
    attachment.chmod(0o640)
    (source / "database.dump").write_bytes(b"synthetic dump")
    with tarfile.open(source / "filestore.tar", "w") as archive:
        archive.add(attachment, arcname="source/attachment")
    stamps: list[str] = []

    def docker(*arguments: str, stdin: bytes | None = None) -> bytes:
        command = list(arguments)
        if command[0] == "cp":
            origin, destination = command[1:]
            if destination.startswith("runner:"):
                shutil.copytree(origin, destination.removeprefix("runner:"), dirs_exist_ok=True)
            return b""
        assert command.pop(0) == "exec"
        uid = 1000
        while command[0] in {"-i", "-u"}:
            if command.pop(0) == "-u":
                uid = 0 if command.pop(0) == "root" else 1000
        container = command.pop(0)
        if container == "database":
            return b""
        assert container == "runner"
        result = subprocess.run(
            command,
            input=stdin,
            user=uid,
            group=uid,
            extra_groups=[],
            capture_output=True,
            check=False,
        )
        if result.returncode:
            raise RuntimeError(result.stderr.decode())
        return result.stdout

    namespace: dict[str, Any] = {}
    exec(sys.stdin.read(), namespace)
    namespace.update(
        {
            "docker": docker,
            "sql": lambda *arguments: "0",
            "stamp": lambda spec, containers, database: stamps.append(database),
            "tempfile": tempfile,
            "tarfile": tarfile,
            "Path": Path,
        }
    )
    containers: dict[str, tuple[str, dict[str, Any]]] = {
        "script-runner": ("runner", {}),
        "database": ("database", {}),
    }
    namespace["restore_preserved_snapshot"](
        {"record": {"rehearsal_id": "fixture"}}, root, containers, source.name, source.name
    )
    namespace["prepare_runner_storage"](containers)
    namespace["clone"]({}, containers, source.name, "destination")
    copied = parent / "destination" / attachment.name
    restored = parent / source.name / attachment.name
    assert copied.read_bytes() == attachment.read_bytes()
    assert copied.stat().st_uid == restored.stat().st_uid == 1000
    assert copied.stat().st_gid == restored.stat().st_gid == 1000
    assert copied.stat().st_mode == attachment.stat().st_mode
    docker("exec", "runner", "touch", "/volumes/data/workflow.lock", "/volumes/logs/upgrade.log")
    assert stamps == [source.name, "destination"]


if __name__ == "__main__":
    main()
