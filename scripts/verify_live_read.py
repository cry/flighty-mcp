"""Live read-only check: search QF1 on 2027-04-30 against the real API.

No account writes. Confirms auth, headers, HTTP/2 and decoding work end-to-end.
"""
import json

from flighty_mcp import server


def main() -> None:
    result = server.search_flights(airline="QF", flight_number="1", date="2027-04-30")
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
