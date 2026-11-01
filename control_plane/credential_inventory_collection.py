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
from urllib.error import HTTPError

from pydantic import ValidationError

from control_plane.credential_inventory import (
    INVENTORY_ROUTE,
    CredentialCollection,
    CredentialSnapshot,
    CollectionFailure,
    collection_receipt_id,
)

COLLECTOR_TIMEOUT_SECONDS = 300
COLLECTION_INTERVAL_SECONDS = 24 * 60 * 60
MAX_REPORT_BYTES = 2 * 1024 * 1024


def collect_inventory(
    *, collector: Path, catalog: Path, manifest: Path | None = None
) -> CredentialCollection:
    snapshot = None
    failure_reason: CollectionFailure | None = None
    try:
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
            if not collector.is_file():
                raise FileNotFoundError
            completed = subprocess.run(
                command, capture_output=True, timeout=COLLECTOR_TIMEOUT_SECONDS
            )
            if completed.returncode not in (0, 1, 2):
                failure_reason = "collector_failed"
            elif len(completed.stdout) > MAX_REPORT_BYTES:
                failure_reason = "report_too_large"
            else:
                snapshot = CredentialSnapshot.model_validate_json(completed.stdout)
    except OSError:
        failure_reason = "collector_unavailable"
    except subprocess.TimeoutExpired:
        failure_reason = "collector_timeout"
    except ValidationError:
        failure_reason = "invalid_snapshot"
    attempted_at = datetime.now(timezone.utc)
    if snapshot is not None and snapshot.generated_at > attempted_at:
        snapshot, failure_reason = None, "invalid_snapshot"
    return CredentialCollection(
        attempted_at=attempted_at,
        status="collected" if snapshot else "failed",
        snapshot=snapshot,
        failure_reason=failure_reason,
    )


def operator_helper(catalog: Path) -> ModuleType:
    scripts = catalog / "skills/launchplane/scripts"
    specification = importlib.util.spec_from_file_location(
        "credential_inventory_operator", scripts / "launchplane-write-action.py"
    )
    if specification is None or specification.loader is None:
        raise ValueError("Authentication helper is unavailable.")
    module = importlib.util.module_from_spec(specification)
    previous_path = sys.path[:]
    try:
        sys.path.insert(0, str(scripts))
        specification.loader.exec_module(module)
    finally:
        sys.path[:] = previous_path
    return module


def publish_inventory(
    collection: CredentialCollection,
    *,
    helper: ModuleType,
) -> str:
    settings = helper.resolve_settings(argparse.Namespace(config=None, env_config=None, url=None))
    result = helper.request_launchplane(
        service_url=settings["service_url"],
        path=INVENTORY_ROUTE,
        settings=settings,
        body=collection.model_dump(mode="json"),
        timeout=30,
    )
    receipt_id = collection_receipt_id(collection)
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
        "--frozen",
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
    content = plistlib.dumps(
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
    )
    created = False
    try:
        with output.open("xb") as stream:
            created = True
            stream.write(content)
    except BaseException:
        if created:
            output.unlink(missing_ok=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Publish metadata-only credential drift to Launchplane."
    )
    parser.add_argument("--collector", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--write-launch-agent", type=Path)
    args = parser.parse_args()
    if args.write_launch_agent:
        try:
            write_launch_agent(args, args.write_launch_agent)
            print("Credential collection schedule written; not loaded.")
            return 0
        except (OSError, ValueError) as error:
            if isinstance(error, FileExistsError):
                reason = "already_exists"
            elif isinstance(error, FileNotFoundError):
                reason = "parent_unavailable"
            elif isinstance(error, ValueError):
                reason = "uv_unavailable"
            else:
                reason = "write_failed"
            print(json.dumps({"status": "schedule_refused", "reason": reason}))
            return 64
    try:
        collection = collect_inventory(
            collector=args.collector, catalog=args.catalog, manifest=args.manifest
        )
        receipt_id = publish_inventory(
            collection,
            helper=operator_helper(args.catalog),
        )
        print(json.dumps({"status": collection.status, "receipt_id": receipt_id}))
        return 0 if collection.status == "collected" else 1
    except HTTPError as error:
        print(json.dumps({"status": "delivery_failed", "http_status": error.code}))
    # noinspection PyBroadException: Keep imported transport failures out of CLI output.
    except Exception:
        print(json.dumps({"status": "delivery_failed"}))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
