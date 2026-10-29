from control_plane.storage.postgres import LaunchplaneProductProfileRow, PostgresRecordStore


def persist_historical_preview_transport(
    store: PostgresRecordStore, product: str, **transport: str
) -> None:
    with store._session_factory() as session:
        row = session.get(LaunchplaneProductProfileRow, product)
        assert row is not None
        row.payload = {
            **row.payload,
            "preview": {**row.payload["preview"], **transport},
        }
        session.commit()
