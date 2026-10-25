#!/bin/sh
# Snapshot the live Flighty database for the containerised read-only server.
#
# The container needs the reference tables (~2,400 airlines, ~13,800 airports)
# and the account's flight list. It does NOT need the Account row — the bearer
# comes from FLIGHTY_AUTH_TOKEN — and that row is the one secret in the file, so
# it is dropped on the way out rather than copied to a second host.
#
# sqlite3 .backup is safe against the running app. A plain cp is not: the app
# keeps a -wal alongside the database and copying one without the other yields a
# torn snapshot.
set -eu

SRC="${FLIGHTY_DB_PATH:-$HOME/Library/Containers/com.flightyapp.flighty/Data/Documents/MainFlightyDatabase.db}"
REMOTE="${FLIGHTY_REMOTE:-blinky}"
REMOTE_DIR="${FLIGHTY_REMOTE_DIR:-docker/flighty-mcp/data}"

[ -f "$SRC" ] || { echo "no Flighty database at $SRC" >&2; exit 1; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
OUT="$TMP/MainFlightyDatabase.db"

sqlite3 "file:$SRC?mode=ro" ".backup '$OUT'"
sqlite3 "$OUT" 'DELETE FROM Account; VACUUM;'

# db.py opens the file with immutable=1, which tells SQLite it will never
# change, so replacing it under a running container is not safe — hence the
# restart. The gap is a couple of seconds and the gateway tolerates a backend
# being briefly absent: only the flighty_* tools disappear.
scp -q "$OUT" "$REMOTE:/tmp/flighty-db-new.db"
ssh "$REMOTE" "
  set -eu
  mkdir -p '$REMOTE_DIR'
  install -m 600 /tmp/flighty-db-new.db '$REMOTE_DIR/MainFlightyDatabase.db'
  rm -f /tmp/flighty-db-new.db
  docker restart flighty-mcp >/dev/null 2>&1 || true
"

echo "exported $(du -h "$OUT" | cut -f1) to $REMOTE:$REMOTE_DIR"
