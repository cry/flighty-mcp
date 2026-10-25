"""Static configuration for the Flighty MCP server.

Nothing here is user-secret. The build token identifies the app build (not you)
and is read live from the installed Flighty app bundle's Info.plist. The user's
bearer token is never stored here; it is read live from the Flighty database at
call time (see db.get_auth_token).
"""
from __future__ import annotations

import os
import subprocess

API_BASE = "https://api.flightyapp.com"

# Default path to the live Flighty app database inside its macOS container.
# Override with FLIGHTY_DB_PATH (e.g. to point at a copy).
DEFAULT_DB_PATH = os.path.expanduser(
    "~/Library/Containers/com.flightyapp.flighty/Data/Documents/MainFlightyDatabase.db"
)

# Headers the app sends. The build token (X-Flighty-Build-Token) is an ES256 JWT
# that identifies the app build, valid into 2031, not tied to the account.
USER_AGENT = "Flighty 4.11.0 (4813) com.flightyapp.flighty"
LOCALE = "en_US"

DEFAULT_APP_PATH = "/Applications/Flighty.app"
_BUILD_TOKEN_FILE = os.path.join(os.path.dirname(__file__), "build_token.txt")


def db_path() -> str:
    return os.environ.get("FLIGHTY_DB_PATH", DEFAULT_DB_PATH)


def app_path() -> str:
    return os.environ.get("FLIGHTY_APP_PATH", DEFAULT_APP_PATH)


def build_token() -> str | None:
    """App build token (not account-specific), resolved in this order:

    1. FLIGHTY_BUILD_TOKEN env var.
    2. The installed Flighty app bundle's Info.plist (FlightyBuildToken) — the
       normal source, so nothing needs to be copied or committed.
    3. A local build_token.txt fallback (legacy / machines without the app).
    """
    env = os.environ.get("FLIGHTY_BUILD_TOKEN")
    if env:
        return env.strip()

    info_plist = os.path.join(app_path(), "Contents", "Info.plist")
    try:
        out = subprocess.run(
            ["plutil", "-extract", "FlightyBuildToken", "raw", info_plist],
            capture_output=True, text=True, timeout=10,
        )
        tok = out.stdout.strip()
        if out.returncode == 0 and tok:
            return tok
    except (OSError, subprocess.SubprocessError):
        pass

    try:
        with open(_BUILD_TOKEN_FILE, "r", encoding="utf-8") as fh:
            tok = fh.read().strip()
            return tok or None
    except FileNotFoundError:
        return None
