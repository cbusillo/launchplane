import argparse
from contextlib import redirect_stdout
import http.client
import io
import json
import os
from pathlib import Path
import runpy
import sys
import urllib.error

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.generic_web_deploy_recovery import (
    GenericWebDeployRecoveryApplyResponse,
    GenericWebDeployRecoveryDryRunResponse,
    GenericWebDeployRecoveryReferenceRequest,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Review and abandon an exact held testing verifier."
    )
    parser.add_argument("mode", choices=("dry-run", "apply", "readback"))
    parser.add_argument("--operator-helper", type=Path, required=True)
    parser.add_argument("--request-file", type=Path, required=True)
    parser.add_argument("--evidence-file", type=Path, required=True)
    parser.add_argument("--reviewed-dry-run", action="store_true")
    parser.add_argument("--config")
    parser.add_argument("--env-config")
    parser.add_argument("--url")
    args = parser.parse_args()
    submitted = False
    accepted = False
    try:
        helper = args.operator_helper.expanduser().resolve(strict=True)
        if helper.name != "launchplane-write-action.py":
            raise ValueError("Use the installed operator transport.")
        transport = runpy.run_path(str(helper), run_name="testing_abandonment_transport")
        for path in (args.request_file, args.evidence_file):
            if path.is_symlink() or path.resolve().is_relative_to(
                Path(__file__).resolve().parents[1]
            ):
                raise ValueError("Keep private request and review files outside the checkout.")
        settings = transport["resolve_settings"](args)
        transport["validate_service_url"](settings["service_url"])
        if not settings["token"]:
            raise ValueError("The existing caller credential is unavailable.")
        request = GenericWebDeployRecoveryReferenceRequest.model_validate(
            transport["read_payload_file"](str(args.request_file))
        ).model_dump(mode="json")
        service_binding = canonical_json_sha256(
            {"service_url": settings["service_url"].rstrip("/")}
        )
        expected_digest = ""
        if args.mode != "dry-run":
            evidence = transport["read_payload_file"](str(args.evidence_file))
            reviewed = GenericWebDeployRecoveryDryRunResponse.model_validate(evidence["result"])
            if (
                evidence["request"] != request
                or evidence["service_binding_sha256"] != service_binding
                or reviewed.product != request["product"]
                or reviewed.instance != "testing"
                or reviewed.proposed_action != "abandon_testing_original"
                or reviewed.provider_outcome != "unknown"
                or reviewed.retry_safe
                or reviewed.reservation_state != "reconcile_required"
            ):
                raise ValueError("Retain the exact request and its eligible review.")
            expected_digest = reviewed.recovery_digest
        if args.mode == "apply":
            if not args.reviewed_dry_run:
                raise ValueError("Apply requires an explicit reviewed dry run.")
            submitted = True
            response = transport["request_launchplane"](
                service_url=settings["service_url"],
                path="/v1/admin/generic-web/deploy-recovery/apply",
                settings=settings,
                body={**request, "expected_recovery_digest": expected_digest},
                timeout=120,
            )
            applied = GenericWebDeployRecoveryApplyResponse.model_validate(response)
            if (
                applied.product != request["product"]
                or applied.instance != "testing"
                or applied.reservation_state != "completed"
                or applied.recovery_action != "abandon_testing_original"
                or applied.recovery_digest != expected_digest
                or applied.retry_safe
            ):
                raise ValueError("The original abandonment response is unverified.")
            accepted = True
        response = transport["request_launchplane"](
            service_url=settings["service_url"],
            path="/v1/admin/generic-web/deploy-recovery/dry-run",
            settings=settings,
            body=request,
            timeout=120,
        )
        result = GenericWebDeployRecoveryDryRunResponse.model_validate(response)
        if result.product != request["product"] or result.instance != "testing":
            raise ValueError("The response belongs to another lane.")
        if args.mode == "dry-run":
            if result.proposed_action != "abandon_testing_original" or result.retry_safe:
                print(
                    json.dumps(
                        {
                            "status": "refused",
                            "mode": args.mode,
                            "result": result.model_dump(mode="json"),
                        }
                    )
                )
                return 1
            with os.fdopen(
                os.open(args.evidence_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w"
            ) as output:
                json.dump(
                    {
                        "request": request,
                        "service_binding_sha256": service_binding,
                        "result": result.model_dump(mode="json"),
                    },
                    output,
                )
        elif (
            result.reservation_state != "completed"
            or result.proposed_action != "replay_completed"
            or result.recovery_digest != expected_digest
            or result.provider_outcome != "not_inspected"
        ):
            raise ValueError("The original abandonment has no matching completed readback.")
        print(
            json.dumps(
                {
                    "status": "verified" if args.mode != "dry-run" else "review_required",
                    "mode": args.mode,
                    "result": result.model_dump(mode="json"),
                }
            )
        )
        return 0
    except urllib.error.HTTPError as error:
        error_output = io.StringIO()
        with redirect_stdout(error_output):
            transport["emit_http_error_payload"](
                operation="testing-abandonment", request={"mode": args.mode}, exc=error
            )
        failure = json.loads(error_output.getvalue())
        failure["retain_original_request"] = True
        if accepted:
            failure["status"] = "accepted_unverified"
        print(json.dumps(failure))
    except (ValueError, KeyError, TypeError, OSError, http.client.HTTPException):
        print(
            json.dumps(
                {
                    "status": "accepted_unverified"
                    if accepted
                    else "outcome_unknown"
                    if submitted
                    else "unverified",
                    "mode": args.mode,
                    "retain_original_request": True,
                }
            )
        )
    return 1


if __name__ == "__main__":
    sys.exit(main())
