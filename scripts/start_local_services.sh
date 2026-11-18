#!/usr/bin/env bash
# 本机（无 systemd、无 Docker）拉起 Postgres 17 与 Redis。
set -euo pipefail
REDIS_DIR="${HOME}/services/redis"
mkdir -p "${REDIS_DIR}"

if pg_ctlcluster 17 main status >/dev/null 2>&1; then
  echo "postgres 17/main already running"
else
  sudo pg_ctlcluster 17 main start
  echo "postgres 17/main started"
fi

if redis-cli -h 127.0.0.1 -p 6379 ping >/dev/null 2>&1; then
  echo "redis already running"
else
  redis-server --daemonize yes --bind 127.0.0.1 --port 6379 --protected-mode yes     --dir "${REDIS_DIR}" --logfile "${REDIS_DIR}/redis.log"     --pidfile "${REDIS_DIR}/redis.pid" --dbfilename dump.rdb
  echo "redis started on 127.0.0.1:6379"
fi

psql -d policy_qa -c "SELECT current_database(), current_user;"
redis-cli ping
