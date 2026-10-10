"""Cutover/pause checkpoints use the existing DB-backed deployment payload."""

from typing import Protocol

from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.odoo_cutover import OdooCutoverRecord
from control_plane.contracts.release_tuple_record import ReleaseTupleRecord


class DeploymentStore(Protocol):
    def write_release_tuple_record(self, record: ReleaseTupleRecord) -> object: ...
    def read_deployment_record(self, record_id: str) -> DeploymentRecord: ...
    def write_deployment_record(self, record: DeploymentRecord) -> object: ...


class DeploymentCutoverStore:
    def __init__(self, store: DeploymentStore, deployment: DeploymentRecord) -> None:
        self.store, self.deployment = store, deployment

    def load(self, record_id: str) -> OdooCutoverRecord:
        deployment = self.store.read_deployment_record(self.deployment.record_id)
        record = deployment.odoo_cutover
        if record is None:
            raise FileNotFoundError(record_id)
        if record.record_id != record_id:
            raise ValueError("deployment already belongs to another cutover")
        return record

    def save(self, record: OdooCutoverRecord) -> None:
        try:
            deployment = self.store.read_deployment_record(self.deployment.record_id)
        except FileNotFoundError:
            deployment = self.deployment
        if deployment.odoo_cutover is not None and (
            deployment.odoo_cutover.record_id != record.record_id
            or deployment.odoo_cutover.plan != record.plan
        ):
            raise ValueError("cannot replace an existing cutover binding")
        self.store.write_deployment_record(deployment.model_copy(update={"odoo_cutover": record}))
