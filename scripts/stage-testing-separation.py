"""Review testing's staging interlock and stage managed configuration without deploying."""

import argparse
import http.client
import json
import os
from pathlib import Path
import runpy
import sys
import urllib.error

from control_plane.live_target_runtime import LiveTargetRuntimeApplyEnvelope
from control_plane.testing_lane_hold import TESTING_HOLD_APPLY_ROUTE, TestingHoldApplyRequest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("hold-dry-run", "hold-apply", "dry-run", "apply"))
    parser.add_argument("--operator-helper", type=Path, required=True)
    parser.add_argument("--product", required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--evidence-file", type=Path, required=True)
    parser.add_argument("--reviewed-dry-run", action="store_true")
    parser.add_argument("--idempotency-key", default="")
    parser.add_argument("--config")
    parser.add_argument("--env-config")
    parser.add_argument("--url")
    args = parser.parse_args()
    applying = args.mode in {"hold-apply", "apply"}
    holding = args.mode.startswith("hold-")
    if applying and (not args.reviewed_dry_run or not args.idempotency_key.strip()):
        parser.error("Apply requires saved review evidence and its stable idempotency key.")
    evidence = args.evidence_file.expanduser().resolve()
    if (
        evidence.is_relative_to(Path(__file__).resolve().parents[1])
        or args.evidence_file.is_symlink()
    ):
        parser.error("Keep private review evidence outside the checkout.")
    helper = args.operator_helper.expanduser().resolve(strict=True)
    if helper.name != "launchplane-write-action.py":
        parser.error("Use the installed authenticated operator transport.")
    transport = runpy.run_path(str(helper), run_name="testing_separation_transport")
    settings = transport["resolve_settings"](args)
    transport["validate_service_url"](settings["service_url"])
    if not settings["token"]:
        parser.error("The configured operator credential is unavailable.")
    draft = (
        TestingHoldApplyRequest(
            product=args.product,
            context=args.context,
            instance="testing",
            mode="dry-run",
            hold=True,
            purpose="isolation_staging",
            reason=args.reason,
        )
        if holding
        else LiveTargetRuntimeApplyEnvelope(
            product=args.product,
            context=args.context,
            instance="testing",
            mode="dry-run",
            separation_only=True,
        )
    )
    body = draft.model_dump(mode="json")
    path = TESTING_HOLD_APPLY_ROUTE if holding else "/v1/live-target-runtime/apply"
    submitted = False
    try:
        if applying:
            saved = json.loads(evidence.read_text())
            if saved["request"] != body or saved["path"] != path:
                raise ValueError("Review belongs to another operation.")
            body["mode"] = "apply"
            field = "reviewed_plan_sha256" if holding else "reviewed_binding_sha256"
            source = "plan_sha256" if holding else "separation_binding_sha256"
            body[field] = saved["result"][source]
            body = (
                TestingHoldApplyRequest.model_validate(body)
                if holding
                else LiveTargetRuntimeApplyEnvelope.model_validate(body)
            ).model_dump(mode="json")
        elif evidence.exists():
            raise ValueError("Preserve existing evidence; select a new path.")
        submitted = applying
        response = transport["request_launchplane"](
            service_url=settings["service_url"],
            path=path,
            settings=settings,
            body=body,
            timeout=60,
            **({"idempotency_key": args.idempotency_key} if applying else {}),
        )
        if response["status"] != "accepted":
            raise ValueError("Operation was not accepted.")
        result = response["result"]
        if holding and applying and not result["read_back_matches"]:
            raise ValueError("Staging interlock readback did not verify.")
        if (
            not holding
            and applying
            and (
                not result["apply"]["applied"]
                or result["apply"]["verification"]["status"] not in {"pass", "skipped"}
            )
        ):
            raise ValueError("Staging persistence did not verify.")
        if not holding and (result["deploy"]["requested"] or result["deploy"]["triggered"]):
            raise ValueError("Unexpected deployment result.")
        if not applying:
            with os.fdopen(
                os.open(evidence, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w"
            ) as output:
                json.dump({"request": body, "path": path, "result": result}, output)
        print(
            json.dumps(
                {
                    "status": "accepted",
                    "mode": args.mode,
                    "trace_id": transport["public_trace_id"](response["trace_id"]),
                    "deployment_requested": False,
                }
            )
        )
        return 0
    except urllib.error.HTTPError as error:
        if submitted and error.code >= 500:
            print(json.dumps({"status": "outcome_unknown", "retain_original_key": True}))
        else:
            transport["emit_http_error_payload"](
                operation="testing-separation", request={"mode": args.mode}, exc=error
            )
    except (
        KeyError,
        TypeError,
        ValueError,
        OSError,
        http.client.HTTPException,
        urllib.error.URLError,
    ):
        print(
            json.dumps(
                {
                    "status": "outcome_unknown" if submitted else "unverified",
                    "retain_original_key": submitted,
                }
            )
        )
    return 1


if __name__ == "__main__":
    sys.exit(main())
