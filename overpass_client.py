"""overpass_client.py — Overpass API client for Map in a Box.

A single shared helper so every query goes through one semaphore,
one cooldown timer, and one mirror-fallback loop.  MapNavigator
(and any future module) imports ``OverpassClient`` and calls
``request(query_bytes)`` instead of duplicating the loop.

Usage::

    from overpass_client import OverpassClient
    _overpass = OverpassClient()
    result = _overpass.request(query_data)  # bytes → parsed JSON or None
"""

import json
import os
import sys
import threading
import time
import urllib.request

# ---------------------------------------------------------------------------
# Mirror list — read from overpass_cache_url.txt if present, else defaults.
# ---------------------------------------------------------------------------

_BASE_DIR = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))

# Human-readable labels matched by index to the URLs below — used in
# announcements so the user knows which server is being tried.
OVERPASS_MIRROR_LABELS: list[str] = [
    "Germany (main)",
    "Germany (CDN)",
]

OVERPASS_MIRRORS: list[str] = [
    "https://overpass-api.de/api/interpreter",
    "https://z.overpass-api.de/api/interpreter",
]


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class OverpassClient:
    """Thread-safe Overpass API wrapper with cooldown and mirror fallback.

    Parameters
    ----------
    cooldown_secs:
        Minimum seconds between requests (default 6).
    mirrors:
        Override the default mirror list if desired.
    """

    def __init__(
        self,
        cooldown_secs: float = 8.0,
        mirrors: list[str] | None = None,
    ) -> None:
        self._sem = threading.Semaphore(1)
        self._last_request = 0.0
        self._cooldown = cooldown_secs
        self._mirrors = list(mirrors or OVERPASS_MIRRORS)
        # Labels parallel to _mirrors for user-facing announcements.
        self._labels  = list(OVERPASS_MIRROR_LABELS[:len(self._mirrors)])
        while len(self._labels) < len(self._mirrors):
            self._labels.append(f"Server {len(self._labels) + 1}")
        self.status_cb = None  # optional callable(str) set by caller
        self._last_successful_mirror = 0  # Rotate between servers

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def request(
        self,
        query_data: bytes,
        timeout: int = 15,
        mirrors: list[str] | None = None,
        start_index: int | None = None,
    ) -> dict | None:
        """Send an Overpass QL query and return the parsed JSON response.

        Tries servers sequentially starting with the last known-good mirror.
        Only tries other servers if the first one fails.

        Returns parsed JSON dict, or ``None`` if all mirrors failed.
        """
        custom_mirrors = mirrors is not None
        mirror_list = list(mirrors or self._mirrors)
        label_list  = list(self._labels[:len(mirror_list)])
        while len(label_list) < len(mirror_list):
            label_list.append(f"Server {len(label_list) + 1}")
        
        n_mirrors = len(mirror_list)
        
        # Apply cooldown once before trying any server
        with self._sem:
            self._wait()
            
            if start_index is None:
                # Start with the next server after the last known-good mirror.
                # This spreads load across mirrors while still preserving the
                # last working choice as a fallback point.
                start_index = (self._last_successful_mirror + 1) % n_mirrors
        
        # Try servers sequentially, starting with rotated position
        for offset in range(n_mirrors):
            index = (start_index + offset) % n_mirrors
            url = mirror_list[index]
            label = label_list[index]
            
            msg = f"Connecting to street server {index + 1} of {n_mirrors}: {label}..."
            print(f"[Overpass] {msg}")
            if self.status_cb:
                try:
                    self.status_cb(msg)
                except Exception:
                    pass
            
            try:
                req = urllib.request.Request(
                    url, data=query_data,
                    headers={
                        "User-Agent":   "MapInABox/1.0",
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                )
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    result = json.loads(resp.read().decode())
                
                # Overpass returns a remark on runtime error
                if "remark" in result and not result.get("elements"):
                    print(f"[Overpass] {label} error remark: {result['remark']}")
                    continue
                
                # Success - update rotation tracker
                if result.get("elements"):
                    print(f"[Overpass] {label} succeeded")
                    if not custom_mirrors:
                        self._last_successful_mirror = index
                    return result
                    
                # Empty but valid
                print(f"[Overpass] {label} returned empty result")
                if not custom_mirrors:
                    self._last_successful_mirror = index
                return result
                
            except Exception as exc:
                print(f"[Overpass] {label} failed: {exc}")
                # Try next server
                continue
        
        # All servers failed
        return None

    def request_one(
        self,
        query_data: bytes,
        url: str,
        label: str = "",
        timeout: int = 15,
    ) -> dict | None:
        """Try exactly one server URL — no rotation, no fallback.

        Used by _live_fetch in street_data.py which drives its own
        outer server loop (server1-name → server1-radius →
        server2-name → server2-radius).

        Returns parsed JSON dict, or ``None`` on any failure.
        """
        with self._sem:
            self._wait()
        tag = label or url
        print(f"[Overpass] Trying {tag} ...")
        if self.status_cb:
            try:
                self.status_cb(f"Connecting to {tag}...")
            except Exception:
                pass
        try:
            req = urllib.request.Request(
                url, data=query_data,
                headers={
                    "User-Agent":   "MapInABox/1.0",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                result = json.loads(resp.read().decode())
            if "remark" in result and not result.get("elements"):
                print(f"[Overpass] {tag} error remark: {result['remark']}")
                return None
            print(f"[Overpass] {tag} succeeded ({len(result.get('elements', []))} elements)")
            return result
        except Exception as exc:
            print(f"[Overpass] {tag} failed: {exc}")
            return None

    def poi_request(
        self,
        query_data: bytes,
        timeout: int = 15,
    ) -> dict | None:
        """Like ``request`` but tries public mirrors first, proxy last.

        POI queries are less likely to benefit from a caching proxy and
        more likely to time out on it, so we swap the order.
        """
        # Keep the mirror list in its original order so labels stay aligned.
        # Start on the public mirror when we have more than one server.
        start_index = 1 if len(self._mirrors) > 1 else 0
        return self.request(query_data, timeout=timeout, start_index=start_index)

    def large_request(
        self,
        query_data: bytes,
        timeout: int = 15,
    ) -> dict | None:
        """Large radius queries with standard timeout."""
        return self.request(query_data, timeout=timeout)
    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _wait(self) -> None:
        """Enforce the inter-request cooldown (called inside the semaphore)."""
        elapsed = time.time() - self._last_request
        if elapsed < self._cooldown:
            time.sleep(self._cooldown - elapsed)
        self._last_request = time.time()
