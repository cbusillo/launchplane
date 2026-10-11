#!/usr/bin/env bash
set -euo pipefail

# Candidate checks use public hosted capacity rather than the PR runner queue.
# Forks never reach a self-hosted lane, even if their branch looks like a train ref.
if [[ "${EVENT_NAME}" == pull_request && "${HEAD_REPOSITORY}" != "${REPOSITORY}" ]]; then
  runner='["ubuntu-latest"]'
elif [[ "${REPOSITORY_VISIBILITY}" == public ]] && \
  { [[ "${EVENT_NAME}" == push && "${REF}" == refs/heads/launchplane/train/* ]] || \
    [[ "${EVENT_NAME}" == pull_request && "${HEAD_REF}" == launchplane/train/* ]]; }; then
  runner='["ubuntu-latest"]'
else
  if [[ -z "${RUNNER_LABEL}" ]]; then
    echo 'The self-hosted CI runner label is not configured.' >&2
    exit 1
  fi
  runner="$(jq -cn --arg label "${RUNNER_LABEL}" '["self-hosted", $label]')"
fi
printf 'runner=%s\n' "${runner}" >> "${GITHUB_OUTPUT}"
