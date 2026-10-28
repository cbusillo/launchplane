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
import re
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
from urllib.parse import parse_qs, unquote, urlsplit


RECOVERY_REQUIRED_EXIT = 75
AUTOMATIC_RECOVERY_EXHAUSTED_EXIT = 76
RECOVERY_WAIT_SECONDS = 920
HEALTH_PROBE_SECONDS = 30
HEALTH_REFREEZE_SECONDS = 45
API_EGRESS_PRIORITY = 300
API_NETWORK_HOOKS = ("input", "output", "postrouting")
WATCHDOG_ATTEMPTS = 3
DATABASE_UNSAFE_EVENT = "database_unsafe"
DEPLOYMENT_RECOVERY_REQUIRED_EVENT = "deployment_recovery_required"
VERIFICATION_BLOCKED_EVENT = "verification_blocked"
AUTOMATIC_RECOVERY_EXHAUSTED_EVENT = "automatic_recovery_exhausted"
RELEASE_INCOMPLETE_EVENT = "release_incomplete"


class RecoveryRequired(RuntimeError):
    def __init__(self, message: str, *, event: str = VERIFICATION_BLOCKED_EVENT) -> None:
        super().__init__(message)
        self.event = event


class AutomaticRecoveryExhausted(RuntimeError):
    pass


def run(
    *arguments: str,
    timeout: float | None = 30,
    input_text: str | None = None,
    pass_fds: tuple[int, ...] = (),
) -> str:
    result = subprocess.run(
        arguments,
        input=input_text,
        text=True,
        capture_output=True,
        timeout=timeout,
        pass_fds=pass_fds,
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
def locked_state(
    path: Path, *, active: bool = False, wait_seconds: float = 15
) -> Iterator[dict[str, Any]]:
    with path.with_suffix(".lock").open("a") as lock:
        deadline = time.monotonic() + wait_seconds
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


def dump_application_name(hold_id: str) -> str:
    name = "lp-maint-dump-" + hold_id
    if len(name.encode()) >= 63:
        raise ValueError("The scoped backup client name exceeds PostgreSQL's identifier limit.")
    return name


def read_marker_transition(
    directory: Path, *, hold_id: str, target_id: str
) -> dict[str, Any] | None:
    path = directory / "marker-transition.json"
    if not path.exists():
        return None
    try:
        transition = json.loads(path.read_text())
        if (
            not isinstance(transition, dict)
            or transition.get("hold_id") != hold_id
            or transition.get("target_id") != target_id
            or transition.get("action") not in {"install", "recovery", "remove"}
            or transition.get("phase") not in {"dispatching", "confirmed", "observed"}
            or not {"before", "desired"} <= transition.keys()
            or any(
                transition.get(key) is not None and not isinstance(transition[key], str)
                for key in ("before", "desired")
            )
        ):
            raise ValueError("Unbound marker transition.")
    except (ValueError, TypeError, KeyError) as error:
        raise RecoveryRequired(
            "The original deployment marker transition is unverified.",
            event=DEPLOYMENT_RECOVERY_REQUIRED_EVENT,
        ) from error
    return transition


def operator_command(state: dict[str, Any], mode: str) -> list[str]:
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
        "--env",
        "PGAPPNAME="
        + state["operator"]
        + ("-release" if mode in {"unfence", "verify", "revision"} else ""),
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
    return command


def operator(state: dict[str, Any], mode: str) -> dict[str, Any]:
    root = Path(state["job_directory"])
    intent_path = root / "operator-intent.json"
    settle_operator(state)
    intent = {
        "name": state["operator"]
        + ("-release" if mode in {"unfence", "verify", "revision"} else ""),
        "read_only": mode != "unfence",
    }
    save(intent_path, intent)
    try:
        output = run(
            *operator_command(state, mode),
            timeout=300 if mode == "verify" else 90,
        )
        result = cast(dict[str, Any], json.loads(output.strip().splitlines()[-1]))
    except Exception as error:
        settle_operator(state)
        if mode == "unfence":
            transition = read_marker_transition(
                root, hold_id=state["hold_id"], target_id=state["target_id"]
            )
            if transition is not None and transition["phase"] != "observed":
                raise RecoveryRequired(
                    "The original deployment marker request requires read-only reconciliation.",
                    event=DEPLOYMENT_RECOVERY_REQUIRED_EVENT,
                ) from error
        raise
    settle_operator(state)
    if result.get("event") in {
        DATABASE_UNSAFE_EVENT,
        DEPLOYMENT_RECOVERY_REQUIRED_EVENT,
        VERIFICATION_BLOCKED_EVENT,
    }:
        raise RecoveryRequired(
            "The original maintenance operation requires exact reconciliation.",
            event=result["event"],
        )
    return result


def settle_operator(state: dict[str, Any]) -> None:
    path = Path(state["job_directory"]) / "operator-intent.json"
    if not path.exists():
        return
    intent = json.loads(path.read_text())
    observed = operator_container(state, name=intent["name"])
    if observed is not None:
        intent["addresses"] = sorted(
            {
                str(ipaddress.ip_address(network[key]))
                for network in observed["NetworkSettings"]["Networks"].values()
                for key in ("IPAddress", "GlobalIPv6Address")
                if network.get(key)
            }
        )
        save(path, intent)
        run("docker", "rm", "-f", observed["Id"])
        if operator_container(state, name=intent["name"]) is not None:
            raise RuntimeError("The exact maintenance one-off has not exited.")
    deadline = time.monotonic() + 30
    while True:
        count = run(
            "docker",
            "exec",
            "-i",
            state["postgres"],
            "sh",
            "-c",
            'exec psql -X -At -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$1" -v reader="$2" -v addresses="$3"',
            "reader",
            state["database"],
            intent["name"],
            ",".join(intent.get("addresses", [])),
            input_text="SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND (application_name=:'reader' OR host(client_addr)=ANY(string_to_array(:'addresses',',')));\n",
        )
        if count.strip() == "0":
            path.unlink()
            return
        if time.monotonic() >= deadline:
            raise TimeoutError(
                "An owned maintenance reader still has an unsettled database session."
            )
        time.sleep(0.2)


def database_credential(environment: dict[str, str]) -> tuple[str, str] | None:
    value = environment.get("LAUNCHPLANE_DATABASE_URL", "")
    if not value:
        return None
    url = urlsplit(value)
    if (
        url.scheme.split("+", 1)[0] not in {"postgres", "postgresql"}
        or not url.username
        or not url.password
        or {"user", "password", "passfile", "service"} & parse_qs(url.query).keys()
        or any(environment.get(key) for key in ("PGUSER", "PGPASSWORD", "PGPASSFILE", "PGSERVICE"))
    ):
        raise RuntimeError("Database credential indirection is not qualified for custody.")
    return unquote(url.username), unquote(url.password)


def require_no_outside_writers(
    api: dict[str, Any], allowed: set[str], *, project: str, operators: set[str]
) -> None:
    credential = database_credential(
        dict(value.split("=", 1) for value in api["Config"]["Env"] if "=" in value)
    )
    if credential is None:
        raise RuntimeError("The API database credential identity is incomplete.")
    identifiers = run("docker", "ps", "-aq", "--no-trunc").split()
    if not identifiers:
        raise RuntimeError("The container credential inventory is unavailable.")
    for other in json.loads(run("docker", "inspect", *identifiers)):
        identifier = other["Id"]
        if identifier in allowed:
            continue
        labels = other["Config"]["Labels"] or {}
        own_operator = (
            other["Name"].lstrip("/") in operators
            and labels.get("com.docker.compose.project") == project
            and labels.get("com.docker.compose.oneoff", "").lower() == "true"
            and other["Image"] == api["Image"]
        )
        if own_operator:
            continue
        values = [
            value
            for key in ("Env", "Cmd", "Entrypoint")
            for value in (other["Config"].get(key) or [])
        ]
        environment = dict(
            value.split("=", 1) for value in (other["Config"].get("Env") or []) if "=" in value
        )
        if (
            any(credential[1] in unquote(value) for value in values)
            or other["Image"] == api["Image"]
            and database_credential(environment) is None
        ):
            raise RuntimeError(
                "Another container has unresolved Launchplane database credential custody."
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
    known = set(state["containers"]) | {state["browser"], state["postgres"]}
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
            safe = state.get("safe_to_resume", {})
            pending = (Path(state["job_directory"]) / "commit-pending.json").exists()
            if (
                not state.get("resume_started")
                or pending
                and (
                    not safe
                    or any(
                        safe.get(key) != state[key]
                        for key in ("hold_id", "system_identifier", "image_id", "revision")
                    )
                )
            ):
                raise RuntimeError("The original process changed before verified release.")
            freezer_identity(observed)
        else:
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


def operator_container(state: dict[str, Any], *, name: str | None = None) -> dict[str, Any] | None:
    name = name or state["operator"]
    if name not in {state["operator"], state["operator"] + "-release"}:
        raise RuntimeError("The one-off intent belongs to another maintenance attempt.")
    identifiers = run("docker", "ps", "-aq", "--filter", "name=^/" + name + "$").split()
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


def install_watchdog(state_path: Path, state: dict[str, Any], *, health: bool = False) -> None:
    unit = "lp-maint-" + state["hold_id"] + ("-health" if health else "")
    root = Path("/etc/systemd/system")
    command = shlex.join(
        [
            sys.executable,
            str(state_path.parent / "driver.py"),
            "refreeze" if health else "recover",
            "--state",
            str(state_path),
        ]
        + (["--phase-id", state["verification_phase"]["phase_id"]] if health else [])
    )
    (root / (unit + ".service")).write_text(
        "[Unit]\nDescription=Launchplane maintenance recovery\nAfter=docker.service\n"
        f"StartLimitIntervalSec=1d\nStartLimitBurst={WATCHDOG_ATTEMPTS + 1}\n"
        "[Service]\nType=oneshot\nExecStart="
        + command
        + f"\nRestart=on-failure\nRestartPreventExitStatus={RECOVERY_REQUIRED_EXIT} {AUTOMATIC_RECOVERY_EXHAUSTED_EXIT}\nRestartSec=15s\nTimeoutStartSec=900s\n"
        + ("[Install]\nWantedBy=multi-user.target\n" if not health else "")
    )
    deadline = (
        datetime.fromisoformat(
            state["verification_phase"]["deadline"] if health else state["expires_at"]
        )
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
    if not health:
        run("systemctl", "enable", unit + ".service")
        sync_path(root / "multi-user.target.wants")
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


def remove_watchdog(state: dict[str, Any], *, health: bool = False) -> None:
    progress = state["verification_phase"] if health else state
    if progress.get("watchdog_removed"):
        return
    unit = "lp-maint-" + state["hold_id"] + ("-health" if health else "")
    state_path = Path(state["job_directory"]) / "state.json"
    if not progress.get("watchdog_disabled"):
        timer = run("systemctl", "show", unit + ".timer", "--property=LoadState")
        if "LoadState=not-found" not in timer:
            run("systemctl", "disable", "--now", unit + ".timer")
        if "ActiveState=active" in run(
            "systemctl", "show", unit + ".timer", "--property=ActiveState"
        ):
            raise RuntimeError("The recovery watchdog remains active after release.")
        progress["watchdog_disabled"] = True
        save(state_path, state)
    if not progress.get("watchdog_service_settled"):
        service = dict(
            line.split("=", 1)
            for line in run(
                "systemctl",
                "show",
                unit + ".service",
                "--property=MainPID",
                "--property=LoadState",
                "--property=ActiveState",
            ).splitlines()
        )
        if service["MainPID"] != str(os.getpid()) and service["LoadState"] != "not-found":
            if service["ActiveState"] == "failed":
                run("systemctl", "reset-failed", unit + ".service")
            run("systemctl", "stop", unit + ".service")
        progress["watchdog_service_settled"] = True
        save(state_path, state)
    for suffix in (".timer", ".service"):
        (Path("/etc/systemd/system") / (unit + suffix)).unlink(missing_ok=True)
    (Path("/etc/systemd/system/timers.target.wants") / (unit + ".timer")).unlink(missing_ok=True)
    boot_links = Path("/etc/systemd/system/multi-user.target.wants")
    (boot_links / (unit + ".service")).unlink(missing_ok=True)
    if boot_links.exists():
        sync_path(boot_links)
    sync_path(Path("/etc/systemd/system"))
    run("systemctl", "daemon-reload")
    progress["watchdog_removed"] = True
    save(state_path, state)


def api_namespace_command(
    state: dict[str, Any], *arguments: str, input_text: str | None = None
) -> str:
    phase = state["verification_phase"]
    path = Path(f"/proc/{state['originals'][state['api']]['pid']}/ns/net")
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    try:
        target = f"/proc/self/fd/{descriptor}"
        if os.readlink(target) != phase["namespace"]:
            raise RecoveryRequired("The API network namespace changed during verification.")
        return run(
            "nsenter",
            "--net=" + target,
            *arguments,
            input_text=input_text,
            timeout=10,
            pass_fds=(descriptor,),
        )
    finally:
        os.close(descriptor)


def read_database(state: dict[str, Any], query: str) -> str:
    return run(
        "docker",
        "exec",
        "--env",
        "PGOPTIONS=-c default_transaction_read_only=on -c statement_timeout=5000",
        "-i",
        state["postgres"],
        "sh",
        "-c",
        'exec psql -X -At -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$1"',
        "census",
        state["database"],
        input_text=query,
        timeout=10,
    ).strip()


def require_no_api_database_clients(state: dict[str, Any]) -> None:
    if (
        read_database(
            state,
            "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND backend_type='client backend' AND pid<>pg_backend_pid();\n",
        )
        != "0"
    ):
        raise RuntimeError("Another database client exists during the physical API fence.")


def require_database_cluster(state: dict[str, Any]) -> None:
    if (
        read_database(state, "SELECT system_identifier FROM pg_control_system();\n")
        != state["system_identifier"]
    ):
        raise RecoveryRequired("The durable resume proof belongs to another live database cluster.")


def require_verification_cpu_hold(state: dict[str, Any], *, api_thawed: bool) -> None:
    for observed in original_containers(state):
        identifier = observed["Id"]
        original = state["originals"][identifier]
        if (
            observed["Image"] != state["image_id"]
            or observed["HostConfig"]["RestartPolicy"]["Name"] != "no"
        ):
            raise RecoveryRequired(
                "An original service lost its verification identity or restart hold."
            )
        if not observed["State"]["Running"]:
            if identifier == state["api"]:
                raise RecoveryRequired(
                    "The stopped API has no qualified namespace for health verification."
                )
            continue
        expected = ("0", "0") if identifier == state["api"] and api_thawed else ("1", "1")
        if (
            observed["State"]["Pid"] != original["pid"]
            or observed["State"]["StartedAt"] != original["started_at"]
            or read_freezer(original) != expected
        ):
            raise RecoveryRequired(
                "An original process left its controlled verification CPU state."
            )


def qualify_api_namespace(state: dict[str, Any], *, frozen: bool = True) -> dict[str, Any]:
    check_originals(state)
    if frozen:
        require_verification_cpu_hold(state, api_thawed=False)
    observed = inspect_container(state["api"])
    original = state["originals"][state["api"]]
    if not observed["State"]["Running"] or observed["State"]["Pid"] != original["pid"]:
        raise RecoveryRequired("The original API must remain running and frozen for verification.")
    namespace = os.readlink(f"/proc/{original['pid']}/ns/net")
    if (
        namespace == os.readlink("/proc/self/ns/net")
        or observed["HostConfig"]["NetworkMode"] == "host"
    ):
        raise RecoveryRequired("The API requires its own non-host network namespace.")
    environment = dict(item.split("=", 1) for item in observed["Config"]["Env"] if "=" in item)
    url = urlsplit(environment["LAUNCHPLANE_DATABASE_URL"])
    if (
        not url.hostname
        or url.hostname == "localhost"
        or {"host", "hostaddr", "service"} & parse_qs(url.query, keep_blank_values=True).keys()
    ):
        raise RecoveryRequired(
            "The database connection has an unqualified local or service override."
        )
    try:
        address = ipaddress.ip_address(url.hostname)
    except ValueError:
        address = None
    if address is not None and (address.is_loopback or address.is_unspecified):
        raise RecoveryRequired("The API database must use a non-loopback network connection.")
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        proxy = urlsplit(environment.get(key, ""))
        if proxy.hostname in {"localhost", "127.0.0.1", "::1"} or environment.get(
            key, ""
        ).startswith("unix:"):
            raise RecoveryRequired(
                "A local API proxy transport is not qualified for the network fence."
            )
    status = dict(
        line.split(":", 1)
        for line in Path(f"/proc/{original['pid']}/status").read_text().splitlines()
        if ":" in line
    )
    if int(status["CapBnd"].strip(), 16) & ((1 << 12) | (1 << 21)):
        raise RecoveryRequired("The API can change its own network fence.")
    if any(
        "sock" in str(item.get("Destination", "")) or item.get("Type") == "bind"
        for item in observed["Mounts"]
    ):
        raise RecoveryRequired(
            "API bind/socket mounts are not qualified for the network-fenced probe."
        )
    for identifier in run("docker", "ps", "-q", "--no-trunc").split():
        if identifier == state["api"]:
            continue
        peer_pid = int(run("docker", "inspect", "--format", "{{.State.Pid}}", identifier).strip())
        if peer_pid > 0 and os.readlink(f"/proc/{peer_pid}/ns/net") == namespace:
            raise RecoveryRequired("Another container shares the API network namespace.")
    phase: dict[str, Any] = {
        "phase_id": uuid4().hex,
        "namespace": namespace,
        "active": True,
        "health_passed": False,
    }
    phase["table"] = "lpmaint_" + phase["phase_id"]
    observed_state = {**state, "verification_phase": phase}
    api_namespace_command(observed_state, "nft", "-j", "list", "tables")
    if api_namespace_command(observed_state, "ss", "-Hlx").strip():
        raise RecoveryRequired("An API Unix listener could bypass the network fence.")
    return phase


def require_api_egress_fence(state: dict[str, Any]) -> None:
    phase = state["verification_phase"]
    table = phase["table"]
    if not re.fullmatch(r"lpmaint_[a-f0-9]{32}", table):
        raise RecoveryRequired("The API fence does not have exact maintenance custody.")
    document = json.loads(api_namespace_command(state, "nft", "-j", "list", "table", "inet", table))
    chains = {
        item["chain"]["name"]: item["chain"] for item in document["nftables"] if "chain" in item
    }
    if set(chains) != set(API_NETWORK_HOOKS) or any(
        chains[name].get("hook") != name
        or chains[name].get("type") != "filter"
        or chains[name].get("prio") != API_EGRESS_PRIORITY
        for name in chains
    ):
        raise RecoveryRequired("The API fence hook readback disagrees.")
    rules = [item["rule"] for item in document["nftables"] if "rule" in item]
    for name in chains:
        selected = [item for item in rules if item.get("chain") == name]
        if len(selected) != 1:
            raise RecoveryRequired("The API fence rule inventory disagrees.")
        expressions = selected[0]["expr"]
        if (
            len(expressions) != 3
            or expressions[0].get("match")
            != {
                "op": "!=",
                "left": {"meta": {"key": "iifname" if name == "input" else "oifname"}},
                "right": "lo",
            }
            or "counter" not in expressions[1]
            or expressions[2] != {"drop": None}
        ):
            raise RecoveryRequired("The API non-loopback drop is not verified.")


def cleanup_api_probe(state: dict[str, Any], *, phase_id: str | None = None) -> None:
    phase = state.get("verification_phase", {})
    if not phase.get("active") or phase_id is not None and phase.get("phase_id") != phase_id:
        return
    observed = inspect_container(state["api"])
    if observed["State"]["Running"]:
        original = state["originals"][state["api"]]
        if (
            observed["State"]["Pid"] != original["pid"]
            or observed["State"]["StartedAt"] != original["started_at"]
        ):
            raise RecoveryRequired("The API process changed before network-fence cleanup.")
        set_freezer(original, True)
        api_namespace_command(state, "ss", "-Knt", "state", "connected")
        remaining = api_namespace_command(state, "ss", "-Hnt", "state", "connected").splitlines()
        if any(line.split()[0] != "TIME-WAIT" for line in remaining):
            raise RuntimeError("Buffered API TCP connections remain before network unfence.")
        tables = json.loads(api_namespace_command(state, "nft", "-j", "list", "tables"))["nftables"]
        if any(item.get("table", {}).get("name") == phase["table"] for item in tables):
            require_api_egress_fence(state)
            api_namespace_command(state, "nft", "delete", "table", "inet", phase["table"])
        tables = json.loads(api_namespace_command(state, "nft", "-j", "list", "tables"))["nftables"]
        if any(item.get("table", {}).get("name") == phase["table"] for item in tables):
            raise RecoveryRequired("The owned API network fence remains after cleanup.")
        if read_freezer(original) != ("1", "1"):
            raise RecoveryRequired("The API left its physical hold during network unfence.")
    require_no_api_database_clients(state)
    phase["active"] = False
    phase["cleaned"] = True
    save(Path(state["job_directory"]) / "state.json", state)


def verify_api_health(state_path: Path) -> None:
    phase_id = None
    process = None
    try:
        with locked_state(state_path) as state:
            receipt_path = state_path.parent / "verification-complete.json"
            if receipt_path.exists():
                receipt = json.loads(receipt_path.read_text())
                if all(
                    receipt.get(key) == state[key]
                    for key in ("hold_id", "system_identifier", "image_id", "revision")
                ) and receipt.get("health_passed"):
                    return
                raise RecoveryRequired(
                    "The existing API verification receipt has different custody."
                )
            if state.get("released") or state.get("resume_started"):
                raise RecoveryRequired("Original service recovery already began.")
            cleanup_api_probe(state)
            if state.get("verification_phase"):
                remove_watchdog(state, health=True)
            phase = qualify_api_namespace(state)
            phase_id = phase["phase_id"]
            phase["deadline"] = (
                datetime.now(timezone.utc) + timedelta(seconds=HEALTH_REFREEZE_SECONDS)
            ).isoformat()
            state["verification_phase"] = phase
            save(state_path, state)
            require_no_api_database_clients(state)
            if api_namespace_command(state, "ss", "-Hlx").strip():
                raise RecoveryRequired("An API Unix listener could bypass the network fence.")
            table = state["verification_phase"]["table"]
            rules = "add table inet " + table + "\n"
            for hook in API_NETWORK_HOOKS:
                rules += (
                    "add chain inet "
                    + table
                    + " "
                    + hook
                    + " { type filter hook "
                    + hook
                    + " priority "
                    + str(API_EGRESS_PRIORITY)
                    + "; policy accept; }\n"
                )
                interface = "iifname" if hook == "input" else "oifname"
                rules += (
                    "add rule inet "
                    + table
                    + " "
                    + hook
                    + " "
                    + interface
                    + ' != "lo" counter drop\n'
                )
            api_namespace_command(state, "nft", "-f", "-", input_text=rules)
            require_api_egress_fence(state)
            install_watchdog(state_path, state, health=True)
            api_namespace_command(state, "ss", "-Knt", "state", "connected")
            connected = api_namespace_command(state, "ss", "-Hnt", "state", "connected")
            if any(line.split()[0] != "TIME-WAIT" for line in connected.splitlines()):
                raise RuntimeError("Inbound API connections remain before controlled thaw.")
            if (
                datetime.fromisoformat(state["verification_phase"]["deadline"])
                - datetime.now(timezone.utc)
            ).total_seconds() < HEALTH_PROBE_SECONDS:
                raise RuntimeError(
                    "The independent API refreeze deadline has insufficient headroom."
                )
            set_freezer(state["originals"][state["api"]], False)
        process = start_process(
            [
                "docker",
                "exec",
                state["api"],
                "python",
                "-c",
                "import json,os,urllib.request\n"
                "port=int(os.environ.get('LAUNCHPLANE_SERVICE_PORT','8080'))\n"
                "with urllib.request.urlopen(f'http://127.0.0.1:{port}/v1/health?maintenance_liveness=true',timeout=5) as response:\n"
                " payload=json.load(response)\n"
                " if response.status!=200 or payload.get('status')!='ok':raise SystemExit(1)\n",
            ]
        )
        deadline = time.monotonic() + HEALTH_PROBE_SECONDS
        while time.monotonic() < deadline:
            with locked_state(state_path) as state:
                phase = state["verification_phase"]
                if phase["phase_id"] != phase_id:
                    raise RuntimeError("Another API phase owns health verification.")
                if not phase["active"]:
                    raise RuntimeError("The controlled API thaw ended before health readback.")
                require_verification_cpu_hold(state, api_thawed=True)
                require_api_egress_fence(state)
                require_no_api_database_clients(state)
            if process.poll() is not None:
                if process.returncode:
                    raise RuntimeError("The original API health command failed.")
                run(
                    "docker",
                    "exec",
                    state["api"],
                    "python",
                    "-c",
                    "import os,socket;from urllib.parse import urlsplit;u=urlsplit(os.environ['LAUNCHPLANE_DATABASE_URL'])\n"
                    "try:\n s=socket.create_connection((u.hostname,u.port or 5432),timeout=1)\n"
                    "except OSError:raise SystemExit(0)\n"
                    "else:s.close();raise SystemExit(1)",
                    timeout=5,
                )
                with locked_state(state_path) as state:
                    phase = state["verification_phase"]
                    if phase["phase_id"] != phase_id:
                        raise RuntimeError("Another API phase owns blocked-egress readback.")
                    if not phase["active"]:
                        raise RuntimeError("The API phase ended before blocked-egress readback.")
                    require_api_egress_fence(state)
                    require_no_api_database_clients(state)
                    document = json.loads(
                        api_namespace_command(
                            state, "nft", "-j", "list", "table", "inet", phase["table"]
                        )
                    )
                    dropped = sum(
                        expression["counter"]["packets"]
                        for item in document["nftables"]
                        if "rule" in item and item["rule"].get("chain") in {"output", "postrouting"}
                        for expression in item["rule"]["expr"]
                        if "counter" in expression
                    )
                    if dropped <= 0:
                        raise RecoveryRequired("No actual non-loopback packet drop was observed.")
                    phase["health_passed"] = True
                    save(state_path, state)
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("The controlled API health check exceeded its deadline.")
    finally:
        if phase_id is not None:
            with locked_state(state_path) as state:
                cleanup_api_probe(state, phase_id=phase_id)
                if state.get("verification_phase", {}).get("phase_id") == phase_id:
                    remove_watchdog(state, health=True)
        if process is not None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            if process.stdout:
                process.stdout.close()
            if process.stdin:
                process.stdin.close()
    with locked_state(state_path) as state:
        phase = state["verification_phase"]
        if (
            phase["phase_id"] != phase_id
            or not phase["health_passed"]
            or not phase["cleaned"]
            or phase["active"]
        ):
            raise RecoveryRequired("The actual API verification did not complete under its fence.")
        save(
            state_path.parent / "verification-complete.json",
            {
                **{
                    key: state[key]
                    for key in ("hold_id", "system_identifier", "image_id", "revision")
                },
                "phase_id": phase["phase_id"],
                "health_passed": True,
            },
        )


def complete_service_verification(state_path: Path) -> None:
    with locked_state(state_path) as state:
        if state.get("released") or state.get("safe_to_resume"):
            return
        if not (state_path.parent / "commit-pending.json").exists():
            return
        state["releasing"] = True
        (state_path.parent / "releasing").touch(mode=0o600)
        save(state_path, state)
        cleanup_api_probe(state)
        terminate_keeper(state)
        for observed in original_containers(state):
            if observed["HostConfig"]["RestartPolicy"]["Name"] != "no":
                raise RecoveryRequired("An unverified service regained automatic restart.")
            if observed["State"]["Running"] and read_freezer(
                state["originals"][observed["Id"]]
            ) != ("1", "1"):
                raise RecoveryRequired("An unverified service is no longer frozen.")
        proof = operator(state, "verify")
        state["revision"] = proof["revision"]
        save(state_path, state)
    if proof.get("outcome") != "before":
        verify_api_health(state_path)


def release(state_path: Path) -> None:
    try:
        finish_release(state_path)
    except Exception as error:
        with locked_state(state_path) as state:
            record_release_failure(state, error)
        raise


def finish_release(state_path: Path) -> None:
    complete_service_verification(state_path)
    with locked_state(state_path) as state:
        if state.get("released"):
            remove_watchdog(state)
            emit_release(state)
            return
        state["releasing"] = True
        (state_path.parent / "releasing").touch(mode=0o600)
        save(state_path, state)
        pending = (state_path.parent / "commit-pending.json").exists()
        try:
            cleanup_api_probe(state)
            terminate_keeper(state)
            safe = state.get("safe_to_resume")
            if safe and any(
                safe.get(key) != state[key] for key in ("hold_id", "system_identifier", "image_id")
            ):
                raise RecoveryRequired(
                    "The durable resume proof belongs to another maintenance runtime."
                )
            if safe:
                require_database_cluster(state)
            if pending and not safe:
                for observed in original_containers(state):
                    if observed["HostConfig"]["RestartPolicy"]["Name"] != "no":
                        raise RecoveryRequired("An unverified service regained automatic restart.")
                    if observed["State"]["Running"] and read_freezer(
                        state["originals"][observed["Id"]]
                    ) != ("1", "1"):
                        raise RecoveryRequired("An unverified service is no longer frozen.")
                proof = operator(state, "verify")
                state["revision"] = proof["revision"]
                receipt_path = state_path.parent / "verification-complete.json"
                if proof.get("outcome") != "before":
                    if not receipt_path.exists():
                        raise RecoveryRequired(
                            "Actual runtime and API health verification is incomplete."
                        )
                    verified = json.loads(receipt_path.read_text())
                    if any(
                        verified.get(key) != state[key]
                        for key in ("hold_id", "system_identifier", "image_id", "revision")
                    ) or not verified.get("health_passed"):
                        raise RecoveryRequired(
                            "The completed runtime verification belongs to another state."
                        )
                state["safe_to_resume"] = {
                    key: state[key]
                    for key in ("hold_id", "system_identifier", "image_id", "revision")
                }
                save(state_path, state)
                save(state_path.parent / "database-safe.json", state["safe_to_resume"])
        except Exception as error:
            if pending:
                raise
            state["keeper_termination_unverified"] = type(error).__name__
            save(state_path, state)
        if state.get("verification_phase"):
            remove_watchdog(state, health=True)
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


def record_release_failure(state: dict[str, Any], error: Exception) -> None:
    if isinstance(error, RecoveryRequired):
        state["recovery_required"] = error.event
        save(Path(state["job_directory"]) / "state.json", state)
    print(
        json.dumps(
            {
                "event": error.event
                if isinstance(error, RecoveryRequired)
                else RELEASE_INCOMPLETE_EVENT,
                "hold_id": state["hold_id"],
                "writers_held": False if state.get("resumed") else None,
                "resume_started": state.get("resume_started", False),
                "error_type": type(error).__name__,
                "revision": state.get("revision"),
            }
        ),
        flush=True,
    )


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


def complete_released_cleanup(state_path: Path) -> None:
    with locked_state(state_path, wait_seconds=RECOVERY_WAIT_SECONDS) as observed:
        if not observed.get("released"):
            raise RuntimeError("The original recovery service has no completed release readback.")
        state = dict(observed)
    for health in (True, False):
        progress = state.get("verification_phase", {}) if health else state
        if not progress or progress.get("watchdog_removed"):
            continue
        if health and progress.get("active"):
            raise RecoveryRequired("Released state still has an active verification phase.")
        unit = "lp-maint-" + state["hold_id"] + ("-health" if health else "")
        for suffix in (".timer", ".service"):
            name = unit + suffix
            try:
                if "LoadState=not-found" not in run(
                    "systemctl", "show", name, "--property=LoadState"
                ):
                    run("systemctl", "stop", name)
            except RuntimeError:
                if "LoadState=not-found" not in run(
                    "systemctl", "show", name, "--property=LoadState"
                ):
                    raise
    with locked_state(state_path, wait_seconds=RECOVERY_WAIT_SECONDS) as state:
        if not state.get("released"):
            raise RecoveryRequired("The completed release proof changed during metadata cleanup.")
        if state.get("verification_phase"):
            remove_watchdog(state, health=True)
        remove_watchdog(state)
        emit_release(state)


def request_release(state_path: Path, *, manual: bool = False) -> None:
    state = json.loads(state_path.read_text())
    if state.get("released"):
        complete_released_cleanup(state_path)
        return
    unit = "lp-maint-" + state["hold_id"] + ".service"
    if manual:
        with locked_state(state_path, wait_seconds=RECOVERY_WAIT_SECONDS) as state:
            state["automatic_attempts"] = 0
    for attempt in range(2 if manual else 1):
        try:
            if manual:
                run("systemctl", "reset-failed", unit)
            run("systemctl", "start", unit, timeout=RECOVERY_WAIT_SECONDS)
        except RuntimeError as error:
            state = json.loads(state_path.read_text())
            if state.get("released"):
                complete_released_cleanup(state_path)
                return
            status = run("systemctl", "show", unit, "--property=ExecMainStatus", "--value").strip()
            if status == str(AUTOMATIC_RECOVERY_EXHAUSTED_EXIT):
                if manual and attempt == 0:
                    continue
                raise AutomaticRecoveryExhausted(
                    "Automatic recovery is exhausted; use explicit manual release."
                ) from error
            if status == str(RECOVERY_REQUIRED_EXIT):
                raise RecoveryRequired(
                    "The original recovery service requires reconciliation.",
                    event=state.get("recovery_required", VERIFICATION_BLOCKED_EVENT),
                ) from error
            raise
        complete_released_cleanup(state_path)
        return


def consume_automatic_attempt(state: dict[str, Any], progress: dict[str, Any]) -> None:
    attempts = progress.get("automatic_attempts", 0)
    if attempts >= WATCHDOG_ATTEMPTS:
        print(
            json.dumps(
                {
                    "event": AUTOMATIC_RECOVERY_EXHAUSTED_EVENT,
                    "hold_id": state["hold_id"],
                    "phase_id": progress.get("phase_id"),
                    "resume_started": state.get("resume_started", False),
                }
            ),
            flush=True,
        )
        raise AutomaticRecoveryExhausted("The durable automatic recovery budget is exhausted.")
    progress["automatic_attempts"] = attempts + 1
    save(Path(state["job_directory"]) / "state.json", state)


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
        "database": unquote(urlsplit(environment["LAUNCHPLANE_DATABASE_URL"]).path.lstrip("/")),
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
        set(state["containers"]) | {state["browser"], state["postgres"]},
        project=state["project"],
        operators=set(),
    )
    qualify_api_namespace(state, frozen=False)
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
                            print(line.decode(), flush=True)
                            return
                        print(line.decode(), flush=True)
    finally:
        try:
            request_release(state_path)
        finally:
            if commands is not None:
                os.close(commands)
            if keeper and keeper.stdin:
                keeper.stdin.close()


def dump(state_path: Path) -> None:
    with locked_state(state_path, active=True) as state:
        if not state.get("held"):
            raise RuntimeError("The held snapshot is not available.")
        if state.get("dump_started"):
            raise RuntimeError(
                "This hold already attempted its dump; release it and start a new hold."
            )
        state["dump_started"] = True
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
            "--env",
            "PGAPPNAME=" + dump_application_name(state["hold_id"]),
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Bounded full writer hold of Launchplane only.")
    parser.add_argument(
        "mode",
        choices=("plan", "hold", "release", "dump", "submit", "verify", "refreeze", "recover"),
    )
    parser.add_argument("--state", type=Path)
    parser.add_argument("--receipt-file", type=Path)
    parser.add_argument("--api-container")
    parser.add_argument("--postgres-container")
    parser.add_argument("--duration-seconds", type=int, default=1800)
    parser.add_argument("--state-root", default="/var/lib/launchplane-maintenance")
    parser.add_argument("--phase-id", default="")
    arguments = parser.parse_args()
    for number in (signal.SIGHUP, signal.SIGTERM):
        signal.signal(number, interrupted)
    if arguments.mode in {"dump", "submit", "release", "verify", "refreeze", "recover"}:
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
            try:
                if arguments.mode == "refreeze":
                    if not arguments.phase_id:
                        parser.error("refreeze requires the exact phase id.")
                    with locked_state(state_path) as state:
                        phase = state.get("verification_phase", {})
                        if phase.get("active") and phase.get("phase_id") == arguments.phase_id:
                            consume_automatic_attempt(state, phase)
                        cleanup_api_probe(state, phase_id=arguments.phase_id)
                elif arguments.mode == "recover":
                    with locked_state(state_path) as state:
                        if not state.get("released"):
                            consume_automatic_attempt(state, state)
                    unit = "lp-maint-" + state["hold_id"] + ".service"
                    if run(
                        "systemctl", "show", unit, "--property=MainPID", "--value"
                    ).strip() != str(os.getpid()):
                        raise RuntimeError(
                            "Only the original systemd service owns recovery execution."
                        )
                    release(state_path)
                else:
                    request_release(state_path, manual=True)
            except AutomaticRecoveryExhausted:
                return AUTOMATIC_RECOVERY_EXHAUSTED_EXIT
            except RecoveryRequired:
                return RECOVERY_REQUIRED_EXIT
        return 0
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
        try:
            hold(state_path)
        except AutomaticRecoveryExhausted:
            return AUTOMATIC_RECOVERY_EXHAUSTED_EXIT
        except RecoveryRequired:
            return RECOVERY_REQUIRED_EXIT
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
