import argparse
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import select
import shutil
import subprocess
import sys
import time
from uuid import uuid4
from typing import Any, cast


def run(*arguments: str, timeout: float = 30, input: str | None = None) -> str:
    result = subprocess.run(arguments, input=input, text=True, capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"{arguments[0]} {arguments[1]} failed with exit {result.returncode}.")
    return result.stdout


def inspect_container(identifier: str) -> dict[str, Any]:
    values = json.loads(run("docker", "inspect", identifier))
    if len(values) != 1:
        raise RuntimeError("Container identity is incomplete.")
    return cast(dict[str, Any], values[0])


def save(path: Path, state: dict[str, Any]) -> None:
    temporary = path.with_suffix(".new")
    temporary.write_text(json.dumps(state, sort_keys=True))
    temporary.chmod(0o600)
    temporary.replace(path)


def operator_command(state: dict[str, Any], mode: str) -> list[str]:
    return [
        "docker",
        "compose",
        "-p",
        state["project"],
        "--project-directory",
        state["directory"],
        "-f",
        state["compose"],
        "-f",
        state["overlay"],
        "run",
        "--rm",
        "--no-deps",
        "-T",
        "--pull",
        "never",
        "--name",
        state["operator"],
        "--entrypoint",
        "python",
        "launchplane",
        "-m",
        "control_plane.database_maintenance_cli",
        mode,
        "--expires-at",
        state["expires_at"],
        "--hold-id",
        state["hold_id"],
    ]


def operator(state: dict[str, Any], mode: str, timeout: float = 90) -> dict[str, Any]:
    output = run(*operator_command(state, mode), timeout=timeout)
    return cast(dict[str, Any], json.loads(output.strip().splitlines()[-1]))


def check_originals(state: dict[str, Any], *, stopped: bool = False) -> None:
    for identifier in state["containers"]:
        observed = inspect_container(identifier)
        if observed["Config"]["Labels"].get("com.docker.compose.project") != state["project"]:
            raise RuntimeError("Original container project identity changed.")
        if stopped and observed["State"]["Running"]:
            raise RuntimeError("A stopped service restarted during the hold.")
    known = set(state["containers"]) | {state["browser"], state["operator"]}
    current = run(
        "docker",
        "ps",
        "-aq",
        "--no-trunc",
        "--filter",
        "label=com.docker.compose.project=" + state["project"],
    ).split()
    for identifier in current:
        if (
            identifier not in known
            and inspect_container(identifier)["Name"].lstrip("/") != state["operator"]
        ):
            raise RuntimeError("An unexpected service or deployment appeared.")


def release(state_path: Path) -> None:
    with state_path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = json.loads(state_path.read_text())
        if state.get("released"):
            return
        check_originals(state)
        if state.get("keeper"):
            keeper = state["keeper"]
            started = keeper["backend_start"].replace("'", "''")
            pid = int(keeper["pid"])
            database = state["database"].replace("'", "''")
            sql = f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE pid={pid} AND backend_start='{started}'::timestamptz AND datname='{database}';"
            run(
                "docker",
                "exec",
                "-i",
                state["postgres"],
                "sh",
                "-c",
                'exec psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"',
                input=sql + "\n",
            )
        operator_rows = run(
            "docker", "ps", "-aq", "--filter", "name=^/" + state["operator"] + "$"
        ).split()
        if operator_rows:
            item = inspect_container(operator_rows[0])
            if item["Config"]["Labels"].get("com.docker.compose.oneoff", "").lower() != "true":
                raise RuntimeError("The keeper name belongs to another runtime.")
            run("docker", "stop", "--timeout", "0", item["Id"])
            until = time.monotonic() + 15
            while run(
                "docker", "ps", "-aq", "--filter", "name=^/" + state["operator"] + "$"
            ).strip():
                if time.monotonic() >= until:
                    raise RuntimeError("The one-off keeper has not exited.")
                time.sleep(0.2)
        if state.get("stopping_started"):
            run("docker", "stop", "--timeout", "-1", *state["containers"], timeout=3600)
            run("docker", "start", *state["containers"], timeout=60)
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                observations = [inspect_container(identifier) for identifier in state["containers"]]
                if all(
                    item["State"]["Running"]
                    and item["State"].get("Health", {}).get("Status", "healthy") == "healthy"
                    for item in observations
                ):
                    time.sleep(2)
                    if all(
                        inspect_container(identifier)["State"]["Running"]
                        for identifier in state["containers"]
                    ):
                        break
                time.sleep(1)
            else:
                raise RuntimeError("Original services did not recover with healthy API readback.")
        if state.get("fenced"):
            operator(state, "unfence")
        state["released"] = True
        save(state_path, state)
        print(
            json.dumps(
                {
                    "event": "released",
                    "hold_id": state["hold_id"],
                    "original_services_running": bool(state.get("stopping_started")),
                }
            ),
            flush=True,
        )


def prepare(arguments: argparse.Namespace) -> tuple[Path, dict[str, Any]]:
    api = inspect_container(arguments.api_container)
    labels = api["Config"]["Labels"]
    if labels.get("com.docker.compose.service") != "launchplane" or not api["State"]["Running"]:
        raise ValueError("A running Launchplane API container is required.")
    root = Path(arguments.state_root)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink() or root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077:
        raise ValueError("Maintenance state requires a private owned directory.")
    hold_id = uuid4().hex
    directory = root / hold_id
    directory.mkdir(mode=0o700)
    state_path = directory / "state.json"
    environment = dict(item.split("=", 1) for item in api["Config"]["Env"] if "=" in item)
    state: dict[str, Any] = {
        "hold_id": hold_id,
        "project": labels["com.docker.compose.project"],
        "directory": labels["com.docker.compose.project.working_dir"],
        "compose": labels["com.docker.compose.project.config_files"],
        "api": api["Id"],
        "operator": "lp-maint-" + hold_id,
        "postgres": inspect_container(arguments.postgres_container)["Id"],
        "expires_at": (
            datetime.now(timezone.utc) + timedelta(seconds=arguments.duration_seconds)
        ).isoformat(),
        "overlay": str(directory / "operator.json"),
        "containers": [],
        "browser": "",
    }
    if "," in state["compose"] or not Path(state["compose"]).is_file():
        raise ValueError("Exactly one existing provider compose file is required.")
    for identifier in run(
        "docker",
        "ps",
        "-aq",
        "--no-trunc",
        "--filter",
        "label=com.docker.compose.project=" + state["project"],
    ).split():
        item = inspect_container(identifier)
        if (
            item["Config"]["Labels"].get("com.docker.compose.oneoff", "").lower() == "true"
            or not item["State"]["Running"]
        ):
            raise ValueError("A one-off or stopped service needs disposition before maintenance.")
        if item["Config"]["Labels"].get("com.docker.compose.service") == "launchplane-browser":
            if any(
                value.startswith("LAUNCHPLANE_DATABASE_URL=") for value in item["Config"]["Env"]
            ):
                raise ValueError("The browser unexpectedly holds database access.")
            state["browser"] = item["Id"]
        elif item["Image"] != api["Image"]:
            raise ValueError("Own services are not all on the same image.")
        else:
            state["containers"].append(item["Id"])
    state["containers"].sort(key=lambda identifier: identifier != state["api"])
    overlay = {
        "services": {
            "launchplane": {
                "image": api["Image"],
                "environment": {"DOCKER_IMAGE_REFERENCE": environment["DOCKER_IMAGE_REFERENCE"]},
                "labels": {"traefik.enable": "false"},
            }
        }
    }
    Path(state["overlay"]).write_text(json.dumps(overlay))
    shutil.copyfile(__file__, directory / "driver.py")
    state["commands"] = str(directory / "commands.fifo")
    os.mkfifo(state["commands"], 0o600)
    state["environment_sha256"] = hashlib.sha256(
        (Path(state["directory"]) / ".env").read_bytes()
    ).hexdigest()
    save(state_path, state)
    plan = operator(state, "plan")
    state.update({"database": plan["database"], "system_identifier": plan["system_identifier"]})
    sql = "SELECT system_identifier FROM pg_control_system();\n"
    observed = run(
        "docker",
        "exec",
        "-i",
        state["postgres"],
        "sh",
        "-c",
        'exec psql -X -At -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"',
        input=sql,
    ).strip()
    if observed != state["system_identifier"]:
        raise ValueError("The PostgreSQL container does not match the Launchplane connection.")
    save(state_path, state)
    return state_path, state


def write_all(descriptor: int, data: bytes) -> None:
    remaining = memoryview(data)
    while remaining:
        written = os.write(descriptor, remaining)
        if not written:
            raise BrokenPipeError("The keeper command pipe closed.")
        remaining = remaining[written:]


def hold(state_path: Path, state: dict[str, Any], seconds: int) -> None:
    unit = "lp-maint-" + state["hold_id"]
    seconds = max(
        1,
        int(
            (
                datetime.fromisoformat(state["expires_at"]) - datetime.now(timezone.utc)
            ).total_seconds()
        ),
    )
    run(
        "systemd-run",
        "--unit=" + unit,
        "--on-active=" + str(seconds) + "s",
        "--timer-property=AccuracySec=1s",
        sys.executable,
        str(state_path.parent / "driver.py"),
        "release",
        "--state",
        str(state_path),
    )
    timer = run(
        "systemctl",
        "show",
        unit + ".timer",
        "--property=ActiveState",
        "--property=NextElapseUSecMonotonic",
    )
    service = run("systemctl", "show", unit + ".service", "--property=ExecStart")
    if "ActiveState=active" not in timer or str(state_path) not in service:
        raise RuntimeError("The independent restart timer was not read back.")
    keeper = None
    commands = None
    buffered = b""
    try:
        state["fenced"] = True
        save(state_path, state)
        operator(state, "fence")
        if (
            hashlib.sha256((Path(state["directory"]) / ".env").read_bytes()).hexdigest()
            != state["environment_sha256"]
        ):
            raise RuntimeError("Provider environment mutation rewrote the host credential source.")
        check_originals(state)
        state["stopping_started"] = True
        save(state_path, state)
        run("docker", "stop", "--timeout", "-1", state["api"], timeout=seconds)
        deadline = time.monotonic() + min(300, seconds / 3)
        while True:
            census = operator(state, "census")
            if not census["blockers"]:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError("Running work did not drain; restart without backup.")
            time.sleep(2)
        run("docker", "stop", "--timeout", "-1", *state["containers"][1:], timeout=seconds)
        check_originals(state, stopped=True)
        keeper = subprocess.Popen(
            operator_command(state, "hold"),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            bufsize=0,
        )
        assert keeper.stdout is not None and keeper.stdin is not None
        commands = os.open(state["commands"], os.O_RDWR | os.O_NONBLOCK)
        held = False
        while True:
            check_originals(state, stopped=True)
            remaining = (
                datetime.fromisoformat(state["expires_at"]) - datetime.now(timezone.utc)
            ).total_seconds()
            if remaining <= 0:
                raise TimeoutError("The bounded maintenance window ended.")
            streams: list[Any] = [keeper.stdout, sys.stdin]
            if held:
                streams.append(commands)
            ready, _, _ = select.select(streams, [], [], min(1, remaining))
            for stream in ready:
                if stream is sys.stdin:
                    line = sys.stdin.readline()
                    if not line:
                        raise EOFError("Maintenance input closed.")
                    write_all(keeper.stdin.fileno(), line.encode())
                elif stream == commands:
                    write_all(keeper.stdin.fileno(), os.read(commands, 65536))
                else:
                    chunk = os.read(keeper.stdout.fileno(), 65536)
                    if not chunk:
                        if keeper.wait(timeout=5):
                            raise RuntimeError("The database keeper failed.")
                        return
                    buffered += chunk
                    while b"\n" in buffered:
                        line, buffered = buffered.split(b"\n", 1)
                        event = json.loads(line)
                        if event["event"] == "keeper":
                            state["keeper"] = event
                            save(state_path, state)
                        if event["event"] == "held":
                            state["held"] = event
                            save(state_path, state)
                        held = held or event["event"] == "held"
                        print(line.decode(), flush=True)
    finally:
        if commands is not None:
            os.close(commands)
        if keeper and keeper.stdin:
            keeper.stdin.close()
        try:
            release(state_path)
        finally:
            if json.loads(state_path.read_text()).get("released"):
                run("systemctl", "stop", unit + ".timer")
                observed = run("systemctl", "show", unit + ".timer", "--property=ActiveState")
                if "ActiveState=active" in observed:
                    raise RuntimeError("The restart timer remains active after release.")


def dump(state_path: Path) -> None:
    state = json.loads(state_path.read_text())
    if state.get("released") or not state.get("held"):
        raise RuntimeError("The held snapshot is not available.")
    check_originals(state, stopped=True)
    snapshot = state["held"]["snapshot"]
    if not all(character.isalnum() or character == "-" for character in snapshot):
        raise ValueError("The exported snapshot identifier is invalid.")
    database = state["database"]
    if not all(character.isalnum() or character == "_" for character in database):
        raise ValueError("The database identifier is not qualified for this dump path.")
    process = subprocess.Popen(
        [
            "docker",
            "exec",
            state["postgres"],
            "sh",
            "-c",
            'exec pg_dump -U "$POSTGRES_USER" --format=custom --no-owner --dbname="$1" --snapshot="$2"',
            "dump",
            database,
            snapshot,
        ],
        stdout=subprocess.PIPE,
    )
    assert process.stdout is not None
    digest = hashlib.sha256()
    try:
        while True:
            data = process.stdout.read(1024 * 1024)
            if not data:
                break
            digest.update(data)
            sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
        if process.wait(timeout=30):
            raise RuntimeError("The staged database dump failed.")
        check_originals(state, stopped=True)
        print(
            json.dumps(
                {
                    "event": "dump_complete",
                    "hold_id": state["hold_id"],
                    "snapshot": snapshot,
                    "sha256": digest.hexdigest(),
                }
            ),
            file=sys.stderr,
            flush=True,
        )
    finally:
        process.stdout.close()


def submit(state_path: Path, receipt_path: Path) -> None:
    state = json.loads(state_path.read_text())
    receipt = json.loads(receipt_path.read_text())
    if state.get("released") or not state.get("held"):
        raise RuntimeError("No held maintenance snapshot accepts a receipt.")
    if any(
        receipt.get(key) != state["held"].get(key) for key in ("hold_id", "snapshot", "transaction")
    ):
        raise ValueError("The receipt belongs to another held snapshot.")
    descriptor = os.open(state["commands"], os.O_WRONLY | os.O_NONBLOCK)
    try:
        os.set_blocking(descriptor, True)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(json.dumps(receipt, separators=(",", ":")) + "\n")
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description="Bounded stop and restart of Launchplane only.")
    parser.add_argument("mode", choices=("plan", "hold", "release", "dump", "submit"))
    parser.add_argument("--state", type=Path)
    parser.add_argument("--receipt-file", type=Path)
    parser.add_argument("--api-container")
    parser.add_argument("--postgres-container")
    parser.add_argument("--duration-seconds", type=int, default=1800)
    parser.add_argument("--state-root", default="/run/launchplane-maintenance")
    arguments = parser.parse_args()
    if arguments.mode in {"dump", "submit", "release"} and arguments.state is None:
        parser.error("This mode requires --state.")
    if arguments.mode == "submit":
        if arguments.receipt_file is None:
            parser.error("submit requires --receipt-file.")
        submit(arguments.state, arguments.receipt_file)
        return
    if arguments.mode == "dump":
        dump(arguments.state)
        return
    if arguments.mode == "release":
        release(arguments.state)
        return
    if (
        not arguments.api_container
        or not arguments.postgres_container
        or not 120 <= arguments.duration_seconds <= 3600
    ):
        parser.error("API, PostgreSQL container and a 120..3600 second bound are required.")
    state_path, state = prepare(arguments)
    print(
        json.dumps(
            {
                "event": "plan",
                "state": str(state_path),
                "containers": state["containers"],
                "expires_at": state["expires_at"],
            }
        ),
        flush=True,
    )
    if arguments.mode == "hold":
        hold(state_path, state, arguments.duration_seconds)


if __name__ == "__main__":
    main()
