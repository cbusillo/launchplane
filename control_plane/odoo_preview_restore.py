from control_plane.odoo_connection_isolation import connection_isolation_library
from pathlib import Path

PREVIEW_WORKFLOW_SSH_DIRECTORY = Path("/tmp/platform-data-workflow-ssh")


def preview_source_process_program() -> str:
    return """import os
from pathlib import Path
status = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
if (os.geteuid() == 0 or os.access('/root', os.X_OK)
    or status.get('NoNewPrivs', '').strip() != '1'
    or any(int(status.get(key, '1').strip(), 16) for key in ('CapEff', 'CapPrm', 'CapAmb'))):
    raise SystemExit('Preview source requires a non-root, unprivileged workflow process and private root directory')
"""


def preview_capture_cleanup_program() -> str:
    return (
        f"IDENTITY_DIRECTORY = {str(PREVIEW_WORKFLOW_SSH_DIRECTORY)!r}\n"
        + """import os
from pathlib import Path
import re
import shutil
database = os.environ['ODOO_DB_NAME']
lock = Path(os.environ['ODOO_DATA_WORKFLOW_LOCK_FILE'])
if not re.fullmatch(r'[a-zA-Z0-9_]+', database) or not lock.is_absolute() or '..' in lock.parts:
    raise SystemExit('Preview capture cleanup requires its exact database and lock path')
for directory in (lock.with_name(f'.{database}-upstream-restore'), Path(IDENTITY_DIRECTORY)):
    if directory.exists():
        shutil.rmtree(directory)
"""
    )


def preview_restore_program() -> str:
    return (
        preview_source_process_program()
        + f"IDENTITY_DIRECTORY = {str(PREVIEW_WORKFLOW_SSH_DIRECTORY)!r}\n"
        + """import os
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
    def _prepare_restored_database(self, target_owner: str | None, *, do_sanitize: bool) -> None:
        source_directory = Path(self.os_env.get('DATA_WORKFLOW_SSH_DIR', ''))
        if source_directory != Path(IDENTITY_DIRECTORY):
            raise RuntimeError('Preview transfer identity is not in its temporary directory')
        shutil.rmtree(source_directory)
        for environment in (self.os_env, os.environ):
            for key in tuple(environment):
                if key.startswith(('DATA_WORKFLOW_SSH_', 'ODOO_UPSTREAM_')):
                    environment.pop(key)
        super()._prepare_restored_database(target_owner, do_sanitize=do_sanitize)

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
