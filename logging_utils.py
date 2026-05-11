"""logging_utils.py — Structured logging helper for Map in a Box.

Categories:
    errors       — exceptions, API failures, missing data
    street       — Overpass queries, cache hits/misses
    api_calls    — HERE/Gemini requests and responses
    challenges   — player, country, time, score
    feature_usage — keys pressed, lookups made
    navigation   — country entries, crossings, jumps
    verbose      — optional troubleshooting traces
"""

import datetime as _dt
import os


def _resolve_log_path(settings) -> str | None:
    if settings is not None:
        path = settings.get("_log_path")
        if path:
            return path
    path = os.environ.get("MIAB_LOG_PATH")
    return path or None


def miab_log(category: str, msg: str, settings=None) -> None:
    """Write a timestamped structured log entry if the category is enabled.

    The log entry is appended directly to miab.log when possible so it does not
    echo to the terminal. If no log path is available, falls back to stdout.
    """
    if settings is not None:
        log_cfg = settings.get("logging", {})
        if not log_cfg.get(category, False):
            return
    ts = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [{category.upper()}] {msg}"
    log_path = _resolve_log_path(settings)
    if log_path:
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            return
        except Exception:
            pass
    print(line)
