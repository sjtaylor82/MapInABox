"""Machine-wide tool policy for the Education edition."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path


EDUCATION_TOOL_CHOICES = (
    ("Detour Calculator", "detour_calculator"),
    ("Suburb Lister", "route_explorer"),
    ("Shared Journey", "rendezvous_point"),
    ("Toll Compare", "toll_compare"),
    ("Journey Planner", "journey_planner"),
    ("Airport Amenity Guide", "airport_amenity_guide"),
    ("Departure Board", "departure_board"),
    ("Flight Search", "flight_search"),
    ("Find Food", "find_food"),
)
EDUCATION_TOOL_KEYS = frozenset(key for _label, key in EDUCATION_TOOL_CHOICES)
EDUCATION_NEVER_AVAILABLE = frozenset({
    "hotel_search", "virgin_booking", "order_uber",
})

# A missing or damaged policy enables only straightforward information tools.
# Schools can enable the other permitted tools with administrator approval.
DEFAULT_EDUCATION_TOOLS = frozenset({
    "detour_calculator",
    "route_explorer",
    "toll_compare",
    "journey_planner",
    "airport_amenity_guide",
    "departure_board",
})


def policy_path(platform: str = sys.platform,
                environ: dict | None = None) -> Path:
    env = os.environ if environ is None else environ
    if platform == "win32":
        root = env.get("PROGRAMDATA") or r"C:\ProgramData"
        return Path(root) / "MapInABox" / "education-policy.json"
    if platform == "darwin":
        return Path("/Library/Application Support/MapInABox/education-policy.json")
    return Path("/etc/mapinabox/education-policy.json")


def normalise_tools(values) -> set[str]:
    if not isinstance(values, (list, tuple, set, frozenset)):
        return set(DEFAULT_EDUCATION_TOOLS)
    return {str(value) for value in values if str(value) in EDUCATION_TOOL_KEYS}


def load_education_tools(path: os.PathLike | str | None = None) -> set[str]:
    destination = Path(path) if path is not None else policy_path()
    try:
        data = json.loads(destination.read_text(encoding="utf-8"))
        if data.get("format") != 1:
            raise ValueError("unsupported policy format")
        return normalise_tools(data.get("enabled_tools"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return set(DEFAULT_EDUCATION_TOOLS)


def write_education_tools(values,
                          path: os.PathLike | str | None = None) -> Path:
    destination = Path(path) if path is not None else policy_path()
    enabled = sorted(normalise_tools(values))
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".tmp")
    temporary.write_text(json.dumps({
        "format": 1,
        "enabled_tools": enabled,
    }, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, destination)
    return destination


def admin_writer_arguments(values, executable: str, core_script: str,
                           frozen: bool) -> tuple[str, list[str]]:
    enabled = ",".join(sorted(normalise_tools(values)))
    arguments = [f"--write-education-policy={enabled}"]
    if not frozen:
        arguments.insert(0, core_script)
    return executable, arguments


def request_admin_write(values, executable: str, core_script: str,
                        frozen: bool, platform: str = sys.platform) -> bool:
    """Ask the operating system to run the small policy writer as admin."""
    program, arguments = admin_writer_arguments(
        values, executable, core_script, frozen)
    if platform == "win32":
        import ctypes
        params = subprocess.list2cmdline(arguments)
        result = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", program, params, None, 1)
        return int(result) > 32
    command = shlex.join([program, *arguments])
    if platform == "darwin":
        apple_command = command.replace("\\", "\\\\").replace('"', '\\"')
        completed = subprocess.run([
            "/usr/bin/osascript", "-e",
            f'do shell script "{apple_command}" with administrator privileges',
        ], check=False)
        return completed.returncode == 0
    completed = subprocess.run(
        ["pkexec", program, *arguments], check=False)
    return completed.returncode == 0
