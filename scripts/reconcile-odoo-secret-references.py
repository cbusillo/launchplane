"""Review or correct an Odoo lane's config-parameter secret references using the installed operator transport."""

import argparse
import http.client
import json
import os
from pathlib import Path
import runpy
import sys
import urllib.error
from urllib.parse import quote

from control_plane.http_routes.odoo_secret_references import ROUTE, SecretReferenceResponse


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("read", "dry-run", "apply"))
    parser.add_argument("--operator-helper", type=Path, required=True)
    parser.add_argument("--product", required=True)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--evidence-file", type=Path)
    parser.add_argument("--reviewed-dry-run", action="store_true")
    parser.add_argument("--config")
    parser.add_argument("--env-config")
    parser.add_argument("--url")
    args = parser.parse_args()
    if args.mode != "read" and not args.evidence_file:
        parser.error("Planning and apply require a private evidence file.")
    if args.mode == "apply" and not args.reviewed_dry_run:
        parser.error("Apply requires the reviewed dry-run evidence.")
    evidence = args.evidence_file.expanduser().resolve() if args.evidence_file else None
    if evidence and (
        evidence.is_relative_to(Path(__file__).resolve().parents[1])
        or args.evidence_file.is_symlink()
    ):
        parser.error("Use a private evidence file outside the repository.")
    if args.mode == "dry-run" and evidence and evidence.exists():
        parser.error("Preserve the existing evidence; select a new file.")
    helper = args.operator_helper.expanduser().resolve(strict=True)
    if helper.name != "launchplane-write-action.py":
        parser.error("Select the installed Launchplane operator helper.")
    transport = runpy.run_path(str(helper), run_name="secret_references_transport")
    settings = transport["resolve_settings"](args)
    transport["validate_service_url"](settings["service_url"])
    if not settings["token"]:
        parser.error("The existing operator credential is unavailable.")
    path = ROUTE.format(
        product=quote(args.product, safe=""), environment=quote(args.environment, safe="")
    )
    submitted = False
    accepted = False
    trace = ""
    try:

        def read() -> SecretReferenceResponse:
            return SecretReferenceResponse.model_validate(
                transport["request_launchplane_read"](
                    service_url=settings["service_url"],
                    path=path,
                    settings=settings,
                    query={},
                    timeout=120,
                )
            )

        report = read()
        trace = transport["public_trace_id"](report.trace_id)
        payload: dict[str, object] = {}
        if args.mode == "dry-run":
            payload = {
                "mode": "dry-run",
                "keys": [entry.key for entry in report.result.entries],
            }
        elif args.mode == "apply":
            assert evidence is not None
            saved = json.loads(evidence.read_text())
            review = SecretReferenceResponse.model_validate(saved["response"])
            if (review.result.product, review.result.instance) != (
                args.product,
                args.environment,
            ):
                raise ValueError("The reviewed lane changed.")
            payload = {
                "mode": "apply",
                "keys": saved["request"]["keys"],
                "review_digest": review.result.review_digest,
                "confirmation": f"APPLY {args.product}/{args.environment}",
            }
        if args.mode != "read":
            submitted = args.mode == "apply"
            response = SecretReferenceResponse.model_validate(
                transport["request_launchplane"](
                    service_url=settings["service_url"],
                    path=path,
                    settings=settings,
                    body=payload,
                    timeout=120,
                )
            )
            trace = transport["public_trace_id"](response.trace_id)
            accepted = submitted
            if args.mode == "dry-run":
                assert evidence is not None
                with os.fdopen(
                    os.open(evidence, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w"
                ) as output:
                    json.dump(
                        {"request": payload, "response": response.model_dump(mode="json")}, output
                    )
                report = response
            else:
                report = read()
                if response.result.read_back_matches is not True or any(
                    entry.action != "already_agrees" for entry in report.result.entries
                ):
                    raise ValueError("Independent readback does not agree.")
        print(
            json.dumps(
                {
                    "status": "verified",
                    "mode": args.mode,
                    "trace_id": trace,
                    "entries": [entry.model_dump(mode="json") for entry in report.result.entries],
                    "review_digest": report.result.review_digest,
                }
            )
        )
        return 0
    except urllib.error.HTTPError as error:
        if not accepted and (not submitted or error.code < 500):
            transport["emit_http_error_payload"](
                operation="odoo-secret-references", request={"mode": args.mode}, exc=error
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
