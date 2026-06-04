from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from pydantic import ValidationError

from control_plane.contracts.product_onboarding_manifest import ProductOnboardingManifest
from control_plane.contracts.runtime_key_safety_policy import RuntimeSecretSafetyRule


def _load_json_file(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SystemExit(f"{path} must contain a JSON object.")
    return payload


def _json_sha256(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _slug(value: str) -> str:
    return "".join(character if character.isalnum() else "-" for character in value).strip("-")


def _required_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SystemExit(f"Seed import entry requires non-empty {key}.")
    return value.strip()


def _selected_imports(
    *, catalog: dict[str, Any], kind: str, import_id: str
) -> list[dict[str, Any]]:
    raw_imports = catalog.get("imports")
    if not isinstance(raw_imports, list):
        raise SystemExit("Seed import catalog requires an imports array.")
    selected: list[dict[str, Any]] = []
    for raw_import in raw_imports:
        if not isinstance(raw_import, dict):
            raise SystemExit("Seed import catalog entries must be JSON objects.")
        entry_kind = _required_string(raw_import, "kind")
        entry_id = _required_string(raw_import, "import_id")
        if kind != "all" and entry_kind != kind:
            continue
        if import_id and entry_id != import_id:
            continue
        selected.append(raw_import)
    if not selected:
        raise SystemExit("No seed imports matched the requested kind/import id.")
    return selected


def _patch_target_ids(entry: dict[str, Any], manifest_payload: dict[str, Any]) -> None:
    raw_mappings = entry.get("target_id_env", [])
    if not isinstance(raw_mappings, list):
        raise SystemExit(f"{entry['import_id']} target_id_env must be an array.")
    raw_targets = manifest_payload.get("provider_targets", [])
    if not isinstance(raw_targets, list):
        raise SystemExit(f"{entry['import_id']} manifest provider_targets must be an array.")

    targets_by_route: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for target in raw_targets:
        if not isinstance(target, dict):
            continue
        route = (str(target.get("context", "")), str(target.get("instance", "")))
        targets_by_route.setdefault(route, []).append(target)
    for raw_mapping in raw_mappings:
        if not isinstance(raw_mapping, dict):
            raise SystemExit(f"{entry['import_id']} target_id_env entries must be objects.")
        context = _required_string(raw_mapping, "context")
        instance = _required_string(raw_mapping, "instance")
        env_key = _required_string(raw_mapping, "env")
        targets = targets_by_route.get((context, instance), [])
        if not targets:
            raise SystemExit(
                f"{entry['import_id']} target_id_env references missing target "
                f"{context}/{instance}."
            )
        target_id = os.environ.get(env_key, "").strip()
        if not target_id:
            raise SystemExit(f"{env_key} is required for seed import {entry['import_id']}.")
        for target in targets:
            target["target_id"] = target_id


def _product_onboarding_payload(entry: dict[str, Any]) -> dict[str, Any]:
    manifest_payload = entry.get("manifest")
    if not isinstance(manifest_payload, dict):
        raise SystemExit(f"{entry['import_id']} requires a manifest object.")
    manifest_payload = json.loads(json.dumps(manifest_payload))
    _patch_target_ids(entry, manifest_payload)
    try:
        manifest = ProductOnboardingManifest.model_validate(manifest_payload)
    except ValidationError as error:
        raise SystemExit(f"{entry['import_id']} manifest failed validation: {error}") from error
    return {
        "schema_version": 1,
        "product": "launchplane",
        "manifest": manifest.model_dump(mode="json"),
    }


def _runtime_key_safety_payload(entry: dict[str, Any]) -> dict[str, Any]:
    raw_rules = entry.get("rules")
    if not isinstance(raw_rules, list):
        raise SystemExit(f"{entry['import_id']} requires a rules array.")
    try:
        rules = tuple(RuntimeSecretSafetyRule.model_validate(rule) for rule in raw_rules)
    except ValidationError as error:
        raise SystemExit(f"{entry['import_id']} rules failed validation: {error}") from error
    source_label = str(entry.get("source_label") or "import-material:runtime-key-safety-policy")
    return {
        "schema_version": 1,
        "product": "launchplane",
        "source_label": source_label,
        "rules": [rule.model_dump(mode="json") for rule in rules],
    }


def _entry_payload(entry: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    entry_kind = _required_string(entry, "kind")
    if entry_kind == "product_onboarding":
        return "/v1/product-onboarding/apply", _product_onboarding_payload(entry)
    if entry_kind == "runtime_key_safety_policy":
        return "/v1/runtime-key-safety/policies/apply", _runtime_key_safety_payload(entry)
    raise SystemExit(f"Unsupported seed import kind: {entry_kind}")


def _post_json(
    *, service_url: str, token: str, path: str, idempotency_key: str, payload: dict[str, Any]
) -> dict[str, Any]:
    request = Request(
        urljoin(service_url.rstrip("/") + "/", path.lstrip("/")),
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Idempotency-Key": idempotency_key,
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=60) as response:
            response_payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")[:2000]
        raise SystemExit(f"Launchplane service returned HTTP {error.code}: {body}") from error
    except URLError as error:
        raise SystemExit(f"Launchplane service request failed: {error}") from error
    if not isinstance(response_payload, dict):
        raise SystemExit("Launchplane service response was not a JSON object.")
    return response_payload


def _idempotency_key(*, import_id: str, payload_sha256: str) -> str:
    run_id = os.environ.get("GITHUB_RUN_ID", "local").strip() or "local"
    run_attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "1").strip() or "1"
    return f"launchplane-seed-import:{import_id}:{payload_sha256[:16]}:{run_id}:{run_attempt}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--kind",
        choices=("all", "product_onboarding", "runtime_key_safety_policy"),
        default="all",
    )
    parser.add_argument("--import-id", default="")
    parser.add_argument("--service-url", default="")
    parser.add_argument("--bearer-token", default="")
    parser.add_argument("--reason", default="")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    if args.apply and not args.reason.strip():
        raise SystemExit("--reason is required when --apply is set.")
    if args.apply and (not args.service_url.strip() or not args.bearer_token.strip()):
        raise SystemExit("--service-url and --bearer-token are required when --apply is set.")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    catalog = _load_json_file(args.catalog)
    entries = _selected_imports(catalog=catalog, kind=args.kind, import_id=args.import_id.strip())
    results: list[dict[str, Any]] = []
    for entry in entries:
        import_id = _required_string(entry, "import_id")
        entry_kind = _required_string(entry, "kind")
        path, payload = _entry_payload(entry)
        payload_sha256 = _json_sha256(payload)
        payload_path = args.output_dir / f"{_slug(import_id)}-payload.json"
        payload_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        idempotency_key = _idempotency_key(import_id=import_id, payload_sha256=payload_sha256)
        response_payload: dict[str, Any] = {}
        if args.apply:
            response_payload = _post_json(
                service_url=args.service_url,
                token=args.bearer_token,
                path=path,
                idempotency_key=idempotency_key,
                payload=payload,
            )
        results.append(
            {
                "import_id": import_id,
                "kind": entry_kind,
                "route_path": path,
                "payload_sha256": payload_sha256,
                "idempotency_key": idempotency_key,
                "payload_file": str(payload_path),
                "applied": args.apply,
                "response": response_payload,
            }
        )

    summary = {
        "schema_version": 1,
        "status": "ok",
        "applied": args.apply,
        "reason": args.reason,
        "count": len(results),
        "imports": results,
    }
    summary_path = args.output_dir / "launchplane-seed-import-summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
