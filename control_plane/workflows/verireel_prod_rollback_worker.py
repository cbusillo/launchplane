from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import subprocess
import sys

import click

from control_plane.workflows.verireel_prod_rollback import (
    VeriReelProdRollbackWorkerRequest,
    VeriReelProdRollbackWorkerResult,
)
from control_plane.workflows.ship import utc_now_timestamp


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
            "Missing explicit SSH material for VeriReel prod rollback worker."
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
        "sudo",
        "-n",
        *command_args,
    ]


def _run_proxmox_command(command_args: list[str], *, timeout_seconds: int) -> None:
    if _env_flag("VERIREEL_PROD_GATE_LOCAL", default=False):
        command = _build_proxmox_command(command_args)
    else:
        with TemporaryDirectory(prefix="launchplane-verireel-rollback-") as material_dir_name:
            identity_file, known_hosts_file = _write_ssh_material(
                material_dir=Path(material_dir_name)
            )
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
                return
            detail = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
            raise click.ClickException(detail)
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=max(timeout_seconds, 1),
        check=False,
    )
    if completed.returncode == 0:
        return
    detail = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
    raise click.ClickException(detail)


def execute_worker(request: VeriReelProdRollbackWorkerRequest) -> VeriReelProdRollbackWorkerResult:
    ctid = _required_env("VERIREEL_PROD_CT_ID")
    started_at = utc_now_timestamp()
    _run_proxmox_command(
        ["pct", "rollback", ctid, request.snapshot_name],
        timeout_seconds=request.timeout_seconds,
    )
    if request.start_after_rollback:
        _run_proxmox_command(
            ["pct", "start", ctid],
            timeout_seconds=request.timeout_seconds,
        )
    finished_at = utc_now_timestamp()
    return VeriReelProdRollbackWorkerResult(
        status="pass",
        snapshot_name=request.snapshot_name,
        started_at=started_at,
        finished_at=finished_at,
        detail=f"Rolled back prod CT {ctid} to snapshot {request.snapshot_name}.",
    )


def main() -> None:
    try:
        payload = json.load(sys.stdin)
        request = VeriReelProdRollbackWorkerRequest.model_validate(payload)
        result = execute_worker(request)
        sys.stdout.write(f"{result.model_dump_json()}\n")
    except Exception as exc:  # noqa: BLE001
        message = str(exc)
        failure = VeriReelProdRollbackWorkerResult(
            status="fail",
            snapshot_name=str(getattr(locals().get("request", None), "snapshot_name", "") or ""),
            started_at="",
            finished_at=utc_now_timestamp(),
            detail=message,
        )
        sys.stdout.write(f"{failure.model_dump_json()}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
