#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from control_plane.merge_train_controller_feedback import build_feedback_payloads  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Render merge-train controller PR feedback payloads."
    )
    parser.add_argument("--response-file", required=True, type=Path)
    parser.add_argument("--source", default="workflow:merge-train-runner")
    parser.add_argument(
        "--phase",
        choices=("controller", "batch-candidate", "stack-collapse", "batch-landing"),
        default="controller",
    )
    args = parser.parse_args()

    response = json.loads(args.response_file.read_text(encoding="utf-8"))
    payloads = build_feedback_payloads(response=response, source=args.source, phase=args.phase)
    json.dump(payloads, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    print(f"Rendered {len(payloads)} feedback payload(s).", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
