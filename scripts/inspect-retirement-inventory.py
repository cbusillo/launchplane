"""Read value-free retirement evidence through the existing operator transport.

This client has no apply mode. Routes come from the canonical OpenAPI artifact.
"""

import argparse
import http.client
import json
from pathlib import Path
import re
import runpy
import sys
from typing import Any
import urllib.error

from control_plane.contracts.authz_retirement_evidence import (
    AUTHZ_RETIREMENT_MAX_CANDIDATES,
    AuthzRetirementEvidenceRequest,
    AuthzRetirementEvidenceResponse,
    AuthzWorkflowEvidenceCandidate,
)
from control_plane.http_routes.operational_records import RuntimeSettingsResponse


def operation_route(operation: str, method: str) -> str:
    document = json.loads(
        (
            Path(__file__).resolve().parents[1] / "frontend/generated/openapi-canonical.json"
        ).read_text()
    )
    matches = [
        path
        for path, methods in document["paths"].items()
        if methods.get(method, {}).get("operationId") == operation
    ]
    if len(matches) != 1:
        raise ValueError("The canonical service operation is unavailable or ambiguous.")
    return str(matches[0])


def identifier(value: str) -> str:
    if value in {".", ".."} or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", value):
        raise argparse.ArgumentTypeError("Use one plain context or instance identifier.")
    return value


def inspect_settings(
    transport: dict[str, Any], settings: dict[str, str], args: argparse.Namespace
) -> dict[str, object]:
    operation = (
        "list_instance_runtime_settings" if args.instance else "list_context_runtime_settings"
    )
    path = operation_route(operation, "get").format(context=args.context, instance=args.instance)
    raw = transport["request_launchplane_read"](
        service_url=settings["service_url"], path=path, settings=settings, query={}, timeout=30
    )
    response = RuntimeSettingsResponse.model_validate(raw)
    if (response.context, response.instance) != (args.context, args.instance):
        raise ValueError("Runtime inventory scope mismatch.")
    records = []
    seen = set()
    for record in response.settings:
        scope = (record.scope, record.context, record.instance)
        if scope in seen or not (
            scope == ("global", "", "")
            or scope == ("context", args.context, "")
            or bool(args.instance)
            and scope == ("instance", args.context, args.instance)
        ):
            raise ValueError("Runtime inventory record scope mismatch.")
        seen.add(scope)
        if (
            len(record.keys) != len(set(record.keys))
            or set(record.values) | set(record.withheld_keys) != set(record.keys)
            or set(record.values) & set(record.withheld_keys)
            or any(not key or key != key.strip() for key in record.keys)
        ):
            raise ValueError("Runtime inventory keys do not reconcile.")
        records.append(
            {
                "scope": record.scope,
                "context": record.context,
                "instance": record.instance,
                "keys": sorted(record.keys),
                "withheld_keys": sorted(record.withheld_keys),
                "updated_at": record.updated_at,
            }
        )
    return {
        "trace_id": transport["public_trace_id"](response.trace_id),
        "settings": records,
        "coverage": "caller_visible_records_only",
        "absence_proven": False,
    }


def validate_metadata(value: object, transport: dict[str, Any]) -> None:
    """Validate value-free metadata as identifiers, rather than free-text logs."""
    if isinstance(value, dict):
        for key, item in value.items():
            if transport["is_denied_key"](key):
                raise ValueError("Unsafe metadata field.")
            validate_metadata(item, transport)
    elif isinstance(value, (list, tuple)):
        for item in value:
            validate_metadata(item, transport)
    elif isinstance(value, str) and value:
        # Permit a leading underscore in an env key, retaining token boundaries.
        transport["public_identifier"]("metadata/" + value)


def inspect_policy(
    transport: dict[str, Any], settings: dict[str, str], args: argparse.Namespace
) -> dict[str, object]:
    raw = transport["request_launchplane_read"](
        service_url=settings["service_url"],
        path=operation_route("read_active_authz_policy", "get"),
        settings=settings,
        query={},
        timeout=30,
    )
    if raw.get("status") != "ok" or not isinstance(raw.get("policy"), dict):
        raise ValueError("Active policy summary is unavailable.")
    policy = raw["policy"]
    # The service summary contains fingerprints, never complete authorization rules.
    rules = []
    for rule in policy["managed_rules"]:
        if rule["principal_type"] == "github_actions":
            rules.append(
                {
                    key: rule[key]
                    for key in (
                        "managed_set_id",
                        "managed_rule_id",
                        "principal_type",
                        "rule_sha256",
                    )
                }
            )
    result: dict[str, object] = {
        "trace_id": transport["public_trace_id"](raw["trace_id"]),
        "policy": {key: policy[key] for key in ("record_id", "revision", "policy_sha256")},
        "managed_workflow_rules": rules,
        "unmanaged_rule_counts": policy["unmanaged_rule_counts"],
        "coverage": "managed_rule_identities_and_selected_unmanaged_workflows",
        "workflow_scope_details_available": False,
    }
    if args.workflow:
        candidates = tuple(
            AuthzWorkflowEvidenceCandidate(
                repository=args.repository, workflow_path=path, binding=args.binding
            )
            for path in args.workflow
        )
        request = AuthzRetirementEvidenceRequest(
            policy_record_id=policy["record_id"],
            policy_revision=policy["revision"],
            policy_sha256=policy["policy_sha256"],
            workflows=candidates,
        )
        evidence = AuthzRetirementEvidenceResponse.model_validate(
            transport["request_launchplane"](
                service_url=settings["service_url"],
                path=operation_route("read_authz_retirement_evidence", "post"),
                settings=settings,
                body=request.model_dump(mode="json"),
                timeout=30,
            )
        )
        expected = request.policy_record_id, request.policy_revision, request.policy_sha256
        if (
            evidence.policy.record_id,
            evidence.policy.revision,
            evidence.policy.policy_sha256,
        ) != expected:
            raise ValueError("Policy changed during the evidence read.")
        if sorted(item.index for item in evidence.unmanaged_workflows) != list(
            range(len(candidates))
        ):
            raise ValueError("Incomplete workflow comparison response.")
        result["workflow_evidence"] = evidence.model_dump(mode="json")
        result["workflow_candidates"] = [
            candidate.model_dump(mode="json") for candidate in candidates
        ]
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operator-helper", type=Path, required=True)
    parser.add_argument("--config")
    parser.add_argument("--env-config")
    parser.add_argument("--url")
    subparsers = parser.add_subparsers(dest="mode", required=True)
    runtime = subparsers.add_parser("settings")
    runtime.add_argument("--context", type=identifier, required=True)
    runtime.add_argument("--instance", type=identifier)
    policy = subparsers.add_parser("policy")
    policy.add_argument("--repository", default="")
    policy.add_argument("--workflow", action="append", default=[])
    policy.add_argument(
        "--binding", choices=("workflow_ref", "job_workflow_ref"), default="workflow_ref"
    )
    args = parser.parse_args()
    if args.mode == "settings":
        args.instance = args.instance or ""
    elif bool(args.repository) != bool(args.workflow):
        parser.error("Use --repository and --workflow together.")
    elif len(args.workflow) > AUTHZ_RETIREMENT_MAX_CANDIDATES or len(args.workflow) != len(
        set(args.workflow)
    ):
        parser.error(f"Use at most {AUTHZ_RETIREMENT_MAX_CANDIDATES} distinct workflow paths.")
    helper = args.operator_helper.expanduser().resolve(strict=True)
    if helper.name != "launchplane-write-action.py":
        parser.error("Select the installed Launchplane operator helper.")
    transport = runpy.run_path(str(helper), run_name="retirement_inventory_transport")
    try:
        settings = transport["resolve_settings"](args)
        transport["validate_service_url"](settings["service_url"])
        if not settings["token"]:
            raise ValueError("Operator configuration is unavailable.")
        result = (inspect_settings if args.mode == "settings" else inspect_policy)(
            transport, settings, args
        )
        validate_metadata(result, transport)
        print(json.dumps({"status": "ok", "mode": args.mode, "result": result}))
        return 0
    except urllib.error.HTTPError as error:
        transport["emit_http_error_payload"](
            operation="retirement-inventory", request={"mode": args.mode}, exc=error
        )
    except (KeyError, TypeError, ValueError):
        print(json.dumps({"status": "unverified", "mode": args.mode, "code": "invalid_inventory"}))
    except (OSError, http.client.HTTPException):
        print(
            json.dumps({"status": "unverified", "mode": args.mode, "code": "transport_unavailable"})
        )
    return 1


if __name__ == "__main__":
    sys.exit(main())
