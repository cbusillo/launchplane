"""Bounded service client using the installed admin helper's private transport.

No host commands, provider credentials, or local live-target CLI fallback.
"""

import argparse
import json
import os
from pathlib import Path
import runpy
import sys
import urllib.error

from control_plane.contracts.lane_service_restart import (
    SERVICE_RESTART_ROUTE,
    LaneServiceRestartRequest,
    LaneServiceRestartResponse,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("dry-run", "apply"))
    parser.add_argument(
        "--operator-helper",
        type=Path,
        required=True,
        help="Installed launchplane skill's scripts/launchplane-write-action.py",
    )
    parser.add_argument("--config")
    parser.add_argument("--env-config")
    parser.add_argument("--url")
    for field in ("product", "context", "instance", "service", "reason"):
        parser.add_argument(f"--{field}", required=True)
    parser.add_argument("--evidence-file", type=Path, required=True)
    parser.add_argument("--reviewed-dry-run", action="store_true")
    parser.add_argument("--idempotency-key", default="")
    args = parser.parse_args()
    payload = LaneServiceRestartRequest(
        product=args.product,
        context=args.context,
        instance=args.instance,
        service=args.service,
        reason=args.reason,
    )
    if args.mode == "apply":
        if not args.reviewed_dry_run or not args.idempotency_key.strip():
            parser.error(
                "Apply requires --reviewed-dry-run and the same --idempotency-key on every retry."
            )
        reviewed = LaneServiceRestartResponse.model_validate_json(args.evidence_file.read_text())
        plan = reviewed.result.plan
        if (
            reviewed.result.status != "ready"
            or plan.digest() != reviewed.result.plan_sha256
            or any(
                getattr(plan, field) != getattr(payload, field)
                for field in ("product", "context", "instance", "service", "reason")
            )
        ):
            parser.error("The evidence file does not match this exact restart request.")
        payload = payload.model_copy(
            update={
                "mode": "apply",
                "reviewed_plan_sha256": reviewed.result.plan_sha256,
            }
        )
    # Reuse the maintained transport for credential sourcing, endpoint validation,
    # same-origin redirects, and HTTP. Nothing in codex-skills is copied or edited.
    helper = args.operator_helper.expanduser().resolve(strict=True)
    if helper.name != "launchplane-write-action.py":
        parser.error("--operator-helper must select the installed Launchplane admin helper.")
    transport = runpy.run_path(str(helper), run_name="lane_restart_transport")
    settings = transport["resolve_settings"](args)
    transport["validate_service_url"](settings["service_url"])
    if not settings["token"]:
        parser.error("The existing local operator credential is not configured.")
    try:
        raw = transport["request_launchplane"](
            service_url=settings["service_url"],
            path=SERVICE_RESTART_ROUTE,
            settings=settings,
            body=payload.model_dump(mode="json"),
            timeout=130,
            idempotency_key=args.idempotency_key,
        )
        response = LaneServiceRestartResponse.model_validate(raw)
    except urllib.error.HTTPError as error:
        transport["emit_http_error_payload"](
            operation="lane-service-restart",
            request={
                "product": payload.product,
                "context": payload.context,
                "instance": payload.instance,
                "service": payload.service,
            },
            exc=error,
        )
        return 1
    except (ValueError, OSError, urllib.error.URLError):
        print(
            json.dumps(
                {
                    "status": "unverified",
                    "message": (
                        "Request did not settle. Inspect product activity; retry apply only with the original evidence and idempotency key."
                    ),
                }
            )
        )
        return 1
    if args.mode == "dry-run":
        descriptor = os.open(args.evidence_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as output:
            output.write(response.model_dump_json(indent=2) + "\n")
    print(
        json.dumps(
            {
                "status": response.result.status,
                "trace_id": response.trace_id,
                "plan_sha256": response.result.plan_sha256,
                "before_container_id": response.result.plan.before.container_id,
                "after_container_id": response.result.after.container_id
                if response.result.after
                else "",
                "replayed": response.replayed or False,
            }
        )
    )
    return 0 if response.result.status in {"ready", "pass"} else 1


if __name__ == "__main__":
    sys.exit(main())
