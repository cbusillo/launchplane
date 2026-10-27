import argparse
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import select
import shlex
import shutil
import signal
import subprocess
import sys
import time
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


def sync_path(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def locked_state(path: Path, *, active: bool = False) -> Iterator[dict[str, Any]]:
    with path.with_suffix(".lock").open("a") as lock:
        deadline = time.monotonic() + 15
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        "Another maintenance operation still owns state custody."
                    ) from None
                time.sleep(0.05)
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
        state["operator"] + ("-release" if mode in {"unfence", "verify", "revision"} else ""),
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
    output = run(*operator_command(state, mode), timeout=300 if mode == "verify" else 90)
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


def check_originals(state: dict[str, Any], *, frozen: bool = False) -> None:
    observations = original_containers(state)
    for observed in observations:
        original = state["originals"][observed["Id"]]
        if observed["Image"] != state["image_id"]:
            raise RuntimeError("An original service image changed.")
        if frozen and (
            not observed["State"]["Running"]
            or observed["State"]["Pid"] != original["pid"]
            or observed["State"]["StartedAt"] != original["started_at"]
            or read_freezer(original) != ("1", "1")
            or observed["HostConfig"]["RestartPolicy"]["Name"] != "no"
        ):
            raise RuntimeError("An original service lost its physical writer hold.")
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


def start_process(command: list[str]) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        bufsize=0,
        start_new_session=True,
    )


def freezer_identity(observed: dict[str, Any]) -> dict[str, Any]:
    pid = int(observed["State"]["Pid"])
    memberships = Path(f"/proc/{pid}/cgroup").read_text().splitlines()
    if len(memberships) != 1 or not memberships[0].startswith("0::/"):
        raise RuntimeError("The original service requires a dedicated cgroup-v2 freezer.")
    root = Path("/sys/fs/cgroup").resolve()
    group = (root / memberships[0][4:]).resolve()
    if (
        not group.is_relative_to(root)
        or group == root
        or group.name not in {observed["Id"], "docker-" + observed["Id"] + ".scope"}
    ):
        raise RuntimeError("The service cgroup is not its exact Docker container.")
    metadata = group.stat()
    original = {
        "pid": pid,
        "started_at": observed["State"]["StartedAt"],
        "cgroup": str(group),
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
        "restart_policy": observed["HostConfig"]["RestartPolicy"],
    }
    if str(pid) not in (group / "cgroup.procs").read_text().split() or read_freezer(original) != (
        "0",
        "0",
    ):
        raise RuntimeError("The original cgroup membership or thawed state is unproved.")
    return original


def freezer_path(original: dict[str, Any]) -> Path:
    path = Path(original["cgroup"])
    metadata = path.stat()
    if (metadata.st_dev, metadata.st_ino) != (original["device"], original["inode"]):
        raise RuntimeError("The recorded cgroup identity changed.")
    return path


def read_freezer(original: dict[str, Any]) -> tuple[str, str]:
    path = freezer_path(original)
    events = dict(line.split() for line in (path / "cgroup.events").read_text().splitlines())
    return (path / "cgroup.freeze").read_text().strip(), events["frozen"]


def set_freezer(original: dict[str, Any], frozen: bool) -> None:
    value = "1" if frozen else "0"
    (freezer_path(original) / "cgroup.freeze").write_text(value)
    deadline = time.monotonic() + 5
    while read_freezer(original) != (value, value):
        if time.monotonic() >= deadline:
            raise TimeoutError("The kernel freezer did not reach its requested state.")
        time.sleep(0.02)


def restart_policy(policy: dict[str, Any]) -> str:
    name = str(policy["Name"])
    if name not in {"no", "always", "unless-stopped", "on-failure"}:
        raise RuntimeError("An original restart policy is unsupported.")
    attempts = int(policy.get("MaximumRetryCount", 0))
    return f"{name}:{attempts}" if name == "on-failure" and attempts else name


def freeze_services(state_path: Path) -> dict[str, Any]:
    state = json.loads(state_path.read_text())
    check_originals(state)
    for identifier in state["containers"]:
        with locked_state(state_path, active=True) as state:
            state.setdefault("policy_changes", []).append(identifier)
            save(state_path, state)
            run("docker", "update", "--restart=no", identifier, timeout=10)
            if inspect_container(identifier)["HostConfig"]["RestartPolicy"]["Name"] != "no":
                raise RuntimeError("The service restart policy was not suppressed.")
    for identifier in state["containers"]:
        with locked_state(state_path, active=True) as state:
            state.setdefault("freeze_requested", []).append(identifier)
            save(state_path, state)
            set_freezer(state["originals"][identifier], True)
    check_originals(state, frozen=True)
    return dict(state)


def resume_original(identifier: str, state: dict[str, Any]) -> str:
    observed = inspect_container(identifier)
    if (
        observed["Config"]["Labels"].get("com.docker.compose.project") != state["project"]
        or observed["Image"] != state["image_id"]
    ):
        raise RuntimeError("Original container project identity changed.")
    if identifier in state.get("freeze_requested", []) and observed["State"]["Running"]:
        original = state["originals"][identifier]
        if (
            observed["State"]["Pid"] != original["pid"]
            or observed["State"]["StartedAt"] != original["started_at"]
        ):
            raise RuntimeError("The original process changed before verified release.")
        set_freezer(original, False)
    if not observed["State"]["Running"]:
        run("docker", "start", identifier, timeout=60)
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        observed = inspect_container(identifier)["State"]
        if observed["Running"] and observed.get("Health", {}).get("Status", "healthy") == "healthy":
            time.sleep(2)
            if inspect_container(identifier)["State"]["Running"]:
                policy = state["originals"][identifier]["restart_policy"]
                run(
                    "docker",
                    "update",
                    "--restart=" + restart_policy(policy),
                    identifier,
                    timeout=10,
                )
                if inspect_container(identifier)["HostConfig"]["RestartPolicy"] != policy:
                    raise RuntimeError("The original restart policy was not restored.")
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


def terminate_keeper(state: dict[str, Any]) -> None:
    if not state.get("keeper"):
        return
    keeper = state["keeper"]
    output = run(
        "docker",
        "exec",
        "-i",
        state["postgres"],
        "sh",
        "-c",
        'exec psql -X -At -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$1" -v keeper_pid="$2" -v keeper_started="$3"',
        "keeper",
        state["database"],
        str(int(keeper["pid"])),
        keeper["backend_start"],
        input_text=(
            "SELECT pg_terminate_backend(pid,5000) FROM pg_stat_activity "
            "WHERE pid=:keeper_pid AND backend_start=:'keeper_started'::timestamptz AND datname=current_database();\n"
            "SELECT NOT EXISTS(SELECT FROM pg_stat_activity WHERE pid=:keeper_pid "
            "AND backend_start=:'keeper_started'::timestamptz AND datname=current_database());\n"
        ),
    )
    if output.strip().splitlines()[-1:] != ["t"]:
        raise RuntimeError("The exact keeper backend has not released its transaction.")


def install_watchdog(state_path: Path, state: dict[str, Any]) -> None:
    unit = "lp-maint-" + state["hold_id"]
    root = Path("/etc/systemd/system")
    command = shlex.join(
        [
            sys.executable,
            str(state_path.parent / "driver.py"),
            "release",
            "--state",
            str(state_path),
        ]
    )
    (root / (unit + ".service")).write_text(
        "[Unit]\nDescription=Launchplane maintenance recovery\nAfter=docker.service\n"
        "[Service]\nType=oneshot\nExecStart="
        + command
        + "\nRestart=on-failure\nRestartSec=15s\nTimeoutStartSec=900s\n"
    )
    deadline = (
        datetime.fromisoformat(state["expires_at"])
        .astimezone(timezone.utc)
        .strftime("%Y-%m-%d %H:%M:%S UTC")
    )
    (root / (unit + ".timer")).write_text(
        "[Unit]\nDescription=Launchplane maintenance deadline\n[Timer]\nOnCalendar="
        + deadline
        + "\nPersistent=true\nAccuracySec=1s\n[Install]\nWantedBy=timers.target\n"
    )
    for path in (root / (unit + ".service"), root / (unit + ".timer"), root):
        sync_path(path)
    run("systemctl", "daemon-reload")
    run("systemctl", "enable", "--now", unit + ".timer")
    sync_path(root / "timers.target.wants")
    timer = run(
        "systemctl",
        "show",
        unit + ".timer",
        "--property=ActiveState",
        "--property=NextElapseUSecRealtime",
    )
    service = run("systemctl", "show", unit + ".service", "--property=ExecStart")
    if "ActiveState=active" not in timer or str(state_path) not in service:
        raise RuntimeError("The durable recovery watchdog was not read back.")


def remove_watchdog(state: dict[str, Any]) -> None:
    if state.get("watchdog_removed"):
        return
    unit = "lp-maint-" + state["hold_id"]
    state_path = Path(state["job_directory"]) / "state.json"
    if not state.get("watchdog_disabled"):
        run("systemctl", "disable", "--now", unit + ".timer")
        if "ActiveState=active" in run(
            "systemctl", "show", unit + ".timer", "--property=ActiveState"
        ):
            raise RuntimeError("The recovery watchdog remains active after release.")
        state["watchdog_disabled"] = True
        save(state_path, state)
    for suffix in (".timer", ".service"):
        (Path("/etc/systemd/system") / (unit + suffix)).unlink(missing_ok=True)
    sync_path(Path("/etc/systemd/system"))
    run("systemctl", "daemon-reload")
    state["watchdog_removed"] = True
    save(state_path, state)


def release(state_path: Path) -> None:
    with locked_state(state_path) as state:
        if state.get("released"):
            remove_watchdog(state)
            emit_release(state)
            return
        state["releasing"] = True
        (state_path.parent / "releasing").touch(mode=0o600)
        save(state_path, state)
        pending = (state_path.parent / "commit-pending.json").exists()
        writers_held: bool | None = None
        try:
            terminate_keeper(state)
            safe = state.get("safe_to_resume")
            if safe and any(
                safe.get(key) != state[key] for key in ("hold_id", "system_identifier", "image_id")
            ):
                raise RuntimeError(
                    "The durable resume proof belongs to another maintenance runtime."
                )
            if pending and not safe:
                for observed in original_containers(state):
                    if observed["HostConfig"]["RestartPolicy"]["Name"] != "no":
                        writers_held = False
                        raise RuntimeError("An unverified service regained automatic restart.")
                    if observed["State"]["Running"] and read_freezer(
                        state["originals"][observed["Id"]]
                    ) != ("1", "1"):
                        writers_held = False
                        raise RuntimeError("An unverified service is no longer frozen.")
                writers_held = True
                proof = operator(state, "verify")
                state["revision"] = proof["revision"]
                state["safe_to_resume"] = {
                    key: state[key]
                    for key in ("hold_id", "system_identifier", "image_id", "revision")
                }
                save(state_path, state)
                save(state_path.parent / "database-safe.json", state["safe_to_resume"])
        except Exception as error:
            if pending:
                state["recovery_required"] = type(error).__name__
                save(state_path, state)
                print(
                    json.dumps(
                        {
                            "event": "recovery_required",
                            "hold_id": state["hold_id"],
                            "writers_held": writers_held,
                            "revision": state.get("revision"),
                        }
                    ),
                    flush=True,
                )
                raise
            state["keeper_termination_unverified"] = type(error).__name__
            save(state_path, state)
        resumed = set(state.get("resumed_containers", []))
        errors = []
        state["resume_started"] = True
        save(state_path, state)
        with ThreadPoolExecutor(max_workers=max(1, len(state["containers"]))) as executor:
            futures = {
                executor.submit(resume_original, identifier, state): identifier
                for identifier in state["containers"]
                if identifier not in resumed
            }
            for future in as_completed(futures):
                try:
                    resumed.add(future.result())
                    state["resumed_containers"] = sorted(resumed)
                    save(state_path, state)
                except Exception as error:
                    errors.append(f"{futures[future]}:{type(error).__name__}")
        if errors:
            raise RuntimeError(
                "Some original services lack recovery readback: " + ", ".join(errors)
            )
        (state_path.parent / "resumed").touch(mode=0o600)
        state["resumed"] = True
        save(state_path, state)
        until = time.monotonic() + 90
        while time.monotonic() < until:
            controller = operator_container(state)
            if controller is None or not controller["State"]["Running"]:
                break
            time.sleep(0.2)
        else:
            raise RuntimeError("The original controller has not completed cleanup.")
        if state.get("fenced"):
            operator(state, "unfence")
        if not state.get("revision"):
            try:
                state["revision"] = operator(state, "revision")["revision"]
            except Exception as error:
                state["revision_readback_unknown"] = type(error).__name__
        state["released"] = True
        state.pop("recovery_required", None)
        save(state_path, state)
        remove_watchdog(state)
        emit_release(state)


def emit_release(state: dict[str, Any]) -> None:
    print(
        json.dumps(
            {
                "event": "released",
                "hold_id": state["hold_id"],
                "original_services_running": True,
                "revision": state.get("revision"),
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
        "originals": {},
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
            state["originals"][item["Id"]] = freezer_identity(item)
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
    sync_path(Path(state["overlay"]))
    sync_path(directory / "driver.py")
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
    keeper = None
    commands = None
    try:
        install_watchdog(state_path, state)
        with locked_state(state_path, active=True) as state:
            state["fenced"] = True
            keeper = start_process(operator_command(state, "hold"))
        assert keeper.stdout is not None and keeper.stdin is not None
        commands = os.open(state["commands"], os.O_RDWR | os.O_NONBLOCK)
        buffered = b""
        held = False
        while True:
            with locked_state(state_path, active=True) as state:
                check_originals(state, frozen=held)
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
                            state = freeze_services(state_path)
                            addresses: dict[str, str] = {}
                            for observed in original_containers(state):
                                for network in observed["NetworkSettings"]["Networks"].values():
                                    for key in ("IPAddress", "GlobalIPv6Address"):
                                        if network.get(key):
                                            address = str(ipaddress.ip_address(network[key]))
                                            if (
                                                address in addresses
                                                and addresses[address] != observed["Id"]
                                            ):
                                                raise RuntimeError(
                                                    "Database-facing container addresses overlap."
                                                )
                                            addresses[address] = observed["Id"]
                            if not addresses:
                                raise RuntimeError(
                                    "The frozen container network identities are unavailable."
                                )
                            write_all(
                                keeper.stdin.fileno(),
                                json.dumps(
                                    {"action": "frozen", "addresses": sorted(addresses)}
                                ).encode()
                                + b"\n",
                            )
                        elif event["event"] == "held":
                            state = update_state(state_path, held=event)
                            held = True
                        elif event["event"] == "database_released":
                            update_state(
                                state_path, database_released=True, revision=event["revision"]
                            )
                            release(state_path)
                            print(line.decode(), flush=True)
                            return
                        print(line.decode(), flush=True)
    finally:
        if commands is not None:
            os.close(commands)
        if keeper and keeper.stdin:
            keeper.stdin.close()
        release(state_path)


def dump(state_path: Path) -> None:
    with locked_state(state_path, active=True) as state:
        if not state.get("held"):
            raise RuntimeError("The held snapshot is not available.")
        check_originals(state, frozen=True)
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
        check_originals(state, frozen=True)
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
    parser = argparse.ArgumentParser(description="Bounded full writer hold of Launchplane only.")
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
