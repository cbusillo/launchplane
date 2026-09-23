"""Resolve only the credential source declared by a merge-train policy."""

import os
from pathlib import Path

from control_plane.contracts.merge_train_policy import MergeTrainGitHubTokenSource
from control_plane.workflows.launchplane import resolve_launchplane_github_token


def resolve_merge_train_github_token(
    *, source: MergeTrainGitHubTokenSource, control_plane_root: Path
) -> str:
    if source.runtime_context:
        return resolve_launchplane_github_token(
            control_plane_root=control_plane_root,
            context_name=source.runtime_context,
        )
    if source.env_var:
        return os.environ.get(source.env_var, "").strip()
    return ""
