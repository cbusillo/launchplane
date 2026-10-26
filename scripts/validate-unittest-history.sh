#!/usr/bin/env bash
set -euo pipefail
jq -e '
  .schema_version == 1 and .record_type == "unittest_module_timings" and
  (.modules | type == "object" and length > 0 and
    all(.[]; type == "object" and (.seconds | type == "number" and . >= 0 and . < 1e100)))
' "$1" >/dev/null
