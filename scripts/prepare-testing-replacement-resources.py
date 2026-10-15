"""Prepare and read back fresh Odoo testing resources through the bounded service."""

import argparse
import http.client
import json
import os
from pathlib import Path
import runpy
import sys
import urllib.error
from urllib.parse import quote

from control_plane.testing_replacement_resources import (
    ROUTE,
    TestingReplacementResourcesPlan,
    TestingReplacementResourcesRequest,
    TestingReplacementResourcesReadback,
    TestingReplacementResourcesResult,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("dry-run", "apply", "readback"))
    parser.add_argument("--operator-helper", type=Path, required=True)
    for field in ("product", "context", "operation-id", "reason"):
        parser.add_argument(f"--{field}", required=True)
    parser.add_argument("--evidence-file", type=Path)
    parser.add_argument("--reviewed-dry-run", action="store_true")
    parser.add_argument("--idempotency-key", default="")
    parser.add_argument("--config")
    parser.add_argument("--env-config")
    parser.add_argument("--url")
    args = parser.parse_args()
    evidence_path = args.evidence_file.expanduser().resolve() if args.evidence_file else None
    if args.mode != "readback" and evidence_path is None:
        parser.error("Planning and apply require private review evidence.")
    if evidence_path is not None and (
        evidence_path.is_relative_to(Path(__file__).resolve().parents[1])
        or args.evidence_file.is_symlink()
    ):
        parser.error("Use a private evidence file outside the repository.")
    if args.mode == "apply" and (not args.reviewed_dry_run or not args.idempotency_key.strip()):
        parser.error("Apply requires a reviewed dry run and the original stable key.")
    if args.mode == "dry-run" and evidence_path is not None and evidence_path.exists():
        parser.error("Choose new evidence; existing review evidence is preserved.")
    helper = args.operator_helper.expanduser().resolve(strict=True)
    if helper.name != "launchplane-write-action.py":
        parser.error("Select the installed Launchplane operator helper.")
    transport = runpy.run_path(str(helper), run_name="testing_resource_transport")
    settings = transport["resolve_settings"](args)
    transport["validate_service_url"](settings["service_url"])
    if not settings["token"]:
        parser.error("The existing operator credential is not configured.")
    draft = TestingReplacementResourcesRequest(
        context=args.context,
        operation_id=args.operation_id,
        reason=args.reason,
    )
    review_request = {"product": args.product, **draft.model_dump(mode="json")}
    submitted = False
    accepted = False
    trace = ""
    try:
        body = draft.model_dump(mode="json")
        if args.mode == "apply":
            assert evidence_path is not None
            saved = json.loads(evidence_path.read_text())
            if saved["request"] != review_request:
                raise ValueError("Review belongs to another lane or operation.")
            plan = TestingReplacementResourcesPlan.model_validate(saved["plan"])
            if plan.applied:
                raise ValueError("The review is not a dry run.")
            body.update(mode="apply", reviewed_plan_sha256=plan.plan_sha256)
        elif args.mode == "readback":
            body.update(mode="readback")
        submitted = args.mode == "apply"
        response = transport["request_launchplane"](
            service_url=settings["service_url"],
            path=ROUTE.format(product=quote(args.product, safe="")),
            settings=settings,
            body=body,
            timeout=600,
            **({"idempotency_key": args.idempotency_key} if submitted else {}),
        )
        if response["status"] != "accepted":
            raise ValueError("The service did not accept the operation.")
        accepted = submitted
        trace = transport["public_trace_id"](response["trace_id"])
        result = response["result"]
        if (result["product"], result["context"], result["instance"], result["operation_id"]) != (
            args.product,
            args.context,
            "testing",
            args.operation_id,
        ):
            raise ValueError("The receipt belongs to another lane or operation.")
        if args.mode == "dry-run":
            plan = TestingReplacementResourcesPlan.model_validate(result)
            if plan.applied:
                raise ValueError("The dry run unexpectedly applied a change.")
            assert evidence_path is not None
            with os.fdopen(
                os.open(evidence_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w"
            ) as output:
                json.dump({"request": review_request, "plan": plan.model_dump(mode="json")}, output)
        elif args.mode == "apply":
            applied = TestingReplacementResourcesResult.model_validate(result)
            if (
                applied.model_dump(include=set(TestingReplacementResourcesPlan.model_fields))
                != plan.model_copy(update={"applied": True}).model_dump()
            ):
                raise ValueError("Apply differs from the reviewed plan.")
        else:
            TestingReplacementResourcesReadback.model_validate(result)
        print(
            json.dumps(
                {
                    "status": "verified",
                    "mode": args.mode,
                    "trace_id": trace,
                    "operation_id": args.operation_id,
                    "resources_verified": args.mode != "dry-run",
                    "restore_started": False,
                    "plan_sha256": result.get("plan_sha256"),
                    "resources_binding_sha256": result.get("resources_binding_sha256"),
                }
            )
        )
        return 0
    except urllib.error.HTTPError as error:
        if not accepted and not (submitted and error.code >= 500):
            transport["emit_http_error_payload"](
                operation="testing-replacement-resources",
                request={"mode": args.mode},
                exc=error,
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
