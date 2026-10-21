"""Open a new source proposal with a service-held App, without delivery authority."""

from pathlib import Path
from datetime import UTC, datetime
from urllib.parse import quote
from hashlib import sha256
import json
import logging
from urllib.error import HTTPError
from typing import Literal

from control_plane.contracts.updater_proposal import (
    UPDATER_PROPOSAL_ACTION,
    UPDATER_PROPOSAL_MAX_CHANGE_BYTES,
    UPDATER_PROPOSAL_MAX_FILE_BYTES,
    UPDATER_PROPOSAL_MAX_MATERIALIZED_BYTES,
    UpdaterFileEdit,
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
from control_plane.updater_proposal_edits import materialize_updater_edit


_LOGGER = logging.getLogger(__name__)


class UpdaterProposalConflict(ValueError):
    pass


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
            and rule.products == ("launchplane",)
            and rule.contexts == ("launchplane",)
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
    app_metadata: dict[str, object] = {}

    def mint_api_request(
        *, path: str, token: str, method: str = "GET", body: dict[str, object] | None = None
    ) -> object:
        payload = api_request(path=path, token=token, method=method, body=body)
        if path == "/app":
            app_metadata.update(json_object(payload, "App metadata", error_type=ValueError))
        return payload

    issued = mint_delivery_installation_token(
        identity=resolve_delivery_github_app_identity(control_plane_root=control_plane_root),
        repository=identity.repository,
        repository_id=identity.repository_id,
        permissions={"contents": "write", "pull_requests": "write"},
        api_request=mint_api_request,
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

        app_slug = required_string_text(
            app_metadata.get("slug"), "App slug unavailable.", error_type=ValueError
        )
        app_user = json_object(
            api_request(path="/users/" + quote(app_slug + "[bot]", safe=""), token=issued.token),
            "App user",
            error_type=ValueError,
        )
        app_user_id = required_positive_int(
            app_user.get("id"), "App user id unavailable.", error_type=ValueError
        )
        workflow_prefix = (
            "launchplane/updater/" + sha256(identity.workflow_ref.encode()).hexdigest() + "/"
        )
        proposal_digest = sha256(
            json.dumps(
                request.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        branch = workflow_prefix + proposal_digest

        def observed_proposal(
            payload: dict[str, object], disposition: Literal["created", "replayed", "pending"]
        ) -> UpdaterProposalResponse:
            head = json_object(payload.get("head"), "PR head", error_type=ValueError)
            head_repository = json_object(head.get("repo"), "PR repository", error_type=ValueError)
            user = json_object(payload.get("user"), "PR author", error_type=ValueError)
            base = json_object(payload.get("base"), "PR base", error_type=ValueError)
            if (
                head_repository.get("id") != int(identity.repository_id)
                or user.get("id") != app_user_id
                or base.get("ref") != default_branch
            ):
                raise UpdaterProposalConflict(
                    "Existing updater proposal has a different source or author."
                )
            return UpdaterProposalResponse(
                repository=identity.repository,
                branch=required_string_text(
                    head.get("ref"), "PR branch unavailable.", error_type=ValueError
                ),
                head_sha=required_string_text(
                    head.get("sha"), "PR head unavailable.", error_type=ValueError
                ),
                pull_request_number=required_positive_int(
                    payload.get("number"), "PR number unavailable.", error_type=ValueError
                ),
                pull_request_url=required_string_text(
                    payload.get("html_url"), "PR URL unavailable.", error_type=ValueError
                ),
                disposition=disposition,
            )

        def pending_proposal() -> UpdaterProposalResponse | None:
            # A bounded, complete read of open proposals; no labels or PR edits.
            for page in range(1, 6):
                payload = api_request(
                    path=prefix
                    + "/pulls?state=open&base="
                    + quote(default_branch, safe="/")
                    + "&per_page=100&page="
                    + str(page),
                    token=issued.token,
                )
                if not isinstance(payload, list):
                    raise ValueError("Open proposal list unavailable.")
                matches: list[dict[str, object]] = []
                for item in payload:
                    item = json_object(item, "Open proposal", error_type=ValueError)
                    head = json_object(
                        item.get("head"), "Open proposal head", error_type=ValueError
                    )
                    ref = head.get("ref")
                    if isinstance(ref, str) and ref.startswith(workflow_prefix):
                        head_repo = head.get("repo")
                        author = item.get("user")
                        if not isinstance(head_repo, dict) or head_repo.get("id") != int(
                            identity.repository_id
                        ):
                            continue
                        if not isinstance(author, dict) or author.get("id") != app_user_id:
                            if ref == branch:
                                raise UpdaterProposalConflict(
                                    "Existing updater proposal has a different author."
                                )
                            continue
                        matches.append(item)
                if matches:
                    exact = next(
                        (
                            item
                            for item in matches
                            if json_object(item.get("head"), "Head", error_type=ValueError).get(
                                "ref"
                            )
                            == branch
                        ),
                        None,
                    )
                    return observed_proposal(
                        exact or matches[0], "replayed" if exact is not None else "pending"
                    )
                if len(payload) < 100:
                    return None
            raise ValueError("Open proposal inventory is incomplete.")

        def closed_proposal() -> UpdaterProposalResponse | None:
            history = api_request(
                path=prefix
                + "/pulls?state=closed&head="
                + quote(identity.repository.split("/", 1)[0] + ":" + branch, safe="")
                + "&per_page=100",
                token=issued.token,
            )
            if not isinstance(history, list):
                raise ValueError("Proposal history unavailable.")
            for item in history:
                item = json_object(item, "Closed proposal", error_type=ValueError)
                head = json_object(item.get("head"), "Closed head", error_type=ValueError)
                if head.get("ref") != branch:
                    continue
                head_repo = head.get("repo")
                author = item.get("user")
                if (
                    not isinstance(head_repo, dict)
                    or head_repo.get("id") != int(identity.repository_id)
                    or not isinstance(author, dict)
                    or author.get("id") != app_user_id
                ):
                    continue
                return observed_proposal(item, "replayed")
            return None

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
        pending = pending_proposal()
        if pending is not None:
            return pending
        closed = closed_proposal()
        if closed is not None:
            return closed
        base = call("/git/ref/heads/" + quote(default_branch, safe="/"))
        base_object = json_object(base.get("object"), "Base ref", error_type=ValueError)
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
        current_modes: dict[str, object] = {}
        current_blob_shas: dict[str, object] = {}
        for item in tree_entries:
            base_entry = json_object(item, "Base tree entry", error_type=ValueError)
            path = required_string_text(
                base_entry.get("path"), "Base path unavailable.", error_type=ValueError
            )
            current_types[path] = base_entry.get("type")
            current_modes[path] = base_entry.get("mode")
            current_blob_shas[path] = base_entry.get("sha")
            if base_entry.get("type") == "blob":
                size = base_entry.get("size")
                if not isinstance(size, int) or isinstance(size, bool) or size < 0:
                    raise UpdaterProposalRefused("Updater base file size is unavailable.")
                current_sizes[path] = size
        for change in request.files:
            if change.path in current_types and current_types[change.path] != "blob":
                raise UpdaterProposalRefused("Updater cannot replace a directory or submodule.")
            if change.path in current_modes and current_modes[change.path] not in (
                "100644",
                "100755",
            ):
                raise UpdaterProposalRefused("Updater requires regular files, not symlinks.")
            ancestors = [
                "/".join(change.path.split("/")[:index])
                for index in range(1, len(change.path.split("/")))
            ]
            if any(path in current_types and current_types[path] != "tree" for path in ancestors):
                raise UpdaterProposalRefused("Updater cannot replace an ancestor file.")
            if isinstance(change, UpdaterFileEdit):
                if (
                    change.path not in current_sizes
                    or current_blob_shas.get(change.path) != change.base_blob_sha
                ):
                    raise UpdaterProposalRefused(
                        "Updater edit requires the exact existing base blob."
                    )
            elif change.content is None and change.path not in current_sizes:
                raise UpdaterProposalRefused("Updater deletion requires an existing file.")
        changed_bytes = sum(
            change.submitted_change_bytes
            + (0 if isinstance(change, UpdaterFileEdit) else current_sizes.get(change.path, 0))
            for change in request.files
        )
        if changed_bytes > UPDATER_PROPOSAL_MAX_CHANGE_BYTES:
            raise UpdaterProposalRefused(
                "Updater change exceeds the old-plus-new content size limit."
            )
        materialized_bytes = 0
        for change in request.files:
            base_size = current_sizes.get(change.path, 0)
            if isinstance(change, UpdaterFileEdit):
                result_size = base_size + change.size_delta
                if (
                    base_size > UPDATER_PROPOSAL_MAX_FILE_BYTES
                    or not 0 <= result_size <= UPDATER_PROPOSAL_MAX_FILE_BYTES
                    or any(
                        edit.start_byte + len(edit.old_text.encode("utf-8")) > base_size
                        for edit in change.edits
                    )
                ):
                    raise UpdaterProposalRefused(
                        "Updater edit exceeds the base or result size limit."
                    )
            else:
                result_size = change.submitted_change_bytes
            materialized_bytes += base_size + result_size
        if materialized_bytes > UPDATER_PROPOSAL_MAX_MATERIALIZED_BYTES:
            raise UpdaterProposalRefused("Updater change exceeds the materialization size limit.")
        edited_contents: dict[str, str] = {}
        for change in request.files:
            if isinstance(change, UpdaterFileEdit):
                blob = call("/git/blobs/" + change.base_blob_sha)
                try:
                    edited_contents[change.path] = materialize_updater_edit(
                        change, base_size=current_sizes[change.path], blob=blob
                    )
                except ValueError as error:
                    raise UpdaterProposalRefused(str(error)) from error
        entries: list[dict[str, object]] = []
        for change in request.files:
            entry: dict[str, object] = {
                "path": change.path,
                "mode": (
                    current_modes[change.path]
                    if isinstance(change, UpdaterFileEdit)
                    else change.mode or current_modes.get(change.path, "100644")
                ),
                "type": "blob",
            }
            if isinstance(change, UpdaterFileEdit):
                entry["content"] = edited_contents[change.path]
            elif change.content is None:
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

        def read_existing_ref() -> dict[str, object] | None:
            try:
                return call("/git/ref/heads/" + quote(branch, safe="/"))
            except Exception as error:
                if _provider_status(error) == 404:
                    return None
                raise

        def verified_head(ref: dict[str, object]) -> str:
            ref_object = json_object(ref.get("object"), "Existing ref", error_type=ValueError)
            sha = required_string_text(
                ref_object.get("sha"), "Existing head unavailable.", error_type=ValueError
            )
            existing_commit = call("/git/commits/" + sha)
            parents = existing_commit.get("parents")
            existing_tree = json_object(
                existing_commit.get("tree"), "Existing tree", error_type=ValueError
            )
            if (
                existing_tree.get("sha") != tree_sha
                or not isinstance(parents, list)
                or len(parents) != 1
                or json_object(parents[0], "Existing parent", error_type=ValueError).get("sha")
                != request.base_sha
            ):
                raise UpdaterProposalConflict(
                    "Existing updater branch does not match this proposal."
                )
            return sha

        if branch == default_branch:
            raise UpdaterProposalRefused("Updater branch conflicts with the default branch.")
        existing_ref = read_existing_ref()
        if existing_ref is not None:
            head_sha = verified_head(existing_ref)
        else:
            if base_object.get("sha") != request.base_sha:
                raise UpdaterProposalConflict(
                    "Updater base changed; rerun from the current default branch."
                )
            new_commit = call(
                "/git/commits",
                {"message": request.title, "tree": tree_sha, "parents": [request.base_sha]},
            )
            head_sha = required_string_text(
                new_commit.get("sha"), "Commit SHA unavailable.", error_type=ValueError
            )
            try:
                call("/git/refs", {"ref": "refs/heads/" + branch, "sha": head_sha})
            except Exception as error:
                existing_ref = read_existing_ref()
                if existing_ref is None:
                    raise error
                head_sha = verified_head(existing_ref)
        pending = pending_proposal()
        if pending is not None:
            return pending
        closed = closed_proposal()
        if closed is not None:
            return closed
        try:
            pull_request = call(
                "/pulls",
                {
                    "title": request.title,
                    "body": request.body,
                    "head": branch,
                    "base": default_branch,
                },
            )
        except Exception:
            pending = pending_proposal()
            if pending is not None:
                return pending
            raise
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
        try:
            revoke_installation_token(installation_token=issued, api_request=api_request)
        except Exception as error:
            _LOGGER.warning(
                "Updater token revocation failed (%s); the issued token retains its provider expiry",
                type(error).__name__,
            )


def _provider_status(error: BaseException) -> int | None:
    seen: set[int] = set()
    while id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, HTTPError):
            return error.code
        if error.__cause__ is None:
            return None
        error = error.__cause__
    return None
