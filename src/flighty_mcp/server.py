"""Flighty MCP server.

Tools, and which of them change the account:

  read   search_flights(airline, flight_number, date): find a flight's legs.
  read   list_my_flights(): your flights, from the local app database.
  WRITE  add_flight(flight_id, is_passenger, ...): add a leg to your account.
  WRITE  set_ticket(flight_id, ...): attach booking info to a flight.      [1]
  WRITE  set_tickets(tickets): booking info on many flights in one call.   [1]
  WRITE  remove_flight(flight_id): remove a flight. Destructive.           [1]

[1] Needs the Flighty app's live sync cursor, so these are registered only
    when FLIGHTY_READONLY is unset. See `_sync_tool` and `config.readonly`.
    The container therefore serves two reads and one write, `add_flight`.

Each is annotated accordingly -- see the ToolAnnotations constants below, which
are what a client reads to decide whether to confirm before calling.

Runs over stdio by default, or streamable-HTTP under FLIGHTY_TRANSPORT for the
containerised deployment behind the MCP gateway.
"""
from __future__ import annotations

import datetime
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from . import client, config, db

# Which tools change the account, declared rather than left to be inferred.
#
# `readOnlyHint` is absent-means-false in the MCP spec, so an unannotated tool
# is already treated as one that might write -- which made `search_flights` and
# `list_my_flights` look as dangerous as `add_flight`, and `add_flight` no more
# dangerous than a search. A client deciding whether to confirm before calling
# had nothing to go on, and the gateway's own read-only split is documented in
# terms of which tools carry this flag.
#
# `openWorldHint` separates the two reads: `search_flights` asks the Flighty
# API about the world's schedules, `list_my_flights` reads the local database
# snapshot and nothing else.
READ_API = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
READ_LOCAL = ToolAnnotations(readOnlyHint=True, openWorldHint=False)

# `idempotentHint` is true for all three writes and is not a formality: it is
# the documented recovery procedure. Re-running `add_flight` on a flight that
# is already active just bumps its server-side `updated_at`, which is how a
# flight stuck behind the app's sync cursor is brought back.
WRITE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)

# `remove_flight` is the only one that takes something away. It is reversible
# through `add_flight` -- the server keeps the record and re-subscribes it --
# but a client should still ask first, and `destructiveHint` is how it knows to.
DESTRUCTIVE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=True,
    openWorldHint=True,
)

mcp = MCPServer(
    "flighty",
    instructions=(
        "Search for flights and add them to the user's Flighty account.\n\n"
        "Recovering flights that vanished from the app: Flighty syncs with a "
        "cursor, so the app only pulls records whose updated_at is newer than "
        "its last sync. If a flight is active on the server but missing from the "
        "app (e.g. after a delete+re-add race, or any out-of-band change), "
        "re-running add_flight on its flight_id re-subscribes it and bumps the "
        "server-side updated_at to 'now', which is past the app's cursor. The "
        "flight then reappears on the app's next sync (foreground / pull-to-"
        "refresh). add_flight is idempotent for this purpose — re-adding a "
        "flight that is already active just refreshes its timestamp and does no "
        "harm. Use list_my_flights to see what the local DB currently shows "
        "(it lags the server by one app sync).\n\n"
        "set_ticket attaches a booking reference / seat / cabin to a flight "
        "already on the account (or pass those fields to add_flight to do both "
        "at once).\n\n"
        "remove_flight deletes a flight from the account (reversible via "
        "add_flight). It is destructive — confirm with the user first."
    ),
)


def _sync_tool(annotations: ToolAnnotations | None = None):
    """Register a tool only when a live Flighty database is available.

    The tools this guards post to the app's sync cursor, which a copied
    database has no usable one of. Under FLIGHTY_READONLY they are not
    registered at all, so they never reach a client's tool list — a model that
    can see `remove_flight` will eventually call it, and "no sync cursor" is a
    confusing way to learn that this deployment never could.
    """
    if config.readonly():
        return lambda fn: fn
    return mcp.tool(annotations=annotations)


# Integer codes the API uses for each ticket seat-position / cabin-class label.
SEAT_POSITIONS = {"aisle": 0, "middle": 1, "window": 2}
CABIN_CLASSES = {"economy": 0, "premiumeconomy": 1, "business": 2, "first": 3}
_CABIN_ALIASES = {"premium economy": "premiumeconomy", "premium_economy": "premiumeconomy"}


def _map_enum(value: str | int | None, mapping: dict[str, int], label: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    key = _CABIN_ALIASES.get(value.strip().lower(), value.strip().lower())
    if key not in mapping:
        allowed = ", ".join(mapping)
        raise ValueError(f"Invalid {label} {value!r}. Allowed: {allowed}.")
    return mapping[key]


def _ticket_from_response(resp: Any, flight_id: str) -> dict[str, Any] | None:
    """Pull the echoed ticket record for a flight out of a SyncResponse."""
    inv_pos = {v: k for k, v in SEAT_POSITIONS.items()}
    inv_cabin = {v: k for k, v in CABIN_CLASSES.items()}
    for r in resp.records:
        if r.HasField("ticket") and r.ticket.flight_id == flight_id:
            t = r.ticket
            return {
                "flight_id": t.flight_id,
                "pnr": t.pnr or None,
                "seat_number": t.seat_number or None,
                "seat_position": inv_pos.get(t.seat_position, t.seat_position)
                if t.HasField("seat_position")
                else None,
                "cabin_class": inv_cabin.get(t.cabin_class, t.cabin_class)
                if t.HasField("cabin_class")
                else None,
            }
    return None


def _ts(t: Any) -> str | None:
    """Format a google.protobuf.Timestamp field as ISO-8601 UTC, or None."""
    if t is None or (t.seconds == 0 and t.nanos == 0):
        return None
    return (
        datetime.datetime.fromtimestamp(t.seconds, datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


def _airport(ap: Any) -> dict[str, Any]:
    return {"iata": ap.iata or None, "name": ap.name or None, "city": ap.city or None}


def _format_leg(flight: Any) -> dict[str, Any]:
    """Turn a decoded Flight (with embedded airports/airline) into a plain dict."""
    dep = flight.departure
    arr = flight.arrival
    leg: dict[str, Any] = {
        "flight_id": flight.id,
        "flight_number": flight.flight_number or None,
        "airline": {
            "name": flight.airline.name or None,
            "iata": flight.airline.iata or None,
        },
        "from": _airport(dep.airport),
        "to": _airport(arr.scheduled_airport),
        "departure_terminal": dep.terminal or None,
        "arrival_terminal": arr.terminal or None,
        "scheduled_departure_utc": _ts(dep.times.scheduled_gate),
        "scheduled_arrival_utc": _ts(arr.times.scheduled_gate),
        "distance_km": flight.distance_km or None,
    }
    if flight.HasField("aircraft") and flight.aircraft.type_name:
        leg["aircraft"] = flight.aircraft.type_name
    return leg


@mcp.tool(annotations=READ_API)
def search_flights(airline: str, flight_number: str, date: str) -> dict[str, Any]:
    """Search Flighty for a scheduled flight by airline, number, and date.

    Args:
        airline: airline ICAO ("QFA"), IATA ("QF"), or name ("Qantas").
            IATA codes can match several carriers; the best match is chosen by
            relevance and alternatives are returned so you can disambiguate.
        flight_number: the flight number without the airline prefix, e.g. "1".
        date: local departure date as "YYYY-MM-DD".

    Returns the resolved airline, any ambiguity alternatives, and one entry per
    leg. Each leg has a flight_id — pass that to add_flight to add it. Times are
    UTC.
    """
    resolved = db.resolve_airline(airline)
    chosen = resolved["chosen"]
    resp = client.search(chosen["id"], flight_number, date)

    legs = [_format_leg(f) for f in resp.result.flights]
    result: dict[str, Any] = {
        "resolved_airline": {
            "name": chosen["name"],
            "iata": chosen["iata"],
            "icao": chosen["icao"],
        },
        "query": {"flight_number": flight_number, "date": date},
        "legs": legs,
        "count": len(legs),
    }
    if resolved["alternatives"]:
        result["airline_alternatives"] = [
            {"name": a["name"], "iata": a["iata"], "icao": a["icao"]}
            for a in resolved["alternatives"]
        ]
        result["note"] = (
            f"{len(legs)} leg(s) found for {chosen['name']} ({chosen['iata']}). "
            "Other airlines share this code; if this is the wrong carrier, "
            "re-run with its ICAO or full name from airline_alternatives."
        )
    if not legs:
        result["note"] = (
            "No flights found. Check the flight number and that the date is the "
            "local departure date (YYYY-MM-DD)."
        )
    return result


@mcp.tool(annotations=WRITE)
def add_flight(
    flight_id: str,
    is_passenger: bool = True,
    pnr: str | None = None,
    seat_number: str | None = None,
    seat_position: str | None = None,
    cabin_class: str | None = None,
) -> dict[str, Any]:
    """Add a flight to your Flighty account. THIS WRITES TO YOUR LIVE ACCOUNT.

    Call search_flights first and take the flight_id of the leg you want.

    Also use this to RECOVER a flight that is active on the server but has
    dropped out of the Flighty app: Flighty's sync is cursor-based (the app only
    pulls records whose updated_at is newer than its last sync), so a flight can
    get stuck hidden in the app even though the server still has it. Re-running
    add_flight on that flight_id re-subscribes it and bumps the server-side
    updated_at to "now" — past the app's cursor — so the flight reappears on the
    app's next sync (foreground / pull-to-refresh). This is safe and idempotent:
    re-adding an already-active flight just refreshes its timestamp.

    Args:
        flight_id: the leg's flight_id from search_flights (or a known flight
            UUID you want to re-subscribe to recover it).
        is_passenger: True if you are flying on it, False to just track it.
        pnr: booking reference / record locator, e.g. "ABC123" (optional).
        seat_number: e.g. "12A" (optional).
        seat_position: "window", "middle", or "aisle" (optional).
        cabin_class: "economy", "premiumEconomy", "business", or "first" (optional).

    Returns the added flight's route, times, share URL, and any booking info set.
    """
    resp = client.subscribe(flight_id, is_passenger=is_passenger)
    uf = resp.user_flight
    result: dict[str, Any] = {
        "added": True,
        "flight": _format_leg(uf.flight),
        "share_url": uf.share_url or None,
        "is_passenger": is_passenger,
    }
    if any(v is not None for v in (pnr, seat_number, seat_position, cabin_class)):
        result["ticket"] = set_ticket(
            flight_id, pnr=pnr, seat_number=seat_number,
            seat_position=seat_position, cabin_class=cabin_class,
        )
    return result


@_sync_tool(WRITE)
def set_ticket(
    flight_id: str,
    pnr: str | None = None,
    seat_number: str | None = None,
    seat_position: str | None = None,
    cabin_class: str | None = None,
) -> dict[str, Any]:
    """Attach booking info (PNR, seat, cabin) to a flight already on your account.

    THIS WRITES TO YOUR LIVE ACCOUNT. The flight must already be added (via
    add_flight). Only the fields you pass are set.

    Args:
        flight_id: a flight already on your account.
        pnr: booking reference / record locator, e.g. "ABC123".
        seat_number: e.g. "12A".
        seat_position: "window", "middle", or "aisle".
        cabin_class: "economy", "premiumEconomy", "business", or "first".

    seat_position is "window", "middle", or "aisle"; cabin_class is "economy",
    "premiumEconomy", "business", or "first". Trip reason isn't supported.
    """
    pos = _map_enum(seat_position, SEAT_POSITIONS, "seat_position")
    cabin = _map_enum(cabin_class, CABIN_CLASSES, "cabin_class")
    resp = client.set_ticket(
        flight_id, pnr=pnr, seat_number=seat_number,
        seat_position=pos, cabin_class=cabin,
    )
    echoed = _ticket_from_response(resp, flight_id)
    result: dict[str, Any] = {
        "set": True,
        "flight_id": flight_id,
        "ticket": echoed,
        "confirmed_by_server": echoed is not None,
    }
    if not db.flight_is_mine(flight_id):
        result["note"] = (
            "This flight isn't in the local database yet (the app may not have "
            "synced the add). The ticket was still pushed; confirmed_by_server "
            "tells you whether the server accepted it."
        )
    return result


class TicketSpec(BaseModel):
    """One flight's booking info for the bulk set_tickets call."""

    flight_id: str = Field(description="A flight already on your account.")
    pnr: str | None = Field(default=None, description="Booking reference / record locator.")
    seat_number: str | None = Field(default=None, description='Seat, e.g. "12A".')
    seat_position: str | None = Field(default=None, description='"window", "middle", or "aisle".')
    cabin_class: str | None = Field(
        default=None, description='"economy", "premiumEconomy", "business", or "first".'
    )


def _set_tickets_sequential(tickets: list[TicketSpec]) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    succeeded = 0
    for spec in tickets:
        try:
            res = set_ticket(
                spec.flight_id,
                pnr=spec.pnr,
                seat_number=spec.seat_number,
                seat_position=spec.seat_position,
                cabin_class=spec.cabin_class,
            )
            if res.get("set"):
                succeeded += 1
            results.append(res)
        except Exception as err:  # isolate per-item failures (e.g. bad enum, API error)
            results.append({"set": False, "flight_id": spec.flight_id, "error": str(err)})
    return {
        "mode": "sequential",
        "count": len(tickets),
        "succeeded": succeeded,
        "failed": len(tickets) - succeeded,
        "results": results,
    }


def _set_tickets_batch(tickets: list[TicketSpec]) -> dict[str, Any]:
    # Map all enums up front; if any is invalid, send nothing and tell the caller
    # to retry sequentially (so one bad item can't void the whole batch silently).
    items: list[dict[str, Any]] = []
    for spec in tickets:
        try:
            items.append({
                "flight_id": spec.flight_id,
                "pnr": spec.pnr,
                "seat_number": spec.seat_number,
                "seat_position": _map_enum(spec.seat_position, SEAT_POSITIONS, "seat_position"),
                "cabin_class": _map_enum(spec.cabin_class, CABIN_CLASSES, "cabin_class"),
            })
        except Exception as err:
            return {
                "mode": "batch",
                "ok": False,
                "sent": False,
                "error": f"{spec.flight_id}: {err}",
                "retry": "Fix the input or call again with mode='sequential'.",
            }
    try:
        resp = client.set_tickets_batch(items)
    except Exception as err:
        return {
            "mode": "batch",
            "ok": False,
            "sent": True,
            "error": str(err),
            "retry": "The batch request failed; call again with mode='sequential' to apply them one by one.",
        }

    # Confirm against the echoed ticket records.
    results = []
    confirmed = 0
    for spec in tickets:
        echoed = _ticket_from_response(resp, spec.flight_id)
        if echoed is not None:
            confirmed += 1
        results.append({
            "flight_id": spec.flight_id,
            "confirmed_by_server": echoed is not None,
            "ticket": echoed,
        })
    return {
        "mode": "batch",
        "ok": True,
        "sent": True,
        "count": len(tickets),
        "confirmed_by_server": confirmed,
        "results": results,
    }


@_sync_tool(WRITE)
def set_tickets(tickets: list[TicketSpec], mode: str = "sequential") -> dict[str, Any]:
    """Set booking info on MANY flights in one call. WRITES TO YOUR LIVE ACCOUNT.

    Use this instead of calling set_ticket once per flight — e.g. to backfill
    booking references/seats across a history of flights. Every flight must
    already be on your account (add it with add_flight first).

    Args:
        tickets: a list of per-flight booking specs, each with flight_id and any
            of pnr, seat_number, seat_position (window/middle/aisle), cabin_class
            (economy/premiumEconomy/business/first).
        mode: "sequential" (default) sends one request per flight, so a single
            failure doesn't stop the rest and you get a per-flight result.
            "batch" sends all tickets in one request (faster for large
            backfills). If a batch fails (ok=False), re-run with
            mode="sequential" to apply them one by one.

    Returns per-flight results and a summary. In batch mode, `ok` indicates
    whether the single request succeeded and `confirmed_by_server` counts how
    many tickets the server echoed back.
    """
    if mode == "batch":
        return _set_tickets_batch(tickets)
    if mode != "sequential":
        return {"error": f"Unknown mode {mode!r}. Use 'sequential' or 'batch'."}
    return _set_tickets_sequential(tickets)


@_sync_tool(DESTRUCTIVE)
def remove_flight(flight_id: str) -> dict[str, Any]:
    """Remove a flight from your Flighty account. THIS WRITES TO YOUR LIVE ACCOUNT.

    Deletes the flight from the account and syncs the removal to all devices.
    Reversible: re-add the same flight_id with add_flight to restore it (that
    re-subscribes it and bumps its timestamp so the app re-pulls it).

    Args:
        flight_id: the flight UUID to remove (from search_flights / list_my_flights).

    Returns confirmation. Prefer confirming with the user before calling this,
    since it removes a real flight from their account.
    """
    client.delete_flight(flight_id)
    return {
        "removed": True,
        "flight_id": flight_id,
        "note": (
            "Removal pushed to the server and will sync to your devices. "
            "Reversible with add_flight on the same flight_id."
        ),
    }


@mcp.tool(annotations=READ_LOCAL)
def list_my_flights(include_archived: bool = False) -> dict[str, Any]:
    """List flights already on your Flighty account, from the local app database.

    Useful to avoid adding a duplicate. Reflects the last time the Flighty app
    synced to disk, so a flight added seconds ago may not appear until the app
    syncs. Times are UTC.

    Args:
        include_archived: include past/archived flights (default False).
    """
    flights = db.list_my_flights(include_archived=include_archived)
    return {"count": len(flights), "flights": flights}


def main() -> None:
    transport = config.transport()
    if transport == "stdio":
        mcp.run()
        return
    # Behind the gateway. No auth here: this listens on an internal Docker
    # network with no Traefik router, so the gateway is the only thing that can
    # reach it. DNS-rebinding protection is left off (the SDK's default when no
    # transport_security is passed) because it guards browsers against local
    # servers, which is not the path here.
    mcp.run(
        transport=transport,
        host=config.host(),
        port=config.port(),
        streamable_http_path="/mcp",
    )


if __name__ == "__main__":
    main()
