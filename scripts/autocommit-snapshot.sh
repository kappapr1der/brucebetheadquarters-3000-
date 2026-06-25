#!/bin/sh
set -eu

APP_DIR="${BRUCEBET_APP_DIR:-/opt/brucebet-3000}"

if [ -f "$APP_DIR/.env" ] && [ "${BRUCEBET_SNAPSHOT_SOURCE_ENV:-0}" = "1" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$APP_DIR/.env"
  set +a
fi

SERVICE="${BRUCEBET_COMPOSE_SERVICE:-brucebet}"
SNAPSHOT_REPO="${BRUCEBET_SNAPSHOT_REPO:-$APP_DIR/data/snapshots}"
SNAPSHOT_CONTAINER_DIR="${BRUCEBET_SNAPSHOT_CONTAINER_DIR:-${BRUCEBET_SNAPSHOT_OUT_DIR:-/app/data/snapshots/current}}"
SNAPSHOT_LABEL="${BRUCEBET_SNAPSHOT_LABEL:-server-auto}"
SNAPSHOT_PUSH="${BRUCEBET_SNAPSHOT_PUSH:-auto}"
GIT_USER_NAME="${BRUCEBET_SNAPSHOT_GIT_USER_NAME:-BruceBet 3000}"
GIT_USER_EMAIL="${BRUCEBET_SNAPSHOT_GIT_USER_EMAIL:-brucebet@localhost}"
DB_PATH="${BRUCEBET_DB_PATH:-/app/data/forecasters.sqlite}"
COMMIT_MESSAGE="${BRUCEBET_SNAPSHOT_COMMIT_MESSAGE:-BruceBet snapshot $(date -u +%Y-%m-%dT%H:%M:%SZ)}"

mkdir -p "$SNAPSHOT_REPO"

if ! git -C "$SNAPSHOT_REPO" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  git -C "$SNAPSHOT_REPO" init -b main >/dev/null 2>&1 || {
    git -C "$SNAPSHOT_REPO" init >/dev/null
    git -C "$SNAPSHOT_REPO" checkout -B main >/dev/null
  }
fi

if [ ! -f "$SNAPSHOT_REPO/.gitignore" ]; then
  cat > "$SNAPSHOT_REPO/.gitignore" <<'EOF'
*.db
*.sqlite
*.sqlite-*
*.tmp
.env
EOF
fi

if [ ! -f "$SNAPSHOT_REPO/README.md" ]; then
  cat > "$SNAPSHOT_REPO/README.md" <<'EOF'
# BruceBet Runtime Snapshots

This repository stores sanitized CSV/JSON exports generated from the live BruceBet SQLite database.

It must not contain `.env`, Telegram tokens, API keys, live SQLite files, or Docker runtime files.
EOF
fi

cd "$APP_DIR"
docker compose exec -T "$SERVICE" \
  python -m brucebet.cli --db "$DB_PATH" snapshot \
  --out-dir "$SNAPSHOT_CONTAINER_DIR" \
  --label "$SNAPSHOT_LABEL"

git -C "$SNAPSHOT_REPO" add .gitignore README.md current

if git -C "$SNAPSHOT_REPO" diff --cached --quiet; then
  echo "No snapshot changes to commit."
  exit 0
fi

git -C "$SNAPSHOT_REPO" \
  -c user.name="$GIT_USER_NAME" \
  -c user.email="$GIT_USER_EMAIL" \
  commit -m "$COMMIT_MESSAGE"

do_push=0
case "$SNAPSHOT_PUSH" in
  1|true|yes)
    do_push=1
    ;;
  auto)
    if git -C "$SNAPSHOT_REPO" remote get-url origin >/dev/null 2>&1; then
      do_push=1
    fi
    ;;
esac

if [ "$do_push" = "1" ]; then
  git -C "$SNAPSHOT_REPO" push origin HEAD
else
  echo "Snapshot committed locally. Configure an origin remote or set BRUCEBET_SNAPSHOT_PUSH=1 to push."
fi
