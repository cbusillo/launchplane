import argparse
import json
import os
from pathlib import Path
import runpy
import urllib.error

from control_plane.contracts.integration_attestation import (
    INTEGRATION_ATTESTATION_ROUTE,
    IntegrationAttestationRequest,
    IntegrationAttestationResponse,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Record or read an exact reviewed integration attestation."
    )
    parser.add_argument("mode", choices=("dry-run", "apply", "readback"))
    parser.add_argument("--operator-helper", type=Path, required=True)
    parser.add_argument("--request-file", type=Path, required=True)
    parser.add_argument("--response-file", type=Path, required=True)
    parser.add_argument("--evidence-file", type=Path)
    parser.add_argument("--config")
    parser.add_argument("--env-config")
    parser.add_argument("--url")
    args = parser.parse_args()
    if args.response_file.exists():
        parser.error("Choose a new private response file; existing evidence is never overwritten.")
    request = IntegrationAttestationRequest.model_validate_json(args.request_file.read_text())
    request = request.model_copy(update={"mode": args.mode})
    if args.mode != "dry-run":
        if args.evidence_file is None:
            parser.error("Apply and readback require --evidence-file from the original dry run.")
        reviewed = IntegrationAttestationResponse.model_validate_json(
            args.evidence_file.read_text()
        )
        evidence = reviewed.evidence
        if (
            reviewed.evidence_sha256 != evidence.digest()
            or request.product != evidence.product
            or request.repository != evidence.repository
            or request.pull_request_number != evidence.pull_request_number
            or request.merge_sha != evidence.merge_sha
            or request.source_decision_id != evidence.source_decision_id
        ):
            parser.error("The saved evidence does not match the exact attestation request.")
        request = request.model_copy(update={"reviewed_evidence_sha256": evidence.digest()})
        if args.mode == "apply" and (
            request.review is None
            or request.review.evidence_sha256 != evidence.digest()
            or not request.statement.strip()
        ):
            parser.error(
                "Apply requires a confirming independent model review and engineering statement."
            )
    helper = args.operator_helper.resolve(strict=True)
    if helper.name != "launchplane-write-action.py":
        parser.error("Select the installed Launchplane admin helper.")
    transport = runpy.run_path(str(helper), run_name="integration_attestation_transport")
    settings = transport["resolve_settings"](args)
    transport["validate_service_url"](settings["service_url"])
    if not settings["token"]:
        parser.error("The existing operator credential is unavailable.")
    try:
        raw = transport["request_launchplane"](
            service_url=settings["service_url"],
            path=INTEGRATION_ATTESTATION_ROUTE,
            settings=settings,
            body=request.model_dump(mode="json"),
            timeout=130,
        )
        response = IntegrationAttestationResponse.model_validate(raw)
        evidence = response.evidence
        if (
            response.evidence_sha256 != evidence.digest()
            or evidence.product != request.product
            or evidence.repository != request.repository
            or evidence.pull_request_number != request.pull_request_number
            or evidence.merge_sha != request.merge_sha
            or evidence.source_decision_id != request.source_decision_id
            or args.mode != "dry-run"
            and (response.record is None or evidence.digest() != request.reviewed_evidence_sha256)
        ):
            raise ValueError("Unverifiable response")
    except urllib.error.HTTPError as error:
        transport["emit_http_error_payload"](
            operation="integration-attestation", request={"mode": args.mode}, exc=error
        )
        return 1
    except (ValueError, OSError, urllib.error.URLError):
        print(
            json.dumps(
                {
                    "status": "unverified",
                    "next_action": "Read back the original exact evidence before any apply retry.",
                }
            )
        )
        return 1
    descriptor = os.open(args.response_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as output:
        output.write(response.model_dump_json(indent=2) + "\n")
    print(
        json.dumps(
            {
                "status": response.status,
                "trace_id": response.trace_id,
                "evidence_sha256": response.evidence_sha256,
                "record_id": response.record.record_id if response.record else None,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
