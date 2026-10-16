"""Review and continue public routing for the existing restored testing generation."""

import argparse
import http.client
import json
import os
from pathlib import Path
import runpy
import urllib.error
from urllib.parse import quote

from control_plane.testing_public_ingress import ROUTE, TestingPublicIngressRequest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("dry-run", "apply", "readback"))
    parser.add_argument("--operator-helper", type=Path, required=True)
    for name in ("product", "context", "operation-id", "reason"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--evidence-file", type=Path, required=True)
    parser.add_argument("--reviewed-dry-run", action="store_true")
    parser.add_argument("--idempotency-key", default="")
    for name in ("config", "env-config", "url"):
        parser.add_argument("--" + name)
    args = parser.parse_args()
    evidence = args.evidence_file.expanduser().resolve()
    if args.evidence_file.is_symlink() or evidence.is_relative_to(
        Path(__file__).resolve().parents[1]
    ):
        parser.error("Use a private evidence file outside the checkout.")
    if args.mode == "dry-run" and evidence.exists():
        parser.error("Preserve existing evidence; select a new file for a new review.")
    if args.mode == "apply" and (not args.reviewed_dry_run or not args.idempotency_key.strip()):
        parser.error("Apply requires reviewed evidence and the original stable key.")
    helper = args.operator_helper.expanduser().resolve(strict=True)
    if helper.name != "launchplane-write-action.py":
        parser.error("Select the installed Launchplane operator helper.")
    transport = runpy.run_path(str(helper), run_name="testing_public_ingress_transport")
    settings = transport["resolve_settings"](args)
    transport["validate_service_url"](settings["service_url"])
    if not settings["token"]:
        parser.error("The existing operator credential is not configured.")
    draft = TestingPublicIngressRequest(
        context=args.context, operation_id=args.operation_id, reason=args.reason
    )
    identity = {
        "product": args.product,
        **draft.model_dump(mode="json", exclude={"mode", "reviewed_plan_sha256"}),
    }
    submitted = False
    try:
        body = draft.model_dump(mode="json")
        digest = ""
        if args.mode != "dry-run":
            if evidence.stat().st_mode & 0o077:
                raise ValueError("Private review evidence must be owner-only.")
            saved = json.loads(evidence.read_text())
            if saved["request"] != identity or saved["result"]["applied"] is not False:
                raise ValueError("Review belongs to another request.")
            digest = str(saved["result"]["plan_sha256"])
            body["reviewed_plan_sha256"] = digest
        body["mode"] = args.mode
        # Validate saved digest/request shape before any apply request is sent.
        checked = TestingPublicIngressRequest.model_validate(body)
        submitted = args.mode == "apply"
        response = transport["request_launchplane"](
            service_url=settings["service_url"],
            path=ROUTE.format(product=quote(args.product, safe="")),
            settings=settings,
            body=checked.model_dump(mode="json"),
            timeout=120,
            **({"idempotency_key": args.idempotency_key} if submitted else {}),
        )
        result = response["result"]
        if response["status"] != "accepted" or any(
            result.get(k) != v for k, v in identity.items() if k != "reason"
        ):
            raise ValueError("The receipt belongs to another testing generation.")
        if result["applied"] is not (args.mode != "dry-run") or (
            digest and result["plan_sha256"] != digest
        ):
            raise ValueError("The receipt differs from the reviewed plan.")
        if args.mode == "dry-run":
            with os.fdopen(
                os.open(evidence, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w"
            ) as file:
                json.dump({"request": identity, "result": result}, file)
        print(
            json.dumps(
                {
                    "status": "verified",
                    "mode": args.mode,
                    "trace_id": transport["public_trace_id"](response["trace_id"]),
                    "plan_sha256": result["plan_sha256"],
                    "applied": result["applied"],
                    "public_health_verified": False,
                }
            )
        )
        return 0
    except urllib.error.HTTPError as error:
        if not submitted or error.code < 500:
            transport["emit_http_error_payload"](
                operation="testing-public-ingress", request={"mode": args.mode}, exc=error
            )
            return 1
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        urllib.error.URLError,
        http.client.HTTPException,
    ):
        pass
    print(
        json.dumps(
            {
                "status": "uncertain" if submitted else "unavailable",
                "mode": args.mode,
                "recommendation": "Preserve the reviewed request, evidence and original key; use readback and the same-key service recovery. Never bootstrap or restore again for TLS.",
            }
        )
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
