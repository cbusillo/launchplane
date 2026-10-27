from typing import Literal

from pydantic import BaseModel, ConfigDict


ReadSourceCoverage = Literal["complete", "limited", "unsupported", "unknown"]
ReadCollection = Literal["product_profiles", "secret_records", "secret_bindings"]


class ReadCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    population: str
    status: Literal["complete", "scoped", "limited", "unsupported", "unknown"] = "unknown"
    source: ReadSourceCoverage = "unknown"
    authorization: Literal["complete", "scoped", "unknown"] = "unknown"
    exclusions: tuple[str, ...] = ()


def read_coverage(
    store: object,
    *,
    collections: tuple[ReadCollection, ...],
    population: str,
    authorization_complete: bool,
    exclusions: tuple[str, ...] = (),
) -> ReadCoverage:
    capability = getattr(store, "collection_read_coverage", None)
    sources = (
        tuple(capability(collection) for collection in collections) if callable(capability) else ()
    )
    if not sources or any(
        item not in ("unsupported", "limited", "unknown", "complete") for item in sources
    ):
        sources = ("unknown",)
    source: ReadSourceCoverage = "unknown"
    for candidate in ("unsupported", "limited", "unknown", "complete"):
        if candidate in sources:
            source = candidate
            break
    authorization: Literal["complete", "scoped"] = (
        "complete" if authorization_complete else "scoped"
    )
    return ReadCoverage(
        population=population,
        source=source,
        authorization=authorization,
        status=source if source != "complete" else authorization,
        exclusions=exclusions,
    )
