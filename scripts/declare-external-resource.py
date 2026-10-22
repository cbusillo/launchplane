import argparse
import http.client
import json
import os
from pathlib import Path
import runpy
import sys
import urllib.error
from urllib.parse import quote

from control_plane.contracts.product_profile_record import ProductSecretConfigRequirement
from control_plane.http_routes.mutation_support import AcceptedEvidenceResponse
from control_plane.http_routes.products import ProductProfileResponse


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Declare a managed binding's external provider without changing its value."
    )
    parser.add_argument("mode", choices=("dry-run", "apply"))
    parser.add_argument("--product", required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--instance", choices=("testing", "prod"), required=True)
    parser.add_argument("--binding-key", required=True)
    parser.add_argument("--provider", choices=("discord_bot",), required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--evidence-file", type=Path, required=True)
    parser.add_argument("--reviewed-dry-run", action="store_true")
    parser.add_argument("--idempotency-key", default="")
    parser.add_argument("--operator-helper", type=Path, required=True)
    parser.add_argument("--config")
    parser.add_argument("--env-config")
    parser.add_argument("--url")
    args = parser.parse_args()
    if not all(
        value.strip() for value in (args.product, args.context, args.binding_key, args.reason)
    ):
        parser.error("Select one exact binding and give a reason.")
    if args.mode == "apply" and (not args.reviewed_dry_run or not args.idempotency_key.strip()):
        parser.error("Apply requires reviewed evidence and its original stable key.")
    evidence = args.evidence_file.expanduser().resolve()
    if (
        evidence.is_relative_to(Path(__file__).resolve().parents[1])
        or args.evidence_file.is_symlink()
    ):
        parser.error("Use a private evidence file outside the repository.")
    if args.mode == "dry-run" and evidence.exists():
        parser.error("Preserve existing evidence; select a new file.")
    helper = args.operator_helper.expanduser().resolve(strict=True)
    if helper.name != "launchplane-write-action.py":
        parser.error("Select the installed Launchplane operator helper.")
    transport = runpy.run_path(str(helper), run_name="external_resource_declaration_transport")
    settings = transport["resolve_settings"](args)
    transport["validate_service_url"](settings["service_url"])
    if not settings["token"]:
        parser.error("The existing operator credential is unavailable.")
    payload = {
        "product": args.product,
        "mode": "dry-run",
        "reason": args.reason,
        "managed_secret_bindings": [
            {
                "binding_key": args.binding_key,
                "context": args.context,
                "instance": args.instance,
                "external_provider": args.provider,
                "testing_local_credential": False,
            }
        ],
    }
    submitted = accepted = False
    trace = ""
    try:

        def profile() -> ProductSecretConfigRequirement:
            result = transport["request_launchplane_read"](
                service_url=settings["service_url"],
                settings=settings,
                path=f"/v1/product-profiles/{quote(args.product, safe='')}",
                query={},
                timeout=120,
            )
            record = ProductProfileResponse.model_validate(result).profile
            if record.product != args.product or not any(
                (lane.context, lane.instance) == (args.context, args.instance)
                for lane in record.lanes
            ):
                raise ValueError("The declared product lane is unavailable.")
            matches = [
                row
                for row in record.expected_config.managed_secret_bindings
                if (row.integration, row.context, row.instance, row.binding_key)
                == ("runtime_environment", args.context, args.instance, args.binding_key)
            ]
            if len(matches) != 1:
                raise ValueError("Select an existing exact runtime requirement.")
            return matches[0]

        before = profile()
        if args.mode == "apply":
            saved = json.loads(evidence.read_text())
            if saved["request"] != payload:
                raise ValueError("The reviewed external binding intent changed.")
            planned = AcceptedEvidenceResponse.model_validate(saved["response"])
            if (
                not planned.result
                or planned.result.get("mode") != "dry-run"
                or planned.result.get("product") != args.product
            ):
                raise ValueError("A matching dry-run receipt is required.")
            reviewed = saved["requirement"]
            expected = {
                **reviewed,
                "external_provider": args.provider,
                "testing_local_credential": False,
            }
            if before not in (
                ProductSecretConfigRequirement.model_validate(reviewed),
                ProductSecretConfigRequirement.model_validate(expected),
            ):
                raise ValueError("The reviewed binding metadata changed.")
            payload["mode"] = "apply"
        submitted = args.mode == "apply"
        result = transport["request_launchplane"](
            service_url=settings["service_url"],
            settings=settings,
            path="/v1/product-profiles/expected-config/apply",
            body=payload,
            timeout=120,
            idempotency_key=args.idempotency_key if submitted else "",
        )
        receipt = AcceptedEvidenceResponse.model_validate(result)
        trace = transport["public_trace_id"](receipt.trace_id)
        if not receipt.result or (receipt.result.get("product"), receipt.result.get("mode")) != (
            args.product,
            args.mode,
        ):
            raise ValueError("Unexpected declaration receipt.")
        accepted = submitted
        if not submitted:
            with os.fdopen(
                os.open(evidence, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w"
            ) as output:
                json.dump(
                    {
                        "request": payload,
                        "response": receipt.model_dump(mode="json"),
                        "requirement": before.model_dump(mode="json"),
                    },
                    output,
                )
        else:
            after = profile()
            expected = {
                **saved["requirement"],
                "external_provider": args.provider,
                "testing_local_credential": False,
            }
            if after != ProductSecretConfigRequirement.model_validate(expected):
                raise ValueError("The declaration readback does not match the reviewed intent.")
        print(
            json.dumps(
                {
                    "status": "verified",
                    "mode": args.mode,
                    "trace_id": trace,
                    "provider": args.provider,
                    "binding_key": args.binding_key,
                    "runtime_modified": False,
                }
            )
        )
        return 0
    except urllib.error.HTTPError as error:
        if not accepted and (not submitted or error.code < 500):
            transport["emit_http_error_payload"](
                operation="external-resource-declaration", request={"mode": args.mode}, exc=error
            )
            return 1
    except (KeyError, TypeError, ValueError, OSError, http.client.HTTPException):
        pass
    print(
        json.dumps(
            {
                "status": "accepted_unverified"
                if accepted
                else "outcome_unknown"
                if submitted
                else "unverified",
                "trace_id": trace or None,
            }
        )
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
