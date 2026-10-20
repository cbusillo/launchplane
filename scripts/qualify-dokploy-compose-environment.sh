#!/usr/bin/env bash
# setup-ci-compose.sh and CI's setup-node provision the consumer versions.
set -euo pipefail
export LAUNCHPLANE_TEST_COMPOSE_COMMAND='["docker", "compose"]'
export LAUNCHPLANE_TEST_NODE_COMMAND='["node"]'
uv run --extra dev python -m unittest tests.test_dokploy_compose_environment \
  tests.test_testing_candidate_application.CandidateApplicationTests.test_candidate_environment_reaches_final_consumer -v
