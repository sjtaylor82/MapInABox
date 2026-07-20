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
# Kumi Systems and its successor private.coffee have both been excluded after
# repeated request timeouts. Keeping them in the fallback list only delays a
# retry against a responsive mirror.

# How long to avoid retrying a mirror after it returns HTTP 429 (rate
# limited). "Germany (main)" and "Germany (CDN)" are the same operator's
# cluster and share a rate limit, so a 429 on one usually means the other
# is limited too — better to jump straight to an independent provider
# (France) than to burn another timeout re-trying them.
RATE_LIMIT_COOLDOWN_SECS = 120.0

TRANSIENT_FAIL_COOLDOWN_SECS = 30.0


class OverpassRequestCancelled(RuntimeError):
    """Raised before more network work when a caller supersedes a request."""


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class OverpassClient:
    """Thread-safe Overpass API wrapper with cooldown and mirror fallback."""

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
        self._last_successful_mirror = 0
        self._rate_limited_until: dict[int, float] = {}  # mirror index -> epoch time
        # The two overpass-api.de hostnames are one operator cluster. Treat
        # them as one concurrency target so a street request and POI request
        # do not hit main and CDN simultaneously and then retry into the same
        # cluster's shared rate limit.
        self._in_flight_groups: set[str] = set()
        self._mirror_lock = threading.Lock()

    def _mirror_group(self, index: int, mirror_list: list[str]) -> str:
        url = mirror_list[index].lower()
        if "overpass-api.de" in url:
            return "overpass-api.de"
        return url.split("/api/", 1)[0]

    def _cool_down_operator(self, index: int, until: float) -> None:
        if index in (0, 1) and len(self._mirrors) > 1:
            self._rate_limited_until[0] = until
            self._rate_limited_until[1] = until
        else:
            self._rate_limited_until[index] = until

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
        cancel_cb=None,
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
        while not self._sem.acquire(timeout=0.2):
            if cancel_cb and cancel_cb():
                raise OverpassRequestCancelled()
        try:
            if cancel_cb and cancel_cb():
                raise OverpassRequestCancelled()
            if start_index is None:
                # Start with the last server that actually worked. Concurrent
                # callers still move to another available mirror below, but a
                # user's next request should not pay to rediscover a known-good
                # endpoint merely for round-robin distribution.
                start_index = self._last_successful_mirror % n_mirrors

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
            # Collapse host aliases belonging to one operator. This ensures
            # max_mirrors means independent providers, rather than allowing
            # Germany main and CDN to consume the whole retry budget.
            independent_indices = []
            seen_groups = set()
            for i in ordered_indices:
                group = self._mirror_group(i, mirror_list)
                if group in seen_groups:
                    continue
                seen_groups.add(group)
                independent_indices.append(i)
            ordered_indices = independent_indices
            if max_mirrors is not None:
                ordered_indices = ordered_indices[:max(1, int(max_mirrors))]

            for index in ordered_indices:
                if cancel_cb and cancel_cb():
                    raise OverpassRequestCancelled()
                group = self._mirror_group(index, mirror_list)
                with self._mirror_lock:
                    if group in self._in_flight_groups:
                        miab_log(
                            "verbose",
                            f"[Overpass] Skipping {label_list[index]} for concurrent request; operator already in use.",
                            getattr(self, "settings", None),
                        )
                        continue
                    self._in_flight_groups.add(group)
                url = mirror_list[index]
                label = label_list[index]
                self._wait(index, cancel_cb=cancel_cb)

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
                        until = time.time() + RATE_LIMIT_COOLDOWN_SECS
                        if custom_mirrors:
                            self._rate_limited_until[index] = until
                        else:
                            self._cool_down_operator(index, until)
                    elif exc.code >= 500:
                        until = time.time() + TRANSIENT_FAIL_COOLDOWN_SECS
                        if custom_mirrors:
                            self._rate_limited_until[index] = until
                        else:
                            self._cool_down_operator(index, until)
                    miab_log("errors", f"[Overpass] {label} failed: {exc}", getattr(self, "settings", None))
                    # Try next server
                    continue
                except Exception as exc:
                    # Socket timeouts, connection errors, etc. — also
                    until = time.time() + TRANSIENT_FAIL_COOLDOWN_SECS
                    if custom_mirrors:
                        self._rate_limited_until[index] = until
                    else:
                        self._cool_down_operator(index, until)
                    miab_log("errors", f"[Overpass] {label} failed: {exc}", getattr(self, "settings", None))
                    # Try next server
                    continue
                finally:
                    with self._mirror_lock:
                        self._in_flight_groups.discard(group)

            # All servers failed
            return None
        finally:
            self._sem.release()

    def poi_request(
        self,
        query_data: bytes,
        timeout: int = 15,
    ) -> dict | None:
        start_index = 1 if len(self._mirrors) > 1 else 0
        return self.request(query_data, timeout=timeout, start_index=start_index)

    def large_request(
        self,
        query_data: bytes,
        timeout: int = 15,
        max_mirrors: int | None = None,
        cancel_cb=None,
    ) -> dict | None:
        """Large radius queries with standard timeout."""
        return self.request(
            query_data, timeout=timeout, max_mirrors=max_mirrors,
            cancel_cb=cancel_cb)
    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _wait(self, mirror_index: int | None = None, cancel_cb=None) -> None:
        """Enforce a cooldown, per mirror when a mirror is known."""
        now = time.time()
        if mirror_index is None:
            last = self._last_request
        else:
            last = self._last_request_by_mirror.get(mirror_index, 0.0)
        elapsed = now - last
        remaining = self._cooldown - elapsed
        while remaining > 0:
            if cancel_cb and cancel_cb():
                raise OverpassRequestCancelled()
            time.sleep(min(0.2, remaining))
            remaining = self._cooldown - (time.time() - last)
        now = time.time()
        self._last_request = now
        if mirror_index is not None:
            self._last_request_by_mirror[mirror_index] = now
