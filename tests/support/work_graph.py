def _work_graph_snapshot_payload() -> dict[str, object]:
    return {
        "generated_at": "2026-05-06T01:45:00Z",
        "repos": [
            {
                "repository": "cbusillo/launchplane",
                "classification": "managed_runtime",
                "product": "launchplane",
                "display_name": "Launchplane",
            }
        ],
        "issues": [
            {
                "repository": "cbusillo/launchplane",
                "number": 190,
                "title": "Build operator work graph",
                "url": "https://github.com/cbusillo/launchplane/issues/190",
                "focus": "Now",
                "manager": "Code",
                "finish_line": "Ranked work queue is available to the operator UI.",
                "labels": ["plan", "plan:active"],
                "blocking": 2,
                "subissues_total": 2,
                "subissues_completed": 1,
                "check_state": "success",
                "deploy_state": "success",
            },
            {
                "repository": "cbusillo/launchplane",
                "number": 164,
                "title": "Absorb product orchestration",
                "url": "https://github.com/cbusillo/launchplane/issues/164",
                "state": "closed",
                "focus": "Done",
            },
        ],
    }
