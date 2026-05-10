"""opensky.py — OpenSky Network API client for Map in a Box.

Handles OAuth2 Client Credentials authentication and all OpenSky API calls.
Credentials are read from credentials.json in the app directory.

credentials.json format:
    {"clientId": "...", "clientSecret": "..."}

If credentials.json is absent, falls back to anonymous access (100 calls/day).
"""

import json
import os
import time
import urllib.parse
import urllib.request
import ssl

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TOKEN_URL   = "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"
API_BASE    = "https://opensky-network.org/api"
CREDS_FILE  = "credentials.json"

# ---------------------------------------------------------------------------

class OpenSkyClient:
    """Thin OpenSky API client with OAuth2 token management."""

    def __init__(self, base_dir: str,
                 client_id: str = "", client_secret: str = ""):
        self._base_dir      = base_dir
        self._client_id     = client_id.strip() if client_id else None
        self._client_secret = client_secret.strip() if client_secret else None
        self._token        = None
        self._token_expiry = 0.0
        self._ssl_ctx      = ssl.create_default_context()
        if self._client_id and self._client_secret:
            print(f"[OpenSky] Credentials loaded from settings for {self._client_id}")
        else:
            self._load_credentials()

    def _load_credentials(self):
        path = os.path.join(self._base_dir, CREDS_FILE)
        if not os.path.exists(path):
            print("[OpenSky] No credentials.json found — using anonymous access")
            return
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            self._client_id     = data.get("clientId", "").strip()
            self._client_secret = data.get("clientSecret", "").strip()
            if self._client_id and self._client_secret:
                print(f"[OpenSky] Credentials loaded for {self._client_id}")
            else:
                print("[OpenSky] credentials.json missing clientId or clientSecret")
        except Exception as exc:
            print(f"[OpenSky] Failed to load credentials.json: {exc}")

    @property
    def authenticated(self) -> bool:
        return bool(self._client_id and self._client_secret)

    def _get_token(self) -> str | None:
        """Return a valid bearer token, refreshing if expired."""
        if not self.authenticated:
            return None
        if self._token and time.time() < self._token_expiry - 30:
            return self._token
        try:
            data = urllib.parse.urlencode({
                "grant_type":    "client_credentials",
                "client_id":     self._client_id,
                "client_secret": self._client_secret,
            }).encode()
            req = urllib.request.Request(
                TOKEN_URL, data=data,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent":   "MapInABox/1.0",
                })
            with urllib.request.urlopen(req, timeout=10, context=self._ssl_ctx) as r:
                resp = json.loads(r.read().decode())
            self._token        = resp["access_token"]
            self._token_expiry = time.time() + int(resp.get("expires_in", 300))
            print("[OpenSky] Token refreshed")
            return self._token
        except Exception as exc:
            print(f"[OpenSky] Token refresh failed: {exc}")
            return None

    def _request(self, endpoint: str, params: dict) -> dict:
        """Make an authenticated (or anonymous) GET request."""
        url = f"{API_BASE}{endpoint}?{urllib.parse.urlencode(params)}"
        headers = {"User-Agent": "MapInABox/1.0"}
        token = self._get_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15, context=self._ssl_ctx) as r:
            return json.loads(r.read().decode())

    # -----------------------------------------------------------------------
    # Public API methods
    # -----------------------------------------------------------------------

    def states_in_bbox(self, lamin, lomin, lamax, lomax) -> list:
        """Return list of state vectors within bounding box."""
        data = self._request("/states/all", {
            "lamin": round(lamin, 4), "lomin": round(lomin, 4),
            "lamax": round(lamax, 4), "lomax": round(lomax, 4),
        })
        return data.get("states") or []

    def flight_route(self, icao24: str) -> dict:
        """Return estimated departure/arrival airports for an airborne aircraft.

        Uses the icao24 hex address (s[0] from state vector).
        Returns {"departure": "YBBN", "arrival": "YSSY"} with ICAO airport codes,
        or an empty dict on failure/no data.
        """
        now   = int(time.time())
        begin = now - 4 * 3600   # look back 4 hours to catch the departure
        end   = now + 3600
        try:
            data = self._request("/flights/aircraft", {
                "icao24": icao24.lower().strip(),
                "begin":  begin,
                "end":    end,
            })
            if not isinstance(data, list) or not data:
                return {}
            flight = data[-1]   # most recent leg
            dep = (flight.get("estDepartureAirport") or "").strip().upper()
            arr = (flight.get("estArrivalAirport")   or "").strip().upper()
            if dep or arr:
                return {"departure": dep, "arrival": arr}
        except Exception as exc:
            if "404" not in str(exc):
                print(f"[OpenSky] flight_route failed for {icao24}: {exc}")
        return {}

    def departures(self, airport_icao: str, hours_ahead: int = 12) -> list:
        """Return upcoming departures from an airport.
        
        airport_icao: e.g. 'YBBN' for Brisbane, 'YSSY' for Sydney
        Returns list of flight dicts.
        """
        now   = int(time.time())
        begin = now - 3600          # last hour (for recently departed)
        end   = now + hours_ahead * 3600
        try:
            data = self._request("/flights/departure", {
                "airport": airport_icao,
                "begin":   begin,
                "end":     end,
            })
            return data if isinstance(data, list) else []
        except Exception as exc:
            print(f"[OpenSky] Departures failed: {exc}")
            return []

    def arrivals(self, airport_icao: str, hours_back: int = 6) -> list:
        """Return recent/upcoming arrivals at an airport."""
        now   = int(time.time())
        begin = now - hours_back * 3600
        end   = now + 3600
        try:
            data = self._request("/flights/arrival", {
                "airport": airport_icao,
                "begin":   begin,
                "end":     end,
            })
            return data if isinstance(data, list) else []
        except Exception as exc:
            print(f"[OpenSky] Arrivals failed: {exc}")
            return []
