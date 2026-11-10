import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any

ROOT = Path("/workspace")
OUT = ROOT / "results"
OUT.mkdir(exist_ok=True)
EXPORT = json.loads((ROOT / "programs.json").read_text())
RESOURCE = EXPORT["resources"]
PROJECT = RESOURCE["namespace"]
IMAGE = EXPORT["image"]
VOLUMES = EXPORT["volumes"]
PROOF: dict[str, Any] = {
    "scope": "real Docker engine in disposable QEMU guest; fabricated resources only"
}


def run(*args: str) -> str:
    completed = subprocess.run(args, text=True, capture_output=True, timeout=120)
    if completed.returncode:
        raise RuntimeError(f"{args[:3]} failed: {completed.stderr}")
    return completed.stdout.strip()


def host(program: str, spec: dict[str, Any]) -> dict[str, Any]:
    result = run(
        sys.executable,
        "-c",
        EXPORT[program],
        base64.b64encode(json.dumps(spec).encode()).decode(),
        "FRAME:",
    )
    frames = [
        json.loads(line.removeprefix("FRAME:"))
        for line in result.splitlines()
        if line.startswith("FRAME:")
    ]
    assert len(frames) == 1, result
    frame: dict[str, Any] = frames[0]
    return frame


def inspect() -> dict[str, dict[str, Any]]:
    result = host(
        "inspection",
        {
            "resources": RESOURCE,
            "project": PROJECT,
            "generation": None,
            "identity": EXPORT["identity"],
            "failed_candidate": EXPORT["failed_candidate"],
        },
    )
    assert result["type"] == "complete", result
    return {row["role"]: row for row in result["volumes"]}


def sweep(row: dict[str, Any], mode: str, **extra: Any) -> dict[str, Any]:
    result = host(
        "sweep",
        {
            "mode": mode,
            "volume": row["name"],
            "identity": row["identity"],
            "engine_identity": row["engine_identity"],
            "operation": "fabricated-cleanup-original",
            "retirement": {
                "binding": hashlib.sha256(b"fabricated-reviewed-ownership").hexdigest(),
                "fresh_until": time.time() + 300,
                "monotonic_deadline": time.monotonic() + 300,
                "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            },
            "physical_readback": True,
            **extra,
        },
    )
    value: dict[str, Any] = result["result"]
    return value


def compose(*args: str) -> str:
    return run(
        "docker", "compose", "--project-name", PROJECT, "--file", str(ROOT / "compose.json"), *args
    )


try:
    run("docker", "swarm", "init", "--advertise-addr", "127.0.0.1", "--listen-addr", "127.0.0.1")
    run("docker", "load", "--input", "/archives/alpine-full.tar")
    image = json.loads(run("docker", "image", "inspect", IMAGE))[0]
    assert IMAGE in image["RepoDigests"], image["RepoDigests"]
    binding = hashlib.sha256(
        json.dumps(RESOURCE, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    volumes = {}
    for role, name in VOLUMES.items():
        run(
            "docker",
            "volume",
            "create",
            "--label",
            "io.launchplane.testing-resources=" + binding,
            "--label",
            "io.launchplane.testing-resource-role=" + role,
            name,
        )
        volumes[role] = {"external": True, "name": name}
    identity = {
        "context": RESOURCE["context"],
        "instance": "testing",
        "artifact_id": "fabricated-artifact",
    }
    environment = {
        "PLATFORM_INSTANCE": "testing",
        "ODOO_DB_HOST": "database",
        "LAUNCHPLANE_RUNTIME_IDENTITY_JSON": json.dumps(identity),
    }
    services = {}
    for role in ("web", "database", "script-runner"):
        mounts = (
            ["ODOO_DB_VOLUME:/var/lib/postgresql/data"]
            if role == "database"
            else ["ODOO_DATA_VOLUME:/volumes/data", "ODOO_LOG_VOLUME:/volumes/logs"]
        )
        services[role] = {
            "image": IMAGE,
            "command": ["sleep", "600"],
            "network_mode": "none",
            "environment": environment if role != "database" else {},
            "volumes": mounts,
            "labels": {"dokploy.application.id": RESOURCE["target_id"]},
        }
    (ROOT / "compose.json").write_text(json.dumps({"services": services, "volumes": volumes}))
    compose("create", "--pull", "never")
    compose("start", "database", "script-runner")
    rows = inspect()
    assert all(row["reason"] == "container_reference" for row in rows.values()), rows
    for row in rows.values():
        assert sweep(row, "apply", operation="fabricated-mount-refusal")["status"] == "held"
    PROOF["mounted_volume_apply_refused"] = True
    paths = {
        role: Path(json.loads(run("docker", "volume", "inspect", name))[0]["Mountpoint"])
        for role, name in VOLUMES.items()
    }
    for role in ("ODOO_DB_VOLUME", "ODOO_DATA_VOLUME"):
        (paths[role] / "fabricated-bytes").write_bytes(os.urandom(2 * 1024 * 1024))
    preserved = paths["ODOO_LOG_VOLUME"] / ".lp-native-fabricated-input"
    preserved.write_text("synthetic preserved source")
    assert inspect()["ODOO_LOG_VOLUME"]["reason"] == "source_inputs_preserved"
    PROOF["staged_source_held"] = True
    spec = {
        "mode": "read",
        "context": RESOURCE["context"],
        "project": PROJECT,
        "identity": identity,
        "allow_unavailable": True,
        "failed_candidate": EXPORT["failed_candidate"],
    }
    custody = host("lifecycle", spec)
    assert custody["type"] == "complete", custody
    spec.update(
        {
            key: custody[key]
            for key in (
                "container_ids",
                "container_snapshot",
                "resource_binding",
                "physical_binding",
            )
        }
    )
    stale = host("lifecycle", {**spec, "container_snapshot": "f" * 64})
    assert stale["type"] == "failed", stale
    PROOF["stale_container_snapshot_refused"] = True
    compose("start", "web")
    active = host("lifecycle", spec)
    assert active["type"] == "failed", active
    PROOF["active_web_refused"] = True
    compose("stop", "web")
    services["web"]["command"] = [
        value.replace("$", "$$") for value in EXPORT["failed_candidate"]["web_command"]
    ]
    (ROOT / "compose.json").write_text(json.dumps({"services": services, "volumes": volumes}))
    compose("up", "--detach", "--force-recreate", "--no-deps", "--pull", "never", "web")
    gate = paths["ODOO_DATA_VOLUME"] / Path(EXPORT["failed_candidate"]["web_gate_path"]).name
    for _ in range(20):
        waiting = host(
            "lifecycle",
            {
                key: value
                for key, value in spec.items()
                if key
                not in (
                    "container_ids",
                    "container_snapshot",
                    "resource_binding",
                    "physical_binding",
                )
            },
        )
        if waiting.get("type") == "complete":
            break
        time.sleep(0.2)
    assert waiting.get("type") == "complete", waiting
    PROOF["bound_web_gate_wait_qualified"] = True
    gate.write_text("fabricated-completed-copy-evidence")
    assert inspect()["ODOO_DATA_VOLUME"]["reason"] != "container_reference"
    assert (
        host(
            "lifecycle",
            {
                key: value
                for key, value in spec.items()
                if key
                not in (
                    "container_ids",
                    "container_snapshot",
                    "resource_binding",
                    "physical_binding",
                )
            },
        )["type"]
        == "failed"
    )
    gate.unlink()
    PROOF["completed_copy_gate_refused_before_detachment"] = True
    custody = host(
        "lifecycle",
        {
            key: value
            for key, value in spec.items()
            if key
            not in ("container_ids", "container_snapshot", "resource_binding", "physical_binding")
        },
    )
    assert custody["type"] == "complete", custody
    spec.update(
        {
            key: custody[key]
            for key in (
                "container_ids",
                "container_snapshot",
                "resource_binding",
                "physical_binding",
            )
        }
    )
    foreign = run(
        "docker",
        "create",
        "--pull",
        "never",
        "--network",
        "none",
        "--mount",
        "type=volume,src=" + VOLUMES["ODOO_DB_VOLUME"] + ",dst=/foreign",
        IMAGE,
        "true",
    )
    try:
        assert host("lifecycle", spec)["type"] == "failed"
    finally:
        run("docker", "rm", foreign)
    PROOF["foreign_mount_refused"] = True
    rows = inspect()
    for role in ("ODOO_DB_VOLUME", "ODOO_DATA_VOLUME"):
        planned = sweep(
            rows[role],
            "dry-run",
            planned_container_removals={
                "ids": spec["container_ids"],
                "snapshot": spec["container_snapshot"],
            },
        )
        assert planned["status"] == "eligible", planned
    compose("stop")
    stopped = host("lifecycle", spec)
    assert stopped["type"] == "complete" and stopped["stopped"] is True, stopped
    removed = host("lifecycle", {**spec, "mode": "remove"})
    assert removed.get("availability") == "absent", removed
    assert host("lifecycle", {**spec, "mode": "removal-readback"})["availability"] == "absent"
    PROOF["exact_container_detachment_and_readback"] = True
    readings = {}
    rows = inspect()
    for role in ("ODOO_DB_VOLUME", "ODOO_DATA_VOLUME"):
        assert rows[role]["reason"] == "", rows[role]
        removed = sweep(rows[role], "apply")
        assert removed["status"] == "removed", removed
        physical = removed["physical"]
        assert (
            physical["observed_change_bytes"] == physical["after_bytes"] - physical["before_bytes"]
        )
        readback = sweep(rows[role], "readback", original_retirement_binding=True)
        assert (
            readback["status"] == "absent"
            and readback["physical"]["before_bytes"] == physical["before_bytes"]
        ), readback
        readings[role] = physical
    assert preserved.read_text() == "synthetic preserved source"
    assert inspect()["ODOO_LOG_VOLUME"]["reason"] == "source_inputs_preserved"
    PROOF.update(
        nonforce_volume_removal_and_original_readback=True,
        preserved_source_intact=True,
        physical=readings,
    )
    (OUT / "proof.json").write_text(json.dumps(PROOF, indent=2))
finally:
    if (ROOT / "compose.json").exists():
        compose("down")
    for name in VOLUMES.values():
        found = subprocess.run(["docker", "volume", "inspect", name], capture_output=True)
        if found.returncode == 0:
            run("docker", "volume", "rm", name)
