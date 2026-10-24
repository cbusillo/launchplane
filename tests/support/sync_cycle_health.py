"""Synthetic source-cycle fixture shared by monitor and HTTP behavior checks."""


def sync_cycle_payload(**updates: object) -> dict[str, object]:
    return {
        "status": "success",
        "mode": "incremental",
        "cycle_id": "b12bc47c132144cd94b90e72385d10dc",
        "cycle_started_at": "2026-05-29T12:00:00Z",
        "cycle_finished_at": "2026-05-29T12:04:00Z",
        # The producer records this with a separate clock read after finishing.
        "last_heartbeat": "2026-05-29T12:04:00.000001Z",
        "is_stale": False,
        "next_cycle_overdue": False,
        "freshness_status": "fresh",
        "stale_threshold_seconds": 900,
        **updates,
    }
