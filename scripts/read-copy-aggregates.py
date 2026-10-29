import argparse
import json
from pathlib import Path
import runpy
import sys
import urllib.error
from urllib.parse import quote

from control_plane.http_routes.odoo_runtime import AggregateCountsResponse
from control_plane.odoo_copy_aggregates import AggregateQuery, ROUTE
from control_plane import odoo_snapshot_version as version


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read counts or original-bound version metadata from an immutable testing snapshot."
    )
    parser.add_argument("--operator-helper", type=Path, required=True)
    parser.add_argument("--product", required=True)
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--table")
    selector.add_argument("--original-reservation-id")
    for name in ("key", "partition", "expected-snapshot-id", "expected-database-sha256"):
        parser.add_argument(f"--{name}")
    parser.add_argument("--active")
    parser.add_argument("--config")
    parser.add_argument("--env-config")
    parser.add_argument("--url")
    args = parser.parse_args()
    if any(
        value == ""
        for value in (
            args.product,
            args.original_reservation_id,
            args.table,
            args.key,
            args.partition,
            args.active,
            args.expected_snapshot_id,
            args.expected_database_sha256,
        )
    ):
        parser.error("Supplied read arguments cannot be empty.")
    if args.original_reservation_id is not None:
        if not args.expected_snapshot_id or not args.expected_database_sha256:
            parser.error(
                "Version reads require --expected-snapshot-id and --expected-database-sha256."
            )
        if args.key is not None or args.partition is not None or args.active is not None:
            parser.error("Version reads do not accept aggregate column options.")
    elif args.key is None:
        parser.error("Aggregate reads require --key.")
    elif args.expected_snapshot_id is not None or args.expected_database_sha256 is not None:
        parser.error("Original snapshot bindings require --original-reservation-id.")
    helper = args.operator_helper.expanduser().resolve(strict=True)
    if helper.name != "launchplane-write-action.py":
        parser.error("Select the installed Launchplane operator helper.")
    try:
        query: AggregateQuery | version.SnapshotVersionQuery
        if args.original_reservation_id is not None:
            query = version.SnapshotVersionQuery.model_validate(
                {
                    "original_reservation_id": args.original_reservation_id,
                    "expected_snapshot_id": args.expected_snapshot_id,
                    "expected_database_sha256": args.expected_database_sha256,
                }
            )
            path = version.ROUTE.format(product=quote(args.product, safe=""))
        else:
            query = AggregateQuery.model_validate(
                {
                    "table": args.table,
                    "key": args.key,
                    "partition": args.partition,
                    "active": "active" if args.active is None else args.active,
                }
            )
            path = ROUTE.format(product=quote(args.product, safe=""), environment="testing")
        transport = runpy.run_path(str(helper), run_name="copy_aggregate_transport")
        settings = transport["resolve_settings"](args)
        transport["validate_service_url"](settings["service_url"])
        if not settings["token"]:
            parser.error("The existing operator credential is not configured.")
        raw = transport["request_launchplane_read"](
            service_url=settings["service_url"],
            path=path,
            settings=settings,
            query=query.model_dump(mode="json", exclude_none=True),
            timeout=180,
        )
        response = (
            version.SnapshotVersionResponse.model_validate(raw)
            if isinstance(query, version.SnapshotVersionQuery)
            else AggregateCountsResponse.model_validate(raw)
        )
        if isinstance(query, version.SnapshotVersionQuery) and isinstance(
            response, version.SnapshotVersionResponse
        ):
            result = response.result
            if (
                result.original_reservation_id,
                result.snapshot_id,
                result.snapshot_database_sha256,
            ) != (
                query.original_reservation_id,
                query.expected_snapshot_id,
                query.expected_database_sha256,
            ):
                raise ValueError("Snapshot response binding mismatch")
        if response.result.product != args.product:
            raise ValueError("Aggregate product mismatch")
        transport["public_trace_id"](response.trace_id)
        print(response.model_dump_json())
        return 0
    except urllib.error.HTTPError as error:
        try:
            raw = json.loads(error.read(4096))
            trace = transport["public_trace_id"](raw.get("trace_id"))
            code = raw.get("error", {}).get("code", "aggregate_unavailable")
            if code not in {
                "database_storage_required",
                "snapshot_version_unavailable",
                "snapshot_original_unavailable",
                "snapshot_binding_mismatch",
                "snapshot_unavailable",
                "archive_tool_unavailable",
                "archive_unreadable",
                "aggregate_unavailable",
                "authorization_denied",
                "invalid_request",
                "invalid_odoo_environment",
                "not_found",
                "authz_policy_unavailable",
                "odoo_runtime_read_unavailable",
                "authentication_required",
            }:
                code = "aggregate_unavailable"
        except Exception:
            trace = ""
            code = "aggregate_unavailable"
        finally:
            error.close()
        print(
            json.dumps(
                {
                    "status": "unavailable",
                    "http_status": error.code,
                    "trace_id": trace,
                    "error_code": code,
                }
            ),
            file=sys.stderr,
        )
    except Exception:
        print('{"status":"unavailable"}', file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
