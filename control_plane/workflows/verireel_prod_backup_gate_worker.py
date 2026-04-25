from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import subprocess
import sys
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import click

from control_plane.workflows.ship import utc_now_timestamp
from control_plane.workflows.verireel_prod_backup_gate import (
    VeriReelProdBackupGateWorkerRequest,
    VeriReelProdBackupGateWorkerResult,
)


DEFAULT_HEALTH_TIMEOUT_SECONDS = 10
DEFAULT_HEALTH_TIMEOUT_MS = DEFAULT_HEALTH_TIMEOUT_SECONDS * 1000
DEFAULT_SNAPSHOT_PREFIX = "ver-predeploy"
DEFAULT_BACKUP_MODE = "both"
LEGACY_SNAPSHOT_PREFIX = "verireel-predeploy"
SSH_PRIVATE_KEY_ENV_VAR = "VERIREEL_PROD_PROXMOX_SSH_PRIVATE_KEY"
SSH_KNOWN_HOSTS_ENV_VAR = "VERIREEL_PROD_PROXMOX_SSH_KNOWN_HOSTS"


def _env_flag(name: str, default: bool = False) -> bool:
    raw_value = os.environ.get(name, "").strip().lower()
    if not raw_value:
        return default
    if raw_value in {"1", "true", "yes", "on"}:
        return True
    if raw_value in {"0", "false", "no", "off"}:
        return False
    raise click.ClickException(f"Invalid boolean value for {name}: {raw_value}")


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise click.ClickException(f"Missing required env var: {name}")
    return value


def _optional_env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _parse_positive_int(name: str, default: int) -> int:
    raw_value = _optional_env(name, str(default))
    if not raw_value.isdigit():
        raise click.ClickException(f"Invalid {name} value: {raw_value}")
    parsed = int(raw_value)
    if parsed <= 0:
        raise click.ClickException(f"Invalid {name} value: {raw_value}")
    return parsed


def _parse_non_negative_int(name: str, default: int) -> int:
    raw_value = _optional_env(name, str(default))
    if raw_value.startswith("-") or not raw_value.lstrip("+").isdigit():
        raise click.ClickException(f"Invalid {name} value: {raw_value}")
    parsed = int(raw_value)
    if parsed < 0:
        raise click.ClickException(f"Invalid {name} value: {raw_value}")
    return parsed


def _normalize_backup_modes(raw_value: str) -> tuple[str, ...]:
    tokens = {
        token.strip().lower()
        for token in raw_value.replace(":", ",").split(",")
        if token.strip()
    }
    if "none" in tokens:
        return ()
    if not tokens:
        return ("snapshot",)
    if "both" in tokens:
        return ("snapshot", "vzdump")
    unsupported = tokens.difference({"snapshot", "vzdump"})
    if unsupported:
        raise click.ClickException(
            f"Unsupported VERIREEL_PROD_BACKUP_MODE value: {sorted(unsupported)[0]}"
        )
    return tuple(sorted(tokens))


def _format_snapshot_name(prefix: str) -> str:
    timestamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    entropy = uuid.uuid4().hex[:6]
    return f"{prefix}-{timestamp}-{entropy}"


def _snapshot_timestamp(snapshot_name: str, prefixes: tuple[str, ...]) -> tuple[str, int]:
    matched_prefix = next(
        (prefix for prefix in prefixes if snapshot_name.startswith(f"{prefix}-")),
        "",
    )
    if not matched_prefix:
        return ("", 1)
    suffix = snapshot_name[len(matched_prefix) + 1 :]
    parts = suffix.split("-", 2)
    if len(parts) < 2:
        return ("", 0)
    timestamp = f"{parts[0]}{parts[1]}"
    if len(parts[0]) != 8 or len(parts[1]) != 6 or not timestamp.isdigit():
        return ("", 0)
    return (timestamp, -1)


def _sorted_snapshots_for_retention(snapshot_names: list[str], prefix: str) -> list[str]:
    prefixes = (prefix, LEGACY_SNAPSHOT_PREFIX)
    return [
        item[0]
        for item in sorted(
            ((name, *_snapshot_timestamp(name, prefixes)) for name in snapshot_names),
            key=lambda item: (item[1], item[2], item[0]),
        )
    ]


def _parse_snapshot_names(output: str) -> list[str]:
    names: list[str] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or line.lower().startswith("snapshot"):
            continue
        parts = line.split()
        if parts:
            names.append(parts[0])
    return names


def _write_ssh_material(*, material_dir: Path) -> tuple[str, str]:
    private_key = _required_env(SSH_PRIVATE_KEY_ENV_VAR)
    known_hosts = _required_env(SSH_KNOWN_HOSTS_ENV_VAR)
    identity_file = material_dir / "proxmox-worker-key"
    known_hosts_file = material_dir / "known_hosts"
    identity_file.write_text(f"{private_key.rstrip()}\n", encoding="utf-8")
    known_hosts_file.write_text(f"{known_hosts.rstrip()}\n", encoding="utf-8")
    identity_file.chmod(0o600)
    known_hosts_file.chmod(0o600)
    return str(identity_file), str(known_hosts_file)


def _build_proxmox_command(
    command_args: list[str],
    *,
    identity_file: str = "",
    known_hosts_file: str = "",
) -> list[str]:
    if _env_flag("VERIREEL_PROD_GATE_LOCAL", default=False):
        return ["sudo", "-n", *command_args]
    host = _required_env("VERIREEL_PROD_PROXMOX_HOST")
    user = _required_env("VERIREEL_PROD_PROXMOX_USER")
    if not identity_file or not known_hosts_file:
        raise click.ClickException(
            "Missing explicit SSH material for VeriReel prod backup gate worker."
        )
    return [
        "ssh",
        "-F",
        "/dev/null",
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={known_hosts_file}",
        "-i",
        identity_file,
        f"{user}@{host}",
        *command_args,
    ]


def _run_proxmox_command(
    command_args: list[str],
    *,
    timeout_seconds: int,
    capture_output: bool = False,
) -> str:
    if _env_flag("VERIREEL_PROD_GATE_LOCAL", default=False):
        command = _build_proxmox_command(command_args)
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=max(timeout_seconds, 1),
            check=False,
        )
        if completed.returncode == 0:
            return completed.stdout.strip() if capture_output else ""
        detail = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
        raise click.ClickException(detail)

    with TemporaryDirectory(prefix="launchplane-verireel-backup-") as material_dir_name:
        identity_file, known_hosts_file = _write_ssh_material(material_dir=Path(material_dir_name))
        command = _build_proxmox_command(
            command_args,
            identity_file=identity_file,
            known_hosts_file=known_hosts_file,
        )
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=max(timeout_seconds, 1),
            check=False,
        )
        if completed.returncode == 0:
            return completed.stdout.strip() if capture_output else ""
        detail = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
        raise click.ClickException(detail)


def _fetch_health_summary(base_url: str) -> dict[str, str]:
    if not base_url:
        return {}
    url = f"{base_url.rstrip('/')}/api/health"
    request = Request(url, headers={"accept": "application/json"})
    timeout_ms = _parse_positive_int(
        "VERIREEL_PROD_GATE_HEALTH_TIMEOUT_MS",
        DEFAULT_HEALTH_TIMEOUT_MS,
    )
    try:
        with urlopen(request, timeout=timeout_ms / 1000) as response:
            if response.status < 200 or response.status >= 300:
                return {"base_url": base_url}
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, ValueError):
        return {"base_url": base_url}
    summary = {"base_url": base_url}
    build_revision = str(payload.get("buildRevision") or "").strip()
    build_tag = str(payload.get("buildTag") or "").strip()
    if build_revision:
        summary["build_revision"] = build_revision
    if build_tag:
        summary["build_tag"] = build_tag
    return summary


def _prune_snapshots(*, ctid: str, prefix: str, keep: int, timeout_seconds: int, preserve_name: str) -> None:
    if keep < 0:
        return
    output = _run_proxmox_command(
        ["pct", "listsnapshot", ctid],
        timeout_seconds=timeout_seconds,
        capture_output=True,
    )
    names = [
        name
        for name in _sorted_snapshots_for_retention(_parse_snapshot_names(output), prefix)
        if name.startswith(f"{prefix}-") or name.startswith(f"{LEGACY_SNAPSHOT_PREFIX}-")
    ]
    if len(names) <= keep:
        return
    removable_names = [name for name in names if name != preserve_name]
    delete_count = max(len(names) - keep, 0)
    for name in removable_names[:delete_count]:
        _run_proxmox_command(
            ["pct", "delsnapshot", ctid, name],
            timeout_seconds=timeout_seconds,
        )


def execute_worker(request: VeriReelProdBackupGateWorkerRequest) -> VeriReelProdBackupGateWorkerResult:
    ctid = _required_env("VERIREEL_PROD_CT_ID")
    backup_mode = _normalize_backup_modes(_optional_env("VERIREEL_PROD_BACKUP_MODE", DEFAULT_BACKUP_MODE))
    storage = _optional_env("VERIREEL_PROD_BACKUP_STORAGE")
    if "vzdump" in backup_mode and not storage:
        raise click.ClickException(
            "VERIREEL_PROD_BACKUP_STORAGE is required when running backup with VERIREEL_PROD_BACKUP_MODE including vzdump."
        )
    snapshot_prefix = _optional_env("VERIREEL_PROD_SNAPSHOT_PREFIX", DEFAULT_SNAPSHOT_PREFIX)
    snapshot_keep = _parse_non_negative_int("VERIREEL_PROD_SNAPSHOT_KEEP", 5)
    testing_base_url = _optional_env("VERIREEL_TESTING_BASE_URL", "https://ver-testing.shinycomputers.com")
    prod_base_url = _optional_env("VERIREEL_PROD_OPERATOR_BASE_URL", "https://ver-prod.shinycomputers.com")

    started_at = utc_now_timestamp()
    snapshot_name = _format_snapshot_name(snapshot_prefix) if "snapshot" in backup_mode else ""

    if "snapshot" in backup_mode:
        _run_proxmox_command(
            ["pct", "snapshot", ctid, snapshot_name],
            timeout_seconds=request.timeout_seconds,
        )
        _prune_snapshots(
            ctid=ctid,
            prefix=snapshot_prefix,
            keep=snapshot_keep,
            timeout_seconds=request.timeout_seconds,
            preserve_name=snapshot_name,
        )

    if "vzdump" in backup_mode:
        _run_proxmox_command(
            ["vzdump", ctid, "--mode", "snapshot", "--storage", storage],
            timeout_seconds=request.timeout_seconds,
        )

    testing_health = _fetch_health_summary(testing_base_url)
    prod_health = _fetch_health_summary(prod_base_url)
    finished_at = utc_now_timestamp()
    evidence = {
        "backup_mode": ",".join(backup_mode),
        "snapshot_name": snapshot_name,
        "proxmox_host": _required_env("VERIREEL_PROD_PROXMOX_HOST"),
        "proxmox_user": _required_env("VERIREEL_PROD_PROXMOX_USER"),
        "proxmox_ctid": ctid,
        "snapshot_prefix": snapshot_prefix,
        "snapshot_keep": str(snapshot_keep),
        "testing_base_url": testing_health.get("base_url", testing_base_url),
        "current_prod_base_url": prod_health.get("base_url", prod_base_url),
    }
    if storage:
        evidence["proxmox_storage"] = storage
    if testing_health.get("build_revision"):
        evidence["testing_build_revision"] = testing_health["build_revision"]
    if testing_health.get("build_tag"):
        evidence["testing_build_tag"] = testing_health["build_tag"]
    if prod_health.get("build_revision"):
        evidence["current_prod_build_revision"] = prod_health["build_revision"]
    if prod_health.get("build_tag"):
        evidence["current_prod_build_tag"] = prod_health["build_tag"]

    return VeriReelProdBackupGateWorkerResult(
        status="pass",
        snapshot_name=snapshot_name,
        started_at=started_at,
        finished_at=finished_at,
        detail=(
            f"Captured VeriReel prod backup gate for CT {ctid}"
            + (f" with snapshot {snapshot_name}." if snapshot_name else ".")
        ),
        evidence=evidence,
    )


def main() -> None:
    try:
        payload = json.load(sys.stdin)
        request = VeriReelProdBackupGateWorkerRequest.model_validate(payload)
        result = execute_worker(request)
        sys.stdout.write(f"{result.model_dump_json()}\n")
    except Exception as exc:  # noqa: BLE001
        message = str(exc)
        failure = VeriReelProdBackupGateWorkerResult(
            status="fail",
            snapshot_name="",
            started_at="",
            finished_at=utc_now_timestamp(),
            detail=message,
            evidence={},
        )
        sys.stdout.write(f"{failure.model_dump_json()}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
