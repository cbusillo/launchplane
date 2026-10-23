#!/usr/bin/env bash
set -euo pipefail

checkout="$(mktemp -d "${TMPDIR:-/tmp}/launchplane-openapi-checkout.XXXXXX")"
trap 'rm -rf "$checkout"' EXIT

git archive HEAD | tar -x -C "$checkout" \
  --exclude=frontend/generated/openapi-canonical.json \
  --exclude=frontend/generated/openapi-ui.json
cd "$checkout"
uv run --extra dev python -m unittest tests.test_docs_contracts tests.test_privileged_operation tests.test_retirement_inventory_client
pnpm --dir frontend install --frozen-lockfile
pnpm --dir frontend validate
