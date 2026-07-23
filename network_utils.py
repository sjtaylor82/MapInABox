"""network_utils.py — Shared network-failure messaging for Map in a Box.

Every background fetch (weather, Wikipedia, sunrise/sunset, flights, and
so on) used to speak its raw exception text on failure, e.g.
"Could not fetch weather: Expecting value: line 1 column 1 (char 0)".
That specific message is what you get when a network filter or captive
portal intercepts a request and returns an HTML "blocked" page instead of
JSON — the connection succeeds, so there's no clean timeout, but the
response isn't the data the app expected. Raw Python exception text is a
poor thing to have a screen reader read aloud in any case.

describe_fetch_error() turns any fetch failure into one consistent,
spoken-friendly message: "Internet not available" for anything that looks
network-related (unreachable host, timeout, connection reset, or a
response that fails to parse as the JSON it should have been), and a
generic fallback for anything else, so genuine bugs are never confused
with a simple connectivity problem.
"""

from __future__ import annotations

import json
import socket
import urllib.error

NETWORK_UNAVAILABLE_MESSAGE = "Internet not available."
_GENERIC_FAILURE_MESSAGE = "Something went wrong with that lookup."

_NETWORK_EXCEPTION_TYPES = (
    urllib.error.URLError,     # covers URLError and its subclass HTTPError
    socket.timeout,
    TimeoutError,
    ConnectionError,
    json.JSONDecodeError,      # a non-JSON response usually means a proxy
                                # or content filter intercepted the request
)


def is_network_error(exc: Exception) -> bool:
    """True if exc looks like a connectivity problem rather than a bug."""
    return isinstance(exc, _NETWORK_EXCEPTION_TYPES)


def describe_fetch_error(exc: Exception, feature: str = "") -> str:
    """Return a clean, spoken-friendly message for a failed network fetch.

    feature, if given, is named first so the user knows what failed, e.g.
    describe_fetch_error(exc, "weather") -> "Could not fetch weather.
    Internet not available."
    """
    base = NETWORK_UNAVAILABLE_MESSAGE if is_network_error(exc) else _GENERIC_FAILURE_MESSAGE
    if feature:
        return f"Could not fetch {feature}. {base}"
    return base
