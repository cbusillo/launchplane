import inspect
from pathlib import Path
import shlex
import subprocess


def normalize_filestore_permissions(
    filestore: Path, owner: str, mountinfo: Path = Path("/proc/self/mountinfo")
) -> None:
    path = filestore.resolve(strict=True)
    overlay = False
    for line in mountinfo.read_text().splitlines():
        mount, filesystem = line.split(" - ", 1)
        if mount.split()[4] == str(path) and filesystem.split()[0] == "overlay":
            overlay = True
    if overlay:
        metadata = path.stat()
        if owner != f"{metadata.st_uid}:{metadata.st_gid}":
            raise RuntimeError("Overlay filestore ownership differs from its workflow identity.")
        return
    subprocess.run(["chown", "-R", owner, str(path)], check=True)
    subprocess.run(["chmod", "-R", "ug+rwX", str(path)], check=True)


FILESTORE_PERMISSION_PROGRAM = (
    "from pathlib import Path\nimport subprocess,sys\n"
    + inspect.getsource(normalize_filestore_permissions)
    + "\nnormalize_filestore_permissions(Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3]))\n"
)


def filestore_permission_shell(mountinfo: Path = Path("/proc/self/mountinfo")) -> str:
    command = shlex.join(["python3", "-I", "-c", FILESTORE_PERMISSION_PROGRAM])
    command += ' "$filestore_database_path" "$target_owner" ' + shlex.quote(str(mountinfo))
    return command.replace("'", "'\"'\"'")


FILESTORE_PERMISSION_SHELL = filestore_permission_shell()
