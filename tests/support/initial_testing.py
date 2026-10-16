"""Fixture source intent for an independent initial Odoo testing copy."""

from control_plane.contracts.product_profile_record import ProductOdooLaneDataPolicy
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.storage.postgres import PostgresRecordStore


def configure_initial_restore(store: PostgresRecordStore, product: str, context: str) -> None:
    profile = store.read_product_profile_record(product)
    production = next(lane for lane in profile.lanes if lane.instance == "prod")
    store.write_product_profile_record(
        profile.model_copy(
            update={
                "lanes": tuple(
                    lane.model_copy(
                        update={
                            "odoo_data_policy": ProductOdooLaneDataPolicy(
                                data_authority="resettable",
                                allowed_rebuild_sources=("upstream_restore",),
                                upstream_source=f"{product}:{production.context}:prod",
                            )
                        }
                    )
                    if (lane.context, lane.instance) == (context, "testing")
                    else lane
                    for lane in profile.lanes
                )
            }
        )
    )
    store.write_runtime_environment_record(
        RuntimeEnvironmentRecord(
            scope="context",
            context=context,
            updated_at=profile.updated_at,
            env={
                "ODOO_UPSTREAM_HOST": "source.invalid",
                "ODOO_UPSTREAM_USER": "restore-reader",
                "ODOO_UPSTREAM_DB_USER": "postgres",
                "ODOO_UPSTREAM_DB_NAME": "source_db",
                "ODOO_UPSTREAM_FILESTORE_PATH": "/source/filestore/source_db",
            },
        )
    )
