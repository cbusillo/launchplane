"""Prepare a private production recipient using the existing installed caller transport."""

import argparse
import http.client
import io
import json
import os
from pathlib import Path
import runpy
import sys
import urllib.error

from control_plane.contracts.production_recipient import (
    ProductionRecipientRequest,
    ProductionRecipientPlan,
    RecipientRecoveryRequest,
    RecipientRecoveryPlan,
)
from control_plane.production_recipient import ROUTE
from control_plane.production_recipient_recovery import RECOVERY_ROUTE
from control_plane.production_recipient_continuation import (
    CONTINUATION_ROUTE,
    RecipientContinuationRequest,
    RecipientContinuationPlan,
)
from control_plane.production_recipient_cutover import (
    CUTOVER_ROUTE,
    RecipientCutoverRequest,
    RecipientCutoverPlan,
)

from control_plane.production_recipient_writer_control import (
    WRITER_ROUTE,
    RecipientWriterRequest,
    RecipientWriterPlan,
)


from control_plane.production_recipient_release_recovery import (
    RELEASE_RECOVERY_ROUTE,
    RecipientReleaseRecoveryRequest,
    RecipientReleaseRecoveryPlan,
)


def save(path: Path, payload: object) -> None:
    if path.is_symlink():
        raise ValueError("Preserve the original evidence.")
    if path.exists():
        previous = json.loads(path.read_text())
        if (
            not isinstance(previous, dict)
            or not isinstance(payload, dict)
            or {key: value for key, value in previous.items() if key != "trace_id"}
            != {key: value for key, value in payload.items() if key != "trace_id"}
        ):
            raise ValueError("Preserve the original evidence.")
        return
    with os.fdopen(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w") as output:
        json.dump(payload, output)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("dry-run", "apply", "readback", "receipt", "custody"))
    parser.add_argument("--operator-helper", type=Path, required=True)
    parser.add_argument("--product", required=True)
    parser.add_argument("--request-file", type=Path, required=True)
    parser.add_argument("--evidence-file", type=Path, required=True)
    parser.add_argument("--idempotency-key", default="")
    parser.add_argument("--reviewed-dry-run", action="store_true")
    parser.add_argument("--config")
    parser.add_argument("--env-config")
    parser.add_argument("--url")
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument(
        "--continuation", action="store_true", help="Review retained same-key custody supersession."
    )
    operation.add_argument(
        "--cutover",
        action="store_true",
        help="Arm exact intent; Client acceptance starts the release.",
    )
    operation.add_argument(
        "--recovery",
        action="store_true",
        help="Qualify isolated cold-data recovery of the exact preparation.",
    )
    operation.add_argument(
        "--writer-control", action="store_true", help="Coordinate a recorded testing API writer."
    )
    operation.add_argument(
        "--release-recovery",
        action="store_true",
        help="Reconcile the original Client release operation.",
    )
    args = parser.parse_args()
    submitted = False
    try:
        for path in (args.request_file, args.evidence_file):
            if path.is_symlink() or path.resolve().is_relative_to(
                Path(__file__).resolve().parents[1]
            ):
                raise ValueError("Keep private evidence outside the source checkout.")
        helper = args.operator_helper.expanduser().resolve(strict=True)
        if helper.name != "launchplane-write-action.py":
            raise ValueError("Use the installed operator transport.")
        transport = runpy.run_path(str(helper), run_name="production_recipient_transport")
        settings = transport["resolve_settings"](args)
        transport["validate_service_url"](settings["service_url"])
        if not settings["token"]:
            raise ValueError("The existing caller credential is unavailable.")
        request_type: (
            type[ProductionRecipientRequest]
            | type[RecipientRecoveryRequest]
            | type[RecipientContinuationRequest]
            | type[RecipientCutoverRequest]
            | type[RecipientWriterRequest]
            | type[RecipientReleaseRecoveryRequest]
        )
        result_type: (
            type[ProductionRecipientPlan]
            | type[RecipientRecoveryPlan]
            | type[RecipientContinuationPlan]
            | type[RecipientCutoverPlan]
            | type[RecipientWriterPlan]
            | type[RecipientReleaseRecoveryPlan]
        )
        if args.release_recovery:
            request_type, result_type, route = (
                RecipientReleaseRecoveryRequest,
                RecipientReleaseRecoveryPlan,
                RELEASE_RECOVERY_ROUTE,
            )
        elif args.writer_control:
            request_type, result_type, route = (
                RecipientWriterRequest,
                RecipientWriterPlan,
                WRITER_ROUTE,
            )
        elif args.continuation:
            request_type, result_type, route = (
                RecipientContinuationRequest,
                RecipientContinuationPlan,
                CONTINUATION_ROUTE,
            )
        elif args.cutover:
            request_type, result_type, route = (
                RecipientCutoverRequest,
                RecipientCutoverPlan,
                CUTOVER_ROUTE,
            )
        elif args.recovery:
            request_type, result_type, route = (
                RecipientRecoveryRequest,
                RecipientRecoveryPlan,
                RECOVERY_ROUTE,
            )
        else:
            request_type, result_type, route = (
                ProductionRecipientRequest,
                ProductionRecipientPlan,
                ROUTE,
            )
        if (args.continuation or args.cutover or args.release_recovery) and args.mode in {
            "receipt",
            "custody",
        }:
            raise ValueError("Use readback for this control operation.")
        draft = request_type.model_validate(json.loads(args.request_file.read_text()))
        if draft.mode != "dry-run" or draft.reviewed_plan_sha256:
            raise ValueError("The request file must be the original planning request.")
        body = draft.model_dump(mode="json")
        if isinstance(draft, RecipientContinuationRequest):
            if args.idempotency_key and args.idempotency_key != draft.original_key:
                raise ValueError("Continuation retains the original preparation key.")
            args.idempotency_key = draft.original_key
        path = route.format(product=args.product)
        if args.mode == "apply":
            if not args.idempotency_key.strip() or not args.reviewed_dry_run:
                raise ValueError("Apply requires saved review and the original stable key.")
            evidence = json.loads(args.evidence_file.read_text())
            if evidence["request"] != body or evidence["path"] != path:
                raise ValueError("The review belongs to another preparation.")
            body["reviewed_plan_sha256"] = result_type.model_validate(
                evidence["result"]
            ).plan_sha256
        if args.mode == "receipt" and not args.idempotency_key.strip():
            raise ValueError("Receipt read requires the original preparation key.")
        if args.mode == "dry-run" and args.evidence_file.exists():
            raise ValueError("Preserve the saved review; choose a fresh evidence path.")
        body["mode"] = args.mode
        submitted = args.mode == "apply"
        response = transport["request_launchplane"](
            service_url=settings["service_url"],
            path=path,
            settings=settings,
            body=body,
            timeout=900,
            **(
                {"idempotency_key": args.idempotency_key}
                if submitted
                or args.mode in {"receipt", "custody"}
                or args.continuation
                or args.release_recovery
                or args.writer_control
                else {}
            ),
        )
        if response["status"] != "accepted":
            raise ValueError("Preparation was not accepted.")
        result = result_type.model_validate(response["result"]).model_dump(mode="json")
        output = (
            args.evidence_file
            if args.mode == "dry-run"
            else args.evidence_file.with_name(args.evidence_file.name + "." + args.mode + ".json")
        )
        save(
            output,
            {
                "request": draft.model_dump(mode="json"),
                "path": path,
                "result": result,
                "trace_id": response["trace_id"],
            },
        )
        print(
            json.dumps(
                {
                    "status": "accepted",
                    "mode": args.mode,
                    "result": result,
                    "trace_id": transport["public_trace_id"](response["trace_id"]),
                }
            )
        )
        return 0
    except urllib.error.HTTPError as error:
        raw = error.read()
        error.close()
        with urllib.error.HTTPError(
            error.url, error.code, error.reason, error.headers, io.BytesIO(raw)
        ) as replay:
            transport["emit_http_error_payload"](
                operation="production-recipient", request={"mode": args.mode}, exc=replay
            )
        if submitted:
            print(json.dumps({"retain_original_key": True}))
    except (
        ValueError,
        KeyError,
        TypeError,
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
