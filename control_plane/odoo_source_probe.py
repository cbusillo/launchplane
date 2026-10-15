"""Isolated, bounded MySQL metadata probe. Never returns server-controlled text."""

import json
import re
import ssl
import sys
from typing import Literal

import pymysql  # type: ignore[import-untyped]

GrantStatus = Literal["read_only", "unsafe", "unknown"]
_READ_PRIVILEGES = frozenset({"SELECT", "SHOW VIEW", "SHOW DATABASES", "USAGE"})
# Accept only simple SHOW GRANTS privilege rows. Role assignments, partial revokes,
# authentication clauses and dialect extensions stay unknown, never read-only.
_GRANT = re.compile(
    r"GRANT ([A-Z ,]+) ON (?:\*|`(?:[^`]|``)+`)\.(?:\*|`(?:[^`]|``)+`)"
    r" TO (?:'[^']*'|`[^`]*`)@(?:'[^']*'|`[^`]*`)( WITH GRANT OPTION)?"
)


def classify_grants(rows: object) -> GrantStatus:
    if not isinstance(rows, (list, tuple)) or not 0 < len(rows) <= 128:
        return "unknown"
    unknown = False
    has_select = False
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) != 1 or not isinstance(row[0], str):
            return "unknown"
        text = row[0]
        if len(text) > 8192:
            return "unknown"
        match = _GRANT.fullmatch(text)
        if match is None:
            unknown = True
            continue
        privileges = {part.strip() for part in match[1].split(",")}
        if match[2] or not privileges <= _READ_PRIVILEGES:
            return "unsafe"
        has_select |= "SELECT" in privileges
    return "unknown" if unknown or not has_select else "read_only"


def probe(settings: dict[str, str]) -> GrantStatus:
    # TLS verification is mandatory. No plaintext, local socket or writer fallback.
    connection = pymysql.connect(
        host=settings["host"],
        user=settings["user"],
        password=settings["password"],
        port=int(settings.get("port", "3306")),
        connect_timeout=3,
        read_timeout=3,
        write_timeout=3,
        ssl=ssl.create_default_context(),
        local_infile=False,
        autocommit=True,
    )
    try:
        with connection.cursor() as cursor:
            # Bare SHOW GRANTS includes mandatory roles on MySQL 8; any role
            # assignment is unexpanded and therefore unknown. No SET ROLE/write probe.
            cursor.execute("SHOW GRANTS")
            return classify_grants(cursor.fetchmany(129))
    finally:
        connection.close()


def main() -> None:
    result: GrantStatus = "unknown"
    try:
        raw = sys.stdin.read(65537)
        if len(raw) <= 65536:
            settings = json.loads(raw)
            if isinstance(settings, dict) and all(
                isinstance(key, str) and isinstance(value, str) for key, value in settings.items()
            ):
                result = probe(settings)
    except Exception:
        # Source DNS/auth/TLS/driver messages may carry credentials, hosts or
        # authentication hashes. No exception chain or stderr leaves this process.
        pass
    sys.stdout.write(json.dumps({"grants": result}))


if __name__ == "__main__":
    main()
