"""Review and retire incomplete testing custody through the supported service."""

import argparse
import http.client
import json
import os
from pathlib import Path
import runpy
import sys
import urllib.error
from urllib.parse import quote

from control_plane.testing_generation_retirement import (
    ROUTE,
    TestingGenerationRetirementPlan,
    TestingGenerationRetirementRequest,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("dry-run", "apply", "readback"))
    parser.add_argument("--operator-helper", type=Path, required=True)
    for name in ("product", "context", "operation-id", "reason"):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--evidence-file", type=Path)
    parser.add_argument("--reviewed-dry-run", action="store_true")
    parser.add_argument("--idempotency-key", default="")
    for name in ("config", "env-config", "url"):
        parser.add_argument(f"--{name}")
    args = parser.parse_args()
    evidence = args.evidence_file.expanduser().resolve() if args.evidence_file else None
    if args.mode != "readback" and evidence is None:
        parser.error("Dry-run and apply require private review evidence.")
    if evidence is not None and (
        evidence.is_relative_to(Path(__file__).resolve().parents[1])
        or args.evidence_file.is_symlink()
    ):
        parser.error("Use a private evidence file outside the checkout.")
    if args.mode == "dry-run" and evidence is not None and evidence.exists():
        parser.error("Existing review evidence must be preserved.")
    if args.mode == "apply" and (not args.reviewed_dry_run or not args.idempotency_key.strip()):
        parser.error("Apply requires reviewed evidence and the original stable key.")
    helper = args.operator_helper.expanduser().resolve(strict=True)
    if helper.name != "launchplane-write-action.py":
        parser.error("Select the installed Launchplane operator helper.")
    transport = runpy.run_path(str(helper), run_name="testing_retirement_transport")
    settings = transport["resolve_settings"](args)
    transport["validate_service_url"](settings["service_url"])
    if not settings["token"]:
        parser.error("The existing operator credential is unavailable.")
    draft = TestingGenerationRetirementRequest(
        context=args.context, operation_id=args.operation_id, reason=args.reason
    )
    review_request = {"product": args.product, **draft.model_dump(mode="json")}
    submitted = accepted = False
    trace = ""
    try:
        expected = None
        if args.mode == "apply":
            assert evidence is not None
            saved = json.loads(evidence.read_text())
            if saved["request"] != review_request:
                raise ValueError("Review belongs to another request.")
            expected = TestingGenerationRetirementPlan.model_validate(saved["plan"])
            if expected.applied:
                raise ValueError("Apply requires an unapplied review.")
            body = draft.model_copy(
                update={"mode": "apply", "reviewed_plan_sha256": expected.plan_sha256}
            )
        else:
            body = draft.model_copy(update={"mode": args.mode})
        submitted = args.mode == "apply"
        response = transport["request_launchplane"](
            service_url=settings["service_url"],
            settings=settings,
            path=ROUTE.format(product=quote(args.product, safe="")),
            body=body.model_dump(mode="json"),
            timeout=60,
            **({"idempotency_key": args.idempotency_key} if submitted else {}),
        )
        accepted = submitted
        trace = transport["public_trace_id"](response["trace_id"])
        if response["status"] != "accepted":
            raise ValueError("Retirement was not accepted.")
        plan = TestingGenerationRetirementPlan.model_validate(response["result"])
        if (plan.product, plan.context, plan.operation_id, plan.applied) != (
            args.product,
            draft.context,
            draft.operation_id,
            args.mode != "dry-run",
        ):
            raise ValueError("Retirement receipt belongs to another request or mode.")
        if expected is not None and plan != expected.model_copy(update={"applied": True}):
            raise ValueError("Retirement differs from its review.")
        if args.mode == "dry-run":
            assert evidence is not None
            with os.fdopen(
                os.open(evidence, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w"
            ) as output:
                json.dump({"request": review_request, "plan": plan.model_dump(mode="json")}, output)
        elif args.mode == "apply":
            readback = transport["request_launchplane"](
                service_url=settings["service_url"],
                settings=settings,
                path=ROUTE.format(product=quote(args.product, safe="")),
                body=draft.model_copy(update={"mode": "readback"}).model_dump(mode="json"),
                timeout=30,
            )
            if (
                readback["status"] != "accepted"
                or TestingGenerationRetirementPlan.model_validate(readback["result"]) != plan
            ):
                raise ValueError("Retirement readback differs from its receipt.")
        print(
            json.dumps(
                {
                    "status": "verified",
                    "mode": args.mode,
                    "trace_id": trace,
                    **plan.model_dump(mode="json"),
                }
            )
        )
        return 0
    except urllib.error.HTTPError as error:
        if not accepted and not (submitted and error.code >= 500):
            transport["emit_http_error_payload"](
                operation="testing-generation-retirement", request={"mode": args.mode}, exc=error
            )
            return 1
    except (
        KeyError,
        TypeError,
        ValueError,
        OSError,
        urllib.error.URLError,
        http.client.HTTPException,
    ):
        pass
    print(
        json.dumps(
            {
                "status": "accepted_unverified"
                if accepted
                else "outcome_unknown"
                if submitted
                else "unverified",
                "mode": args.mode,
                "trace_id": trace or None,
            }
        )
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
