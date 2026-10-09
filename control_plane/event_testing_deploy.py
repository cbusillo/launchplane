"""The request shape shared by event deploy and existing-reservation recovery."""

from control_plane.drivers.generic_web_dispatch import GenericWebDeployEnvelope
from control_plane.workflows.generic_web_deploy import GenericWebDeployRequest


def event_testing_deploy_request(
    *, product: str, image_reference: str, source_commit: str, deploy_reference: str = ""
) -> GenericWebDeployEnvelope:
    return GenericWebDeployEnvelope(
        product=product,
        deploy=GenericWebDeployRequest(
            product=product,
            instance="testing",
            artifact_id=image_reference,
            deploy_reference=deploy_reference,
            source_git_ref=source_commit,
        ),
    )
