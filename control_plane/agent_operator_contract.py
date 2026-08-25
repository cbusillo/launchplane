from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.routing import APIRoute

from control_plane.openapi_export import (
    build_deterministic_export_app,
    canonical_openapi_document,
)


SCHEMA_VERSION = 1
NORMALIZATION_VERSION = 1
_REPOSITORY_ROOT = Path(__file__).resolve().parent.parent

_SCHEMA_KEYS = frozenset(
    {
        "additionalProperties",
        "allOf",
        "anyOf",
        "const",
        "default",
        "deprecated",
        "enum",
        "exclusiveMaximum",
        "exclusiveMinimum",
        "format",
        "items",
        "maxItems",
        "maxLength",
        "maxProperties",
        "maximum",
        "minItems",
        "minLength",
        "minProperties",
        "minimum",
        "multipleOf",
        "oneOf",
        "pattern",
        "properties",
        "required",
        "type",
        "uniqueItems",
    }
)
_SEMANTICALLY_UNORDERED_LIST_KEYS = frozenset({"enum", "required"})
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_SHA_PATTERN = re.compile(r"^(unknown|[0-9a-f]{40}|[0-9a-f]{64})$")
_UNSAFE_STRING_PATTERNS = (
    re.compile(r"https?://", re.IGNORECASE),
    re.compile(r"\bLAUNCHPLANE_[A-Z0-9_]+\b"),
    re.compile(r"\b(?:token|password|private[_-]?key)\s*[:=]", re.IGNORECASE),
)


class AgentOperatorContractError(ValueError):
    pass


@dataclass(frozen=True)
class OperationSpec:
    method: str
    path: str
    purpose: str
    supported_surfaces: tuple[str, ...]
    modes: tuple[str, ...]
    idempotency: str
    reviewed_evidence: tuple[str, ...]


OPERATION_SPECS = (
    OperationSpec(
        "GET",
        "/v1/agent/context",
        "Read public-safe Launchplane context for an agent task.",
        ("agent_helper", "read_only_service"),
        ("read",),
        "none",
        (),
    ),
    OperationSpec(
        "POST",
        "/v1/agent/write-intents/evaluate",
        "Evaluate a bounded agent write intent before mutation.",
        ("agent_helper", "service_api"),
        ("preflight",),
        "optional",
        (),
    ),
    OperationSpec(
        "POST",
        "/v1/product-config/apply",
        "Dry-run or apply reviewed product configuration.",
        ("agent_helper", "operator_ui", "service_api"),
        ("dry-run", "apply"),
        "apply",
        ("reviewed_dry_run",),
    ),
    OperationSpec(
        "POST",
        "/v1/change-impact/policies/apply",
        "Dry-run or apply a reviewed change-impact policy revision.",
        ("agent_helper", "operator_ui", "service_api"),
        ("dry-run", "apply"),
        "none",
        ("reviewed_dry_run", "expected_policy_digest"),
    ),
    OperationSpec(
        "GET",
        "/v1/change-impact/policy",
        "Read active change-impact policy revision evidence.",
        ("agent_helper", "read_only_service"),
        ("read",),
        "none",
        (),
    ),
    OperationSpec(
        "POST",
        "/v1/work-graph/merge-train/controller/run-once",
        "Advance one merge-train controller phase with bounded recovery evidence.",
        ("agent_helper", "operator_ui", "service_api"),
        ("dry-run", "mutate"),
        "mutate",
        (),
    ),
    OperationSpec(
        "POST",
        "/v1/previews/pr-feedback/remediation",
        "Dry-run or apply bounded historical preview-feedback remediation.",
        ("agent_helper", "operator_ui", "service_api"),
        ("dry-run", "apply"),
        "apply",
        ("reviewed_dry_run",),
    ),
    OperationSpec(
        "POST",
        "/v1/product-retirement",
        "Plan or apply protected product retirement.",
        ("protected_workflow", "operator_ui", "service_api"),
        ("plan", "apply"),
        "always",
        ("reviewed_plan_digest",),
    ),
    OperationSpec(
        "POST",
        "/v1/detached-application-retirement",
        "Plan or apply detached application retirement with zero authority writes.",
        ("protected_workflow", "operator_ui", "service_api"),
        ("plan", "apply"),
        "always",
        ("reviewed_plan_digest",),
    ),
    OperationSpec(
        "POST",
        "/v1/authz-policies/managed-rule-sets/reconcile",
        "Dry-run or apply managed authorization reconciliation.",
        ("protected_workflow", "operator_ui", "service_api"),
        ("dry-run", "apply"),
        "apply",
        ("reviewed_plan_digest",),
    ),
    OperationSpec(
        "POST",
        "/v1/product-profiles/stable-lane-repair/apply",
        "Dry-run or apply a bounded stable-lane profile repair.",
        ("protected_workflow", "operator_ui", "service_api"),
        ("dry-run", "apply"),
        "apply",
        ("reviewed_plan_digest",),
    ),
    OperationSpec(
        "GET",
        "/v1/governance/projection",
        "Read governance evidence without granting mutation authority.",
        ("read_only_service", "operator_ui"),
        ("read",),
        "none",
        (),
    ),
)

PROTECTED_WORKFLOWS = (
    {
        "workflow_file": ".github/workflows/product-retirement.yml",
        "reusable_workflow_file": ".github/workflows/reusable-product-retirement.yml",
        "route": "/v1/product-retirement",
    },
    {
        "workflow_file": ".github/workflows/detached-application-retirement.yml",
        "reusable_workflow_file": ".github/workflows/reusable-detached-application-retirement.yml",
        "route": "/v1/detached-application-retirement",
    },
    {
        "workflow_file": ".github/workflows/authz-policy-reconcile.yml",
        "reusable_workflow_file": ".github/workflows/reusable-authz-policy-reconcile.yml",
        "route": "/v1/authz-policies/managed-rule-sets/reconcile",
    },
    {
        "workflow_file": ".github/workflows/product-onboarding-manifest.yml",
        "route": "/v1/product-profiles/stable-lane-repair/apply",
    },
)

INVARIANTS = {
    "product_lifecycle": {
        "states": ["active", "retiring", "retired"],
        "transitions": ["active->retiring", "retiring->retired"],
        "active_automation_states": ["active"],
        "retired_previews_enabled": False,
    },
    "detached_retirement": {
        "authority_write_count": 0,
        "completion_requires_candidate_absence": True,
    },
    "stable_deploy_identity": {
        "target_category": "application",
        "artifact_id": "digest_pinned_evidence_and_runtime_identity",
        "deploy_reference": "published_same_repository_immutable_sha_tag",
        "image_reference": "provider_tag_readback",
        "floating_tags_allowed": False,
    },
    "reconciliation": {
        "pre_effect": "retry_only_after_fixing_the_block",
        "effect_unknown": "reconciliation_required_before_reexecution",
        "landing_sha_source": "terminal_controller_result_only",
    },
    "governance": {
        "owner_acceptance_authoritative": True,
        "github_projection_role": "routing_and_status_only",
        "engineering_review_advisory": True,
        "authorization_admission_and_landing_are_independent": True,
    },
    "protected_workflow_policy": {
        "dispatch_and_watch_helper": "github_workflow_babysit.py",
        "raw_workflow_dispatch_allowed": False,
    },
}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _collect_definitions(value: Any, definitions: dict[str, Any]) -> None:
    if not isinstance(value, Mapping):
        return
    nested = value.get("$defs")
    if isinstance(nested, Mapping):
        definitions.update({str(key): item for key, item in nested.items()})


def _resolve_schema(
    schema: Any,
    components: Mapping[str, Any],
    definitions: Mapping[str, Any],
    resolving: tuple[str, ...],
) -> Any:
    if not isinstance(schema, Mapping) or "$ref" not in schema:
        return schema
    reference = schema["$ref"]
    if not isinstance(reference, str):
        raise AgentOperatorContractError("OpenAPI schema reference must be a string")
    if reference.startswith("#/components/schemas/"):
        name = reference.rsplit("/", maxsplit=1)[-1]
        resolution_key = f"component:{name}"
        if resolution_key in resolving:
            return {"type": "object", "recursive_reference": reference}
        if name not in components:
            raise AgentOperatorContractError(f"Missing OpenAPI schema reference: {reference}")
        return _normalize_schema(components[name], components, {}, (*resolving, resolution_key))
    elif reference.startswith("#/$defs/"):
        name = reference.rsplit("/", maxsplit=1)[-1]
        resolution_key = f"definition:{name}"
        if resolution_key in resolving:
            return {"type": "object", "recursive_reference": reference}
        if name not in definitions:
            raise AgentOperatorContractError(f"Missing OpenAPI schema reference: {reference}")
        return _normalize_schema(
            definitions[name],
            components,
            definitions,
            (*resolving, resolution_key),
        )
    else:
        raise AgentOperatorContractError(f"Unsupported OpenAPI schema reference: {reference}")


def _normalize_schema(
    schema: Any,
    components: Mapping[str, Any],
    definitions: Mapping[str, Any],
    resolving: tuple[str, ...] = (),
) -> Any:
    local_definitions = dict(definitions)
    _collect_definitions(schema, local_definitions)
    resolved = _resolve_schema(schema, components, local_definitions, resolving)
    if resolved is not schema:
        siblings = {key: value for key, value in schema.items() if key != "$ref"}
        normalized_siblings = _normalize_schema(
            siblings,
            components,
            local_definitions,
            resolving,
        )
        if not normalized_siblings:
            return resolved
        return {"allOf": sorted([resolved, normalized_siblings], key=_canonical_json)}
    if isinstance(schema, bool):
        return schema
    if not isinstance(schema, Mapping):
        raise AgentOperatorContractError("OpenAPI schema must be an object or boolean")
    normalized: dict[str, Any] = {}
    for key in sorted(_SCHEMA_KEYS & set(schema)):
        value = schema[key]
        if key == "properties":
            if not isinstance(value, Mapping):
                raise AgentOperatorContractError("OpenAPI properties must be an object")
            normalized[key] = {
                str(name): _normalize_schema(item, components, local_definitions, resolving)
                for name, item in sorted(value.items())
            }
        elif key in {"items", "additionalProperties"} and isinstance(value, (Mapping, bool)):
            normalized[key] = _normalize_schema(value, components, local_definitions, resolving)
        elif key in {"anyOf", "oneOf", "allOf"}:
            if not isinstance(value, Sequence) or isinstance(value, str | bytes):
                raise AgentOperatorContractError(f"OpenAPI {key} must be an array")
            normalized_items = [
                _normalize_schema(item, components, local_definitions, resolving) for item in value
            ]
            normalized[key] = sorted(normalized_items, key=_canonical_json)
        elif key in _SEMANTICALLY_UNORDERED_LIST_KEYS:
            if not isinstance(value, Sequence) or isinstance(value, str | bytes):
                raise AgentOperatorContractError(f"OpenAPI {key} must be an array")
            normalized[key] = sorted(deepcopy(list(value)), key=_canonical_json)
        else:
            normalized[key] = deepcopy(value)
    return normalized


def _normalize_content(
    content: Any,
    components: Mapping[str, Any],
    definitions: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(content, Mapping):
        return {}
    return {
        str(media_type): _normalize_schema(payload.get("schema", {}), components, definitions)
        for media_type, payload in sorted(content.items())
        if isinstance(payload, Mapping)
    }


def _normalize_operation(
    operation: Mapping[str, Any],
    components: Mapping[str, Any],
    definitions: Mapping[str, Any],
) -> dict[str, Any]:
    parameters = []
    for parameter in operation.get("parameters", []):
        if not isinstance(parameter, Mapping):
            raise AgentOperatorContractError("OpenAPI parameters must be objects")
        parameters.append(
            {
                "name": str(parameter.get("name", "")),
                "in": str(parameter.get("in", "")),
                "required": bool(parameter.get("required", False)),
                "schema": _normalize_schema(parameter.get("schema", {}), components, definitions),
            }
        )
    parameters.sort(key=lambda item: (item["in"], item["name"]))

    request_body = operation.get("requestBody")
    normalized_request = None
    if isinstance(request_body, Mapping):
        normalized_request = {
            "required": bool(request_body.get("required", False)),
            "content": _normalize_content(request_body.get("content", {}), components, definitions),
        }

    responses = operation.get("responses", {})
    if not isinstance(responses, Mapping):
        raise AgentOperatorContractError("OpenAPI responses must be an object")
    normalized_responses = {
        str(status): _normalize_content(response.get("content", {}), components, definitions)
        for status, response in sorted(responses.items(), key=lambda item: str(item[0]))
        if isinstance(response, Mapping)
    }
    return {
        "parameters": parameters,
        "request_body": normalized_request,
        "responses": normalized_responses,
    }


def _live_routes(app: FastAPI) -> dict[tuple[str, str], APIRoute]:
    allowed = {(spec.method, spec.path) for spec in OPERATION_SPECS}
    routes: dict[tuple[str, str], APIRoute] = {}
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        for method in route.methods or set():
            key = (method.upper(), route.path)
            if key not in allowed:
                continue
            if key in routes:
                raise AgentOperatorContractError(f"Duplicate allow-listed route: {key}")
            routes[key] = route
    missing = sorted(allowed - set(routes))
    if missing:
        raise AgentOperatorContractError(f"Missing allow-listed routes: {missing}")
    return routes


def _dependency_names(route: APIRoute) -> list[str]:
    names = []
    for dependency in route.dependant.dependencies:
        call = dependency.call
        name = getattr(call, "__name__", "") if call is not None else ""
        if name and name != "get_record_store":
            names.append(name)
    return sorted(set(names))


def _source_commit_sha() -> str:
    configured = os.environ.get("GITHUB_SHA")
    if configured:
        return configured
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=_REPOSITORY_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return completed.stdout.strip() or "unknown"


def _semantic_payload(contract: Mapping[str, Any]) -> dict[str, Any]:
    return {"normalization_version": NORMALIZATION_VERSION, "contract": contract}


def build_agent_operator_contract(
    *,
    openapi_document: Mapping[str, Any] | None = None,
    app: FastAPI | None = None,
    source_commit_sha: str | None = None,
) -> dict[str, Any]:
    document = openapi_document or canonical_openapi_document()
    paths = document.get("paths")
    components = document.get("components", {}).get("schemas", {})
    if not isinstance(paths, Mapping) or not isinstance(components, Mapping):
        raise AgentOperatorContractError("Canonical OpenAPI paths or schemas are unavailable")
    routes = _live_routes(app or build_deterministic_export_app())

    operations = []
    for spec in OPERATION_SPECS:
        path_item = paths.get(spec.path)
        operation = path_item.get(spec.method.lower()) if isinstance(path_item, Mapping) else None
        if not isinstance(operation, Mapping):
            raise AgentOperatorContractError(
                f"Missing allow-listed OpenAPI operation: {spec.method} {spec.path}"
            )
        operation_id = operation.get("operationId")
        if not isinstance(operation_id, str) or not operation_id:
            raise AgentOperatorContractError(f"Missing operation ID: {spec.method} {spec.path}")
        normalized_operation = _normalize_operation(operation, components, {})
        parameter_names = {
            str(parameter.get("name", "")).lower()
            for parameter in operation.get("parameters", [])
            if isinstance(parameter, Mapping)
        }
        has_idempotency_header = "idempotency-key" in parameter_names
        if (spec.idempotency != "none") != has_idempotency_header:
            raise AgentOperatorContractError(
                f"Idempotency metadata differs from OpenAPI: {spec.method} {spec.path}"
            )
        operations.append(
            {
                "method": spec.method,
                "path": spec.path,
                "operation_id": operation_id,
                "identity_dependencies": _dependency_names(routes[(spec.method, spec.path)]),
                "purpose": spec.purpose,
                "supported_surfaces": list(spec.supported_surfaces),
                "modes": list(spec.modes),
                "idempotency": spec.idempotency,
                "reviewed_evidence": list(spec.reviewed_evidence),
                "response_statuses": sorted(
                    (str(status) for status in operation.get("responses", {})),
                    key=lambda status: (not status.isdigit(), status),
                ),
                "schema_fingerprint_sha256": _digest(normalized_operation),
            }
        )

    for workflow in PROTECTED_WORKFLOWS:
        for key in ("workflow_file", "reusable_workflow_file"):
            value = workflow.get(key)
            if value and not (_REPOSITORY_ROOT / value).is_file():
                raise AgentOperatorContractError(f"Missing protected workflow: {value}")

    contract = {
        "operations": operations,
        "protected_workflows": deepcopy(list(PROTECTED_WORKFLOWS)),
        "invariants": deepcopy(INVARIANTS),
    }
    artifact = {
        "schema_version": SCHEMA_VERSION,
        "normalization_version": NORMALIZATION_VERSION,
        "semantic_digest_sha256": _digest(_semantic_payload(contract)),
        "provenance": {"source_commit_sha": source_commit_sha or _source_commit_sha()},
        "contract": contract,
    }
    validate_agent_operator_contract(artifact)
    return artifact


def _assert_exact_keys(value: Mapping[str, Any], expected: set[str], location: str) -> None:
    if set(value) != expected:
        raise AgentOperatorContractError(f"Unexpected {location} keys: {sorted(value)}")


def _validate_public_safety(value: Any, location: str = "artifact") -> None:
    if isinstance(value, str):
        if any(pattern.search(value) for pattern in _UNSAFE_STRING_PATTERNS):
            raise AgentOperatorContractError(f"Unsafe public contract value at {location}")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            _validate_public_safety(str(key), f"{location}.key")
            _validate_public_safety(item, f"{location}.{key}")
        return
    if isinstance(value, list | tuple):
        for index, item in enumerate(value):
            _validate_public_safety(item, f"{location}[{index}]")
        return
    if value is not None and not isinstance(value, bool | int | float):
        raise AgentOperatorContractError(f"Non-JSON contract value at {location}")


def validate_agent_operator_contract(artifact: Mapping[str, Any]) -> None:
    _assert_exact_keys(
        artifact,
        {
            "schema_version",
            "normalization_version",
            "semantic_digest_sha256",
            "provenance",
            "contract",
        },
        "artifact",
    )
    if artifact["schema_version"] != SCHEMA_VERSION:
        raise AgentOperatorContractError("Unsupported schema_version")
    if artifact["normalization_version"] != NORMALIZATION_VERSION:
        raise AgentOperatorContractError("Unsupported normalization_version")
    digest = artifact["semantic_digest_sha256"]
    if not isinstance(digest, str) or not _SHA256_PATTERN.fullmatch(digest):
        raise AgentOperatorContractError("Invalid semantic_digest_sha256")
    provenance = artifact["provenance"]
    if not isinstance(provenance, Mapping):
        raise AgentOperatorContractError("Invalid provenance")
    _assert_exact_keys(provenance, {"source_commit_sha"}, "provenance")
    source_sha = provenance["source_commit_sha"]
    if not isinstance(source_sha, str) or not _SOURCE_SHA_PATTERN.fullmatch(source_sha):
        raise AgentOperatorContractError("Invalid provenance source_commit_sha")
    contract = artifact["contract"]
    if not isinstance(contract, Mapping):
        raise AgentOperatorContractError("Invalid contract")
    _assert_exact_keys(contract, {"operations", "protected_workflows", "invariants"}, "contract")

    operations = contract["operations"]
    if not isinstance(operations, list) or len(operations) != len(OPERATION_SPECS):
        raise AgentOperatorContractError("Invalid operation allow-list")
    expected_operations = {(spec.method, spec.path) for spec in OPERATION_SPECS}
    expected_specs = {(spec.method, spec.path): spec for spec in OPERATION_SPECS}
    seen_operations = set()
    operation_keys = {
        "method",
        "path",
        "operation_id",
        "identity_dependencies",
        "purpose",
        "supported_surfaces",
        "modes",
        "idempotency",
        "reviewed_evidence",
        "response_statuses",
        "schema_fingerprint_sha256",
    }
    for operation in operations:
        if not isinstance(operation, Mapping):
            raise AgentOperatorContractError("Invalid operation entry")
        _assert_exact_keys(operation, operation_keys, "operation")
        operation_key = (operation["method"], operation["path"])
        if operation_key not in expected_operations or operation_key in seen_operations:
            raise AgentOperatorContractError("Unexpected or duplicate operation")
        seen_operations.add(operation_key)
        spec = expected_specs[operation_key]
        for field_name in (
            "method",
            "path",
            "operation_id",
            "purpose",
            "idempotency",
        ):
            if not isinstance(operation[field_name], str) or not operation[field_name]:
                raise AgentOperatorContractError(f"Invalid operation {field_name}")
        if operation["purpose"] != spec.purpose:
            raise AgentOperatorContractError("Operation purpose differs from the allow-list")
        if operation["supported_surfaces"] != list(spec.supported_surfaces):
            raise AgentOperatorContractError("Operation surfaces differ from the allow-list")
        if operation["modes"] != list(spec.modes):
            raise AgentOperatorContractError("Operation modes differ from the allow-list")
        if operation["idempotency"] != spec.idempotency:
            raise AgentOperatorContractError("Operation idempotency differs from the allow-list")
        if operation["reviewed_evidence"] != list(spec.reviewed_evidence):
            raise AgentOperatorContractError(
                "Operation reviewed evidence differs from the allow-list"
            )
        for field_name in (
            "identity_dependencies",
            "supported_surfaces",
            "modes",
            "reviewed_evidence",
            "response_statuses",
        ):
            value = operation[field_name]
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise AgentOperatorContractError(f"Invalid operation {field_name}")
        if not operation["identity_dependencies"]:
            raise AgentOperatorContractError("Operation has no identity dependencies")
        fingerprint = operation["schema_fingerprint_sha256"]
        if not isinstance(fingerprint, str) or not _SHA256_PATTERN.fullmatch(fingerprint):
            raise AgentOperatorContractError("Invalid operation fingerprint")
    if seen_operations != expected_operations:
        raise AgentOperatorContractError("Operation allow-list is incomplete")

    workflows = contract["protected_workflows"]
    if not isinstance(workflows, list | tuple) or len(workflows) != len(PROTECTED_WORKFLOWS):
        raise AgentOperatorContractError("Invalid protected workflows")
    if workflows != list(PROTECTED_WORKFLOWS):
        raise AgentOperatorContractError("Protected workflows differ from the allow-list")
    invariants = contract["invariants"]
    if not isinstance(invariants, Mapping) or invariants != INVARIANTS:
        raise AgentOperatorContractError("Invalid invariant set")
    expected_digest = _digest(_semantic_payload(contract))
    if digest != expected_digest:
        raise AgentOperatorContractError("Semantic digest does not match contract")
    _validate_public_safety(artifact)


def write_agent_operator_contract(
    output_path: Path,
    *,
    source_commit_sha: str | None = None,
) -> Path:
    artifact = build_agent_operator_contract(source_commit_sha=source_commit_sha)
    if output_path.exists():
        try:
            current = json.loads(output_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            current = None
        current_provenance = current.get("provenance") if isinstance(current, Mapping) else None
        current_source_sha = (
            current_provenance.get("source_commit_sha")
            if isinstance(current_provenance, Mapping)
            else None
        )
        if (
            isinstance(current, Mapping)
            and current.get("semantic_digest_sha256") == artifact["semantic_digest_sha256"]
            and isinstance(current_source_sha, str)
            and current_source_sha != "unknown"
            and _SOURCE_SHA_PATTERN.fullmatch(current_source_sha)
        ):
            artifact["provenance"] = {"source_commit_sha": current_source_sha}
    validate_agent_operator_contract(artifact)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(artifact, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return output_path
