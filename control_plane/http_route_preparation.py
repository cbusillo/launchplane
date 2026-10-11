"""Reuse route analysis while keeping every application's bindings independent."""

from collections import OrderedDict
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from threading import RLock
from types import FunctionType
from typing import Any, get_args, get_origin

from fastapi.datastructures import DefaultPlaceholder, _Unset
from fastapi.dependencies.utils import get_typed_signature, get_typed_return_annotation
from fastapi.params import Depends
from fastapi.routing import APIRoute, request_response
from fastapi.utils import generate_unique_id
from pydantic.fields import FieldInfo
from pydantic import TypeAdapter


class _UncacheableRoute(Exception):
    pass


@dataclass(frozen=True)
class _Binding:
    index: int


def _value_key(value: Any, bindings: list[Callable[..., Any]]) -> Any:
    if isinstance(value, Depends):
        if value.dependency is None:
            raise _UncacheableRoute
        return (
            type(value),
            _call_key(value.dependency, bindings),
            _value_key(vars(value) | {"dependency": None}, bindings),
        )
    if isinstance(value, FieldInfo):
        extra_attributes = vars(value) if hasattr(value, "__dict__") else {}
        return (
            type(value),
            _value_key(value.asdict(), bindings),
            _value_key(extra_attributes, bindings),
        )
    if isinstance(value, DefaultPlaceholder):
        return type(value), value is _Unset, _value_key(value.value, bindings)
    origin = get_origin(value)
    if origin is not None:
        return origin, tuple(_value_key(item, bindings) for item in get_args(value))
    if isinstance(value, dict):
        return type(value), tuple((key, _value_key(item, bindings)) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return type(value), tuple(_value_key(item, bindings) for item in value)
    if isinstance(value, (set, frozenset)):
        return type(value), frozenset(_value_key(item, bindings) for item in value)
    try:
        hash(value)
    except TypeError as error:
        raise _UncacheableRoute from error
    return type(value), value


def _call_key(call: Callable[..., Any], bindings: list[Callable[..., Any]]) -> Any:
    # Callable instances, partials and decorated signatures may carry extra
    # preparation semantics. Let FastAPI analyze those normally.
    if not isinstance(call, FunctionType) or hasattr(call, "__wrapped__"):
        raise _UncacheableRoute
    if any(bound is call for bound in bindings):
        return _Binding(next(index for index, bound in enumerate(bindings) if bound is call))
    bindings.append(call)
    signature = get_typed_signature(call)
    return (
        call.__code__,
        call.__name__,
        call.__doc__,
        tuple(
            (
                parameter.name,
                parameter.kind,
                _value_key(parameter.annotation, bindings),
                _value_key(parameter.default, bindings),
            )
            for parameter in signature.parameters.values()
        ),
        _value_key(get_typed_return_annotation(call), bindings),
    )


class PreparedAPIRoute(APIRoute):
    # Bounded, process-local preparation only: no app, store, policy, endpoint
    # closure, lifespan, middleware or override provider is retained here.
    _prepared: OrderedDict[Any, dict[str, Any]] = OrderedDict()
    _lock = RLock()

    def __init__(self, path: str, endpoint: Callable[..., Any], **kwargs: Any) -> None:
        bindings: list[Callable[..., Any]] = []
        preparation_kwargs = kwargs.copy()
        preparation_kwargs.pop("dependency_overrides_provider", None)
        try:
            if kwargs.get("callbacks"):
                raise _UncacheableRoute
            generator = kwargs.get("generate_unique_id_function")
            if isinstance(generator, DefaultPlaceholder):
                generator = generator.value
            if generator is not None and generator is not generate_unique_id:
                raise _UncacheableRoute
            key = (
                path,
                _call_key(endpoint, bindings),
                _value_key(preparation_kwargs, bindings),
            )
        except _UncacheableRoute:
            super().__init__(path, endpoint, **kwargs)
            return

        with self._lock:
            prepared = self._prepared.get(key)
            if prepared is None:
                super().__init__(path, endpoint, **kwargs)
                state = self.__dict__.copy()
                state.pop("app")
                state.pop("dependency_overrides_provider")
                memo: dict[int, Any] = {
                    id(call): _Binding(index) for index, call in enumerate(bindings)
                }
                memo.update(_schema_memo(state))
                prepared = deepcopy(state, memo)
                self._prepared[key] = prepared
                if len(self._prepared) > 512:
                    self._prepared.popitem(last=False)
                return
            self._prepared.move_to_end(key)
            # Each copy gets fresh metadata/dependency nodes and the current
            # endpoint functions. Compiled Pydantic validators contain schemas,
            # never the application-specific dependency callables.
            memo = {id(binding): bindings[binding.index] for binding in _bindings_in(prepared)}
            memo.update(_schema_memo(prepared))
            self.__dict__.update(deepcopy(prepared, memo))
        self.dependency_overrides_provider = kwargs.get("dependency_overrides_provider")
        self.app = request_response(self.get_route_handler())


def _bindings_in(value: Any) -> list[_Binding]:
    """Find placeholders in the template without traversing compiled validators."""
    from fastapi.dependencies.models import Dependant

    result: list[_Binding] = []
    if isinstance(value, _Binding):
        result.append(value)
    elif isinstance(value, dict):
        for item in value.values():
            result.extend(_bindings_in(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            result.extend(_bindings_in(item))
    elif isinstance(value, Depends):
        result.extend(_bindings_in(value.dependency))
    elif isinstance(value, Dependant):
        result.extend(_bindings_in(value.call))
        result.extend(_bindings_in(value.dependencies))
    return result


def _schema_memo(value: Any) -> dict[int, Any]:
    """Share compiled schemas, but copy the mutable route and field metadata."""
    from fastapi.dependencies.models import Dependant
    from fastapi._compat import ModelField

    result: dict[int, Any] = {id(_Unset): _Unset}
    if isinstance(value, ModelField):
        adapter = value._type_adapter
        if isinstance(adapter, TypeAdapter):
            result[id(adapter)] = adapter
    elif isinstance(value, dict):
        for item in value.values():
            result.update(_schema_memo(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            result.update(_schema_memo(item))
    elif isinstance(value, Dependant):
        for name in (
            "path_params",
            "query_params",
            "header_params",
            "cookie_params",
            "body_params",
            "dependencies",
        ):
            result.update(_schema_memo(getattr(value, name)))
    return result
