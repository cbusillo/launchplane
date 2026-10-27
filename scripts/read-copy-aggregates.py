import argparse
import json
from pathlib import Path
import runpy
import sys
import urllib.error
from urllib.parse import quote

from control_plane.http_routes.odoo_runtime import AggregateCountsResponse
from control_plane.odoo_copy_aggregates import AggregateQuery, ROUTE


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read counts from a qualified disposable testing copy."
    )
    parser.add_argument("--operator-helper", type=Path, required=True)
    parser.add_argument("--product", required=True)
    for name in ("table", "key", "partition"):
        parser.add_argument(f"--{name}", required=name != "partition")
    parser.add_argument("--active", default="active")
    parser.add_argument("--config")
    parser.add_argument("--env-config")
    parser.add_argument("--url")
    args = parser.parse_args()
    helper = args.operator_helper.expanduser().resolve(strict=True)
    if helper.name != "launchplane-write-action.py":
        parser.error("Select the installed Launchplane operator helper.")
    try:
        query = AggregateQuery(
            table=args.table, key=args.key, partition=args.partition, active=args.active
        )
        transport = runpy.run_path(str(helper), run_name="copy_aggregate_transport")
        settings = transport["resolve_settings"](args)
        transport["validate_service_url"](settings["service_url"])
        if not settings["token"]:
            parser.error("The existing operator credential is not configured.")
        raw = transport["request_launchplane"](
            service_url=settings["service_url"],
            path=ROUTE.format(product=quote(args.product, safe=""), environment="testing"),
            settings=settings,
            body=query.model_dump(mode="json"),
            timeout=180,
        )
        response = AggregateCountsResponse.model_validate(raw)
        if response.result.product != args.product:
            raise ValueError("Aggregate product mismatch")
        transport["public_trace_id"](response.trace_id)
        print(response.model_dump_json())
        return 0
    except urllib.error.HTTPError as error:
        try:
            raw = json.loads(error.read(4096))
            trace = transport["public_trace_id"](raw.get("trace_id"))
        except Exception:
            trace = ""
        finally:
            error.close()
        print(
            json.dumps({"status": "unavailable", "http_status": error.code, "trace_id": trace}),
            file=sys.stderr,
        )
    except Exception:
        print('{"status":"unavailable"}', file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
