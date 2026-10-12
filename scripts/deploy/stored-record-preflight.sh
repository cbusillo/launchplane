#!/usr/bin/env bash
# Inputs are an immutable candidate and a scrubbed HTTP response, never live DB
# wiring or provider credentials. Nothing is inherited into either container.
set -euo pipefail

image_reference="${1:?candidate image required}"
snapshot_file="${2:?scrubbed service response required}"
postgres_image="${3:?isolated PostgreSQL image required}"
if [[ ! "$image_reference" =~ @sha256:[0-9a-f]{64}$ ]] || [[ ! "$postgres_image" =~ @sha256:[0-9a-f]{64}$ ]]; then
  echo "Preflight requires immutable candidate and PostgreSQL images." >&2
  exit 1
fi
if [[ ! -f "$snapshot_file" ]]; then
  echo "Scrubbed snapshot response is missing." >&2
  exit 1
fi
snapshot_file="$(realpath "$snapshot_file")"
project="launchplane-snapshot-$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')"
network="$project"
database="$project-db"
candidate="$project-candidate"
cleanup() {
  docker rm -fv "$candidate" "$database" >/dev/null 2>&1 || true
  docker network rm "$network" >/dev/null 2>&1 || true
}

# Pulls happen outside the rehearsal network. Startup/migrations/record loads
# have only a private DB peer; no host socket, published ports or live volumes.
docker pull "$image_reference" >/dev/null
docker pull "$postgres_image" >/dev/null
docker network create --internal "$network" >/dev/null
trap cleanup EXIT
trap 'exit 124' TERM INT
docker run -d --name "$database" --network "$network" --network-alias snapshot-db \
  --memory 1g --cpus 2 --pids-limit 256 \
  --tmpfs /var/lib/postgresql/data:rw,noexec,nosuid,size=768m,mode=0700 \
  -e POSTGRES_USER=snapshot -e POSTGRES_DB=snapshot -e POSTGRES_PASSWORD=rehearsal-only \
  "$postgres_image" >/dev/null
ready=false
for ((attempt=0; attempt<30; attempt++)); do
  if docker exec "$database" pg_isready -h 127.0.0.1 -U snapshot -d snapshot >/dev/null 2>&1; then
    ready=true
    break
  fi
  sleep 1
done
if [[ "$ready" != true ]]; then
  echo "Isolated preflight database did not start." >&2
  exit 1
fi
docker run --rm --name "$candidate" --network "$network" \
  --read-only --cap-drop ALL --security-opt no-new-privileges \
  --memory 2g --cpus 2 --pids-limit 256 --tmpfs /tmp:rw,noexec,nosuid,size=256m \
  --mount "type=bind,source=$snapshot_file,target=/snapshot.json,readonly" \
  -e LAUNCHPLANE_PREFLIGHT_DATABASE_URL=postgresql+psycopg://snapshot:rehearsal-only@snapshot-db/snapshot \
  --entrypoint /app/.venv/bin/python "$image_reference" \
  -m control_plane.stored_record_preflight --snapshot /snapshot.json
