"""Review testing holds and staging configuration through the maintained authenticated transport."""

import argparse
import http.client
import io
import json
import os
from pathlib import Path
import runpy
import sys
import urllib.error

from control_plane.live_target_runtime import LiveTargetRuntimeApplyEnvelope
from control_plane.testing_separation_resources import (
    ROUTE,
    SeparationResourcesRequest,
    SeparationResourcesPlan,
    SeparationResourcesResult,
)
from control_plane.testing_lane_hold import (
    TESTING_HOLD_APPLY_ROUTE,
    TESTING_HOLD_ROUTE,
    TestingHoldApplyRequest,
    TestingHoldReadResult,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode",
        choices=(
            "hold-read",
            "hold-dry-run",
            "hold-apply",
            "lift-dry-run",
            "lift-apply",
            "dry-run",
            "apply",
            "resources-dry-run",
            "resources-apply",
            "resources-read",
        ),
    )
    parser.add_argument("--operator-helper", type=Path, required=True)
    parser.add_argument("--product", required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--reason", default="")
    parser.add_argument("--activation-plan-sha256", default="")
    parser.add_argument(
        "--hold-purpose",
        choices=("staff_testing", "isolation_staging"),
        default="isolation_staging",
    )
    parser.add_argument("--evidence-file", type=Path)
    parser.add_argument("--reviewed-dry-run", action="store_true")
    parser.add_argument("--require-rehearsal", action="store_true")
    parser.add_argument("--idempotency-key", default="")
    parser.add_argument("--config")
    parser.add_argument("--env-config")
    parser.add_argument("--url")
    args = parser.parse_args()
    applying = args.mode in {"hold-apply", "lift-apply", "apply", "resources-apply"}
    resources = args.mode.startswith("resources-")
    resource_read = args.mode == "resources-read"
    if resource_read and not args.idempotency_key.strip():
        parser.error("Receipt read requires the original preparation key.")
    holding = args.mode.startswith(("hold-", "lift-"))
    lifting = args.mode.startswith("lift-")
    if args.require_rehearsal and (
        not holding or args.hold_purpose != "staff_testing" or args.activation_plan_sha256
    ):
        parser.error(
            "Rehearsal requires a staff-testing hold mode without an activation interlock."
        )
    if applying and (not args.reviewed_dry_run or not args.idempotency_key.strip()):
        parser.error("Apply requires saved review evidence and its stable idempotency key.")
    reading = args.mode == "hold-read"
    if not reading and (not args.reason.strip() or args.evidence_file is None):
        parser.error("Planning and apply require a reason and private review evidence file.")
    evidence = args.evidence_file.expanduser().resolve() if args.evidence_file else None
    if (
        not reading
        and evidence is not None
        and (
            evidence.is_relative_to(Path(__file__).resolve().parents[1])
            or args.evidence_file.is_symlink()
        )
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
            hold=not lifting,
            rehearsal=args.require_rehearsal,
            purpose=args.hold_purpose,
            activation_plan_sha256=args.activation_plan_sha256,
            reason=args.reason or "Inspect testing interlock.",
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
    if resources:
        body = SeparationResourcesRequest(context=args.context, reason=args.reason).model_dump(
            mode="json"
        )
        path = ROUTE.replace("{product}", args.product)
    submitted = False
    try:
        if args.mode == "hold-read":
            response = transport["request_launchplane_read"](
                service_url=settings["service_url"],
                path=TESTING_HOLD_ROUTE,
                settings=settings,
                query={"product": args.product, "context": args.context, "instance": "testing"},
                timeout=30,
            )
            held = TestingHoldReadResult.model_validate(response["result"])
            print(
                json.dumps(
                    {
                        "status": "verified",
                        "held": held.hold is not None,
                        "purpose": held.hold.purpose if held.hold else None,
                        "rehearsal": bool(held.hold and held.hold.rehearsal),
                        "trace_id": transport["public_trace_id"](response["trace_id"]),
                    }
                )
            )
            return 0
        assert evidence is not None
        if applying:
            saved = json.loads(evidence.read_text())
            if saved["request"] != body or saved["path"] != path:
                raise ValueError("Review belongs to another operation.")
            body["mode"] = "apply"
            field = "reviewed_plan_sha256" if holding or resources else "reviewed_binding_sha256"
            source = "plan_sha256" if holding or resources else "separation_binding_sha256"
            body[field] = saved["result"][source]
            body = (
                SeparationResourcesRequest.model_validate(body)
                if resources
                else TestingHoldApplyRequest.model_validate(body)
                if holding
                else LiveTargetRuntimeApplyEnvelope.model_validate(body)
            ).model_dump(mode="json")
        elif not resource_read and evidence.exists():
            raise ValueError("Preserve existing evidence; select a new path.")
        if resource_read:
            body["mode"] = "receipt"
        submitted = applying
        response = transport["request_launchplane"](
            service_url=settings["service_url"],
            path=path,
            settings=settings,
            body=body,
            timeout=360 if resources else 60,
            **({"idempotency_key": args.idempotency_key} if applying or resource_read else {}),
        )
        if response["status"] != "accepted":
            raise ValueError("Operation was not accepted.")
        result = response["result"]
        rehearsal_hold = result.get("before" if lifting else "after") if holding else None
        has_rehearsal = isinstance(rehearsal_hold, dict) and bool(rehearsal_hold.get("rehearsal"))
        if holding and args.require_rehearsal and not has_rehearsal:
            raise ValueError("This request did not qualify as a disposable rehearsal hold.")
        if holding and (
            result.get("reconcile_requested")
            is not (
                applying
                and lifting
                and (args.hold_purpose == "staff_testing" or bool(args.activation_plan_sha256))
            )
            or (applying and not result["read_back_matches"])
        ):
            raise ValueError("Staging interlock readback or no-deploy result did not verify.")
        if (
            not holding
            and not resources
            and applying
            and (
                not result["apply"]["applied"]
                or result["apply"]["verification"]["status"] not in {"pass", "skipped"}
            )
        ):
            raise ValueError("Staging persistence did not verify.")
        if (
            not holding
            and not resources
            and (result["deploy"]["requested"] or result["deploy"]["triggered"])
        ):
            raise ValueError("Unexpected deployment result.")
        if resources:
            result = (
                SeparationResourcesResult.model_validate(result)
                if applying or resource_read
                else SeparationResourcesPlan.model_validate(result)
            ).model_dump(mode="json")
        if not applying and not resource_read:
            with os.fdopen(
                os.open(evidence, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w"
            ) as output:
                json.dump({"request": body, "path": path, "result": result}, output)
        elif resources:
            receipt = evidence.with_name(evidence.name + ".receipt.json")
            completed = {
                "path": path,
                "result": result,
                "idempotency_key": args.idempotency_key,
            }
            if receipt.exists():
                if receipt.is_symlink() or json.loads(receipt.read_text()) != completed:
                    raise ValueError("Preserve the existing resource receipt.")
            else:
                with os.fdopen(
                    os.open(receipt, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w"
                ) as output:
                    json.dump(completed, output)
        print(
            json.dumps(
                {
                    "status": "accepted",
                    "mode": args.mode,
                    "trace_id": transport["public_trace_id"](response["trace_id"]),
                    **(
                        {
                            "reconcile_requested": result["reconcile_requested"],
                            "rehearsal": has_rehearsal,
                        }
                        if holding
                        else {"deployment_requested": False}
                    ),
                }
            )
        )
        return 0
    except urllib.error.HTTPError as error:
        raw_error = error.read()
        error.close()
        try:
            code = json.loads(raw_error).get("error", {}).get("code", "")
        except (ValueError, AttributeError):
            code = ""
        with urllib.error.HTTPError(
            error.url, error.code, error.reason, error.headers, io.BytesIO(raw_error)
        ) as error:
            if submitted and (
                error.code >= 500
                or code in {"mutation_reconciliation_required", "mutation_in_progress"}
            ):
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
