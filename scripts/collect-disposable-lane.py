"""Bounded evidence-only client; use the installed operator helper's transport."""

import argparse
import json
from pathlib import Path
import runpy
import sys
import urllib.error

from control_plane.contracts.disposable_lane import (
    DISPOSABLE_LANE_OBSERVATION_ROUTE,
    DisposableLaneObservationRequest,
)
from control_plane.disposable_lane_collection import disposable_collection_coverage


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("inspect", "collect"))
    parser.add_argument("--operator-helper", type=Path, required=True)
    parser.add_argument("--config")
    parser.add_argument("--env-config")
    parser.add_argument("--url")
    for field in ("product", "context", "instance"):
        parser.add_argument(f"--{field}", required=True)
    parser.add_argument("--preview-id", default="")
    args = parser.parse_args()
    helper = args.operator_helper.expanduser().resolve(strict=True)
    if helper.name != "launchplane-write-action.py":
        parser.error("Select the installed Launchplane operator helper.")
    transport = runpy.run_path(str(helper), run_name="disposable_collection_transport")
    settings = transport["resolve_settings"](args)
    transport["validate_service_url"](settings["service_url"])
    if not settings["token"]:
        parser.error("The existing local operator credential is not configured.")
    payload = DisposableLaneObservationRequest(
        product=args.product,
        context=args.context,
        instance=args.instance,
        preview_id=args.preview_id,
    )
    try:
        response = transport["request_launchplane"](
            service_url=settings["service_url"],
            path=DISPOSABLE_LANE_OBSERVATION_ROUTE,
            settings=settings,
            body=payload.model_dump(mode="json"),
            timeout=30,
        )
        if response["result"]["mode"] != "inspect":
            raise ValueError("The inspection did not settle as requested.")
        if args.mode == "collect":
            digest = response["result"]["binding_sha256"]
            payload = DisposableLaneObservationRequest(
                **{**payload.model_dump(), "mode": "collect", "expected_binding_sha256": digest}
            )
            response = transport["request_launchplane"](
                service_url=settings["service_url"],
                path=DISPOSABLE_LANE_OBSERVATION_ROUTE,
                settings=settings,
                body=payload.model_dump(mode="json"),
                timeout=30,
            )
        result = response["result"]
        if result["mode"] != args.mode:
            raise ValueError("The collection did not settle as requested.")
        coverage = result["source_coverage"]
        if coverage != disposable_collection_coverage():
            raise ValueError("The collection coverage differs from this client.")
        facts = {key: result[key] for key in ("usage", "data", "production_resources")}
        for key, allowed in (
            ("usage", {"unknown", "unused", "in_use"}),
            ("data", {"unknown", "reproducible", "only_copy"}),
            ("production_resources", {"unknown", "isolated", "shared", "separation_ready"}),
        ):
            if facts[key] not in allowed:
                raise ValueError("Invalid fact response.")
        print(
            json.dumps(
                {
                    "mode": args.mode,
                    "facts": facts,
                    "source_coverage": coverage,
                    "trace_id": transport["public_trace_id"](response["trace_id"]),
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
    except (KeyError, TypeError, ValueError, OSError, urllib.error.URLError):
        print(json.dumps({"status": "unverified", "mode": args.mode}))
    return 1


if __name__ == "__main__":
    sys.exit(main())
