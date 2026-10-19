import sqlite3
from typing import Any


def _skip_sqlite_disk_writes(dbapi_connection: Any, _connection_record: Any) -> None:
    if not isinstance(dbapi_connection, sqlite3.Connection):
        return
    dbapi_connection.execute("PRAGMA synchronous = OFF")
    (journal_mode,) = dbapi_connection.execute("PRAGMA journal_mode").fetchone()
    if journal_mode == "delete":
        dbapi_connection.execute("PRAGMA journal_mode = MEMORY")


# Test databases are disposable, so SQLite need not sync each commit to disk or
# create and delete a journal file per transaction. Locking and what other
# connections and processes see are unchanged. A test that chose another journal
# mode, such as WAL, keeps it.
try:
    from sqlalchemy import event
    from sqlalchemy.engine import Engine
except ImportError:  # Some workflows run dependency-free tests without the project.
    pass
else:
    event.listen(Engine, "connect", _skip_sqlite_disk_writes)


def _check_controller_replies() -> None:
    """Fail any test whose merge-train controller pass replies outside the published schema.

    Every controller reply is built from one result type. Checking each one a
    test produces keeps `contracts/merge-train-controller-reply.schema.json`
    complete: a new reply field fails here until its contract declares it.
    """

    from control_plane import merge_train_controller_run_once as controller
    from control_plane.contracts.merge_train_controller_reply import (
        validate_merge_train_controller_reply,
    )
    from control_plane.http_routes.mutation_support import accepted_evidence_response

    result_type = controller.MergeTrainControllerRunOnceResult
    build = result_type.__init__

    def build_checked(self: Any, *args: Any, **kwargs: Any) -> None:
        build(self, *args, **kwargs)
        reply = accepted_evidence_response(
            trace_id="trace-reply-contract", records=self.records, result=self.accepted_result
        ).model_dump(mode="json", exclude_none=True)
        validate_merge_train_controller_reply(reply)

    result_type.__init__ = build_checked  # type: ignore[method-assign]


try:
    _check_controller_replies()
except ImportError:  # Some workflows run dependency-free tests without the project.
    pass
