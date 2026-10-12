"""Validate an operator-supplied candidate against the bootstrap image repository."""

import os

from control_plane.contracts.deploy_reference import docker_image_digest


def main() -> None:
    image = os.environ.get("CANDIDATE_IMAGE", "")
    repository = os.environ.get("LAUNCHPLANE_IMAGE_REPOSITORY", "") or (
        "ghcr.io/" + os.environ["GITHUB_REPOSITORY"].lower()
    )
    digest = docker_image_digest(image)
    if not digest or image != f"{repository}@{digest}":
        raise ValueError("Candidate requires the configured repository and an immutable digest.")


if __name__ == "__main__":
    main()
