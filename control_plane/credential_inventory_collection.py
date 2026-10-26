from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import ModuleType
from urllib.error import HTTPError, URLError

from pydantic import ValidationError

from control_plane.credential_inventory import (
    INVENTORY_ROUTE,
    CredentialCollection,
    CredentialSnapshot,
    collection_digest,
)

COLLECTOR_TIMEOUT_SECONDS = 300
COLLECTION_INTERVAL_SECONDS = 24 * 60 * 60
MAX_REPORT_BYTES = 2 * 1024 * 1024


def collect_inventory(
    *, collector: Path, catalog: Path, manifest: Path | None = None
) -> CredentialCollection:
    snapshot = None
    with TemporaryDirectory(prefix="credential-metadata-") as directory:
        command = [
            sys.executable,
            str(collector),
            "--catalog",
            str(catalog),
            "--output",
            str(Path(directory) / "inventory.md"),
        ]
        if manifest:
            command.extend(["--manifest", str(manifest)])
        try:
            completed = subprocess.run(
                command, capture_output=True, timeout=COLLECTOR_TIMEOUT_SECONDS, check=False
            )
            if completed.returncode in (0, 1, 2) and len(completed.stdout) <= MAX_REPORT_BYTES:
                snapshot = CredentialSnapshot.model_validate_json(completed.stdout)
        except (OSError, subprocess.TimeoutExpired, ValidationError):
            snapshot = None
    return CredentialCollection(
        attempted_at=datetime.now(timezone.utc),
        status="collected" if snapshot else "failed",
        snapshot=snapshot,
    )


def operator_helper(catalog: Path) -> ModuleType:
    scripts = catalog / "skills/launchplane/scripts"
    sys.path.insert(0, str(scripts))
    specification = importlib.util.spec_from_file_location(
        "credential_inventory_operator", scripts / "launchplane-write-action.py"
    )
    if specification is None or specification.loader is None:
        raise ValueError("Authentication helper is unavailable.")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def publish_inventory(
    collection: CredentialCollection, *, helper: ModuleType, config: str | None
) -> str:
    settings = helper.resolve_settings(argparse.Namespace(config=config, env_config=None, url=None))
    result = helper.request_launchplane(
        service_url=settings["service_url"],
        path=INVENTORY_ROUTE,
        settings=settings,
        body=collection.model_dump(mode="json"),
        timeout=30,
    )
    receipt_id = f"credential-inventory-{collection_digest(collection)}"
    if result.get("status") != "ok" or result.get("receipt_id") != receipt_id:
        raise ValueError("Collection receipt is unavailable.")
    return receipt_id


def write_launch_agent(args: argparse.Namespace, output: Path) -> None:
    uv = shutil.which("uv")
    if not uv:
        raise ValueError("uv is unavailable.")
    root = Path(__file__).resolve().parents[1]
    arguments = [
        uv,
        "run",
        "--project",
        str(root),
        "launchplane-credential-inventory",
        "--collector",
        str(args.collector.resolve()),
        "--catalog",
        str(args.catalog.resolve()),
    ]
    if args.manifest:
        arguments.extend(["--manifest", str(args.manifest.resolve())])
    if args.config:
        arguments.extend(["--config", str(Path(args.config).expanduser().resolve())])
    with output.open("xb") as stream:
        plistlib.dump(
            {
                "Label": "com.launchplane.credential-inventory",
                "ProgramArguments": arguments,
                "WorkingDirectory": str(root),
                "RunAtLoad": True,
                "StartInterval": COLLECTION_INTERVAL_SECONDS,
                "ProcessType": "Background",
                "EnvironmentVariables": {
                    "PATH": str(Path(uv).parent) + ":/opt/homebrew/bin:/usr/bin:/bin"
                },
            },
            stream,
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Publish metadata-only credential drift to Launchplane."
    )
    parser.add_argument("--collector", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--config")
    parser.add_argument("--write-launch-agent", type=Path)
    args = parser.parse_args()
    try:
        if args.write_launch_agent:
            write_launch_agent(args, args.write_launch_agent)
            print("Credential collection schedule written; not loaded.")
            return 0
        collection = collect_inventory(
            collector=args.collector, catalog=args.catalog, manifest=args.manifest
        )
        receipt_id = publish_inventory(
            collection, helper=operator_helper(args.catalog), config=args.config
        )
        print(json.dumps({"status": collection.status, "receipt_id": receipt_id}))
        return 0 if collection.status == "collected" else 1
    except HTTPError as error:
        print(json.dumps({"status": "delivery_failed", "http_status": error.code}))
    except (OSError, URLError, ValueError, KeyError, ImportError, RuntimeError):
        print(json.dumps({"status": "delivery_failed"}))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
