#!/bin/sh
# Pull the Flighty database snapshot from the Mac. Runs ON BLINKY, on a timer.
#
# This replaces scripts/export-db.sh, which pushed from the Mac under launchd
# and FAILED ON EVERY RUN:
#
#   Error: unable to open database "file:/Users/carey/Library/Containers/
#   com.flightyapp.flighty/Data/Documents/MainFlightyDatabase.db?mode=ro":
#   authorization denied
#
# macOS protects another app's container directory, and a launchd agent has no
# Full Disk Access grant to get past it. An interactive shell does, which is
# why the export worked by hand and then never again -- the snapshot on blinky
# sat two days stale with nothing saying so.
#
# An *ssh session* also gets past it, verified on this pair: Remote Login is on,
# blinky's key is in the Mac's authorized_keys, and a bare
# `ssh mac sqlite3 ...` reads the live database and counts 2,400 airlines. So
# the fix is to invert the direction. Pulling has two other advantages worth
# having: the schedule lives next to the container it feeds rather than on a
# machine that might be asleep, and a failure lands in blinky's journal where
# the alert unit can see it.
#
# Everything here is idempotent and safe to run by hand.
set -eu

MAC="${FLIGHTY_MAC:-carey@10.10.10.200}"
DATA_DIR="${FLIGHTY_DATA_DIR:-$HOME/docker/flighty-mcp/data}"
DEST="$DATA_DIR/MainFlightyDatabase.db"
CONTAINER="${FLIGHTY_CONTAINER:-flighty-mcp}"

# The Mac is wired at a fixed 10.10.10.200 (en0, Ethernet). Its Wi-Fi address is
# a DHCP lease on the same subnet and is NOT reachable from here, so the address
# above is load-bearing rather than a convenience.

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
NEW="$TMP/new.db"

log() { echo "$*"; }
die() { echo "$*" >&2; exit 1; }

# --- 1. snapshot on the Mac, and strip the credential there ----------------
#
# `.backup` rather than `cp`: the app is running, and its -wal holds committed
# data a plain copy would miss or tear.
#
# The Account row is deleted ON THE MAC, before the file crosses the network.
# It holds `authToken`, a permanent account bearer with no `exp` whose only
# revocation is a password change that signs out every device. The container
# gets its own copy from FLIGHTY_AUTH_TOKEN in its env file, so shipping a
# second one inside the database would widen where that credential lives for
# no gain.
log "pulling from $MAC"
ssh -T -o BatchMode=yes -o ConnectTimeout=15 "$MAC" /bin/sh >"$NEW" <<'REMOTE'
set -eu
SRC="$HOME/Library/Containers/com.flightyapp.flighty/Data/Documents/MainFlightyDatabase.db"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
OUT="$TMP/db"
sqlite3 "file:$SRC?mode=ro" ".backup '$OUT'"
sqlite3 "$OUT" 'DELETE FROM Account; VACUUM;'
cat "$OUT"
REMOTE

[ -s "$NEW" ] || die "the pull produced an empty file"

# --- 2. prove it before trusting it ---------------------------------------
#
# The failure this guards against is the quiet one: a partial transfer, or a
# backup taken while the app was mid-write, installed over a snapshot that was
# fine. Everything below is checked BEFORE anything is replaced, so a bad pull
# leaves the previous snapshot serving.
count() { sqlite3 "$NEW" "SELECT count(*) FROM $1;" 2>/dev/null || echo -1; }

integrity="$(sqlite3 "$NEW" 'PRAGMA integrity_check;' 2>&1 || true)"
[ "$integrity" = "ok" ] || die "integrity_check says: $integrity"

# The reference tables are effectively static -- 13,833 airports and 2,400
# airlines as of 2026-10 -- so a floor well under those catches a truncated
# file without needing maintenance when Flighty adds an airport.
airports="$(count Airport)"
airlines="$(count Airline)"
flights="$(count Flight)"
accounts="$(count Account)"

[ "$airports" -ge 12000 ] || die "only $airports airports; expected ~13833"
[ "$airlines" -ge 2000 ] || die "only $airlines airlines; expected ~2400"
[ "$flights" -ge 1 ] || die "no flights at all; expected the account's history"

# Not a sanity check but a security one: if the DELETE above ever silently
# stops working, this must not ship the account bearer to the container.
[ "$accounts" -eq 0 ] || die "$accounts Account row(s) survived; refusing to \
install a snapshot containing the auth token"

if [ -f "$DEST" ]; then
  was="$(sqlite3 "$DEST" 'SELECT count(*) FROM Flight;' 2>/dev/null || echo -1)"
  if [ "$was" -gt 0 ] && [ "$flights" -lt "$was" ]; then
    # Flights can legitimately disappear (archived, deleted), so this is said
    # rather than refused -- but said loudly, because the other way it happens
    # is a half-synced app.
    log "NOTE: flight count fell from $was to $flights"
  fi
fi
log "ok: $flights flights, $airports airports, $airlines airlines, integrity ok"

# --- 3. install, and only restart if something changed --------------------
#
# db.py opens the file `mode=ro&immutable=1`, which tells SQLite it will never
# change and licenses it to cache accordingly -- so swapping the file under a
# running container is not enough, and the restart is not optional. It is a
# two-second gap and the gateway tolerates a backend being briefly absent.
mkdir -p "$DATA_DIR"
if [ -f "$DEST" ] && cmp -s "$NEW" "$DEST"; then
  log "unchanged; leaving $CONTAINER alone"
  exit 0
fi

# Into the destination directory first, then renamed: `mv` within one
# filesystem is atomic, so there is no moment where the container could be
# started against a half-written file.
install -m 600 "$NEW" "$DEST.incoming"
mv -f "$DEST.incoming" "$DEST"
log "installed $DEST"

docker restart "$CONTAINER" >/dev/null
# Confirm it came back, rather than reporting success for a container that is
# now crash-looping on the file just installed.
sleep 3
state="$(docker inspect -f '{{.State.Status}}' "$CONTAINER" 2>/dev/null || echo missing)"
[ "$state" = "running" ] || die "$CONTAINER is '$state' after the restart"
log "$CONTAINER restarted and running"
