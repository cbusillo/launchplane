#!/usr/bin/env bash
set -euo pipefail

# setup-compose-action stages its binary through a shared per-user cache.
# Sibling runners can truncate that file while another job copies its plugin.
# Read the runner-local tool cache directly and publish only to this job.
version="${DOCKER_COMPOSE_VERSION:?DOCKER_COMPOSE_VERSION is required}"
case "$(uname -m)" in
  x86_64) architecture=x86_64; cache_architecture=x64 ;;
  aarch64|arm64) architecture=aarch64; cache_architecture=arm64 ;;
  *) echo "Unsupported Compose runner architecture" >&2; exit 1 ;;
esac
platform="$(uname -s | tr '[:upper:]' '[:lower:]')"
config_directory="${DOCKER_CONFIG:-${HOME}/.docker}"
plugin_directory="${config_directory}/cli-plugins"
mkdir -p "${plugin_directory}"
staged_binary="$(mktemp "${plugin_directory}/compose-install.XXXXXXXX")"
trap 'rm -f "${staged_binary}"' EXIT
cached_binary="${RUNNER_TOOL_CACHE:-${RUNNER_TEMP:?}}/compose-dl-bin/${version#v}/${platform}-${cache_architecture}/docker-compose"
if [ -s "${cached_binary}" ]; then
  cp "${cached_binary}" "${staged_binary}"
  chmod 0755 "${staged_binary}"
fi
# Older action runs can also have cached a truncated shared staging file.
if ! "${staged_binary}" docker-cli-plugin-metadata 2>/dev/null |
  jq --exit-status --arg version "${version}" '.Version == $version' >/dev/null; then
  curl --fail --silent --show-error --location \
    "https://github.com/docker/compose/releases/download/${version}/docker-compose-${platform}-${architecture}" \
    --output "${staged_binary}"
fi
chmod 0755 "${staged_binary}"
"${staged_binary}" docker-cli-plugin-metadata |
  jq --exit-status --arg version "${version}" '.Version == $version' >/dev/null
mv "${staged_binary}" "${plugin_directory}/docker-compose"
docker compose version
