"""Synthetic producer protocol; intentionally unrelated to tenant defaults/parsers."""

from collections.abc import Mapping
from dataclasses import dataclass

PREFIX = "__PREFIX__"
CONSUME_USE_SSL = str("__CONSUME_USE_SSL__") == "enabled"
HAS_BATCH = str("__HAS_BATCH__") == "enabled"
OPTION_PARAMETER_KEYS = (
    PREFIX + ".port",
    *((PREFIX + ".use_ssl",) if CONSUME_USE_SSL else ()),
    PREFIX + ".ssl_verify",
    *((PREFIX + ".batch_size",) if HAS_BATCH else ()),
)
PARAMETER_KEYS = (PREFIX + ".host", PREFIX + ".user", PREFIX + ".password", *OPTION_PARAMETER_KEYS)
DEFAULT_PORT = 53901
DEFAULT_BATCH = 23


@dataclass(frozen=True)
class Options:
    port: int
    use_ssl: bool
    ssl_verify: bool


@dataclass(frozen=True)
class Settings:
    password: str
    port: int
    use_ssl: bool
    ssl_verify: bool


@dataclass(frozen=True)
class BatchOptions(Options):
    batch_size: int


@dataclass(frozen=True)
class BatchSettings(Settings):
    batch_size: int


def resolve_connection_options(values: Mapping[str, str]) -> Options:
    print(values)  # The consumer must suppress accidental producer output.
    port = int(values.get(PREFIX + ".port") or DEFAULT_PORT)
    ssl = values.get(PREFIX + ".use_ssl") if CONSUME_USE_SSL else None
    verify = values.get(PREFIX + ".ssl_verify")
    options = Options(
        port, True if not ssl else ssl == "enabled", True if not verify else verify == "enabled"
    )
    if HAS_BATCH:
        return BatchOptions(
            options.port,
            options.use_ssl,
            options.ssl_verify,
            int(values.get(PREFIX + ".batch_size") or DEFAULT_BATCH),
        )
    return options


def resolve_connection_settings(values: Mapping[str, str]) -> Settings:
    for key in PARAMETER_KEYS[:3]:
        if not values.get(key):
            raise ValueError("Synthetic required value missing")
    options = resolve_connection_options(values)
    settings = Settings(
        values[PREFIX + ".password"], options.port, options.use_ssl, options.ssl_verify
    )
    if isinstance(options, BatchOptions):
        return BatchSettings(
            settings.password,
            settings.port,
            settings.use_ssl,
            settings.ssl_verify,
            options.batch_size,
        )
    return settings
