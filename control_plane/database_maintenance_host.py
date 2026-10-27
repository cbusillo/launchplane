import argparse
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import select
import shutil
import signal
import subprocess
import sys
import time
from threading import Lock
from typing import Any, cast
from uuid import uuid4


def run(*arguments: str, timeout: float | None = 30, input_text: str | None = None) -> str:
    result = subprocess.run(
        arguments, input=input_text, text=True, capture_output=True, timeout=timeout
    )
    if result.returncode:
        raise RuntimeError(f"{arguments[0]} {arguments[1]} failed with exit {result.returncode}.")
    return result.stdout


def inspect_container(identifier: str) -> dict[str, Any]:
    values = json.loads(run("docker", "inspect", identifier))
    if len(values) != 1:
        raise RuntimeError("Container identity is incomplete.")
    return cast(dict[str, Any], values[0])


def save(path: Path, state: dict[str, Any]) -> None:
    temporary = path.with_suffix("." + uuid4().hex)
    with temporary.open("x") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(json.dumps(state, sort_keys=True))
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def remaining(state: dict[str, Any]) -> float:
    return (
        datetime.fromisoformat(state["expires_at"]) - datetime.now(timezone.utc)
    ).total_seconds()


@contextmanager
def locked_state(path: Path, *, active: bool = False) -> Iterator[dict[str, Any]]:
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = json.loads(path.read_text())
        original = json.dumps(state, sort_keys=True)
        if active and (state.get("releasing") or state.get("released") or remaining(state) <= 0):
            raise RuntimeError("The maintenance window is already ending or released.")
        try:
            yield state
        finally:
            if json.dumps(state, sort_keys=True) != original:
                save(path, state)


def update_state(path: Path, **changes: Any) -> dict[str, Any]:
    with locked_state(path, active=True) as state:
        state.update(changes)
        return dict(state)


def operator_command(state: dict[str, Any], mode: str) -> list[str]:
    observe_only = mode == "observe-unfence"
    mode = "unfence" if observe_only else mode
    command = [
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
        state["operator"] + ("-release" if mode == "unfence" else ""),
        "--volume",
        state["job_directory"] + ":/maintenance",
        "--entrypoint",
        "python",
        "launchplane-maintenance",
        "-m",
        "control_plane.database_maintenance_cli",
        mode,
        "--expires-at",
        state["expires_at"],
        "--hold-id",
        state["hold_id"],
        "--target-id",
        state.get("target_id", ""),
    ]
    return command + (["--readback-only"] if observe_only else [])


def operator(state: dict[str, Any], mode: str) -> dict[str, Any]:
    output = run(*operator_command(state, mode), timeout=90)
    return cast(dict[str, Any], json.loads(output.strip().splitlines()[-1]))


def require_no_outside_writers(
    api: dict[str, Any], allowed: set[str], *, project: str, operators: set[str]
) -> None:
    source = api["Config"]["Labels"].get("org.opencontainers.image.source", "")
    database = dict(value.split("=", 1) for value in api["Config"]["Env"] if "=" in value).get(
        "LAUNCHPLANE_DATABASE_URL", ""
    )
    if not source or not database:
        raise RuntimeError("The API source/database identity is incomplete.")
    for identifier in run(
        "docker",
        "ps",
        "-aq",
        "--no-trunc",
        "--filter",
        "label=org.opencontainers.image.source=" + source,
    ).split():
        if identifier in allowed:
            continue
        other = inspect_container(identifier)
        labels = other["Config"]["Labels"]
        own_operator = (
            other["Name"].lstrip("/") in operators
            and labels.get("com.docker.compose.project") == project
            and labels.get("com.docker.compose.oneoff", "").lower() == "true"
            and other["Image"] == api["Image"]
        )
        if (
            not own_operator
            and dict(value.split("=", 1) for value in other["Config"]["Env"] if "=" in value).get(
                "LAUNCHPLANE_DATABASE_URL"
            )
            == database
        ):
            raise RuntimeError(
                "Another Launchplane container holds this database credential; its custody is unresolved."
            )


def original_containers(state: dict[str, Any]) -> list[dict[str, Any]]:
    observations = []
    for identifier in state["containers"]:
        observed = inspect_container(identifier)
        if observed["Config"]["Labels"].get("com.docker.compose.project") != state["project"]:
            raise RuntimeError("Original container project identity changed.")
        observations.append(observed)
    return observations


def check_originals(state: dict[str, Any], *, stopped: bool = False) -> None:
    observations = original_containers(state)
    if stopped and any(observed["State"]["Running"] for observed in observations):
        raise RuntimeError("A stopped service restarted during the hold.")
    known = set(state["containers"]) | {state["browser"]}
    require_no_outside_writers(
        inspect_container(state["api"]),
        known,
        project=state["project"],
        operators={state["operator"], state["operator"] + "-release"},
    )
    current = run(
        "docker",
        "ps",
        "-aq",
        "--no-trunc",
        "--filter",
        "label=com.docker.compose.project=" + state["project"],
    ).split()
    for identifier in current:
        if identifier not in known and inspect_container(identifier)["Name"].lstrip("/") not in {
            state["operator"],
            state["operator"] + "-release",
        }:
            raise RuntimeError("An unexpected service or deployment appeared.")


def boot_identity() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def start_process(
    command: list[str], *, attached: bool = False
) -> tuple[subprocess.Popen[bytes], dict[str, Any]]:
    boot = boot_identity()
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE if attached else subprocess.DEVNULL,
        stdout=subprocess.PIPE if attached else subprocess.DEVNULL,
        stderr=None if attached else subprocess.DEVNULL,
        bufsize=0,
        start_new_session=True,
    )
    identity = process_identity(process.pid)
    return process, {"pid": process.pid, "start": identity[0] if identity else None, "boot": boot}


def process_identity(pid: int) -> tuple[str, str] | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
    except FileNotFoundError:
        return None
    return fields[19], fields[0]


def process_running(process: dict[str, Any]) -> bool:
    if int(process["pid"]) <= 0:
        return False
    if not process.get("boot"):
        raise RuntimeError("The stop process has no qualified boot identity.")
    if process["boot"] != boot_identity():
        return False
    identity = process_identity(int(process["pid"]))
    return identity is not None and identity[0] == process["start"] and identity[1] != "Z"


def launch_stop(state_path: Path, identifier: str) -> subprocess.Popen[bytes]:
    with locked_state(state_path, active=True) as state:
        if identifier not in state["containers"]:
            raise ValueError("Only an original service may be stopped.")
        check_originals(state)
        process, identity = start_process(["docker", "stop", "--timeout", "-1", identifier])
        state.setdefault("stops", {})[identifier] = identity
        return process


def stop_services(state_path: Path, identifiers: list[str]) -> None:
    processes = [launch_stop(state_path, identifier) for identifier in identifiers]
    while any(process.poll() is None for process in processes):
        with locked_state(state_path, active=True):
            pass
        time.sleep(0.2)
    if any(process.returncode for process in processes):
        raise RuntimeError("An original service did not stop gracefully.")
    with locked_state(state_path, active=True) as state:
        for identifier in identifiers:
            if inspect_container(identifier)["State"]["Running"]:
                raise RuntimeError("An original service did not finish stopping.")
            state["stops"][identifier]["complete"] = True


def restart_original(
    identifier: str, state: dict[str, Any], settled: Callable[[str], None] | None = None
) -> str:
    observed = inspect_container(identifier)
    if observed["Config"]["Labels"].get("com.docker.compose.project") != state["project"]:
        raise RuntimeError("Original container project identity changed.")
    stop = state.get("stops", {}).get(identifier)
    while stop and process_running(stop):
        time.sleep(0.2)
    if stop and not stop.get("complete") and stop["boot"] == boot_identity():
        deadline = time.monotonic() + 120
        while inspect_container(identifier)["State"]["Running"]:
            if time.monotonic() >= deadline:
                raise RuntimeError("The original stop has no settled container readback.")
            time.sleep(0.2)
        if settled is not None:
            settled(identifier)
    if not inspect_container(identifier)["State"]["Running"]:
        run("docker", "start", identifier, timeout=60)
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        observed = inspect_container(identifier)["State"]
        if observed["Running"] and observed.get("Health", {}).get("Status", "healthy") == "healthy":
            time.sleep(2)
            if inspect_container(identifier)["State"]["Running"]:
                return identifier
        time.sleep(1)
    raise RuntimeError("An original service did not recover with healthy readback.")


def operator_container(state: dict[str, Any]) -> dict[str, Any] | None:
    identifiers = run(
        "docker", "ps", "-aq", "--filter", "name=^/" + state["operator"] + "$"
    ).split()
    if not identifiers:
        return None
    observed = inspect_container(identifiers[0])
    labels = observed["Config"]["Labels"]
    if (
        labels.get("com.docker.compose.oneoff", "").lower() != "true"
        or labels.get("com.docker.compose.project") != state["project"]
        or observed["Image"] != state["image_id"]
    ):
        raise RuntimeError("The keeper name belongs to another runtime.")
    return observed


def release(state_path: Path) -> None:
    with locked_state(state_path) as state:
        if state.get("released"):
            return
        state["releasing"] = True
        flag = state_path.parent / "releasing"
        flag.touch(mode=0o600)
        save(state_path, state)
        if state.get("keeper") and not state.get("database_released"):
            keeper = state["keeper"]
            started = keeper["backend_start"].replace("'", "''")
            database = state["database"].replace("'", "''")
            sql = f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE pid={int(keeper['pid'])} AND backend_start='{started}'::timestamptz AND datname='{database}';"
            try:
                run(
                    "docker",
                    "exec",
                    "-i",
                    state["postgres"],
                    "sh",
                    "-c",
                    'exec psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"',
                    input_text=sql + "\n",
                )
            except Exception:
                state["keeper_termination_unverified"] = True
                save(state_path, state)
        restarted = set(state.get("restarted_containers", []))
        errors = []
        progress_lock = Lock()

        def settled(identifier: str) -> None:
            with progress_lock:
                state["stops"][identifier]["complete"] = True
                save(state_path, state)

        with ThreadPoolExecutor(max_workers=max(1, len(state["containers"]))) as executor:
            futures = {
                executor.submit(restart_original, identifier, state, settled): identifier
                for identifier in state["containers"]
                if identifier not in restarted
            }
            for future in as_completed(futures):
                try:
                    identifier = future.result()
                    with progress_lock:
                        restarted.add(identifier)
                        state["restarted_containers"] = sorted(restarted)
                        save(state_path, state)
                except Exception as error:
                    errors.append(f"{futures[future]}:{type(error).__name__}")
        if errors:
            raise RuntimeError(
                "Some original services lack recovery readback: " + ", ".join(errors)
            )
        state["restarted"] = True
        save(state_path, state)
        (state_path.parent / "restarted").touch(mode=0o600)
        receipt_path = state_path.parent / "marker-release.json"
        until = time.monotonic() + 210
        while not receipt_path.exists() and time.monotonic() < until:
            if operator_container(state) is None:
                break
            time.sleep(0.2)
        marker_released = False
        if receipt_path.exists():
            receipt = json.loads(receipt_path.read_text())
            if receipt.get("uncertain"):
                if operator_container(state) is not None:
                    raise RuntimeError(
                        "Provider marker outcome needs reconciliation; no cleanup write was replayed."
                    )
                operator(state, "observe-unfence")
                marker_released = True
            marker_released = marker_released or receipt == {
                "released": True,
                "deadline": state["expires_at"],
                "target_id": state["target_id"],
            }
        current_controller = operator_container(state)
        if not marker_released and current_controller and current_controller["State"]["Running"]:
            raise RuntimeError("The original controller has not completed provider cleanup.")
        if state.get("cleanup_uncertain"):
            operator(state, "observe-unfence")
            marker_released = True
        if state.get("fenced") and not marker_released:
            try:
                operator(state, "unfence")
            except Exception:
                state["cleanup_uncertain"] = True
                raise
        state["released"] = True
        save(state_path, state)
        print(
            json.dumps(
                {
                    "event": "released",
                    "hold_id": state["hold_id"],
                    "original_services_running": True,
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
        "job_directory": str(directory),
        "image_id": api["Image"],
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
    state["containers"].sort(key=lambda container_id: container_id != state["api"])
    require_no_outside_writers(
        api,
        set(state["containers"]) | {state["browser"]},
        project=state["project"],
        operators=set(),
    )
    overlay = {
        "services": {
            "launchplane-maintenance": {
                "image": api["Image"],
                "env_file": [str(Path(state["directory"]) / ".env")],
                "environment": {"DOCKER_IMAGE_REFERENCE": environment["DOCKER_IMAGE_REFERENCE"]},
                "networks": ["default", "launchplane-external-network"],
                "read_only": True,
                "tmpfs": ["/tmp"],
                "restart": "no",
                "logging": {"driver": "none"},
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
    state.update(
        {
            "database": plan["database"],
            "system_identifier": plan["system_identifier"],
            "target_id": plan["target_id"],
        }
    )
    sql = "SELECT system_identifier FROM pg_control_system();\n"
    observed = run(
        "docker",
        "exec",
        "-i",
        state["postgres"],
        "sh",
        "-c",
        'exec psql -X -At -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"',
        input_text=sql,
    ).strip()
    if observed != state["system_identifier"]:
        raise ValueError("The PostgreSQL container does not match the Launchplane connection.")
    save(state_path, state)
    return state_path, state


def write_all(descriptor: int, data: bytes) -> None:
    pending = memoryview(data)
    while pending:
        written = os.write(descriptor, pending)
        if not written:
            raise BrokenPipeError("The keeper command pipe closed.")
        pending = pending[written:]


def hold(state_path: Path) -> None:
    state = json.loads(state_path.read_text())
    unit = "lp-maint-" + state["hold_id"]
    run(
        "systemd-run",
        "--unit=" + unit,
        "--on-active=" + str(max(1, int(remaining(state)))) + "s",
        "--timer-property=AccuracySec=1s",
        sys.executable,
        str(state_path.parent / "driver.py"),
        "release",
        "--state",
        str(state_path),
    )
    keeper = None
    commands = None
    try:
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
        with locked_state(state_path, active=True) as state:
            state["fenced"] = True
            keeper, _ = start_process(operator_command(state, "hold"), attached=True)
        assert keeper.stdout is not None and keeper.stdin is not None
        commands = os.open(state["commands"], os.O_RDWR | os.O_NONBLOCK)
        buffered = b""
        held = False
        while True:
            with locked_state(state_path, active=True) as state:
                check_originals(state, stopped=held)
            streams: list[Any] = [keeper.stdout, sys.stdin]
            if held:
                streams.append(commands)
            ready, _, _ = select.select(streams, [], [], min(1, max(0, remaining(state))))
            for stream in ready:
                if stream is sys.stdin:
                    if not sys.stdin.readline():
                        raise EOFError("Maintenance input closed.")
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
                        if event["event"] == "deployment_held":
                            state = update_state(
                                state_path,
                                keeper={
                                    "pid": event["pid"],
                                    "backend_start": event["backend_start"],
                                },
                            )
                            if (
                                hashlib.sha256(
                                    (Path(state["directory"]) / ".env").read_bytes()
                                ).hexdigest()
                                != state["environment_sha256"]
                            ):
                                raise RuntimeError(
                                    "Provider mutation rewrote the host credential source."
                                )
                            stop_services(state_path, [state["api"]])
                            stop_services(
                                state_path,
                                [
                                    identifier
                                    for identifier in state["containers"]
                                    if identifier != state["api"]
                                ],
                            )
                            check_originals(state, stopped=True)
                            write_all(keeper.stdin.fileno(), b'{"action":"stopped"}\n')
                        elif event["event"] == "held":
                            state = update_state(state_path, held=event)
                            held = True
                        elif event["event"] == "database_released":
                            update_state(state_path, database_released=True)
                            release(state_path)
                            print(line.decode(), flush=True)
                            return
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
                if "ActiveState=active" in run(
                    "systemctl", "show", unit + ".timer", "--property=ActiveState"
                ):
                    raise RuntimeError("The restart timer remains active after release.")


def dump(state_path: Path) -> None:
    with locked_state(state_path, active=True) as state:
        if not state.get("held"):
            raise RuntimeError("The held snapshot is not available.")
        check_originals(state, stopped=True)
        state = dict(state)
    snapshot = state["held"]["snapshot"]
    database = state["database"]
    if not all(character.isalnum() or character == "-" for character in snapshot) or not all(
        character.isalnum() or character == "_" for character in database
    ):
        raise ValueError("The snapshot or database identifier is unqualified.")
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
        while data := process.stdout.read(1024 * 1024):
            digest.update(data)
            sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
        if process.wait(timeout=30):
            raise RuntimeError("The staged database dump failed.")
        check_originals(state, stopped=True)
        update_state(state_path, backup_sha256=digest.hexdigest())
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
    receipt = json.loads(receipt_path.read_text())
    with locked_state(state_path, active=True) as state:
        if not state.get("held") or not state.get("backup_sha256"):
            raise RuntimeError("No verified dump identity is available for this held snapshot.")
        if receipt.get("backup_sha256") != state["backup_sha256"] or any(
            receipt.get(key) != state["held"].get(key)
            for key in ("hold_id", "snapshot", "transaction")
        ):
            raise ValueError("The receipt belongs to another dump or held snapshot.")
        commands = state["commands"]
    descriptor = os.open(commands, os.O_WRONLY | os.O_NONBLOCK)
    os.set_blocking(descriptor, True)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(json.dumps(receipt, separators=(",", ":")) + "\n")


def interrupted(_signum: int, _frame: object) -> None:
    raise InterruptedError("Maintenance interrupted; release its bounded hold.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Bounded stop and restart of Launchplane only.")
    parser.add_argument("mode", choices=("plan", "hold", "release", "dump", "submit"))
    parser.add_argument("--state", type=Path)
    parser.add_argument("--receipt-file", type=Path)
    parser.add_argument("--api-container")
    parser.add_argument("--postgres-container")
    parser.add_argument("--duration-seconds", type=int, default=1800)
    parser.add_argument("--state-root", default="/var/lib/launchplane-maintenance")
    arguments = parser.parse_args()
    for number in (signal.SIGHUP, signal.SIGTERM):
        signal.signal(number, interrupted)
    if arguments.mode in {"dump", "submit", "release"}:
        if not isinstance(arguments.state, Path):
            parser.error("This mode requires --state.")
        state_path = arguments.state
        if arguments.mode == "submit":
            if not isinstance(arguments.receipt_file, Path):
                parser.error("submit requires --receipt-file.")
            submit(state_path, arguments.receipt_file)
        elif arguments.mode == "dump":
            dump(state_path)
        else:
            release(state_path)
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
        hold(state_path)


if __name__ == "__main__":
    main()
