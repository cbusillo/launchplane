"""Audit or generate testing-local credentials using the installed operator transport."""

import argparse
import http.client
import json
import os
from pathlib import Path
import runpy
import sys
import urllib.error
from urllib.parse import quote

from control_plane.product_config_http import ProductConfigApplyResponse
from control_plane.testing_local_credentials import (
    TestingCredentialAudit,
    TestingCredentialAuditResponse,
    TestingCredentialInventoryResponse,
)


def local_readback_passes(lane: TestingCredentialAudit) -> bool:
    return (
        bool(lane.local_keys)
        and set(lane.local_keys) == set(lane.generated_keys)
        and (
            lane.status in {"pass", "no_production", "overlap"}
            and not lane.missing_keys
            and not any(overlap.testing_key in lane.local_keys for overlap in lane.overlaps)
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("audit", "dry-run", "apply", "readback"))
    parser.add_argument("--operator-helper", type=Path, required=True)
    parser.add_argument("--product")
    parser.add_argument("--context")
    parser.add_argument("--reason", default="")
    parser.add_argument("--evidence-file", type=Path)
    parser.add_argument("--reviewed-dry-run", action="store_true")
    parser.add_argument("--idempotency-key", default="")
    parser.add_argument("--config")
    parser.add_argument("--env-config")
    parser.add_argument("--url")
    args = parser.parse_args()
    if args.mode != "audit" and (not args.product or not args.context):
        parser.error("Select one product and testing context.")
    if args.mode in {"dry-run", "apply"} and (not args.reason.strip() or not args.evidence_file):
        parser.error("Planning and apply require a reason and private evidence file.")
    if args.mode == "apply" and (not args.reviewed_dry_run or not args.idempotency_key.strip()):
        parser.error("Apply requires reviewed evidence and a stable idempotency key.")
    evidence = args.evidence_file.expanduser().resolve() if args.evidence_file else None
    if evidence and (
        evidence.is_relative_to(Path(__file__).resolve().parents[1])
        or args.evidence_file.is_symlink()
    ):
        parser.error("Use a private evidence file outside the repository.")
    if args.mode == "dry-run" and evidence and evidence.exists():
        parser.error("Preserve the existing evidence; select a new file.")
    helper = args.operator_helper.expanduser().resolve(strict=True)
    if helper.name != "launchplane-write-action.py":
        parser.error("Select the installed Launchplane operator helper.")
    transport = runpy.run_path(str(helper), run_name="testing_credentials_transport")
    settings = transport["resolve_settings"](args)
    transport["validate_service_url"](settings["service_url"])
    if not settings["token"]:
        parser.error("The existing operator credential is unavailable.")
    submitted = False
    accepted = False
    trace = ""
    try:

        def read() -> TestingCredentialAuditResponse | TestingCredentialInventoryResponse:
            path = (
                f"/v1/products/{quote(args.product, safe='')}/testing-credentials"
                if args.product
                else "/v1/testing-credentials"
            )
            response = transport["request_launchplane_read"](
                service_url=settings["service_url"],
                path=path,
                settings=settings,
                query={},
                timeout=120,
            )
            model = (
                TestingCredentialAuditResponse
                if args.product
                else TestingCredentialInventoryResponse
            )
            return model.model_validate(response)

        report = read()
        trace = transport["public_trace_id"](report.trace_id)
        if args.mode == "audit":
            print(report.model_dump_json())
            return (
                1 if any(lane.status in {"unavailable", "overlap"} for lane in report.lanes) else 0
            )
        lane = next(lane for lane in report.lanes if lane.context == args.context)
        if args.mode == "readback":
            print(json.dumps({"trace_id": trace, **lane.model_dump(mode="json")}))
            return 0 if local_readback_passes(lane) else 1
        payload: dict[str, object] = {
            "product": args.product,
            "context": args.context,
            "instance": "testing",
            "mode": "dry-run",
            "reason": args.reason,
            "secrets": [
                {
                    "binding_key": key,
                    "name": name,
                    "generate": "testing_local",
                    "secret_class": "testing",
                }
                for key, name in sorted(lane.local_binding_names.items())
            ],
        }
        if not payload["secrets"]:
            raise ValueError("No declared local credentials.")
        if args.mode == "apply":
            assert evidence is not None
            saved = json.loads(evidence.read_text())
            if saved["request"] != payload:
                raise ValueError("The reviewed credential target changed.")
            review = ProductConfigApplyResponse.model_validate(saved["response"])
            if review.result.mode != "dry-run":
                raise ValueError("Dry-run evidence required.")
            payload["mode"] = "apply"
        submitted = args.mode == "apply"
        result = transport["request_launchplane"](
            service_url=settings["service_url"],
            path="/v1/product-config/apply",
            settings=settings,
            body=payload,
            timeout=120,
            idempotency_key=args.idempotency_key if submitted else "",
        )
        receipt = ProductConfigApplyResponse.model_validate(result)
        trace = transport["public_trace_id"](receipt.trace_id)
        if (
            (
                receipt.result.product,
                receipt.result.context,
                receipt.result.instance,
                receipt.result.mode,
            )
            != (args.product, args.context, "testing", args.mode)
            or {secret.binding_key for secret in receipt.result.secrets} != set(lane.local_keys)
            or any(secret.value_source != "generated" for secret in receipt.result.secrets)
        ):
            raise ValueError("Unexpected credential receipt.")
        accepted = submitted
        if args.mode == "dry-run":
            assert evidence is not None
            with os.fdopen(
                os.open(evidence, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w"
            ) as output:
                json.dump({"request": payload, "response": receipt.model_dump(mode="json")}, output)
        else:
            lane = next(lane for lane in read().lanes if lane.context == args.context)
            if not local_readback_passes(lane):
                raise ValueError("Independent readback is unavailable or overlapping.")
        print(
            json.dumps(
                {
                    "status": "verified",
                    "mode": args.mode,
                    "trace_id": trace,
                    "keys": lane.local_keys,
                    "production_modified": False,
                    "runtime_synced": False,
                    "audit_status": lane.status,
                }
            )
        )
        return 0
    except urllib.error.HTTPError as error:
        if not accepted and (not submitted or error.code < 500):
            transport["emit_http_error_payload"](
                operation="testing-credentials", request={"mode": args.mode}, exc=error
            )
            return 1
    except (KeyError, TypeError, ValueError, StopIteration, OSError, http.client.HTTPException):
        pass
    print(
        json.dumps(
            {
                "status": "accepted_unverified"
                if accepted
                else "outcome_unknown"
                if submitted
                else "unverified",
                "trace_id": trace or None,
            }
        )
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
