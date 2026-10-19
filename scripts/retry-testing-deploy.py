"""Deploy an exact recorded artifact to Odoo testing through the service, without rebuilding."""

import argparse
import http.client
import json
from pathlib import Path
import runpy
import urllib.error
from urllib.parse import quote

from pydantic import ValidationError

from control_plane.contracts.odoo_stable_target_replacement import (
    OdooStableTargetReplacementApplyRequest,
    OdooStableTargetReplacementRequest,
)
from control_plane.contracts.odoo_stable_target_replacement_operation import (
    OdooStableTargetReplacementOperationRecord,
)
from control_plane.odoo_target_replacement_apply_http import (
    ODOO_TARGET_REPLACEMENT_APPLY_ROUTE,
    OdooTargetReplacementApplyEnvelope,
)
from control_plane.odoo_target_replacement_plan_http import ODOO_TARGET_REPLACEMENT_PLAN_ROUTE
from control_plane.testing_lane_hold import TESTING_HOLD_ROUTE, TestingHoldReadResult
from control_plane.workflows.odoo_stable_target_replacement import OdooStableTargetReplacementPlan


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operator-helper", type=Path, required=True)
    for name in ("product", "context", "artifact-id", "source-commit", "idempotency-key"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--apply", action="store_true")
    for name in ("config", "env-config", "url"):
        parser.add_argument("--" + name)
    args = parser.parse_args()
    if not args.idempotency_key.strip():
        parser.error("Use a stable idempotency key for this exact testing deploy.")
    try:
        request = OdooStableTargetReplacementApplyRequest(
            product=args.product,
            instance="testing",
            artifact_id=args.artifact_id,
            source_git_ref=args.source_commit,
        )
    except ValidationError:
        parser.error("Select an exact recorded artifact and its source commit.")
    if not request.artifact_id or not request.source_git_ref:
        parser.error("Select an exact recorded artifact and its source commit.")
    helper = args.operator_helper.expanduser().resolve(strict=True)
    if helper.name != "launchplane-write-action.py":
        parser.error("Select the installed Launchplane operator helper.")
    transport = runpy.run_path(str(helper), run_name="testing_retry_transport")
    settings = transport["resolve_settings"](args)
    transport["validate_service_url"](settings["service_url"])
    if not settings["token"]:
        parser.error("The existing operator credential is unavailable.")
    submitted = False
    trace = None
    try:
        hold_response = transport["request_launchplane_read"](
            service_url=settings["service_url"],
            path=TESTING_HOLD_ROUTE,
            settings=settings,
            query={"product": request.product, "context": args.context, "instance": "testing"},
            timeout=120,
        )
        hold = TestingHoldReadResult.model_validate(hold_response["result"])
        if (hold.product, hold.context, hold.instance) != (
            request.product,
            args.context,
            "testing",
        ):
            raise ValueError("Testing hold identity could not be verified.")
        if hold.hold is not None:
            print(json.dumps({"status": "held", "trace_id": trace}))
            return 1
        reconcile = transport["request_launchplane_read"](
            service_url=settings["service_url"],
            path=f"/v1/product-profiles/{quote(request.product, safe='')}/reconcile-requests",
            settings=settings,
            query={},
            timeout=120,
        )
        if reconcile["product"] != request.product:
            raise ValueError("Reconcile product identity could not be verified.")
        testing = [
            item["last_plan"] for item in reconcile["requests"] if item["target_kind"] == "testing"
        ]
        if len(testing) != 1:
            raise ValueError("An exact testing reconcile plan is required.")
        desired = testing[0]
        current = desired["current_artifact_id"]
        if (
            desired["context"] != args.context
            or desired["target"] != "testing"
            or desired["desired_artifact_id"] != request.artifact_id
            or desired["desired_commit"] != request.source_git_ref
            or not isinstance(current, str)
            or not current.strip()
        ):
            raise ValueError("The recorded testing artifact or current inventory is unverified.")
        request = OdooStableTargetReplacementApplyRequest.model_validate(
            request.model_dump() | {"expected_current_artifact_id": current}
        )
        plan_request = OdooStableTargetReplacementRequest.model_validate(
            {
                name: getattr(request, name)
                for name in OdooStableTargetReplacementRequest.model_fields
            }
        )
        plan_response = transport["request_launchplane"](
            service_url=settings["service_url"],
            path=ODOO_TARGET_REPLACEMENT_PLAN_ROUTE,
            settings=settings,
            body={"product": request.product, "replacement": plan_request.model_dump(mode="json")},
            timeout=120,
        )
        plan = OdooStableTargetReplacementPlan.model_validate(plan_response["result"])
        trace = transport["public_trace_id"](plan_response.get("trace_id"))
        if (
            plan.plan_status != "ready"
            or (plan.product, plan.context, plan.instance)
            != (request.product, args.context, "testing")
            or plan.expected_artifact_id != request.artifact_id
            or plan.expected_source_git_ref != request.source_git_ref
        ):
            print(
                json.dumps(
                    {"status": "blocked", "trace_id": trace, "blocker_codes": plan.blocker_codes}
                )
            )
            return 1
        if not args.apply:
            print(
                json.dumps(
                    {"status": "ready", "trace_id": trace, "artifact_id": request.artifact_id}
                )
            )
            return 0
        submitted = True
        response = transport["request_launchplane"](
            service_url=settings["service_url"],
            path=ODOO_TARGET_REPLACEMENT_APPLY_ROUTE,
            settings=settings,
            body=OdooTargetReplacementApplyEnvelope(
                product=request.product, replacement=request
            ).model_dump(mode="json"),
            idempotency_key=args.idempotency_key,
            timeout=120,
        )
        result = response["result"]
        queued = OdooStableTargetReplacementOperationRecord.model_validate(
            {key: value for key, value in result.items() if key != "poll_url"}
        )
        if (
            response["status"] != "accepted"
            or (queued.product, queued.context, queued.instance, queued.request)
            != (request.product, args.context, "testing", request)
            or queued.idempotency_key != args.idempotency_key
        ):
            raise ValueError("The retry receipt did not match the request.")
        print(
            json.dumps(
                {
                    "status": "accepted",
                    "trace_id": transport["public_trace_id"](response.get("trace_id")),
                    "operation_id": queued.operation_id,
                    "operation_status": queued.status,
                    "idempotency_key": args.idempotency_key,
                }
            )
        )
        return 0
    except urllib.error.HTTPError as error:
        if not submitted or error.code < 500:
            transport["emit_http_error_payload"](
                operation="retry-testing-deploy", request={"product": args.product}, exc=error
            )
            return 1
    except (KeyError, TypeError, ValueError, OSError, http.client.HTTPException):
        pass
    print(
        json.dumps(
            {
                "status": "outcome_unknown" if submitted else "unverified",
                "trace_id": trace,
                "recommendation": "Read product activity; preserve the request and original idempotency key. Never retry under a new key after an uncertain response.",
            }
        )
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
