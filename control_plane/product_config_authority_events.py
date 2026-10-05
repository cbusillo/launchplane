"""Run the existing product gate on immutable GitHub source snapshots.

Activation is per repository in DB-backed policy, disabled by default. No
product build, workflow, checkout, or caller-supplied configuration is executed.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Callable, Protocol, cast
import time
from urllib.parse import quote

from pydantic import JsonValue

from control_plane import secrets
from control_plane.advisory_check_projection import write_github_check_projection
from control_plane.contracts.advisory_check_projection import (
    ConfigAuthorityCheckProjection,
    CONFIG_AUTHORITY_CHECK_NAME,
)
from control_plane.build_provenance import BuildProvenanceTransport, GitHubBuildProvenanceTransport
from control_plane.config_authority_audit import (
    GIT_COMMIT_SHA_PATTERN,
    MAX_SCANNED_FILE_BYTES,
    build_config_authority_audit,
    evaluate_config_authority_gate,
)
from control_plane.contracts.merge_train_policy import MergeTrainRepositoryPolicy
from control_plane.contracts.repository_inventory import RepositoryInventoryRecord
from control_plane.contracts.product_reconcile import GitHubAppWebhookDeliveryRecord
from control_plane.repository_inventory import get_repository_inventory_read_model
from control_plane.github_app_identity import (
    GitHubAppIdentity,
    mint_source_control_read_installation_token,
    resolve_advisory_github_app_identity,
    mint_repository_installation_token,
)
from control_plane.merge_train_github_token import MERGE_TRAIN_GITHUB_APP_SECRET_INTEGRATION
from control_plane.merge_train_policy_source import (
    MergeTrainPolicyStoreMissingError,
    resolve_merge_train_policy_record,
)


class GitHubConfigAuthoritySource:
    """Read exact two-tree changes, avoiding GitHub compare's merge-base semantics."""

    def __init__(self, transport: BuildProvenanceTransport, repository: str) -> None:
        self.deadline = time.monotonic() + 240
        self.transport = transport
        self.repository_package = repository.split("/")[1].lower()
        self.prefix = "/repos/" + "/".join(quote(part, safe="") for part in repository.split("/"))
        self.trees: dict[str, dict[str, dict[str, object]]] = {}
        self.blobs: dict[str, bytes] = {}

    def read(self, path: str) -> object:
        if time.monotonic() >= self.deadline:
            raise ValueError("Source scan exceeded its work budget.")
        return self.transport.get_json(path)

    def resolve_commit(self, revision: str) -> str:
        revision = _sha(revision)
        commit = _object(self.read(f"{self.prefix}/git/commits/{revision}"))
        if commit.get("sha") != revision:
            raise ValueError("Source commit identity does not match.")
        tree_sha = _sha(_object(commit.get("tree")).get("sha"))
        response = _object(self.read(f"{self.prefix}/git/trees/{tree_sha}?recursive=1"))
        if response.get("sha") != tree_sha or response.get("truncated") is not False:
            raise ValueError("Source tree is unavailable or truncated.")
        entries = response.get("tree")
        if not isinstance(entries, list):
            raise ValueError("Source tree entries are unavailable.")
        tree: dict[str, dict[str, object]] = {}
        for raw_entry in entries:
            entry = _object(raw_entry)
            path = entry.get("path")
            if (
                not isinstance(path, str)
                or not path
                or path.startswith("/")
                or any(part in {"", ".", ".."} for part in path.split("/"))
                or path in tree
            ):
                raise ValueError("Source tree has an invalid or duplicate path.")
            mode = entry.get("mode")
            expected_type = {
                "040000": "tree",
                "160000": "commit",
                "100644": "blob",
                "100755": "blob",
                "120000": "blob",
            }.get(str(mode))
            if expected_type is None or entry.get("type") != expected_type:
                raise ValueError("Source tree has an unsupported entry.")
            _sha(entry.get("sha"))
            tree[path] = entry
        # A recursive Git tree must contain every path's directory ancestors.
        for path in tree:
            for parent in PurePosixPath(path).parents:
                if str(parent) != "." and tree.get(str(parent), {}).get("mode") != "040000":
                    raise ValueError("Source tree is incomplete.")
        self.trees[revision] = tree
        return revision

    def changed_paths(self, base: str, head: str) -> list[str]:
        before, after = self.trees[base], self.trees[head]
        return sorted(
            path
            for path, entry in after.items()
            if entry["mode"] != "040000"
            and (entry["sha"], entry["mode"])
            != (before.get(path, {}).get("sha"), before.get(path, {}).get("mode"))
        )

    def file_modes(self, revision: str) -> dict[str, str]:
        return {".": "040000"} | {
            path: str(entry["mode"]) for path, entry in self.trees[revision].items()
        }

    def blob_size(self, revision: str, path: str) -> int:
        size = self.trees[revision][path].get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ValueError("Source blob size is unavailable.")
        return size

    def blob_sha(self, revision: str, path: str) -> str:
        return _sha(self.trees[revision][path]["sha"])

    def read_blob(self, revision: str, path: str) -> bytes:
        size, sha = self.blob_size(revision, path), self.blob_sha(revision, path)
        if size > MAX_SCANNED_FILE_BYTES:
            raise ValueError("Source blob exceeds scanner size limit.")
        if sha in self.blobs:
            data = self.blobs[sha]
            if len(data) != size:
                raise ValueError("Source blob size does not match its immutable identity.")
            return data
        blob = _object(self.read(f"{self.prefix}/git/blobs/{sha}"))
        content = blob.get("content")
        if (
            blob.get("sha") != sha
            or blob.get("size") != size
            or blob.get("encoding") != "base64"
            or not isinstance(content, str)
            or len(content) > 2 * MAX_SCANNED_FILE_BYTES
        ):
            raise ValueError("Source blob evidence is unavailable or mismatched.")
        try:
            data = base64.b64decode("".join(content.split()), validate=True)
        except (binascii.Error, ValueError) as error:
            raise ValueError("Source blob is not valid base64.") from error
        actual_sha = hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()
        if len(data) != size or actual_sha != sha:
            raise ValueError("Source blob content does not match its immutable identity.")
        self.blobs[sha] = data
        return data


def scan_product_config_authority_event(
    *,
    transport: BuildProvenanceTransport,
    inventory: RepositoryInventoryRecord,
    event: str,
    payload: dict[str, object],
    expected_base_branch: str = "",
) -> dict[str, JsonValue]:
    source = GitHubConfigAuthoritySource(transport, inventory.repository)
    repository = _object(source.read(source.prefix))
    if (
        str(repository.get("id")) != inventory.repository_id
        or str(_object(repository.get("owner")).get("id")) != inventory.repository_owner_id
        or str(repository.get("full_name")).casefold() != inventory.repository.casefold()
    ):
        raise ValueError("Source repository identity does not match inventory.")
    if event == "pull_request":
        number = payload.get("number")
        if not isinstance(number, int) or isinstance(number, bool) or number < 1:
            raise ValueError("Source pull request number is unavailable.")
        pr = _object(source.read(f"{source.prefix}/pulls/{number}"))
        base = _object(pr.get("base"))
        if str(_object(base.get("repo")).get("id")) != inventory.repository_id:
            raise ValueError("Source pull request targets another repository.")
        if expected_base_branch and base.get("ref") != expected_base_branch:
            return {"status": "superseded"}
        base_sha, head_sha = _sha(base.get("sha")), _sha(_object(pr.get("head")).get("sha"))
    elif event == "push":
        base_sha, head_sha = _sha(payload.get("before")), _sha(payload.get("after"))
    elif event == "merge_group":
        group = _object(payload.get("merge_group"))
        base_sha, head_sha = _sha(group.get("base_sha")), _sha(group.get("head_sha"))
    else:
        raise ValueError("Unsupported config-authority source event.")
    audit = build_config_authority_audit(
        control_plane_root=Path("."),
        mode="changed-files-gate",
        base_sha=base_sha,
        head_sha=head_sha,
        committed_source=source,
    )
    gate = evaluate_config_authority_gate(audit, profile="product-repo")
    # Persist only redacted structured evidence, never file contents or literal values.
    return cast(
        dict[str, JsonValue],
        {
            "status": gate["status"],
            "repository": inventory.repository,
            "repository_id": inventory.repository_id,
            "event": event,
            "base_sha": base_sha,
            "head_sha": head_sha,
            "gate": gate,
            "coverage": audit["coverage"],
            "hashes": audit["hashes"],
        },
    )


def config_authority_event_request(event: str, payload: dict[str, object]) -> dict[str, JsonValue]:
    """Keep only source selectors from the verified delivery, never instructions."""
    if event == "pull_request":
        pr = payload.get("pull_request")
        pr = pr if isinstance(pr, dict) else {}
        base, head = pr.get("base"), pr.get("head")
        return {
            "number": cast(JsonValue, payload.get("number")),
            "base_branch": cast(JsonValue, base.get("ref") if isinstance(base, dict) else None),
            "head_sha": cast(JsonValue, head.get("sha") if isinstance(head, dict) else None),
        }
    if event == "push":
        ref = payload.get("ref")
        return {
            "before": cast(JsonValue, payload.get("before")),
            "after": cast(JsonValue, payload.get("after")),
            "base_branch": ref.removeprefix("refs/heads/")
            if isinstance(ref, str) and ref.startswith("refs/heads/")
            else "",
        }
    group = payload.get("merge_group")
    group = group if isinstance(group, dict) else {}
    ref = group.get("base_ref")
    return {
        "merge_group": {
            "base_sha": cast(JsonValue, group.get("base_sha")),
            "head_sha": cast(JsonValue, group.get("head_sha")),
        },
        "base_branch": ref.removeprefix("refs/heads/")
        if isinstance(ref, str) and ref.startswith("refs/heads/")
        else "",
    }


def request_product_config_authority_event(
    record_store: object,
    inventory: RepositoryInventoryRecord,
    event: str,
    payload: dict[str, object],
) -> dict[str, JsonValue]:
    """Select dormant branch policy without making any provider read."""
    if event == "push" and (
        payload.get("deleted") is True
        or payload.get("created") is True
        or payload.get("before") == "0" * 40
    ):
        return {}
    request = config_authority_event_request(event, payload)
    if _enabled_policy(record_store, inventory, str(request.get("base_branch") or "")) is None:
        return {}
    return {"status": "pending", "request": request}


def config_authority_events_enabled(record_store: object) -> bool:
    try:
        return any(
            entry.config_authority_events_enabled
            for entry in resolve_merge_train_policy_record(record_store).policy.policies
        )
    except MergeTrainPolicyStoreMissingError:
        return False


def _enabled_policy(
    record_store: object, inventory: RepositoryInventoryRecord, base_branch: str
) -> MergeTrainRepositoryPolicy | None:
    try:
        policy = resolve_merge_train_policy_record(record_store).policy
    except MergeTrainPolicyStoreMissingError:
        return None
    matches = [
        entry
        for entry in policy.policies
        if entry.repository.casefold() == inventory.repository.casefold()
        and entry.base_branch == base_branch
        and entry.config_authority_events_enabled
    ]
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError("Config-authority source policy is ambiguous.")
    return matches[0]


def run_product_config_authority_event(
    record_store: object,
    inventory: RepositoryInventoryRecord,
    event: str,
    payload: dict[str, object],
) -> dict[str, JsonValue]:
    """Use the existing train App's read-only source token; never fall back."""
    policy = _enabled_policy(record_store, inventory, str(payload.get("base_branch") or ""))
    if policy is None:
        return {"status": "disabled"}
    app = policy.github_token.github_app
    if app is None or str(app.repository_id) != inventory.repository_id:
        raise ValueError("Config-authority source App does not match inventory.")
    private_key = secrets.resolve_context_secret_value(
        integration=MERGE_TRAIN_GITHUB_APP_SECRET_INTEGRATION,
        context_name=app.private_key_context,
        binding_key="private_key",
    )
    token = mint_source_control_read_installation_token(
        identity=GitHubAppIdentity(app_id=app.app_id, private_key=private_key),
        repository=inventory.repository,
        repository_id=inventory.repository_id,
    )
    return scan_product_config_authority_event(
        transport=GitHubBuildProvenanceTransport(token=token.token),
        inventory=inventory,
        event=event,
        payload=payload,
        expected_base_branch=policy.base_branch,
    )


def config_authority_event_supported(event: str, payload: dict[str, object]) -> bool:
    return (
        event == "push"
        or (event == "merge_group" and payload.get("action") == "checks_requested")
        or (
            event == "pull_request"
            and (
                payload.get("action") in {"opened", "reopened", "synchronize"}
                or (
                    payload.get("action") == "edited"
                    and isinstance(payload.get("changes"), dict)
                    and "base" in cast(dict[str, object], payload["changes"])
                )
            )
        )
    )


def _sha(value: object) -> str:
    if (
        not isinstance(value, str)
        or not GIT_COMMIT_SHA_PATTERN.fullmatch(value)
        or value == "0" * 40
    ):
        raise ValueError("Source evidence requires an available explicit commit SHA.")
    return value


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("Source evidence requires a JSON object.")
    return cast(dict[str, object], value)


def publish_product_config_authority_evidence(
    inventory: RepositoryInventoryRecord,
    evidence: dict[str, JsonValue],
    root: Path,
) -> dict[str, JsonValue]:
    """Project with the existing checks-only App; this check is not self-excluded."""
    head = _sha(evidence.get("head_sha"))
    token = mint_repository_installation_token(
        identity=resolve_advisory_github_app_identity(control_plane_root=root),
        repository=inventory.repository,
        repository_id=inventory.repository_id,
    )
    identity = hashlib.sha256(
        json.dumps(
            {
                "repository_id": inventory.repository_id,
                "base_sha": evidence.get("base_sha"),
                "event": evidence.get("event"),
                "base_branch": evidence.get("base_branch"),
                "head_sha": head,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    gate = evidence.get("gate")
    coverage = evidence.get("coverage")
    gate = gate if isinstance(gate, dict) else {}
    coverage = coverage if isinstance(coverage, dict) else {}
    pending = evidence.get("retry_pending") is True
    summary = (
        f"Source comparison: {evidence.get('base_sha', 'unavailable')} → {head}.\n"
        + (
            "Source verification is pending retry."
            if pending
            else "Product-repo configuration gate passed."
            if evidence.get("status") == "pass"
            else "Verification failed or source evidence is unavailable."
        )
        + "\n"
        + json.dumps(
            {
                "rejected_finding_count": gate.get("rejected_finding_count"),
                "coverage_gap_count": coverage.get("coverage_gap_count"),
                "hashes": evidence.get("hashes"),
                "rejected_findings_sample": cast(
                    list[JsonValue], gate.get("rejected_findings") or []
                )[:10],
            },
            sort_keys=True,
        )
    )
    summary = summary.encode()[:60000].decode("utf-8", errors="ignore")
    summary += (
        "\nFull evidence: GET /v1/repository-inventory?repository_id="
        + quote(inventory.repository_id, safe="")
        + "&delivery_id="
        + quote(str(evidence["delivery_id"]), safe="")
    )
    projection = ConfigAuthorityCheckProjection(
        name=(
            f"{CONFIG_AUTHORITY_CHECK_NAME}/{str(evidence.get('event')).replace('_', '-')}/"
            + quote(str(evidence["base_branch"]), safe="")
        ),
        repository=inventory.repository,
        repository_id=inventory.repository_id,
        head_sha=head,
        external_id=identity,
        details_url=f"https://github.com/{inventory.repository}/commit/{head}",
        title="Product configuration authority",
        summary=summary,
        check_status="in_progress" if pending else "completed",
        conclusion=None
        if pending
        else "success"
        if evidence.get("status") == "pass"
        else "failure",
    )
    return cast(
        dict[str, JsonValue],
        write_github_check_projection(
            projection=projection,
            installation_token=token,
        ).model_dump(mode="json"),
    )


class ConfigAuthorityEventStore(Protocol):
    def claim_next_config_authority_delivery(
        self,
        lease_owner: str,
        lease_seconds: int,
    ) -> GitHubAppWebhookDeliveryRecord | None: ...

    def complete_config_authority_delivery(
        self,
        claimed: GitHubAppWebhookDeliveryRecord,
        evidence: dict[str, JsonValue],
        publish: Callable[[], None] | None = None,
    ) -> GitHubAppWebhookDeliveryRecord: ...

    def list_repository_inventory_records(
        self,
        *,
        repository_id: str = "",
        limit: int | None = None,
    ) -> tuple[RepositoryInventoryRecord, ...]: ...


def run_product_config_authority_once(
    record_store: ConfigAuthorityEventStore,
    lease_owner: str,
    *,
    control_plane_root: Path = Path("."),
    publish: Callable[
        [RepositoryInventoryRecord, dict[str, JsonValue], Path], dict[str, JsonValue]
    ] = publish_product_config_authority_evidence,
    scan: Callable[
        [object, RepositoryInventoryRecord, str, dict[str, object]], dict[str, JsonValue]
    ] = run_product_config_authority_event,
) -> GitHubAppWebhookDeliveryRecord | None:
    claimed = record_store.claim_next_config_authority_delivery(lease_owner, 600)
    if claimed is None:
        return None
    inventory: RepositoryInventoryRecord | None = None
    try:
        inventory = get_repository_inventory_read_model(
            repository_id=claimed.repository_id,
            store=record_store,
        ).current_record
        if inventory is None or inventory.inventory_state != "tracked":
            raise ValueError("Source repository is no longer tracked.")
        evidence = scan(
            record_store,
            inventory,
            claimed.event,
            cast(dict[str, object], claimed.config_authority_request),
        )
    except Exception:
        # Provider errors can contain credentials; keep the refusal structured.
        evidence = {"status": "unavailable", "error_code": "source_evidence_unavailable"}
    evidence["event"] = claimed.event
    evidence["base_branch"] = claimed.config_authority_request.get("base_branch")
    evidence["delivery_id"] = claimed.delivery_id

    def project() -> None:
        if evidence.get("status") in {"disabled", "superseded"} or inventory is None:
            return
        if not evidence.get("head_sha"):
            request = claimed.config_authority_request
            group = request.get("merge_group")
            evidence["head_sha"] = (
                request.get("after")
                or request.get("head_sha")
                or (group.get("head_sha") if isinstance(group, dict) else None)
            )
        evidence["retry_pending"] = (
            evidence.get("status") == "unavailable" and claimed.config_authority_attempt < 3
        )
        try:
            evidence["projection"] = publish(inventory, evidence, control_plane_root)
        except Exception:
            evidence["scan_status"] = evidence.get("status")
            evidence["status"] = "unavailable"
            evidence["projection_status"] = "unavailable"
            evidence["retry_pending"] = claimed.config_authority_attempt < 3

    # The storage fence is held through publication and terminal persistence.
    return record_store.complete_config_authority_delivery(claimed, evidence, publish=project)
