"""Live write check: add one real flight to your Flighty account.

Usage:
    uv run python scripts/verify_live_add.py <AIRLINE> <FLIGHT_NUMBER> <YYYY-MM-DD> [leg_index]
    # or, if you already have a flight_id:
    uv run python scripts/verify_live_add.py --flight-id <UUID>

This subscribes the chosen leg to your account. You can delete it in the
Flighty app afterward.
"""
import json
import sys

from flighty_mcp import server


def main() -> None:
    args = sys.argv[1:]
    if args and args[0] == "--flight-id":
        flight_id = args[1]
    else:
        airline, number, date = args[0], args[1], args[2]
        leg_index = int(args[3]) if len(args) > 3 else 0
        found = server.search_flights(airline=airline, flight_number=number, date=date)
        legs = found["legs"]
        print(json.dumps({"legs": legs}, indent=2, default=str))
        if not legs:
            print("No legs found; nothing to add.")
            return
        flight_id = legs[leg_index]["flight_id"]
        print(f"\nAdding leg index {leg_index}: {flight_id}")

    result = server.add_flight(flight_id=flight_id, is_passenger=True)
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
