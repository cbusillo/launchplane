"""Isolated, bounded MySQL metadata probe. Never returns server-controlled text."""

import json
import inspect
import re
import socket
import ssl
import sys
from typing import Literal

import pymysql

GrantStatus = Literal["read_only", "unsafe", "unknown"]
ProbeFailure = Literal["dns", "tcp", "tls", "timeout", "auth", "driver"]
_MYSQL_ACCESS_DENIED_CODES = frozenset({1044, 1045, 1698})
MAX_GRANT_NAMES = 32
_READ_PRIVILEGES = frozenset({"SELECT", "SHOW VIEW", "SHOW DATABASES", "USAGE"})
# Accept only simple SHOW GRANTS privilege rows. Role assignments, partial revokes,
# authentication clauses and dialect extensions stay unknown, never read-only.
_GRANT = re.compile(
    r"GRANT ([A-Z ,]+) ON (?:\*|`(?:[^`]|``)+`)\.(?:\*|`(?:[^`]|``)+`)"
    r" TO (?:'[^']*'|`[^`]*`)@(?:'[^']*'|`[^`]*`)( WITH GRANT OPTION)?"
)


def classify_grants(rows: object, *, mariadb_authentication: bool = False) -> GrantStatus:
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
        if mariadb_authentication:
            text = re.sub(
                r" IDENTIFIED BY PASSWORD '\*[A-F0-9]{40}'(?= WITH GRANT OPTION$|$)", "", text
            )
        match = _GRANT.fullmatch(text)
        if match is None:
            unknown = True
            continue
        privileges = {part.strip() for part in match[1].split(",")}
        if match[2] or not privileges <= _READ_PRIVILEGES:
            return "unsafe"
        has_select |= "SELECT" in privileges
    return "unknown" if unknown or not has_select else "read_only"


def grant_names(rows: object) -> tuple[str, ...]:
    if not isinstance(rows, (list, tuple)):
        return ()
    names: set[str] = set()
    for row in rows:
        text = row[0] if isinstance(row, (list, tuple)) and len(row) == 1 else None
        match = _GRANT.fullmatch(text) if isinstance(text, str) else None
        if match is not None:
            names.update(part.strip() for part in match[1].split(",") if part.strip())
    return tuple(sorted(names))[:MAX_GRANT_NAMES]


def grant_classifier_program() -> str:
    return (
        "from __future__ import annotations\nimport re\n"
        + "_READ_PRIVILEGES = "
        + repr(_READ_PRIVILEGES)
        + "\n"
        + "_GRANT = re.compile("
        + repr(_GRANT.pattern)
        + ")\n"
        + inspect.getsource(classify_grants)
    )


def failure_kind(error: BaseException) -> ProbeFailure:
    cause = getattr(error, "original_exception", error)
    if isinstance(cause, socket.gaierror):
        return "dns"
    if isinstance(cause, ssl.SSLError):
        return "tls"
    if isinstance(cause, socket.timeout):
        return "timeout"
    if isinstance(cause, OSError):
        return "tcp"
    if error.args and error.args[0] in _MYSQL_ACCESS_DENIED_CODES:
        return "auth"
    return "driver"


def _tls_context(settings: dict[str, str]) -> ssl.SSLContext:
    context = ssl.create_default_context()
    if settings.get("ssl_verify") == "false":
        # insecure-tls: only the trusted private Fishbowl source sends this; its certificate names no host.
        # Decision: https://github.com/cbusillo/launchplane/issues/5810#issuecomment-6742151038
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


def observe(settings: dict[str, str]) -> tuple[GrantStatus, tuple[str, ...]]:
    connection = pymysql.connect(
        host=settings["host"],
        user=settings["user"],
        password=settings["password"],
        port=int(settings.get("port", "3306")),
        connect_timeout=3,
        read_timeout=3,
        write_timeout=3,
        ssl=_tls_context(settings),
        local_infile=False,
        autocommit=True,
    )
    try:
        with connection.cursor() as cursor:
            # Bare SHOW GRANTS includes mandatory roles on MySQL 8; any role
            # assignment is unexpanded and therefore unknown. No SET ROLE/write probe.
            cursor.execute("SHOW GRANTS")
            rows = cursor.fetchmany(129)
            return classify_grants(rows), grant_names(rows)
    finally:
        connection.close()


def probe(settings: dict[str, str]) -> GrantStatus:
    return observe(settings)[0]


def main() -> None:
    result: GrantStatus = "unknown"
    names: tuple[str, ...] = ()
    failure: ProbeFailure | None = None
    report_names = False
    try:
        raw = sys.stdin.read(65537)
        if len(raw) <= 65536:
            settings = json.loads(raw)
            if isinstance(settings, dict) and all(
                isinstance(key, str) and isinstance(value, str) for key, value in settings.items()
            ):
                report_names = settings.get("report_names") == "1"
                if report_names:
                    result, names = observe(settings)
                else:
                    result = probe(settings)
    except Exception as error:
        # Source DNS/auth/TLS/driver messages may carry credentials, hosts or
        # authentication hashes. No exception chain or stderr leaves this process.
        failure = failure_kind(error)
    sys.stdout.write(
        json.dumps(
            {"grants": result, "names": list(names), "failure": failure}
            if report_names
            else {"grants": result}
        )
    )


if __name__ == "__main__":
    main()
