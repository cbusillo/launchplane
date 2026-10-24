from control_plane.odoo_connection_isolation import connection_isolation_library


def preview_restore_program() -> str:
    return (
        """import os
from pathlib import Path
import shutil
import sys
sys.path.insert(0, '/volumes/scripts')
import run_odoo_data_workflows as workflow
"""
        + connection_isolation_library()
        + """
if os.environ.get("PLATFORM_INSTANCE") != "preview":
    raise SystemExit("Preview restore requires its preview instance")
os.environ.update(BOOTSTRAP="false", NO_SANITIZE="false", ENV_OVERRIDE_DISABLE_CRON="true",
                  ODOO_RESTORE_KEPT_INTEGRATIONS="")
if not callable(getattr(workflow.OdooDataWorkflowRunner, "prepare_credentials_before_registry", None)):
    raise SystemExit("Artifact lacks the pre-registry credential boundary")
settings = workflow.LocalServerSettings()
if settings.host != "database" or settings.platform_instance != "preview":
    raise SystemExit("Preview restore settings do not name its preview database")
dump_directory = settings.data_workflow_lock_file.with_name(f".{settings.db_name}-upstream-restore")


class PreviewRestoreRunner(workflow.OdooDataWorkflowRunner):
    def prepare_credentials_before_registry(self, *, restored_copy: bool = False, do_sanitize: bool = False) -> None:
        super().prepare_credentials_before_registry(restored_copy=restored_copy, do_sanitize=do_sanitize)
        connection = self.connect_to_db()
        with connection.cursor() as cursor:
            if connection_isolation(cursor, apply=True):
                raise RuntimeError("Preview pre-registry connection isolation failed")
        connection.commit()
        observer = self._connect_with_retry(self.local.db_name)
        try:
            with observer.cursor() as cursor:
                cursor.execute("SET TRANSACTION READ ONLY")
                if connection_isolation(cursor, apply=False):
                    raise RuntimeError("Preview pre-registry connection readback failed")
            observer.rollback()
        finally:
            observer.close()
        (dump_directory / "database.dump").unlink(missing_ok=True)


workflow.OdooDataWorkflowRunner = PreviewRestoreRunner
try:
    raise SystemExit(workflow.main([]))
finally:
    if dump_directory.exists():
        shutil.rmtree(dump_directory)
"""
    )
