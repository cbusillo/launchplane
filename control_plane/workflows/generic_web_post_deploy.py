"""Select shared post-deploy executors from product-owned lane declarations."""

from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.workflows.generic_web_deploy import GenericWebPostDeployExecutor


def declares_verireel_testing_extension(
    profile: LaunchplaneProductProfileRecord, *, instance: str
) -> bool:
    return (
        profile.driver_id.strip() == "generic-web"
        and instance == "testing"
        and any(
            lane.instance == instance
            and lane.generic_web_extensions.verification_extension == "verireel-testing"
            for lane in profile.lanes
        )
    )


def generic_web_post_deploy_executor_for_profile(
    profile: LaunchplaneProductProfileRecord,
    *,
    instance: str,
) -> GenericWebPostDeployExecutor | None:
    if declares_verireel_testing_extension(profile, instance=instance):
        from control_plane.workflows.verireel_testing_post_deploy import (
            execute_verireel_testing_post_deploy,
        )

        return execute_verireel_testing_post_deploy
    return generic_web_post_deploy_executor_for_driver_id(profile.driver_id, instance=instance)


def generic_web_post_deploy_executor_for_driver_id(
    driver_id: str,
    *,
    instance: str = "",
) -> GenericWebPostDeployExecutor | None:
    if driver_id.strip() == "odoo":
        from control_plane.workflows.odoo_generic_web_post_deploy import (
            execute_odoo_generic_web_post_deploy,
        )

        return execute_odoo_generic_web_post_deploy
    if driver_id.strip() == "verireel" and instance == "testing":
        from control_plane.workflows.verireel_testing_post_deploy import (
            execute_verireel_testing_post_deploy,
        )

        return execute_verireel_testing_post_deploy
    return None
