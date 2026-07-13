import json
from pathlib import Path
import stat


def write_fake_gh_sequence(directory: Path, *, responses: list[dict[str, object]]) -> Path:
    script = directory / "gh"
    responses_path = directory / "responses.jsonl"
    responses_path.write_text("\n".join(json.dumps(response) for response in responses) + "\n")
    script.write_text(
        "#!/bin/sh\n"
        'printf \'%s\\n\' "$@" >> "$FAKE_GH_ARGS"\n'
        f"responses_path={json.dumps(str(responses_path))}\n"
        'if [ -s "$responses_path" ]; then\n'
        "  response=$(sed -n '1p' \"$responses_path\")\n"
        '  tail -n +2 "$responses_path" > "$responses_path.tmp"\n'
        '  mv "$responses_path.tmp" "$responses_path"\n'
        "else\n"
        '  response=\'{"stdout":[],"exit_code":0,"stderr":""}\'\n'
        "fi\n"
        "printf '%s' \"$response\" | jq -c '.stdout'\n"
        "stderr=$(printf '%s' \"$response\" | jq -r '.stderr // \"\"')\n"
        'if [ -n "$stderr" ]; then printf \'%s\' "$stderr" >&2; fi\n'
        "exit_code=$(printf '%s' \"$response\" | jq -r '.exit_code // 0')\n"
        'exit "$exit_code"\n'
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


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
