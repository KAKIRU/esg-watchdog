#!/usr/bin/env bash
# 로컬 compose Postgres 로 마이그레이션 왕복 검증 (D-36).
#   docker compose up -d → alembic upgrade head → downgrade -1 → upgrade head → alembic current → psql \dt
# 실패하면 그 자리에서 종료한다. 레포 루트의 .env 를 읽는다 (값 뒤 인라인 주석 금지).
set -euo pipefail

cd "$(dirname "$0")/../.."

if [[ ! -f .env ]]; then
  echo "ERROR: .env 가 없다. .env.example 을 복사해 로컬 값을 채워라." >&2
  exit 1
fi

set -a
# shellcheck disable=SC1091
source .env
set +a

: "${POSTGRES_DB:?POSTGRES_DB 가 .env 에 없다}"
: "${POSTGRES_USER:?POSTGRES_USER 가 .env 에 없다}"

step() { printf '\n==> %s\n' "$*"; }

step "docker compose up -d"
docker compose up -d

step "pg_isready 대기"
for _ in $(seq 1 30); do
  if docker compose exec -T postgres pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
docker compose exec -T postgres pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"

step "alembic upgrade head"
uv run alembic upgrade head

step "alembic downgrade -1"
uv run alembic downgrade -1

step "alembic upgrade head (재적용)"
uv run alembic upgrade head

step "alembic current"
uv run alembic current

step "psql \\dt (alembic_version 포함 13개여야 한다)"
docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c '\dt'

echo
echo "OK: 마이그레이션 왕복 검증 통과"
