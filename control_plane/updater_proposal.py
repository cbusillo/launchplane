"""Open a new source proposal with a service-held App, without delivery authority."""

from pathlib import Path
from datetime import UTC, datetime
from urllib.parse import quote
from hashlib import sha256
import json

from control_plane.contracts.updater_proposal import (
    UPDATER_PROPOSAL_ACTION,
    UPDATER_PROPOSAL_MAX_CHANGE_BYTES,
    UpdaterProposalRequest,
    UpdaterProposalResponse,
)
from control_plane.github_app_identity import (
    GitHubApiRequest,
    mint_delivery_installation_token,
    revoke_installation_token,
)
from control_plane.github_payload import json_object, required_positive_int, required_string_text
from control_plane.launchplane_github_delivery import resolve_delivery_github_app_identity
from control_plane.service_auth import GitHubActionsIdentity, LaunchplaneAuthzPolicy
from control_plane.workflows.launchplane import github_api_request


class UpdaterProposalRefused(ValueError):
    pass


def authorize_updater_proposal(
    *,
    identity: GitHubActionsIdentity,
    policy: LaunchplaneAuthzPolicy,
    request: UpdaterProposalRequest,
) -> None:
    # General wildcard grants must never authorize this credential-bearing route.
    # These exact rules live in the existing managed authorization policy record.
    expires_at = identity.raw_claims.get("exp")
    if (
        not isinstance(expires_at, int)
        or isinstance(expires_at, bool)
        or expires_at <= datetime.now(UTC).timestamp()
        or identity.repository != request.repository
        or identity.sha != request.base_sha
        or identity.ref_type != "branch"
        or not identity.ref.startswith("refs/heads/")
        or identity.event_name not in ("schedule", "workflow_dispatch", "push")
        or not identity.workflow_ref.startswith(identity.repository + "/.github/workflows/")
        or not identity.workflow_ref.endswith("@" + identity.ref)
    ):
        raise UpdaterProposalRefused("Updater caller does not match the source change.")
    for rule in policy.github_actions:
        if (
            rule.repository == identity.repository
            and rule.repository_id == identity.repository_id
            and rule.repository_owner_id == identity.repository_owner_id
            and bool(rule.repository_id)
            and bool(rule.repository_owner_id)
            and rule.workflow_refs == (identity.workflow_ref,)
            and rule.refs == (identity.ref,)
            and UPDATER_PROPOSAL_ACTION in rule.actions
            and rule.allows(
                identity=identity,
                action=UPDATER_PROPOSAL_ACTION,
                product="launchplane",
                context="launchplane",
                schema_version=policy.schema_version,
            )
        ):
            return
    raise UpdaterProposalRefused("Updater repository and workflow are not allow-listed.")


def open_updater_proposal(
    *,
    identity: GitHubActionsIdentity,
    request: UpdaterProposalRequest,
    control_plane_root: Path,
    api_request: GitHubApiRequest = github_api_request,
) -> UpdaterProposalResponse:
    """Only POST new Git objects, a new ref, and a new PR; never update a ref or PR."""
    issued = mint_delivery_installation_token(
        identity=resolve_delivery_github_app_identity(control_plane_root=control_plane_root),
        repository=identity.repository,
        repository_id=identity.repository_id,
        permissions={"contents": "write", "pull_requests": "write"},
        api_request=api_request,
    )
    try:
        prefix = "/repos/" + "/".join(
            quote(part, safe="") for part in identity.repository.split("/")
        )

        def call(path: str, body: dict[str, object] | None = None) -> dict[str, object]:
            return json_object(
                api_request(
                    path=prefix + path,
                    token=issued.token,
                    method="GET" if body is None else "POST",
                    body=body,
                ),
                "Updater provider response",
                error_type=ValueError,
            )

        repository = call("")
        owner = json_object(repository.get("owner"), "Repository owner", error_type=ValueError)
        if (
            repository.get("id") != int(identity.repository_id)
            or repository.get("full_name") != identity.repository
            or owner.get("id") != int(identity.repository_owner_id)
        ):
            raise UpdaterProposalRefused("Updater repository identity changed.")
        default_branch = required_string_text(
            repository.get("default_branch"),
            "Default branch unavailable.",
            error_type=ValueError,
        )
        if identity.ref != "refs/heads/" + default_branch:
            raise UpdaterProposalRefused("Updater must run on the current default branch.")
        base = call("/git/ref/heads/" + quote(default_branch, safe=""))
        base_object = json_object(base.get("object"), "Base ref", error_type=ValueError)
        if base_object.get("sha") != request.base_sha:
            raise UpdaterProposalRefused("Updater base no longer matches the default branch.")
        commit = call("/git/commits/" + request.base_sha)
        base_tree = json_object(commit.get("tree"), "Base tree", error_type=ValueError)
        base_tree_sha = required_string_text(
            base_tree.get("sha"),
            "Base tree SHA unavailable.",
            error_type=ValueError,
        )
        observed_tree = call("/git/trees/" + base_tree_sha + "?recursive=1")
        tree_entries = observed_tree.get("tree")
        if observed_tree.get("truncated") is not False or not isinstance(tree_entries, list):
            raise UpdaterProposalRefused("Updater base tree cannot be completely verified.")
        current_sizes: dict[str, int] = {}
        current_types: dict[str, object] = {}
        for item in tree_entries:
            base_entry = json_object(item, "Base tree entry", error_type=ValueError)
            path = required_string_text(
                base_entry.get("path"), "Base path unavailable.", error_type=ValueError
            )
            current_types[path] = base_entry.get("type")
            if base_entry.get("type") == "blob":
                size = base_entry.get("size")
                if not isinstance(size, int) or isinstance(size, bool) or size < 0:
                    raise UpdaterProposalRefused("Updater base file size is unavailable.")
                current_sizes[path] = size
        for change in request.files:
            if change.path in current_types and current_types[change.path] != "blob":
                raise UpdaterProposalRefused("Updater cannot replace a directory or submodule.")
            ancestors = [
                "/".join(change.path.split("/")[:index])
                for index in range(1, len(change.path.split("/")))
            ]
            if any(path in current_types and current_types[path] != "tree" for path in ancestors):
                raise UpdaterProposalRefused("Updater cannot replace an ancestor file.")
            if change.content is None and change.path not in current_sizes:
                raise UpdaterProposalRefused("Updater deletion requires an existing file.")
        changed_bytes = sum(
            current_sizes.get(change.path, 0) + len((change.content or "").encode("utf-8"))
            for change in request.files
        )
        if changed_bytes > UPDATER_PROPOSAL_MAX_CHANGE_BYTES:
            raise UpdaterProposalRefused(
                "Updater change exceeds the old-plus-new content size limit."
            )
        entries: list[dict[str, object]] = []
        for change in request.files:
            entry: dict[str, object] = {"path": change.path, "mode": change.mode, "type": "blob"}
            if change.content is None:
                entry["sha"] = None
            else:
                entry["content"] = change.content
            entries.append(entry)
        tree = call("/git/trees", {"base_tree": base_tree_sha, "tree": entries})
        tree_sha = required_string_text(
            tree.get("sha"), "Tree SHA unavailable.", error_type=ValueError
        )
        if tree_sha == base_tree_sha:
            raise UpdaterProposalRefused("Updater change is empty.")
        new_commit = call(
            "/git/commits",
            {
                "message": request.title,
                "tree": tree_sha,
                "parents": [request.base_sha],
            },
        )
        head_sha = required_string_text(
            new_commit.get("sha"),
            "Commit SHA unavailable.",
            error_type=ValueError,
        )
        proposal_digest = sha256(
            json.dumps(
                request.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        # Network retries from launchplane-request must not create duplicate branches.
        # A retry can only attempt to create this same ref, never update it.
        branch = "launchplane/updater/" + proposal_digest
        if branch == default_branch:
            raise UpdaterProposalRefused("Updater branch conflicts with the default branch.")
        call("/git/refs", {"ref": "refs/heads/" + branch, "sha": head_sha})
        pull_request = call(
            "/pulls",
            {
                "title": request.title,
                "body": request.body,
                "head": branch,
                "base": default_branch,
            },
        )
        return UpdaterProposalResponse(
            repository=identity.repository,
            branch=branch,
            head_sha=head_sha,
            pull_request_number=required_positive_int(
                pull_request.get("number"),
                "PR number unavailable.",
                error_type=ValueError,
            ),
            pull_request_url=required_string_text(
                pull_request.get("html_url"),
                "PR URL unavailable.",
                error_type=ValueError,
            ),
        )
    finally:
        revoke_installation_token(installation_token=issued, api_request=api_request)
