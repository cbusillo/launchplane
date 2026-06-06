from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Generic, TypeVar

from pydantic import BaseModel, ConfigDict

from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneProfile,
)
from control_plane.service_auth import LaunchplaneIdentity


class _ProductRouteEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product: str


_DriverRouteEnvelopeT = TypeVar("_DriverRouteEnvelopeT", bound=_ProductRouteEnvelope)
_StartResponse = Callable[[str, list[tuple[str, str]]], None]


@dataclass(frozen=True)
class _DriverRouteExecutionMetadata(Generic[_DriverRouteEnvelopeT]):
    route_path: str
    envelope_model: type[_DriverRouteEnvelopeT]
    denial_message: str


@dataclass(frozen=True)
class _DescriptorDriverDispatchContext:
    product: str
    context: str
    authorization_context: str = ""
    use_preview_context_for_authorization: bool = False
    use_resolved_profile_product_for_authorization: bool = True
    instance: str = ""
    require_profile: bool = False


@dataclass(frozen=True)
class _DescriptorDriverDispatchResult:
    result: dict[str, object]
    driver_result: BaseModel | dict[str, object] | None = None


@dataclass(frozen=True)
class _ResolvedProductDriverContext:
    profile: LaunchplaneProductProfileRecord | None
    lane: ProductLaneProfile | None = None


_DescriptorDriverDispatchContextResolver = Callable[
    [_DriverRouteEnvelopeT], _DescriptorDriverDispatchContext
]
_DescriptorDriverAuthorizationActionResolver = Callable[[_DriverRouteEnvelopeT], str]
_DescriptorDriverDispatchHandler = Callable[
    [
        _DriverRouteEnvelopeT,
        _ResolvedProductDriverContext,
        object,
        Path,
    ],
    _DescriptorDriverDispatchResult,
]
_DescriptorDriverCustomDispatchHandler = Callable[
    [
        _DriverRouteEnvelopeT,
        _ResolvedProductDriverContext,
        object,
        Path,
        Path,
        str | None,
        LaunchplaneIdentity,
        str,
        str,
        str,
        _StartResponse,
        str,
    ],
    tuple[dict[str, object], BaseModel | dict[str, object] | None] | list[bytes],
]
_DescriptorDriverDispatchValidator = Callable[
    [
        _DriverRouteEnvelopeT,
        _ResolvedProductDriverContext,
        object,
        Path,
    ],
    None,
]
_DescriptorDriverPreAuthorizationValidator = Callable[
    [
        _DriverRouteEnvelopeT,
        _ResolvedProductDriverContext,
        LaunchplaneIdentity,
        _StartResponse,
        str,
    ],
    list[bytes] | None,
]


@dataclass(frozen=True)
class _DescriptorDriverDispatchRoute(Generic[_DriverRouteEnvelopeT]):
    execution_metadata: _DriverRouteExecutionMetadata[_DriverRouteEnvelopeT]
    context_resolver: _DescriptorDriverDispatchContextResolver[_DriverRouteEnvelopeT]
    handler: _DescriptorDriverDispatchHandler[_DriverRouteEnvelopeT] | None = None
    pre_idempotency_validator: _DescriptorDriverDispatchValidator[_DriverRouteEnvelopeT] | None = (
        None
    )
    pre_authorization_validator: (
        _DescriptorDriverPreAuthorizationValidator[_DriverRouteEnvelopeT] | None
    ) = None
    authorization_action_resolver: (
        _DescriptorDriverAuthorizationActionResolver[_DriverRouteEnvelopeT] | None
    ) = None
    custom_dispatch_handler: (
        _DescriptorDriverCustomDispatchHandler[_DriverRouteEnvelopeT] | None
    ) = None
    skip_pre_idempotency_check: bool = False
    skip_driver_context_resolution: bool = False
