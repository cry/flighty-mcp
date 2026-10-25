# flighty-mcp

An MCP server for managing flights in your [Flighty](https://flighty.com)
account. Runs locally on macOS alongside the Flighty app.

## Tools

- `search_flights(airline, flight_number, date)` — find a flight's legs.
  `airline` is an ICAO, IATA, or name; `date` is the local departure date
  `YYYY-MM-DD`. Returns a `flight_id` per leg.
- `add_flight(flight_id, is_passenger=true, pnr?, seat_number?, seat_position?, cabin_class?)`
  — add a flight, optionally with booking info.
- `set_ticket(flight_id, pnr?, seat_number?, seat_position?, cabin_class?)` —
  set booking info on a flight already added. `seat_position`:
  `window|middle|aisle`. `cabin_class`: `economy|premiumEconomy|business|first`.
- `set_tickets(tickets, mode="sequential")` — set booking info on many flights
  at once. `mode="batch"` sends one request; on failure, retry with
  `mode="sequential"`.
- `remove_flight(flight_id)` — remove a flight. Reversible via `add_flight`.
- `list_my_flights(include_archived=false)` — list flights already in the
  account.

Add/ticket/remove write to your live account.

## Setup

Requires [`uv`](https://docs.astral.sh/uv/) and the Flighty Mac app, signed in.

```sh
cd flighty-mcp
uv sync
```

Credentials are read from the local Flighty install at runtime; nothing to copy.

### Claude Code

```sh
claude mcp add flighty -- uv --directory /path/to/flighty-mcp run flighty-mcp
```

### Claude Desktop

```json
{
  "mcpServers": {
    "flighty": {
      "command": "uv",
      "args": ["--directory", "/path/to/flighty-mcp", "run", "flighty-mcp"]
    }
  }
}
```

## Configuration

| Env var | Default |
|---|---|
| `FLIGHTY_AUTH_TOKEN` | read from the Flighty database |
| `FLIGHTY_BUILD_TOKEN` | read from the app bundle |
| `FLIGHTY_DB_PATH` | `~/Library/Containers/com.flightyapp.flighty/Data/Documents/MainFlightyDatabase.db` |
| `FLIGHTY_APP_PATH` | `/Applications/Flighty.app` |
| `FLIGHTY_TRANSPORT` | `stdio` (or `streamable-http`) |
| `FLIGHTY_HOST` | `127.0.0.1` (HTTP transport only) |
| `FLIGHTY_PORT` | `8000` (HTTP transport only) |
| `FLIGHTY_READONLY` | unset — see [Running without the app](#running-without-the-app) |

Set `FLIGHTY_AUTH_TOKEN` and `FLIGHTY_BUILD_TOKEN` to run without the Flighty
app present (e.g. on another machine). On a Mac with the app, read them with:

```sh
# FLIGHTY_AUTH_TOKEN
sqlite3 "$HOME/Library/Containers/com.flightyapp.flighty/Data/Documents/MainFlightyDatabase.db" \
  "SELECT authToken FROM Account ORDER BY lastUpdated DESC LIMIT 1"

# FLIGHTY_BUILD_TOKEN
plutil -extract FlightyBuildToken raw /Applications/Flighty.app/Contents/Info.plist
```

The auth token is account-specific — treat it like a password. It carries no
`exp` claim, so it does not expire and the only way to revoke it is to change
the account password. The build token identifies the app version, not you, and
expires in September 2031.

## Running without the app

The two tokens are not sufficient on their own. `db.py` also supplies the
airline and airport reference tables that `search_flights` needs (the API wants
an airline UUID, not an IATA code), the account's flight list, and the Flighty
app's **live sync cursor** — `SyncInfo.nextURL`, which the app rewrites on every
sync.

That cursor is what the three write tools post to, so they cannot work from a
copied database. `FLIGHTY_READONLY=1` leaves them unregistered rather than
registered and failing: a model that can see `remove_flight` will eventually
call it, and "no sync cursor" is a confusing way to learn this deployment never
could. Under it, the tool list is `search_flights`, `add_flight` and
`list_my_flights`.

So a deployment away from the Mac needs both tokens and a database snapshot,
with `FLIGHTY_DB_PATH` pointing at it. `scripts/export-db.sh` makes one with
`sqlite3 .backup` (safe against the running app, unlike `cp`, which would miss
the `-wal`), drops the `Account` table so the snapshot is not a second copy of
the bearer token, and ships it to the host named by `FLIGHTY_REMOTE`.

`Dockerfile` builds that deployment. It is driven from
[`mcp-gateway`](../mcp-gateway), which fronts it with OAuth; the container
itself authenticates nothing because it is only reachable from the gateway on an
internal network.

## Notes

- `list_my_flights` reads the local database, which lags the server until the
  app next syncs.
- A flight added via the API may not show in the app immediately. Re-running
  `add_flight` refreshes its timestamp so the app picks it up on the next sync.
- Search is by airline + flight number + date.
