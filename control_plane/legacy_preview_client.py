"""Bounded service client; configuration and authority come from the caller."""

import argparse
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests

from control_plane.contracts.product_retirement import canonical_sha256
from control_plane.legacy_preview_reconciliation import LegacyPreviewReconciliationRequest


def prepare_request(
    payload: dict[str, Any], reviewed: dict[str, Any] | None
) -> LegacyPreviewReconciliationRequest:
    if payload.get("mode") == "apply":
        if not reviewed or reviewed.get("status") != "accepted":
            raise ValueError("Apply requires a saved successful helper plan.")
        result = reviewed.get("result", {})
        if (
            result.get("mode") != "plan"
            or result.get("apply_eligible") is not True
            or result.get("provider_absence_verified") is not True
            or result.get("provider_state") != "absent"
            or result.get("provider_writes") is not False
        ):
            raise ValueError("Saved plan is not apply eligible.")
        plan_input = {
            k: v
            for k, v in payload.items()
            if k not in {"plan_idempotency_key", "expected_plan_digest", "reviewed_plan"}
        }
        plan_input["mode"] = "plan"
        plan = LegacyPreviewReconciliationRequest.model_validate(plan_input)
        if reviewed.get("request_digest") != canonical_sha256(plan.model_dump(mode="json")):
            raise ValueError("Saved helper plan belongs to another request.")
        payload = {
            **payload,
            "plan_idempotency_key": reviewed.get("idempotency_key", ""),
            "expected_plan_digest": result.get("plan_digest", ""),
            "reviewed_plan": True,
        }
    return LegacyPreviewReconciliationRequest.model_validate(payload)


def send_request(
    *,
    payload: dict[str, Any],
    reviewed: dict[str, Any] | None,
    idempotency_key: str,
    service_url: str,
    token: str,
    contract: dict[str, Any],
) -> dict[str, Any]:
    request = prepare_request(payload, reviewed)
    parsed = urlsplit(service_url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Caller must configure an HTTPS service origin.")
    if not token or (request.mode != "inspect" and not idempotency_key.strip()):
        raise ValueError("Caller credentials and plan/apply idempotency are required.")
    operations = [
        op
        for op in contract.get("contract", {}).get("operations", [])
        if op.get("operation_id") == "reconcile_legacy_generic_web_preview"
    ]
    if (
        len(operations) != 1
        or operations[0].get("method") != "POST"
        or request.mode not in operations[0].get("modes", [])
    ):
        raise ValueError("The public contract does not project this operation unambiguously.")
    route = operations[0].get("path", "")
    if (
        not isinstance(route, str)
        or not route.startswith("/v1/")
        or "?" in route
        or "#" in route
        or ".." in route
    ):
        raise ValueError("Projected route is invalid.")
    response = requests.post(
        service_url.rstrip("/") + route,
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": idempotency_key},
        json=request.model_dump(mode="json"),
        timeout=60,
        allow_redirects=False,
    )
    if response.status_code != 202:
        # Error/provider messages may contain private details. Keep only protocol evidence.
        return {"status": "unavailable", "http_status": response.status_code}
    data = response.json()
    result = data.get("result", {})
    if (
        data.get("status") != "accepted"
        or result.get("product") != request.product
        or result.get("preview_id") != request.preview_id
        or result.get("mode") != request.mode
    ):
        raise ValueError("Service response does not match the request.")
    fields = {
        "product",
        "preview_id",
        "context",
        "preview_slug",
        "preview_state",
        "destroyed_at",
        "action",
        "apply_eligible",
        "authority_digest",
        "provider_state",
        "provider_absence_verified",
        "inventory_digest",
        "provider_application_count",
        "provider_writes",
        "plan_digest",
        "mode",
    }
    return {
        "status": "accepted",
        "trace_id": data.get("trace_id", ""),
        "idempotency_key": idempotency_key,
        "request_digest": canonical_sha256(request.model_dump(mode="json")),
        "result": {k: v for k, v in result.items() if k in fields},
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Inspect, plan or apply one reviewed legacy preview reconciliation."
    )
    parser.add_argument("--payload-file", required=True, type=Path)
    parser.add_argument("--reviewed-plan-file", type=Path)
    parser.add_argument("--idempotency-key", default="")
    parser.add_argument(
        "--contract-file",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "contracts/agent-operator-contract.json",
    )
    args = parser.parse_args()
    try:
        result = send_request(
            payload=json.loads(args.payload_file.read_text()),
            reviewed=json.loads(args.reviewed_plan_file.read_text())
            if args.reviewed_plan_file
            else None,
            idempotency_key=args.idempotency_key,
            service_url=os.environ.get("LAUNCHPLANE_OPERATOR_URL", ""),
            token=os.environ.get("LAUNCHPLANE_LOCAL_OPERATOR_TOKEN", ""),
            contract=json.loads(args.contract_file.read_text()),
        )
    except (ValueError, OSError, requests.RequestException):
        print(
            json.dumps({"status": "unavailable", "reason": "invalid_input_or_unconfirmed_response"})
        )
        raise SystemExit(1) from None
    print(json.dumps(result, sort_keys=True))
    if result["status"] != "accepted":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
