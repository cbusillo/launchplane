from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Literal

from control_plane.contracts.canonical_json import (
    canonical_json_sha256,
    nonvalidating_compact_sorted_json_sha256,
)
from control_plane.contracts.candidate_custody_evidence import (
    ConsumerEvidence,
    Outcome,
    Predicate,
    QualificationEvidence,
    Shape,
)


def check(evidence: QualificationEvidence | None, predicate: Predicate, passed: bool) -> bool:
    if evidence is not None:
        evidence.checks[predicate] = "pass" if passed else "fail"
    return passed


@contextmanager
def acquiring(
    evidence: QualificationEvidence | None,
    predicate: Predicate,
    *,
    failure: Literal["fail", "unavailable"] = "unavailable",
) -> Iterator[None]:
    if evidence is not None:
        evidence.checks[predicate] = "unavailable"
    try:
        yield
    except Exception:
        if evidence is not None:
            evidence.checks[predicate] = failure
        raise
    if evidence is not None:
        evidence.checks[predicate] = "pass"


def shape(value: object, *, present: bool = True) -> Shape:
    if not present:
        return "missing"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dict):
        return "mapping"
    if isinstance(value, list):
        return "list"
    if isinstance(value, str):
        return "string"
    if isinstance(value, int | float):
        return "number"
    return "other"


def placement(evidence: ConsumerEvidence | None, payload: dict[str, Any], host: str) -> None:
    if evidence is None:
        return
    value = payload.get("serverId")
    evidence.payload_sha256 = nonvalidating_compact_sorted_json_sha256(payload)
    evidence.server_identity = (
        "missing"
        if "serverId" not in payload
        else "local"
        if value is None
        else "remote"
        if isinstance(value, str) and value
        else "malformed"
    )
    if evidence.server_identity in {"local", "remote"}:
        evidence.server_sha256 = canonical_json_sha256({"server": value})
        evidence.endpoint_sha256 = canonical_json_sha256({"host": host, "server": value})
    source = payload.get("sourceType")
    evidence.source_type = (
        "missing"
        if "sourceType" not in payload
        else "null"
        if source is None
        else "empty"
        if source == ""
        else source
        if source in ("raw", "git", "github", "gitlab", "bitbucket", "gitea")
        else "other"
    )


def readback_outcome(value: Literal["pass", "fail", "unavailable", "skipped"]) -> Outcome:
    return "not_reached" if value == "skipped" else value
