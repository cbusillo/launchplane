"""Bounded evidence-only client; use the installed operator helper's transport."""

import argparse
import http.client
import json
from pathlib import Path
import runpy
import re
import sys
import urllib.error

from control_plane.contracts.disposable_lane import (
    DISPOSABLE_LANE_OBSERVATION_ROUTE,
    DisposableLaneObservationRequest,
)
from control_plane.disposable_lane_collection import disposable_collection_coverage
from control_plane.contracts.application_resource_observation import ApplicationResourceObservation
from control_plane.odoo_testing_usage import ODOO_USAGE_REASON_CODES


def observation_body(payload: DisposableLaneObservationRequest) -> dict[str, object]:
    excluded = {"expected_observation_sha256"} if payload.mode != "clear" else set()
    if not payload.include_application_resources:
        excluded.add("include_application_resources")
    return payload.model_dump(mode="json", exclude=excluded)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("inspect", "collect", "clear"))
    parser.add_argument("--operator-helper", type=Path, required=True)
    parser.add_argument("--config")
    parser.add_argument("--env-config")
    parser.add_argument("--url")
    for field in ("product", "context", "instance"):
        parser.add_argument(f"--{field}", required=True)
    parser.add_argument("--preview-id", default="")
    parser.add_argument("--expected-binding-sha256", default="")
    parser.add_argument("--expected-observation-sha256", default="")
    parser.add_argument("--lifetime-seconds", type=int)
    parser.add_argument(
        "--application-resources",
        action="store_true",
        help="Observe existing testing application storage metadata without qualifying safety.",
    )
    args = parser.parse_args()
    if args.application_resources and (
        args.mode != "collect" or args.instance != "testing" or args.preview_id
    ):
        parser.error("--application-resources requires collect for testing without a preview ID.")
    if args.mode == "clear" and not re.fullmatch(r"[0-9a-f]{64}", args.expected_binding_sha256):
        parser.error("Clearing requires --expected-binding-sha256 from a reviewed inspection.")
    if args.mode == "clear" and not re.fullmatch(r"[0-9a-f]{64}", args.expected_observation_sha256):
        parser.error(
            "Clearing requires --expected-observation-sha256 from the same reviewed inspection."
        )
    if args.mode != "clear" and (args.expected_binding_sha256 or args.expected_observation_sha256):
        parser.error("Only clear accepts an explicitly reviewed binding.")
    helper = args.operator_helper.expanduser().resolve(strict=True)
    if helper.name != "launchplane-write-action.py":
        parser.error("Select the installed Launchplane operator helper.")
    transport = runpy.run_path(str(helper), run_name="disposable_collection_transport")
    settings = transport["resolve_settings"](args)
    transport["validate_service_url"](settings["service_url"])
    if not settings["token"]:
        parser.error("The existing local operator credential is not configured.")
    payload = DisposableLaneObservationRequest(
        mode="clear" if args.mode == "clear" else "inspect",
        product=args.product,
        context=args.context,
        instance=args.instance,
        preview_id=args.preview_id,
        expected_binding_sha256=args.expected_binding_sha256,
        expected_observation_sha256=args.expected_observation_sha256,
    )
    try:
        response = transport["request_launchplane"](
            service_url=settings["service_url"],
            path=DISPOSABLE_LANE_OBSERVATION_ROUTE,
            settings=settings,
            body=observation_body(payload),
            timeout=30,
        )
        if args.mode == "clear":
            if (
                response["result"]["mode"] != "clear"
                or response["result"]["provider_mutation"] is not False
                or response["result"]["binding_sha256"] != payload.expected_binding_sha256
                or response["result"]["observation_sha256"] != payload.expected_observation_sha256
            ):
                raise ValueError("The observation clear did not settle as requested.")
            print(
                json.dumps(
                    {
                        "mode": "clear",
                        "trace_id": transport["public_trace_id"](response["trace_id"]),
                    }
                )
            )
            return 0
        if response["result"]["mode"] != "inspect":
            raise ValueError("The inspection did not settle as requested.")
        if args.mode == "collect":
            digest = response["result"]["binding_sha256"]
            payload = DisposableLaneObservationRequest(
                **{
                    **payload.model_dump(),
                    "mode": args.mode,
                    "expected_binding_sha256": digest,
                    "include_application_resources": args.application_resources,
                    **(
                        {"lifetime_seconds": args.lifetime_seconds}
                        if args.lifetime_seconds is not None
                        else {}
                    ),
                }
            )
            response = transport["request_launchplane"](
                service_url=settings["service_url"],
                path=DISPOSABLE_LANE_OBSERVATION_ROUTE,
                settings=settings,
                body=observation_body(payload),
                timeout=240,
            )
        result = response["result"]
        inspection: dict[str, object] = {}
        if args.mode == "inspect":
            digest = result["binding_sha256"]
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError("The inspection did not return a valid binding.")
            inspection["binding_sha256"] = digest
            if "observation_sha256" in result:
                observation_digest = result["observation_sha256"]
                if not isinstance(observation_digest, str) or not re.fullmatch(
                    r"[0-9a-f]{64}", observation_digest
                ):
                    raise ValueError("The inspection did not return a valid observation digest.")
                report = result["observation"]
                if report is not None:
                    report = {key: report[key] for key in ("usage", "data", "production_resources")}
                    if (
                        report["usage"] not in {"unknown", "unused", "in_use"}
                        or report["data"] not in {"unknown", "reproducible", "only_copy"}
                        or report["production_resources"]
                        not in {"unknown", "isolated", "shared", "separation_ready"}
                    ):
                        raise ValueError("Invalid recorded observation.")
                inspection.update(observation_sha256=observation_digest, observation=report)
        if result["mode"] != args.mode:
            raise ValueError("The collection did not settle as requested.")
        if args.mode == "collect" and (
            result["binding_sha256"] != payload.expected_binding_sha256
            or result["provider_mutation"] is not False
        ):
            raise ValueError("The collection receipt does not match the inert inspection.")
        coverage = result["source_coverage"]
        if coverage != disposable_collection_coverage():
            raise ValueError("The collection coverage differs from this client.")
        facts = {key: result[key] for key in ("usage", "data", "production_resources")}
        for key, allowed in (
            ("usage", {"unknown", "in_use", "unused"}),
            ("data", {"unknown", "reproducible", "only_copy"}),
            ("production_resources", {"unknown", "shared", "isolated", "separation_ready"}),
        ):
            if facts[key] not in allowed:
                raise ValueError("Invalid fact response.")
        usage = result.get("odoo_usage", {})
        if not isinstance(usage, dict):
            raise ValueError("Invalid Odoo usage evidence.")
        usage_facts = {key: usage.get(key) for key in ("recent_signins", "authenticated_sessions")}
        reason_code = usage.get("reason_code", "diagnostics_unavailable")
        if not isinstance(reason_code, str) or not re.fullmatch(
            r"[a-z][a-z0-9_]{0,63}", reason_code
        ):
            raise ValueError("Invalid Odoo usage diagnostic.")
        if reason_code not in ODOO_USAGE_REASON_CODES:
            reason_code = "diagnostics_unrecognized"
        if "reason_code" in usage and (
            (facts["usage"] == "unused" and reason_code not in {"complete", "never_served"})
            or (facts["usage"] == "unknown" and reason_code in {"complete", "never_served"})
        ):
            raise ValueError("Odoo usage and diagnostic disagree.")
        if any(value is not None and type(value) is not bool for value in usage_facts.values()):
            raise ValueError("Invalid Odoo usage evidence.")
        historical_usage_retained = result.get("historical_usage_retained", False)
        if type(historical_usage_retained) is not bool or (
            historical_usage_retained and facts["usage"] != "in_use"
        ):
            raise ValueError("Invalid retained usage evidence.")
        if (
            args.mode == "collect"
            and facts["usage"] == "unused"
            and any(
                value is not (None if reason_code == "never_served" else False)
                for value in usage_facts.values()
            )
        ):
            raise ValueError("Unused requires complete Odoo usage evidence.")
        qualification = result.get(
            "never_served_qualification", "never_served_diagnostics_unavailable"
        )
        if qualification not in {
            "never_served",
            "never_served_records_incomplete",
            "never_served_protection_seen",
            "never_served_not_attempted",
        }:
            qualification = "never_served_diagnostics_unavailable"
        if (reason_code == "never_served") != (qualification == "never_served"):
            raise ValueError("Never-served qualification and usage disagree.")
        application_resources = {}
        if args.application_resources:
            application_resources["application_resources"] = (
                ApplicationResourceObservation.model_validate(
                    result.get("application_resources")
                ).model_dump(mode="json")
            )
        print(
            json.dumps(
                {
                    "mode": args.mode,
                    **inspection,
                    "facts": facts,
                    "historical_usage_retained": historical_usage_retained,
                    "odoo_usage": {**usage_facts, "reason_code": reason_code},
                    "never_served_qualification": qualification,
                    "source_coverage": coverage,
                    "trace_id": transport["public_trace_id"](response["trace_id"]),
                    **application_resources,
                }
            )
        )
        return 0
    except urllib.error.HTTPError as error:
        transport["emit_http_error_payload"](
            operation="disposable-lane-collection",
            request={"product": args.product, "context": args.context, "instance": args.instance},
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
        print(json.dumps({"status": "unverified", "mode": args.mode}))
    return 1


if __name__ == "__main__":
    sys.exit(main())
