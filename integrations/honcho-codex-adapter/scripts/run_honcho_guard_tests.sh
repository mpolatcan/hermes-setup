#!/usr/bin/env bash
set -euo pipefail

HONCHO_ROOT="${HONCHO_ROOT:-/Users/mutlupolatcan/honcho-stack/server}"
POSTGRES_IMAGE="${POSTGRES_IMAGE:-pgvector/pgvector:pg15}"
TEST_FILE="tests/dreamer/test_dreamer_integration.py"

if [[ ! -d "$HONCHO_ROOT" ]]; then
    printf 'Honcho checkout not found: %s\n' "$HONCHO_ROOT" >&2
    exit 2
fi

for command in docker python3; do
    if ! command -v "$command" >/dev/null 2>&1; then
        printf 'Required command not found: %s\n' "$command" >&2
        exit 2
    fi
done

api_id="$({
    cd "$HONCHO_ROOT"
    docker compose ps -q api 2>/dev/null || true
} | python3 -c '
import sys
ids = [line.strip() for line in sys.stdin if line.strip()]
if len(ids) != 1:
    raise SystemExit(f"expected one running Honcho API container, found {len(ids)}")
print(ids[0])
')"
api_image="$(docker inspect --format '{{.Image}}' "$api_id")"

suffix="$$-$RANDOM"
network="honcho-guard-test-$suffix"
database="honcho-guard-db-$suffix"

cleanup() {
    docker rm -f "$database" >/dev/null 2>&1 || true
    docker network rm "$network" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

docker network create "$network" >/dev/null
docker run -d --rm \
    --name "$database" \
    --network "$network" \
    -e POSTGRES_USER=honcho \
    -e POSTGRES_HOST_AUTH_METHOD=trust \
    -e POSTGRES_DB=honcho \
    "$POSTGRES_IMAGE" >/dev/null

ready=false
for _ in $(seq 1 30); do
    if docker exec "$database" pg_isready -U honcho -d honcho >/dev/null 2>&1; then
        ready=true
        break
    fi
    sleep 1
done

if [[ "$ready" != true ]]; then
    printf 'Temporary pgvector database did not become ready\n' >&2
    exit 3
fi

database_uri="postgresql+psycopg://honcho@${database}:5432/honcho"

docker run --rm \
    --network "$network" \
    -e DB_CONNECTION_URI="$database_uri" \
    -v "$HONCHO_ROOT:/workspace:ro" \
    --entrypoint sh \
    "$api_image" -lc '
        set -eu
        cp -a /app/.venv /tmp/honcho-test-venv
        uv pip install --quiet --python /tmp/honcho-test-venv/bin/python \
            pytest pytest-asyncio pytest-xdist sqlalchemy-utils fakeredis
        cd /workspace
        PYTHONPATH=/workspace/src \
            /tmp/honcho-test-venv/bin/python -m pytest \
            tests/dreamer/test_dreamer_integration.py \
            -q -n 0 -p no:cacheprovider
    '

printf 'Honcho guard integration tests passed; isolated database removed.\n'
