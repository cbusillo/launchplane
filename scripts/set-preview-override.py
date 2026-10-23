import argparse
import copy
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import runpy
from typing import Any
import urllib.error
import urllib.request
from urllib.parse import quote

from control_plane.runtime_platform_credentials import (
    PlatformCredentialRefusedError,
    refuse_platform_credentials,
)


class PreviewOverrideRefused(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _request(
    transport: dict[str, Any],
    settings: dict[str, str],
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    revision: str = "",
    idempotency_key: str = "",
) -> tuple[dict[str, Any], str]:
    headers = {"Authorization": f"Bearer {settings['token']}", "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body, separators=(",", ":")).encode()
        headers["Content-Type"] = "application/json"
    if revision:
        headers["If-Match"] = revision
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    request = urllib.request.Request(
        transport["build_url"](settings["service_url"], path),
        data=data,
        headers=headers,
        method=method,
    )
    with transport["safe_urlopen"](request, timeout=30) as response:
        return transport["_read_service_response"](response), response.headers.get("ETag", "")


def _private_file(path: Path, repository: Path) -> Path:
    expanded = path.expanduser()
    resolved = expanded.resolve()
    if (
        expanded.is_symlink()
        or resolved.is_relative_to(repository)
        or any((parent / ".git").exists() for parent in resolved.parents)
    ):
        raise PreviewOverrideRefused("private_file_inside_checkout_or_symlink")
    if resolved.exists() and resolved.stat().st_mode & 0o077:
        raise PreviewOverrideRefused("private_file_permissions")
    return resolved


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Review and save one preview runtime override without printing its value."
    )
    parser.add_argument("mode", choices=("dry-run", "apply", "readback"))
    for name in ("product", "key", "reason"):
        parser.add_argument("--" + name, required=True)
    for name in ("value-file", "evidence-file", "operator-helper"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--reviewed-dry-run", action="store_true")
    parser.add_argument("--idempotency-key", default="")
    for name in ("config", "env-config", "url"):
        parser.add_argument("--" + name)
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", args.key) or not args.reason.strip():
        parser.error("Choose one runtime key and give a reason.")
    if args.mode == "apply" and (not args.reviewed_dry_run or not args.idempotency_key.strip()):
        parser.error("Apply requires reviewed evidence and a stable idempotency key.")
    submitted = accepted = refused = False
    reason = "invalid_input_or_response"
    transport: dict[str, Any] = {}
    try:
        repository = Path(__file__).resolve().parents[1]
        value_file = _private_file(args.value_file, repository)
        evidence = _private_file(args.evidence_file, repository)
        value = value_file.read_text().strip()
        refuse_platform_credentials(
            {args.key: value}, target="preview", source="private preview override input"
        )
        identity = {
            "product": args.product,
            "key": args.key,
            "reason": args.reason,
            "value_sha256": hashlib.sha256(value.encode()).hexdigest(),
        }
        helper = args.operator_helper.expanduser().resolve(strict=True)
        if helper.name != "launchplane-write-action.py":
            raise PreviewOverrideRefused("operator_helper_required")
        transport = runpy.run_path(str(helper), run_name="preview_override_transport")
        settings = transport["resolve_settings"](args)
        transport["validate_service_url"](settings["service_url"])
        if not settings["token"]:
            raise PreviewOverrideRefused("operator_credential_unavailable")
        path = f"/v1/product-profiles/{quote(args.product, safe='')}"
        response, revision = _request(transport, settings, "GET", path)
        current = response["profile"]
        if current["product"] != args.product:
            raise PreviewOverrideRefused("product_identity_differs")
        if args.mode != "readback":
            if current["preview"]["enabled"] is not True:
                raise PreviewOverrideRefused("preview_enabled_product_required")
            if current["preview"].get("historical_transport"):
                raise PreviewOverrideRefused("unsupported_preview_transport_history")
            if not re.fullmatch(r'"[^"\r\n]+"', revision):
                raise PreviewOverrideRefused("conditional_save_unsupported")
        if args.mode == "dry-run":
            after = copy.deepcopy(current)
            after.setdefault("preview", {}).setdefault("override_env", {})[args.key] = value
            with os.fdopen(
                os.open(evidence, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w"
            ) as output:
                json.dump(
                    {"request": identity, "before": current, "after": after, "revision": revision},
                    output,
                )
        else:
            saved = json.loads(evidence.read_text())
            if saved["request"] != identity:
                raise PreviewOverrideRefused("reviewed_intent_changed")
            after = copy.deepcopy(saved["before"])
            after.setdefault("preview", {}).setdefault("override_env", {})[args.key] = value
            if after != saved["after"]:
                raise PreviewOverrideRefused("review_changes_other_fields")
            if args.mode == "apply" and current != after:
                if current != saved["before"] or revision != saved["revision"]:
                    raise PreviewOverrideRefused("profile_changed_since_review")
                submitted = True
                receipt, _ = _request(
                    transport,
                    settings,
                    "POST",
                    "/v1/product-profiles",
                    body=after,
                    revision=revision,
                    idempotency_key=args.idempotency_key,
                )
                if receipt.get("status") != "accepted":
                    raise PreviewOverrideRefused("apply_not_accepted")
                accepted = True
                response, _ = _request(transport, settings, "GET", path)
                current = response["profile"]
            if current["preview"]["override_env"].get(args.key) != value:
                raise PreviewOverrideRefused("readback_differs")
        print(
            json.dumps(
                {
                    "status": "verified",
                    "mode": args.mode,
                    "product": args.product,
                    "key": args.key,
                    "applied": args.mode != "dry-run",
                    "read_back_matches": args.mode != "dry-run",
                }
            )
        )
        return 0
    except urllib.error.HTTPError as error:
        transport["emit_http_error_payload"](
            operation="preview-override", request={"mode": args.mode}, exc=error
        )
        if not accepted and (not submitted or error.code in {400, 403, 409, 412}):
            refused = True
            reason = "profile_changed_since_review" if error.code == 412 else "service_refused"
        else:
            reason = "http_response_unverified"
    except PreviewOverrideRefused as error:
        reason = error.code
    except PlatformCredentialRefusedError:
        reason = "runtime_platform_credential_refused"
    except FileExistsError:
        reason = "evidence_file_already_exists"
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        urllib.error.URLError,
        http.client.HTTPException,
    ):
        reason = "invalid_input_or_response"
    print(
        json.dumps(
            {
                "status": "accepted_unverified"
                if accepted
                else "outcome_unknown"
                if submitted and not refused
                else "unverified",
                "mode": args.mode,
                "reason": reason,
            }
        )
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
