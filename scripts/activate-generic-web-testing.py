"""Activate one service-owned testing allocation using the installed caller transport."""

import argparse
import http.client
import io
import json
import os
from pathlib import Path
import runpy
import sys
import urllib.error

from control_plane.contracts.testing_activation import (
    TestingActivationRequest,
    TestingActivationPlan,
    TestingActivationResult,
    TestingActivationRefusedResult,
    TestingActivationRefusalCode,
)
from control_plane.discord_identity import DiscordIdentityResult
from control_plane.operator_error_evidence import public_error_reference
from control_plane.testing_activation import ROUTE
from control_plane.testing_activation_inspection import TestingActivationInspection


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


def external_refusal(raw: bytes) -> DiscordIdentityResult | None:
    try:
        payload = json.loads(raw)
        if (
            payload.get("error", {}).get("code")
            == TestingActivationRefusalCode.EXTERNAL_CAPABILITY_UNVERIFIED
        ):
            return DiscordIdentityResult.model_validate_json(payload["records"]["discord_identity"])
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("dry-run", "apply", "readback", "receipt", "inspect"))
    parser.add_argument("--operator-helper", type=Path, required=True)
    parser.add_argument("--product", required=True)
    parser.add_argument("--request-file", type=Path, required=True)
    parser.add_argument("--evidence-file", type=Path, required=True)
    parser.add_argument("--idempotency-key", default="")
    parser.add_argument("--reviewed-dry-run", action="store_true")
    parser.add_argument("--config")
    parser.add_argument("--env-config")
    parser.add_argument("--url")
    args = parser.parse_args()
    retain_original_key = bool(args.idempotency_key.strip())
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
        transport = runpy.run_path(str(helper), run_name="testing_activation_transport")
        settings = transport["resolve_settings"](args)
        transport["validate_service_url"](settings["service_url"])
        if not settings["token"]:
            raise ValueError("The existing caller credential is unavailable.")
        draft = TestingActivationRequest.model_validate(json.loads(args.request_file.read_text()))
        if draft.mode != "dry-run" or draft.reviewed_plan_sha256:
            raise ValueError("The request file must be the original planning request.")
        body = draft.model_dump(mode="json")
        path = ROUTE.format(product=args.product)
        if args.mode == "apply":
            if not args.idempotency_key.strip() or not args.reviewed_dry_run:
                raise ValueError("Apply requires saved review and the original stable key.")
            evidence = json.loads(args.evidence_file.read_text())
            if evidence["request"] != body or evidence["path"] != path:
                raise ValueError("The review belongs to another activation.")
            body["reviewed_plan_sha256"] = TestingActivationPlan.model_validate(
                evidence["result"]
            ).plan_sha256
        if args.mode in {"receipt", "inspect"} and not args.idempotency_key.strip():
            raise ValueError("Receipt read requires the original activation key.")
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
                if submitted or args.mode in {"receipt", "inspect"}
                else {}
            ),
        )
        if response["status"] != "accepted":
            raise ValueError("Activation was not accepted.")
        result_model = (
            TestingActivationInspection
            if args.mode == "inspect"
            else TestingActivationRefusedResult
            if response["result"].get("outcome") == "refused"
            else TestingActivationPlan
            if args.mode == "dry-run"
            else TestingActivationResult
        )
        result = result_model.model_validate(response["result"]).model_dump(mode="json")
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
        observed = external_refusal(raw)
        if observed is not None:
            payload = json.loads(raw)
            print(
                json.dumps(
                    {
                        "status": "rejected",
                        "trace_id": public_error_reference(
                            payload.get("trace_id", ""), transport["public_trace_id"]
                        ),
                        "external_identity": observed.model_dump(mode="json"),
                        "retain_original_key": retain_original_key,
                    }
                )
            )
            return 1
        with urllib.error.HTTPError(
            error.url, error.code, error.reason, error.headers, io.BytesIO(raw)
        ) as replay:
            transport["emit_http_error_payload"](
                operation="testing-activation",
                request={"mode": args.mode, "retain_original_key": retain_original_key},
                exc=replay,
            )
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
                    "retain_original_key": retain_original_key,
                }
            )
        )
    return 1


if __name__ == "__main__":
    sys.exit(main())
