"""HTTP client for the Flighty API (protobuf over HTTP/2).

Two calls make up "add a flight": search() to find the legs, then subscribe()
to add a chosen leg to the account. Auth is read live from the local database
on every request so it tracks token rotation.
"""
from __future__ import annotations

import datetime
from typing import Any

import httpx

from . import config, db
from . import flighty_pb as fp


class FlightyApiError(RuntimeError):
    """A non-protobuf / error response from the Flighty API."""


def _headers() -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {db.get_auth_token()}",
        "Content-Type": "application/x-protobuf",
        "Accept": "application/x-protobuf",
        "User-Agent": config.USER_AGENT,
        "X-Flighty-Locale": config.LOCALE,
        "Accept-Language": "en-US,en;q=0.9",
    }
    token = config.build_token()
    if token:
        headers["X-Flighty-Build-Token"] = token
    return headers


def _client() -> httpx.Client:
    return httpx.Client(base_url=config.API_BASE, http2=True, timeout=30.0)


def _require_protobuf(resp: httpx.Response, what: str) -> bytes:
    ctype = resp.headers.get("content-type", "")
    if resp.status_code != 200 or "protobuf" not in ctype:
        snippet = resp.text[:300].strip().replace("\n", " ")
        raise FlightyApiError(
            f"{what} failed: HTTP {resp.status_code}, content-type {ctype!r}. "
            f"Body: {snippet}"
        )
    return resp.content


def search(airline_id: str, flight_number: str, date: str) -> Any:
    """POST /v1/search. date is the local departure date 'YYYY-MM-DD'.

    Returns a decoded SearchResponse (result.flights, one per leg).
    """
    req = fp.pb.SearchRequest()
    req.query.airline_id = airline_id
    req.query.flight_number = str(flight_number)
    req.date = date
    req.mode = "FLIGHT_NUMBER"
    body = fp.encode(req)
    with _client() as client:
        resp = client.post("/v1/search", content=body, headers=_headers())
    data = _require_protobuf(resp, "search")
    return fp.decode("SearchResponse", data)


def subscribe(flight_id: str, is_passenger: bool = True) -> Any:
    """POST /v1/flight/{id}/subscribe. Adds the flight to the account.

    This is a real write. Returns a decoded SubscribeFlightResponse.
    """
    params = {"is_passenger": "true" if is_passenger else "false", "source": ""}
    headers = _headers()
    headers["Content-Length"] = "0"
    with _client() as client:
        resp = client.post(
            f"/v1/flight/{flight_id}/subscribe", params=params, headers=headers
        )
    data = _require_protobuf(resp, "subscribe")
    return fp.decode("SubscribeFlightResponse", data)


def set_ticket(
    flight_id: str,
    pnr: str | None = None,
    seat_number: str | None = None,
    seat_position: int | None = None,
    cabin_class: int | None = None,
) -> Any:
    """Push booking info (PNR/seat/cabin) for a flight via the sync endpoint.

    The flight must already be on the account. Sends only a ticket change
    (SyncChange.ticket) — never a bare flight-ref change (SyncChange.flight),
    which removes the flight. Returns the decoded SyncResponse, which echoes the
    ticket back as a record.
    """
    now = fp.ts(datetime.datetime.now(datetime.timezone.utc))

    req = fp.pb.SyncRequest()
    ticket_change = req.change.add()
    ticket_change.client_time.CopyFrom(now)
    t = ticket_change.ticket
    t.flight_id = flight_id
    if pnr is not None:
        t.pnr = pnr
    if seat_number is not None:
        t.seat_number = seat_number
    if seat_position is not None:
        t.seat_position = seat_position
    if cabin_class is not None:
        t.cabin_class = cabin_class

    # NOTE: no flight-ref change here — see docstring. That change == delete.
    body = fp.encode(req)
    url = db.get_sync_url()  # absolute URL with the current cursor token
    with _client() as client:
        resp = client.post(url, content=body, headers=_headers())
    data = _require_protobuf(resp, "set_ticket")
    return fp.decode("SyncResponse", data)


def set_tickets_batch(items: list[dict[str, Any]]) -> Any:
    """Set booking info on several flights in ONE sync request.

    items: list of dicts with flight_id and optional pnr, seat_number,
    seat_position (int), cabin_class (int) — enums already mapped. Sends one
    ticket change per item (no flight-ref changes). Returns the decoded
    SyncResponse, which echoes the tickets back.
    """
    now = fp.ts(datetime.datetime.now(datetime.timezone.utc))
    req = fp.pb.SyncRequest()
    for item in items:
        change = req.change.add()
        change.client_time.CopyFrom(now)
        t = change.ticket
        t.flight_id = item["flight_id"]
        if item.get("pnr") is not None:
            t.pnr = item["pnr"]
        if item.get("seat_number") is not None:
            t.seat_number = item["seat_number"]
        if item.get("seat_position") is not None:
            t.seat_position = item["seat_position"]
        if item.get("cabin_class") is not None:
            t.cabin_class = item["cabin_class"]
    body = fp.encode(req)
    url = db.get_sync_url()
    with _client() as client:
        resp = client.post(url, content=body, headers=_headers())
    data = _require_protobuf(resp, "set_tickets_batch")
    return fp.decode("SyncResponse", data)


def delete_flight(flight_id: str) -> Any:
    """Remove a flight from the account via the sync stream.

    Sends a bare flight-ref change (SyncChange.flight) to /v1/sync/full with
    fast_flight_sync=true. Returns the decoded SyncResponse. Reversible by
    re-subscribing (add_flight) the same flight_id.
    """
    now = fp.ts(datetime.datetime.now(datetime.timezone.utc))
    req = fp.pb.SyncRequest()
    change = req.change.add()
    change.client_time.CopyFrom(now)
    change.flight.id = flight_id
    body = fp.encode(req)

    url = db.get_sync_url()
    url += ("&" if "?" in url else "?") + "fast_flight_sync=true"
    with _client() as client:
        resp = client.post(url, content=body, headers=_headers())
    data = _require_protobuf(resp, "delete_flight")
    return fp.decode("SyncResponse", data)
