#!/bin/sh
# Container entrypoint. Runs as root just long enough to make the SQLite directory
# writable by the unprivileged `app` user (Fly.io mounts volumes root-owned), then drops
# privileges and starts uvicorn. If the container is already running unprivileged the
# chown step is skipped and uvicorn starts directly.
set -eu

url="${DATABASE_URL:-sqlite:////data/app.db}"
dir="/data"
case "$url" in
  sqlite:///*)
    path="${url#sqlite:///}"   # sqlite:////data/app.db -> /data/app.db ; sqlite:///./x.db -> ./x.db
    case "$path" in
      /*) dir="$(dirname "$path")" ;;
      *)  dir="$(dirname "/app/$path")" ;;
    esac
    ;;
esac

port="${PORT:-8080}"

# Make the resolved database location obvious in `fly logs` / `docker logs`: a ledger
# written to the container layer instead of the mounted volume is otherwise silent.
echo "edge-finder: database=${url} dir=${dir} port=${port}" >&2

if [ "$(id -u)" = "0" ]; then
  mkdir -p "$dir"
  chown -R app:app "$dir"
  # setpriv comes from util-linux, which Debian ships in the slim image; su is the
  # fallback so a base-image change cannot turn "drop privileges" into "fail to boot".
  if command -v setpriv >/dev/null 2>&1; then
    exec setpriv --reuid=app --regid=app --init-groups \
      uvicorn app.main:app --host 0.0.0.0 --port "$port"
  fi
  echo "edge-finder: setpriv not found, dropping privileges with su" >&2
  exec su app -s /bin/sh -c "exec uvicorn app.main:app --host 0.0.0.0 --port $port"
fi

mkdir -p "$dir" 2>/dev/null || true
exec uvicorn app.main:app --host 0.0.0.0 --port "$port"
