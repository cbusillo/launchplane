import argparse
import json
import os
from pathlib import Path
import runpy
import urllib.error

from control_plane.contracts.preview_fixture import PreviewFixtureRequest, PreviewFixtureReceipt
from control_plane.odoo_app_maintenance_http import ODOO_APP_MAINTENANCE_ROUTE


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Apply an image-owned fixture to an exact preview."
    )
    parser.add_argument("mode", choices=("dry-run", "apply", "readback"))
    parser.add_argument("--operator-helper", type=Path, required=True)
    # noinspection DuplicatedCode
    parser.add_argument("--payload-file", type=Path, required=True)
    parser.add_argument("--evidence-file", type=Path)
    parser.add_argument("--idempotency-key", default="")
    parser.add_argument("--reviewed-dry-run", action="store_true")
    for name in ("config", "env-config", "url"):
        parser.add_argument("--" + name)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if args.mode != "readback" and args.evidence_file is None:
        parser.error("Dry-run and apply require a saved review file.")
    for path in (args.payload_file, args.evidence_file):
        if path is None:
            continue
        if path.is_symlink() or path.resolve().is_relative_to(root):
            parser.error("Use private payload/evidence files outside the repository.")
    payload = json.loads(args.payload_file.read_text())
    draft = PreviewFixtureRequest.model_validate(payload["maintenance"])
    intent = {
        "product": payload["product"],
        "maintenance": draft.model_dump(exclude={"mode", "reviewed_plan_sha256"}),
    }
    if args.mode == "dry-run" and args.evidence_file.exists():
        parser.error("Preserve existing evidence; select a new review file.")
    digest = ""
    if args.mode == "apply":
        if not args.reviewed_dry_run or not args.idempotency_key.strip():
            parser.error("Apply requires saved review acknowledgement and the original stable key.")
        saved = json.loads(args.evidence_file.read_text())
        if saved["intent"] != intent:
            parser.error("The saved dry run belongs to another request.")
        digest = saved["response"]["result"]["plan_sha256"]
    draft = PreviewFixtureRequest.model_validate(
        {**draft.model_dump(), "mode": args.mode, "reviewed_plan_sha256": digest}
    )
    helper = args.operator_helper.resolve(strict=True)
    if helper.name != "launchplane-write-action.py":
        parser.error("Select the installed Launchplane transport.")
    transport = runpy.run_path(str(helper), run_name="preview_fixture_transport")
    settings = transport["resolve_settings"](args)
    transport["validate_service_url"](settings["service_url"])
    if not settings["token"]:
        parser.error("The existing Launchplane credential is unavailable.")
    try:
        response = transport["request_launchplane"](
            service_url=settings["service_url"],
            path=ODOO_APP_MAINTENANCE_ROUTE,
            settings=settings,
            body={"product": payload["product"], "maintenance": draft.model_dump(mode="json")},
            timeout=600,
            idempotency_key=args.idempotency_key,
        )
        receipt = PreviewFixtureReceipt.model_validate(response["result"]["receipt"])
        if (
            response["status"] != "accepted"
            or receipt.fixture != draft.fixture
            or receipt.identity.preview_generation_id != draft.generation_id
        ):
            raise ValueError("The returned receipt does not match this fixture request.")
    except urllib.error.HTTPError as error:
        transport["emit_http_error_payload"](
            operation="preview-fixture", request={"mode": args.mode}, exc=error
        )
        return 1
    except (KeyError, TypeError, ValueError, OSError, urllib.error.URLError):
        print(
            json.dumps(
                {
                    "status": "unverified",
                    "message": "Read back the original fixture operation; preserve its review and key.",
                }
            )
        )
        return 1
    if args.mode == "dry-run":
        with os.fdopen(
            os.open(args.evidence_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w"
        ) as output:
            json.dump({"intent": intent, "response": response}, output)
    print(
        json.dumps(
            {
                "trace_id": transport["public_trace_id"](response["trace_id"]),
                "receipt": receipt.model_dump(mode="json"),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
