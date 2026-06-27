from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Generic, TypeVar, cast

from pydantic import BaseModel, ConfigDict

from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneProfile,
)
from control_plane.contracts.promotion_record import ReleaseStatus


class _ProductRouteEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product: str


_DriverRouteEnvelopeT = TypeVar("_DriverRouteEnvelopeT", bound=_ProductRouteEnvelope)


@dataclass(frozen=True)
class _DriverRouteExecutionMetadata(Generic[_DriverRouteEnvelopeT]):
    route_path: str
    envelope_model: type[_DriverRouteEnvelopeT]
    denial_message: str


@dataclass(frozen=True)
class _ResolvedProductDriverContext:
    profile: LaunchplaneProductProfileRecord | None
    lane: ProductLaneProfile | None = None


def _validate_driver_envelope_product(product: str, *, label: str) -> None:
    if not product.strip():
        raise ValueError(f"{label} requires product.")


class ProductDriverMismatchError(ValueError):
    pass


class DriverRouteDependencyNotFoundError(ValueError):
    pass


def _repo_token(value: str) -> str:
    normalized = value.strip().replace("_", "-")
    normalized = "-".join(filter(None, re.split(r"[^A-Za-z0-9]+", normalized)))
    if not normalized:
        raise ValueError("repository token is required")
    return normalized.lower()


def _image_reference_tail(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        return ""
    for separator in ("@", ":"):
        if separator in normalized:
            normalized = normalized.rsplit(separator, maxsplit=1)[1]
    return normalized.strip()


def _normalize_release_status(value: object, *, label: str) -> ReleaseStatus:
    normalized = str(value or "").strip().lower()
    if normalized in {"success", "passed", "pass"}:
        return "pass"
    if normalized in {"failure", "failed", "fail", "cancelled", "canceled", "timed_out"}:
        return "fail"
    if normalized in {"skipped", "not-run", "not_run", ""}:
        return "skipped"
    if normalized in {"pending", "in_progress", "in-progress"}:
        return "pending"
    raise ValueError(f"{label} must be pass, fail, skipped, or pending.")


def _normalize_preview_verification_checked_urls(value: object, *, label: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        raw_values = tuple(value)
    else:
        raise ValueError(f"{label} checked_urls must be a list.")
    if any(not isinstance(item, str) for item in raw_values):
        raise ValueError(f"{label} checked_urls must be strings.")
    checked_urls = tuple(item.strip() for item in cast(tuple[str, ...], raw_values))
    if any(not item for item in checked_urls):
        raise ValueError(f"{label} checked_urls cannot contain blanks.")
    return checked_urls
