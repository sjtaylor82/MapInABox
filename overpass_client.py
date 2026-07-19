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
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from logging_utils import miab_log

try:
    import certifi
    _SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except Exception:
    _SSL_CONTEXT = ssl.create_default_context()

# ---------------------------------------------------------------------------
# Mirror list — read from overpass_cache_url.txt if present, else defaults.
# ---------------------------------------------------------------------------

_BASE_DIR = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))

# Human-readable labels matched by index to the URLs below — used in
# announcements so the user knows which server is being tried.
OVERPASS_MIRROR_LABELS: list[str] = [
    "Germany (main)",
    "Germany (CDN)",
    "France",
]

OVERPASS_MIRRORS: list[str] = [
    "https://overpass-api.de/api/interpreter",
    "https://z.overpass-api.de/api/interpreter",
    "https://overpass.openstreetmap.fr/api/interpreter",
]
# Kumi Systems (overpass.kumi.systems) was tried as a fourth mirror but
# consistently hung for 20-45s before timing out in every observed test
# run, never once succeeding — worse than not having it, since it delays
# fallback to a working mirror. Dropped rather than kept as dead weight.

# How long to avoid retrying a mirror after it returns HTTP 429 (rate
# limited). "Germany (main)" and "Germany (CDN)" are the same operator's
# cluster and share a rate limit, so a 429 on one usually means the other
# is limited too — better to jump straight to an independent provider
# (Kumi Systems / France) than to burn another timeout re-trying them.
RATE_LIMIT_COOLDOWN_SECS = 120.0

# How long to avoid retrying a mirror after ANY other failure (5xx server
# errors, connection errors, timeouts). A single street-mode entry can fire
# several separate request() calls in a row (id-based boundary probe, then
# a boundary probe, then the full name-based query) — without this, a
# mirror that just failed with a 504 moments ago gets hit again in the very
# next call with no memory of the failure, doubling or tripling the total
# wait for no benefit. Shorter than the 429 cooldown since these tend to be
# more transient (momentary overload) than a hard rate-limit block.
TRANSIENT_FAIL_COOLDOWN_SECS = 30.0


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class OverpassClient:
    """Thread-safe Overpass API wrapper with cooldown and mirror fallback.

    Parameters
    ----------
    cooldown_secs:
        Minimum seconds between requests (default 8).
    mirrors:
        Override the default mirror list if desired.
    """

    def __init__(
        self,
        cooldown_secs: float = 2.0,
        mirrors: list[str] | None = None,
    ) -> None:
        # 2, not 1: street-data fetches and the background POI fetch share
        # this one client, and Semaphore(1) forced them to run strictly
        # back-to-back — a POI fetch queued behind a slow/retrying street
        # fetch (or vice versa) could not even start until the other
        # finished its own full mirror-retry loop, compounding the total
        # wait badly. 2 lets one street request and one POI request be in
        # flight at the same time, which matches the public Overpass
        # instances' own published fair-use guidance of about 2 concurrent
        # slots per client — enough to fix the serialization without
        # opening the door to unbounded concurrent load if more callers
        # pile on.
        self._sem = threading.Semaphore(2)
        self._last_request = 0.0
        self._last_request_by_mirror: dict[int, float] = {}
        self._cooldown = cooldown_secs
        self._mirrors = list(mirrors or OVERPASS_MIRRORS)
        # Labels parallel to _mirrors for user-facing announcements.
        self._labels  = list(OVERPASS_MIRROR_LABELS[:len(self._mirrors)])
        while len(self._labels) < len(self._mirrors):
            self._labels.append(f"Server {len(self._labels) + 1}")
        self.status_cb = None  # optional callable(str) set by caller
        self._last_successful_mirror = 0  # Rotate between servers
        self._rate_limited_until: dict[int, float] = {}  # mirror index -> epoch time
        self._in_flight_mirrors: set[int] = set()
        self._mirror_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def request(
        self,
        query_data: bytes,
        timeout: int = 15,
        mirrors: list[str] | None = None,
        start_index: int | None = None,
        max_mirrors: int | None = None,
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

        # Hold the semaphore for the entire request lifecycle, not just the
        # cooldown wait. Previously the semaphore was released as soon as
        # the wait finished, which only paced how far apart requests started
        # - it didn't stop two requests actually being in flight to the
        # public Overpass instance at the same time (e.g. a background
        # batch fetch and a live F11 press). That risks tripping the public
        # server's own concurrency limits and causing timeouts on both,
        # rather than just queuing safely one at a time.
        with self._sem:
            if start_index is None:
                # Start with the next server after the last known-good mirror.
                # This spreads load across mirrors while still preserving the
                # last working choice as a fallback point.
                start_index = (self._last_successful_mirror + 1) % n_mirrors

            # Order servers: rotated position first, but push any mirror
            # that was recently 429'd to the back of the queue so we don't
            # immediately re-hit a host that just told us to back off.
            now = time.time()
            ordered_indices = [(start_index + offset) % n_mirrors for offset in range(n_mirrors)]
            available_indices = [
                i for i in ordered_indices
                if self._rate_limited_until.get(i, 0) <= now
            ]
            if available_indices:
                ordered_indices = available_indices
            else:
                ordered_indices.sort(key=lambda i: self._rate_limited_until.get(i, 0))
            if max_mirrors is not None:
                ordered_indices = ordered_indices[:max(1, int(max_mirrors))]

            for index in ordered_indices:
                with self._mirror_lock:
                    if index in self._in_flight_mirrors:
                        alternatives = [
                            i for i in ordered_indices
                            if i != index
                            and i not in self._in_flight_mirrors
                            and self._rate_limited_until.get(i, 0) <= time.time()
                        ]
                        if alternatives:
                            miab_log(
                                "verbose",
                                f"[Overpass] Skipping {label_list[index]} for concurrent request; already in use.",
                                getattr(self, "settings", None),
                            )
                            continue
                    self._in_flight_mirrors.add(index)
                url = mirror_list[index]
                label = label_list[index]
                self._wait(index)

                msg = f"Connecting to street server {index + 1} of {n_mirrors}: {label}..."
                miab_log("verbose", f"[Overpass] {msg}", getattr(self, "settings", None))
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
                    with urllib.request.urlopen(
                        req, timeout=timeout, context=_SSL_CONTEXT
                    ) as resp:
                        result = json.loads(resp.read().decode())

                    # Overpass returns a remark on runtime error
                    if "remark" in result and not result.get("elements"):
                        miab_log("errors", f"[Overpass] {label} error remark: {result['remark']}", getattr(self, "settings", None))
                        continue

                    # Success - update rotation tracker
                    if result.get("elements"):
                        miab_log("verbose", f"[Overpass] {label} succeeded", getattr(self, "settings", None))
                        if not custom_mirrors:
                            self._last_successful_mirror = index
                        return result

                    # Empty but valid
                    miab_log("verbose", f"[Overpass] {label} returned empty result", getattr(self, "settings", None))
                    if not custom_mirrors:
                        self._last_successful_mirror = index
                    return result

                except urllib.error.HTTPError as exc:
                    if exc.code == 429:
                        self._rate_limited_until[index] = time.time() + RATE_LIMIT_COOLDOWN_SECS
                        if not custom_mirrors and index in (0, 1):
                            self._rate_limited_until[0] = time.time() + RATE_LIMIT_COOLDOWN_SECS
                            self._rate_limited_until[1] = time.time() + RATE_LIMIT_COOLDOWN_SECS
                    elif exc.code >= 500:
                        self._rate_limited_until[index] = time.time() + TRANSIENT_FAIL_COOLDOWN_SECS
                    miab_log("errors", f"[Overpass] {label} failed: {exc}", getattr(self, "settings", None))
                    # Try next server
                    continue
                except Exception as exc:
                    # Socket timeouts, connection errors, etc. — also
                    # deprioritize briefly so a subsequent request() call
                    # (boundary probe, full query) doesn't immediately
                    # re-hit the same mirror that just failed.
                    self._rate_limited_until[index] = time.time() + TRANSIENT_FAIL_COOLDOWN_SECS
                    miab_log("errors", f"[Overpass] {label} failed: {exc}", getattr(self, "settings", None))
                    # Try next server
                    continue
                finally:
                    with self._mirror_lock:
                        self._in_flight_mirrors.discard(index)

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
        miab_log("verbose", f"[Overpass] Trying {tag} ...", getattr(self, "settings", None))
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
            with urllib.request.urlopen(
                req, timeout=timeout, context=_SSL_CONTEXT
            ) as resp:
                result = json.loads(resp.read().decode())
            if "remark" in result and not result.get("elements"):
                miab_log("errors", f"[Overpass] {tag} error remark: {result['remark']}", getattr(self, "settings", None))
                return None
            miab_log("verbose", f"[Overpass] {tag} succeeded ({len(result.get('elements', []))} elements)", getattr(self, "settings", None))
            return result
        except Exception as exc:
            miab_log("errors", f"[Overpass] {tag} failed: {exc}", getattr(self, "settings", None))
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
        max_mirrors: int | None = None,
    ) -> dict | None:
        """Large radius queries with standard timeout."""
        return self.request(query_data, timeout=timeout, max_mirrors=max_mirrors)
    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _wait(self, mirror_index: int | None = None) -> None:
        """Enforce a cooldown, per mirror when a mirror is known."""
        now = time.time()
        if mirror_index is None:
            last = self._last_request
        else:
            last = self._last_request_by_mirror.get(mirror_index, 0.0)
        elapsed = now - last
        if elapsed < self._cooldown:
            time.sleep(self._cooldown - elapsed)
        now = time.time()
        self._last_request = now
        if mirror_index is not None:
            self._last_request_by_mirror[mirror_index] = now
