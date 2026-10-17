"""Custody of a testing candidate, independent of the lane serving its address."""

from datetime import datetime
from typing import Literal, Self

from pydantic import Field, model_validator

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.prepared_public_site import FrozenRecord
from control_plane.contracts.testing_candidate_application import TestingCandidateApplication


class TestingCandidateAllocation(FrozenRecord):
    namespace: str = Field(pattern=r"^lp-testing-[a-f0-9]{32}$")
    product: str = Field(min_length=1)
    context: str = Field(min_length=1)
    operation_id: str = Field(min_length=1)
    intent_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    serving_target_id: str = Field(min_length=1)
    server_id: str = Field(min_length=1)
    project_id: str = ""
    environment_id: str = ""
    compose_id: str = ""
    effect: Literal["", "project_id", "environment_id", "compose_id", "storage"] = ""
    resources_binding_sha256: str = Field(default="", pattern=r"^(?:[a-f0-9]{64})?$")
    created_at: datetime
    application: TestingCandidateApplication | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def validate_custody(self) -> Self:
        if self.created_at.utcoffset() is None:
            raise ValueError("Candidate custody requires an aware creation time.")
        if self.namespace != testing_candidate_namespace(
            self.product, self.context, self.operation_id
        ):
            raise ValueError("Candidate namespace must belong to its original operation.")
        if (
            self.environment_id
            and not self.project_id
            or self.compose_id
            and not self.environment_id
        ):
            raise ValueError("Candidate placement requires its recorded parents.")
        if self.compose_id == self.serving_target_id:
            raise ValueError("Candidate allocation cannot adopt the serving target.")
        required_parent = {
            "environment_id": self.project_id,
            "compose_id": self.environment_id,
            "storage": self.compose_id,
        }
        if self.effect in required_parent and not required_parent[self.effect]:
            raise ValueError("Candidate effect requires its recorded parent custody.")
        if self.resources_binding_sha256 and not self.compose_id:
            raise ValueError("Candidate storage requires its recorded target.")
        if self.application is not None:
            plan = self.application.plan
            if not self.resources_binding_sha256 or (
                plan.product,
                plan.context,
                plan.operation_id,
                plan.namespace,
                plan.target_id,
            ) != (self.product, self.context, self.operation_id, self.namespace, self.compose_id):
                raise ValueError(
                    "Candidate application requires this allocation's qualified storage."
                )
            if (
                plan.generation is not None
                and plan.generation.resources_binding_sha256 != self.resources_binding_sha256
            ):
                raise ValueError("Copy generation differs from this candidate's storage receipt.")
        return self

    def require_successor(self, previous: Self) -> None:
        mutable = {
            "project_id",
            "environment_id",
            "compose_id",
            "effect",
            "resources_binding_sha256",
            "application",
        }
        if self.model_dump(exclude=mutable) != previous.model_dump(exclude=mutable):
            raise ValueError("Candidate allocation cannot replace its original lane or intent.")
        for field in mutable - {"effect", "application"}:
            if getattr(previous, field) and getattr(self, field) != getattr(previous, field):
                raise ValueError("Recorded candidate custody is immutable.")
        if previous.application is not None:
            if self.application is None:
                raise ValueError("Candidate continuation cannot discard its application intent.")
            self.application.require_successor(previous.application)
        phases = ("", "project_id", "environment_id", "compose_id", "storage")
        if phases.index(self.effect) < phases.index(previous.effect):
            raise ValueError("Candidate cannot forget a dispatched effect.")


def testing_candidate_namespace(product: str, context: str, operation_id: str) -> str:
    if not all(value.strip() for value in (product, context, operation_id)):
        raise ValueError("Candidate identity requires its original durable operation.")
    return (
        "lp-testing-"
        + canonical_json_sha256(
            {"product": product, "context": context, "operation": operation_id}
        )[:32]
    )
