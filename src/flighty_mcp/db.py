"""Read-only access to the live Flighty app database.

Supplies the bearer token and the offline airline/airport reference tables that
the API needs (search wants an airline UUID, not an IATA code). The database is
opened read-only and immutable so the running Flighty app's -wal is never
touched and we can never write to it.
"""
from __future__ import annotations

import datetime
import os
import sqlite3
from typing import Any

from . import config


def _connect() -> sqlite3.Connection:
    uri = f"file:{config.db_path()}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def get_auth_token() -> str:
    """The account bearer JWT. From FLIGHTY_AUTH_TOKEN, else Account.authToken
    in the Flighty database. Treat it like a password."""
    env = os.environ.get("FLIGHTY_AUTH_TOKEN")
    if env:
        return env.strip()
    with _connect() as conn:
        row = conn.execute(
            "SELECT authToken FROM Account ORDER BY lastUpdated DESC LIMIT 1"
        ).fetchone()
    if not row or not row["authToken"]:
        raise RuntimeError(
            "No auth token found. Set FLIGHTY_AUTH_TOKEN, or sign in to the "
            f"Flighty app (looked in {config.db_path()})."
        )
    return row["authToken"]


def get_sync_url() -> str:
    """The app's current sync URL (SyncInfo.nextURL), token included.

    Posting a change (e.g. a ticket) goes to this URL as-is, the way the app
    reuses next.url. Using the app's own cursor keeps the server's response
    small.
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT nextURL FROM SyncInfo ORDER BY lastSync DESC LIMIT 1"
        ).fetchone()
    if not row or not row["nextURL"]:
        raise RuntimeError(
            "No sync cursor found in the Flighty database (SyncInfo.nextURL). "
            "Open Flighty once so it syncs, then retry."
        )
    return row["nextURL"]


def flight_is_mine(flight_id: str) -> bool:
    """Whether the given flight is on the account (active, not deleted)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM UserFlight WHERE flightId = ? AND isMyFlight = 1 "
            "AND deleted IS NULL LIMIT 1",
            (flight_id,),
        ).fetchone()
    return row is not None


def _airline_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "iata": row["iata"],
        "icao": row["icao"],
        "name": row["name"],
        "relevance": row["relevance"],
    }


def resolve_airline(query: str) -> dict[str, Any]:
    """Resolve an airline by ICAO, IATA, or name to its UUID.

    IATA codes are not unique (``QF`` maps to several carriers), so matches are
    ranked by Flighty's ``relevance`` column. Returns the best match plus any
    alternatives so the caller can disambiguate.
    """
    q = query.strip()
    qu = q.upper()
    results: list[sqlite3.Row] = []
    with _connect() as conn:
        # 1) exact ICAO (3-letter, unique per carrier)
        results = conn.execute(
            "SELECT id, iata, icao, name, relevance FROM Airline "
            "WHERE deleted IS NULL AND UPPER(icao) = ? ORDER BY relevance DESC",
            (qu,),
        ).fetchall()
        # 2) exact IATA (2-char), may be ambiguous
        if not results:
            results = conn.execute(
                "SELECT id, iata, icao, name, relevance FROM Airline "
                "WHERE deleted IS NULL AND UPPER(iata) = ? ORDER BY relevance DESC",
                (qu,),
            ).fetchall()
        # 3) name contains
        if not results:
            results = conn.execute(
                "SELECT id, iata, icao, name, relevance FROM Airline "
                "WHERE deleted IS NULL AND name LIKE ? ORDER BY relevance DESC LIMIT 25",
                (f"%{q}%",),
            ).fetchall()

    if not results:
        raise LookupError(f"No airline matched {query!r} (tried ICAO, IATA, then name).")

    chosen = _airline_dict(results[0])
    alternatives = [_airline_dict(r) for r in results[1:10]]
    return {"chosen": chosen, "alternatives": alternatives}


def resolve_airport(airport_id: str) -> dict[str, Any]:
    """Look up an airport by UUID for display (iata, name, city)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, iata, icao, name, city, country, timeZoneIdentifier "
            "FROM Airport WHERE id = ?",
            (airport_id,),
        ).fetchone()
    if not row:
        return {"id": airport_id, "iata": None, "name": None, "city": None}
    return {
        "id": row["id"],
        "iata": row["iata"],
        "icao": row["icao"],
        "name": row["name"],
        "city": row["city"],
        "country": row["country"],
        "timezone": row["timeZoneIdentifier"],
    }


def _iso(epoch_seconds: int | None) -> str | None:
    if not epoch_seconds:
        return None
    return (
        datetime.datetime.fromtimestamp(epoch_seconds, datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


def list_my_flights(include_archived: bool = False) -> list[dict[str, Any]]:
    """List the account's own flights from the local database.

    Reflects the last time the Flighty app synced to disk, so a flight added
    moments ago may not appear until the app syncs. Good enough for a
    duplicate check before adding.
    """
    where = "uf.isMyFlight = 1 AND uf.deleted IS NULL"
    if not include_archived:
        where += " AND uf.isArchived = 0"
    sql = (
        "SELECT f.id AS flight_id, f.number AS number, f.airlineId AS airline_id, "
        "       f.departureAirportId AS dep_id, f.scheduledArrivalAirportId AS arr_id, "
        "       f.lastKnownDepartureDate AS dep_ts, f.lastKnownArrivalDate AS arr_ts, "
        "       uf.importSource AS import_source "
        "FROM UserFlight uf JOIN Flight f ON f.id = uf.flightId "
        f"WHERE {where} "
        "ORDER BY f.lastKnownDepartureDate DESC"
    )
    out: list[dict[str, Any]] = []
    with _connect() as conn:
        rows = conn.execute(sql).fetchall()
        # Cache airport/airline lookups within this call.
        airports: dict[str, dict[str, Any]] = {}
        airlines: dict[str, str] = {}

        def airport(aid: str) -> dict[str, Any]:
            if aid not in airports:
                r = conn.execute(
                    "SELECT iata, name, city FROM Airport WHERE id = ?", (aid,)
                ).fetchone()
                airports[aid] = (
                    {"iata": r["iata"], "name": r["name"], "city": r["city"]}
                    if r
                    else {"iata": None, "name": None, "city": None}
                )
            return airports[aid]

        def airline(aid: str | None) -> str | None:
            if not aid:
                return None
            if aid not in airlines:
                r = conn.execute(
                    "SELECT iata, name FROM Airline WHERE id = ?", (aid,)
                ).fetchone()
                airlines[aid] = (r["name"] if r else None)
            return airlines[aid]

        for row in rows:
            dep = airport(row["dep_id"])
            arr = airport(row["arr_id"])
            out.append(
                {
                    "flight_id": row["flight_id"],
                    "flight_number": row["number"],
                    "airline": airline(row["airline_id"]),
                    "from": dep["iata"] or dep["name"],
                    "from_city": dep["city"],
                    "to": arr["iata"] or arr["name"],
                    "to_city": arr["city"],
                    "departure_utc": _iso(row["dep_ts"]),
                    "arrival_utc": _iso(row["arr_ts"]),
                    "import_source": row["import_source"],
                }
            )
    return out
