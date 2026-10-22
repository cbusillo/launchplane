"""Review, operate and read an isolated Odoo rehearsal using the installed transport."""

import argparse
import http.client
import json
import os
from pathlib import Path
import runpy
import re
import urllib.error
from urllib.parse import quote

from control_plane.contracts.host_network_capacity import HOST_BRIDGE_NETWORK_FAILURE_MESSAGES
from control_plane.contracts.odoo_upgrade_rehearsal import (
    ODOO_UPGRADE_REHEARSAL_ROUTE,
    ODOO_UPGRADE_REHEARSAL_PREFLIGHT_ROUTE,
    REHEARSAL_PUBLIC_FAILURE_MESSAGES,
    REHEARSAL_RELEASED_RESERVATION_HEADERS,
    OdooUpgradeRehearsalRequest,
    IncompleteRehearsalRetirementReceipt,
    RehearsalPreflightResult,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("preflight", "dry-run", "apply", "readback"))
    parser.add_argument("--product", required=True)
    parser.add_argument("--payload-file", type=Path)
    parser.add_argument("--source-context", default="")
    parser.add_argument("--operator-helper", type=Path, required=True)
    parser.add_argument("--evidence-file", type=Path)
    parser.add_argument("--idempotency-key", default="")
    parser.add_argument("--reviewed-dry-run", action="store_true")
    for name in ("config", "env-config", "url"):
        parser.add_argument("--" + name)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    evidence = args.evidence_file.expanduser().resolve() if args.evidence_file else None
    if args.mode in {"dry-run", "apply"} and evidence is None:
        parser.error("Review requires an explicit private evidence file outside the repository.")
    if evidence is not None and (evidence.is_relative_to(repo) or args.evidence_file.is_symlink()):
        parser.error("Use a private evidence file outside the repository.")
    if args.mode in {"dry-run", "preflight"} and evidence is not None and evidence.exists():
        parser.error("Choose a new review file; preserve existing evidence.")
    if args.mode == "apply" and (not args.reviewed_dry_run or not args.idempotency_key.strip()):
        parser.error(
            "Apply requires the exact saved review, acknowledgement and original stable key."
        )
    draft = None
    intent = None
    if args.mode == "preflight":
        if not args.source_context or args.payload_file or args.idempotency_key:
            parser.error("Preflight uses --source-context, without a rehearsal intent or key.")
    else:
        if (
            args.payload_file is None
            or args.payload_file.is_symlink()
            or args.payload_file.expanduser().resolve().is_relative_to(repo)
        ):
            parser.error("Use an explicit private intent file outside the repository.")
        draft = OdooUpgradeRehearsalRequest.model_validate_json(args.payload_file.read_bytes())
        draft = draft.model_copy(update={"mode": args.mode, "reviewed_plan_sha256": ""})
        if not draft.rehearsal_id and not args.idempotency_key.strip():
            parser.error("Initial planning and prepare require their original stable key.")
        intent = {
            "product": args.product,
            "request": {
                key: value
                for key, value in draft.model_dump(mode="json").items()
                if key not in {"mode", "reviewed_plan_sha256"}
            },
            "key": args.idempotency_key,
        }
    helper = args.operator_helper.expanduser().resolve(strict=True)
    if helper.name != "launchplane-write-action.py":
        parser.error("Select the installed Launchplane operator transport.")
    transport = runpy.run_path(str(helper), run_name="odoo_rehearsal_transport")
    settings = transport["resolve_settings"](args)
    transport["validate_service_url"](settings["service_url"])
    if not settings["token"]:
        parser.error("The existing operator credential is unavailable.")
    try:
        if args.mode == "preflight":
            response = transport["request_launchplane_read"](
                service_url=settings["service_url"],
                path=ODOO_UPGRADE_REHEARSAL_PREFLIGHT_ROUTE.format(
                    product=quote(args.product, safe="")
                ),
                query={"source_context": args.source_context},
                settings=settings,
                timeout=500,
            )
            if response.get("status") != "accepted":
                raise ValueError("The preflight observation is unavailable.")
            observed = RehearsalPreflightResult.model_validate(response["result"])
            if (observed.product, observed.source_context) != (args.product, args.source_context):
                raise ValueError("The preflight belongs to another source.")
            public = {
                "status": "verified",
                "mode": args.mode,
                "trace_id": transport["public_trace_id"](response["trace_id"]),
                **observed.model_dump(mode="json"),
            }
            if evidence is not None:
                with os.fdopen(
                    os.open(evidence, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w"
                ) as output:
                    json.dump(public, output)
            print(json.dumps(public))
            return 0
        assert draft is not None
        if args.mode == "apply" or (
            draft.action == "validate-business" and args.mode == "readback"
        ):
            if evidence is None:
                raise ValueError("Business readback requires its original saved review.")
            saved = json.loads(evidence.read_text())
            if saved["intent"] != intent or saved["plan"]["applied"] is not False:
                raise ValueError("Review belongs to another intent.")
            draft = draft.model_copy(update={"reviewed_plan_sha256": saved["plan"]["plan_sha256"]})
        response = transport["request_launchplane"](
            service_url=settings["service_url"],
            path=ODOO_UPGRADE_REHEARSAL_ROUTE.format(product=quote(args.product, safe="")),
            settings=settings,
            body=draft.model_dump(mode="json"),
            timeout=7200,
            idempotency_key=args.idempotency_key,
        )
        if response.get("status") != "accepted":
            raise ValueError("The operation outcome is unverified.")
        result = response["result"]
        if not isinstance(result, dict):
            raise ValueError("The operation receipt is malformed.")
        if result.get("state") == "checkpointed":
            records = response.get("records")
            operation = records.get("operation") if isinstance(records, dict) else None
            trace = transport["public_trace_id"](response.get("trace_id"))
            if (
                args.mode != "apply"
                or draft.action != "execute"
                or set(result) != {"state", "original_key_required"}
                or result.get("original_key_required") is not True
                or not trace
                or not isinstance(operation, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}", operation)
            ):
                raise ValueError("The response is not an execute checkpoint.")
            print(
                json.dumps(
                    {
                        "status": "checkpointed",
                        "state": result["state"],
                        "mode": args.mode,
                        "action": draft.action,
                        "trace_id": trace,
                        "operation_id": operation,
                        "original_key_required": True,
                    }
                )
            )
            return 1
        if result.get("product") != args.product or (
            draft.rehearsal_id and result.get("rehearsal_id") != draft.rehearsal_id
        ):
            raise ValueError("The receipt belongs to another rehearsal.")
        if args.mode == "dry-run":
            if (
                result.get("applied") is not False
                or result.get("action") != draft.action
                or not result.get("plan_sha256")
            ):
                raise ValueError("The response is not the requested inert review.")
            with os.fdopen(
                os.open(evidence, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w"
            ) as output:
                json.dump({"intent": intent, "plan": result}, output)
        if (
            args.mode in {"apply", "readback"}
            and result.get("action", draft.action) != draft.action
        ):
            raise ValueError("The receipt describes a different rehearsal action.")
        if draft.action in {"retire-incomplete", "abandon-validation"} and args.mode != "dry-run":
            IncompleteRehearsalRetirementReceipt.model_validate(result)
        unobserved_readback = (
            args.mode == "readback"
            and result.get("completed") is False
            and result.get("host_observation_unavailable") is True
        )
        if not unobserved_readback and (
            result.get("public_route") is not False
            or result.get("business_host_frozen") is not False
        ):
            raise ValueError("The receipt does not establish an isolated rehearsal.")
        if args.mode == "readback" and (result.get("completed") is not True):
            print(
                json.dumps(
                    {
                        "status": "unavailable"
                        if result.get("recovery_disposition") == "completed_refusal"
                        else "reconciliation_required",
                        "trace_id": transport["public_trace_id"](response["trace_id"]),
                        "mode": args.mode,
                        "original_key_required": True,
                        **result,
                    }
                )
            )
            return 1
        failed_validation = (
            draft.action == "validate-business"
            and args.mode in {"apply", "readback"}
            and result.get("business_validation", {}).get("passed") is False
        )
        print(
            json.dumps(
                {
                    "status": "failed" if failed_validation else "verified",
                    "mode": args.mode,
                    "action": draft.action,
                    "trace_id": transport["public_trace_id"](response["trace_id"]),
                    **result,
                }
            )
        )
        return 1 if failed_validation else 0
    except urllib.error.HTTPError as error:
        try:
            failure = json.loads(error.read())
            trace = transport["public_trace_id"](failure.get("trace_id", ""))
            code = str(failure.get("error", {}).get("code", "rehearsal_unavailable"))
        except (ValueError, TypeError, AttributeError):
            trace = ""
            code = "rehearsal_unavailable"
        released = (
            args.mode == "apply"
            and error.headers is not None
            and all(
                error.headers.get(name) == value
                for name, value in REHEARSAL_RELEASED_RESERVATION_HEADERS.items()
            )
        )
        refused = (
            released
            or error.code == 409
            and (
                code in REHEARSAL_PUBLIC_FAILURE_MESSAGES
                or code in HOST_BRIDGE_NETWORK_FAILURE_MESSAGES
            )
        )
        print(
            json.dumps(
                {
                    "status": "reconciliation_required"
                    if args.mode == "apply" and not refused
                    else "unavailable",
                    "mode": args.mode,
                    "http_status": error.code,
                    "trace_id": trace,
                    "error_code": code if code.isidentifier() else "rehearsal_unavailable",
                    "original_key_required": (args.mode == "apply" and not refused)
                    or (
                        args.mode == "readback" and draft is not None and bool(args.idempotency_key)
                    ),
                }
            )
        )
        return 1
    except (
        ValueError,
        KeyError,
        TypeError,
        OSError,
        urllib.error.URLError,
        http.client.HTTPException,
    ):
        print(
            json.dumps(
                {
                    "status": "reconciliation_required" if args.mode == "apply" else "unavailable",
                    "mode": args.mode,
                    "original_key_required": args.mode == "apply"
                    or (args.mode == "readback" and bool(args.idempotency_key)),
                }
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
