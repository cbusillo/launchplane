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
