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

Set `FLIGHTY_AUTH_TOKEN` and `FLIGHTY_BUILD_TOKEN` to run without the Flighty
app present (e.g. on another machine). On a Mac with the app, read them with:

```sh
# FLIGHTY_AUTH_TOKEN
sqlite3 "$HOME/Library/Containers/com.flightyapp.flighty/Data/Documents/MainFlightyDatabase.db" \
  "SELECT authToken FROM Account ORDER BY lastUpdated DESC LIMIT 1"

# FLIGHTY_BUILD_TOKEN
plutil -extract FlightyBuildToken raw /Applications/Flighty.app/Contents/Info.plist
```

The auth token is account-specific — treat it like a password. The build token
identifies the app version, not you.

## Notes

- `list_my_flights` reads the local database, which lags the server until the
  app next syncs.
- A flight added via the API may not show in the app immediately. Re-running
  `add_flight` refreshes its timestamp so the app picks it up on the next sync.
- Search is by airline + flight number + date.
