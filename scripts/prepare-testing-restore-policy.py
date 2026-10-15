"""Review and prepare testing's upstream-restore policy through the service."""

import argparse
import http.client
import json
import os
from pathlib import Path
import runpy
import sys
import urllib.error
from urllib.parse import quote

from control_plane.http_routes.product_testing_restore_policy import ROUTE
from control_plane.product_testing_restore_policy import (
    TestingRestorePolicyPlan,
    TestingRestorePolicyRequest,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("dry-run", "apply"))
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
    if args.mode == "apply" and (not args.reviewed_dry_run or not args.idempotency_key.strip()):
        parser.error("Apply requires the reviewed dry run and its stable idempotency key.")
    evidence_path = args.evidence_file.expanduser().resolve()
    repo = Path(__file__).resolve().parents[1]
    if evidence_path.is_relative_to(repo) or args.evidence_file.is_symlink():
        parser.error("Use a private evidence file outside the repository.")
    if args.mode == "dry-run" and evidence_path.exists():
        parser.error("Choose a new evidence file; existing review evidence is preserved.")
    helper = args.operator_helper.expanduser().resolve(strict=True)
    if helper.name != "launchplane-write-action.py":
        parser.error("Select the installed Launchplane operator helper.")
    transport = runpy.run_path(str(helper), run_name="testing_restore_policy_transport")
    settings = transport["resolve_settings"](args)
    transport["validate_service_url"](settings["service_url"])
    if not settings["token"]:
        parser.error("The existing local operator credential is not configured.")
    draft = TestingRestorePolicyRequest(context=args.context, reason=args.reason)
    review_request = {"product": args.product, **draft.model_dump(mode="json")}
    submitted = False
    accepted = False
    accepted_trace = ""
    try:
        if args.mode == "apply":
            saved = json.loads(evidence_path.read_text())
            if saved["request"] != review_request:
                raise ValueError("Review evidence belongs to another request.")
            plan = TestingRestorePolicyPlan.model_validate(saved["plan"])
        else:
            response = transport["request_launchplane"](
                service_url=settings["service_url"],
                path=ROUTE.format(product=quote(args.product, safe="")),
                settings=settings,
                body=draft.model_dump(mode="json"),
                timeout=30,
            )
            if response["status"] != "accepted":
                raise ValueError("Planning was not accepted.")
            plan = TestingRestorePolicyPlan.model_validate(response["result"])
        if (plan.product, plan.context) != (args.product, draft.context) or plan.applied:
            raise ValueError("The plan does not match the testing request.")
        if args.mode == "dry-run":
            with os.fdopen(
                os.open(evidence_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w"
            ) as output:
                json.dump({"request": review_request, "plan": plan.model_dump(mode="json")}, output)
        else:
            # The service revalidates the saved review atomically. Sending the
            # original request also permits same-key replay after a lost reply.
            apply = TestingRestorePolicyRequest(
                **{**draft.model_dump(), "mode": "apply", "reviewed_plan_sha256": plan.plan_sha256}
            )
            submitted = True
            response = transport["request_launchplane"](
                service_url=settings["service_url"],
                path=ROUTE.format(product=quote(args.product, safe="")),
                settings=settings,
                body=apply.model_dump(mode="json"),
                timeout=30,
                idempotency_key=args.idempotency_key,
            )
            accepted = True
            accepted_trace = transport["public_trace_id"](response["trace_id"])
            if response["status"] != "accepted":
                raise ValueError("Apply was not accepted.")
            applied = TestingRestorePolicyPlan.model_validate(response["result"])
            if applied != plan.model_copy(update={"applied": True}):
                raise ValueError("The accepted policy differs from the review.")
            readback = transport["request_launchplane_read"](
                service_url=settings["service_url"],
                path=transport["_product_read_path"]("product-profile-read", product=args.product),
                settings=settings,
                query={},
                timeout=30,
            )
            lanes = [
                lane
                for lane in readback["profile"]["lanes"]
                if (lane["context"], lane["instance"]) == (draft.context, "testing")
            ]
            if len(lanes) != 1 or lanes[0]["odoo_data_policy"] != plan.policy_after.model_dump(
                mode="json"
            ):
                raise ValueError("Testing policy readback differs from the review.")
        print(
            json.dumps(
                {
                    "status": "verified",
                    "mode": args.mode,
                    "plan_sha256": plan.plan_sha256,
                    "changed": plan.changed,
                    "applied": args.mode == "apply",
                    "read_back_matches": True if args.mode == "apply" else None,
                    "restore_started": False,
                    "trace_id": transport["public_trace_id"](response["trace_id"]),
                }
            )
        )
        return 0
    except urllib.error.HTTPError as error:
        if accepted or (submitted and error.code >= 500):
            print(
                json.dumps(
                    {
                        "status": "accepted_unverified" if accepted else "outcome_unknown",
                        "mode": args.mode,
                        "trace_id": accepted_trace or None,
                    }
                )
            )
        else:
            transport["emit_http_error_payload"](
                operation="testing-restore-policy",
                request={"mode": args.mode},
                exc=error,
            )
    except (
        KeyError,
        TypeError,
        ValueError,
        OSError,
        urllib.error.URLError,
        http.client.HTTPException,
    ):
        print(
            json.dumps(
                {
                    "status": "accepted_unverified"
                    if accepted
                    else "outcome_unknown"
                    if submitted
                    else "unverified",
                    "mode": args.mode,
                    "trace_id": accepted_trace or None,
                }
            )
        )
    return 1


if __name__ == "__main__":
    sys.exit(main())
