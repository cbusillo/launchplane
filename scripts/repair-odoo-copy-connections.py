"""Scoped Odoo copy repair using the installed admin helper's private transport."""

import argparse
import json
import os
from pathlib import Path
import runpy
import sys
import urllib.error

from control_plane.odoo_post_deploy_http import ODOO_POST_DEPLOY_ROUTE, OdooPostDeployEnvelope
from control_plane.workflows.odoo_post_deploy import OdooPostDeployRequest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("dry-run", "apply", "readback"))
    parser.add_argument("--operator-helper", type=Path, required=True)
    for field in ("config", "env-config", "url"):
        parser.add_argument(f"--{field}")
    for field in ("product", "context", "instance", "reason"):
        parser.add_argument(f"--{field}", required=True)
    parser.add_argument("--evidence-file", type=Path)
    parser.add_argument("--reviewed-dry-run", action="store_true")
    parser.add_argument("--idempotency-key", default="")
    args = parser.parse_args()
    reviewed_digest = ""
    if args.mode == "dry-run" and (not args.evidence_file or args.evidence_file.exists()):
        parser.error("Dry-run requires a new private --evidence-file.")
    if args.mode == "apply":
        if not args.reviewed_dry_run or not args.idempotency_key or not args.evidence_file:
            parser.error("Apply requires reviewed dry-run evidence and a stable idempotency key.")
        reviewed = json.loads(args.evidence_file.read_text())
        if reviewed["request"] != {
            key: getattr(args, key) for key in ("product", "context", "instance", "reason")
        }:
            parser.error("The dry-run evidence belongs to another lane.")
        reviewed_digest = reviewed["evidence"]["connection_isolation_plan_sha256"]
    payload = OdooPostDeployEnvelope(
        product=args.product,
        post_deploy=OdooPostDeployRequest(
            context=args.context,
            instance=args.instance,
            connection_isolation=args.mode,
            reviewed_dry_run=args.reviewed_dry_run,
            reviewed_plan_sha256=reviewed_digest,
            reason=args.reason,
        ),
    )
    helper = args.operator_helper.expanduser().resolve(strict=True)
    if helper.name != "launchplane-write-action.py":
        parser.error("Select the installed Launchplane operator helper.")
    transport = runpy.run_path(str(helper), run_name="copy_connection_transport")
    settings = transport["resolve_settings"](args)
    transport["validate_service_url"](settings["service_url"])
    if not settings["token"]:
        parser.error("The existing local operator credential is not configured.")
    request = {key: getattr(args, key) for key in ("product", "context", "instance", "reason")}
    try:
        response = transport["request_launchplane"](
            service_url=settings["service_url"],
            path=ODOO_POST_DEPLOY_ROUTE,
            settings=settings,
            body=payload.model_dump(mode="json"),
            timeout=330,
            idempotency_key=args.idempotency_key,
        )
        result = response["result"]
        evidence = result["override_evidence"]
        # Only closed booleans, the plan binding, and setting names leave the client.
        safe = {
            key: value
            for key, value in evidence.items()
            if key
            in {
                "connection_isolation_ok",
                "connection_isolation_plan_sha256",
                "integration_readback_ok",
                "integration_readback_refused",
                "integration_readback_allowed",
            }
        }
        receipt = {
            "mode": args.mode,
            "request": request,
            "evidence": safe,
            "trace_id": transport["public_trace_id"](response["trace_id"]),
        }
        if result["post_deploy_status"] != "pass" or safe.get("connection_isolation_ok") not in {
            "true",
            "false",
        }:
            raise ValueError("Incomplete operation evidence")
        if args.mode == "dry-run":
            descriptor = os.open(args.evidence_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w") as output:
                output.write(json.dumps(receipt, indent=2) + "\n")
        print(json.dumps(receipt))
        return 0 if args.mode == "dry-run" or safe.get("integration_readback_ok") == "true" else 1
    except urllib.error.HTTPError as error:
        transport["emit_http_error_payload"](
            operation="odoo-copy-connections", request=request, exc=error
        )
    except (ValueError, KeyError, TypeError, OSError, urllib.error.URLError):
        print(
            json.dumps(
                {
                    "status": "unverified",
                    "mode": args.mode,
                    "message": "Inspect the original operation; retain its evidence and idempotency key.",
                }
            )
        )
    return 1


if __name__ == "__main__":
    sys.exit(main())
